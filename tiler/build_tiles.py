#!/usr/bin/env python3
"""Navia offline road data: OSM (Geofabrik extract) -> Overpass-style JSON sub-tiles.

Output (in --out):
  p{PI}_{PJ}.zip   one pack per 0.5 x 0.5 degree, entries t{I}_{J}.json (0.025 deg sub-tiles,
                   I = floor(lat / 0.025), J = floor(lon / 0.025), PI = I // 20, PJ = J // 20)
  manifest.json    data date, per-pack sha256 / size / bbox / tile count, ODbL attribution

Each sub-tile is {"elements": [...]} in the format of Overpass `out body geom`, limited to
exactly what the Navia launcher parser (RoadContextParser) reads:
  - drivable highway ways (plus construction) with nodes + geometry and the used tags,
  - nodes that classify as a traffic POI,
  - turn restriction relations (via node),
  - built-up / industrial landuse and admin_level=8 boundaries, clipped to the sub-tile
    (with a small buffer), as synthetic multipolygon relations with negative ids.
A way is written to every sub-tile one of its segments touches.
"""
import argparse, datetime, hashlib, json, math, os, sys, zipfile
from collections import defaultdict

import osmium
import shapely
from shapely import wkb as shapely_wkb
from shapely.geometry import box

CELL = 0.025
PACK = 20  # sub-tiles per pack side (pack = 0.5 deg)
AREA_BUFFER = 0.002
POLAND_BBOX = (48.9, 13.9, 55.0, 24.3)  # lat0, lon0, lat1, lon1 (geofabrik extract bounds incl. margin)

ROAD_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "residential", "unclassified",
    "service", "living_street", "track", "road", "construction",
}
SKIP_SERVICE = {"parking_aisle", "driveway", "drive-through", "emergency_access"}
WAY_KEYS = {
    "highway", "oneway", "junction", "maxspeed", "zone:maxspeed", "zone:traffic", "source:maxspeed",
    "source:maxspeed:forward", "source:maxspeed:backward", "ref", "name", "lanes", "lanes:forward",
    "lanes:backward", "turn:lanes", "turn:lanes:forward", "turn:lanes:backward", "dual_carriageway",
    "expressway", "surface", "construction", "service", "motorroad",
}
WAY_PREFIXES = ("maxspeed:",)
NODE_KEYS = {
    "highway", "traffic_calming", "railway", "amenity", "barrier", "enforcement", "traffic_sign",
    "traffic_sign:direction", "direction", "crossing", "crossing:signals", "give_way:direction",
    "stop:direction", "traffic_signals:direction", "name", "maxspeed", "brand", "operator",
}
POI_HIGHWAY = {"speed_camera", "stop", "give_way", "traffic_signals", "crossing", "rest_area"}
AREA_LANDUSE = {"residential", "commercial", "retail", "industrial"}


def cell(lat, lon):
    return int(math.floor(lat / CELL)), int(math.floor(lon / CELL))


def pack_of(i, j):
    return i // PACK, j // PACK


def r6(x):
    return round(x, 6)


def is_poi(tags):
    hw = tags.get("highway")
    if hw in POI_HIGHWAY:
        return True
    if "traffic_calming" in tags:
        return True
    if tags.get("railway") == "level_crossing":
        return True
    if tags.get("amenity") in ("fuel", "school"):
        return True
    if tags.get("barrier") in ("gate", "lift_gate", "bollard"):
        return True
    if tags.get("enforcement") == "maxspeed":
        return True
    if any(t.strip() == "PL:B-2" for t in tags.get("traffic_sign", "").split(";")):
        return True
    return False


class Sink:
    """Appends element JSON lines per pack to temp files: '<I> <J>\t<json>'."""

    def __init__(self, tmp):
        self.tmp = tmp
        os.makedirs(tmp, exist_ok=True)
        self.handles = {}

    def put(self, cells, element):
        line = json.dumps(element, ensure_ascii=False, separators=(",", ":"))
        by_pack = defaultdict(list)
        for (i, j) in cells:
            by_pack[pack_of(i, j)].append((i, j))
        for p, cs in by_pack.items():
            h = self.handles.get(p)
            if h is None:
                if len(self.handles) > 400:
                    for x in self.handles.values():
                        x.close()
                    self.handles.clear()
                h = open(os.path.join(self.tmp, f"p{p[0]}_{p[1]}.lines"), "a", encoding="utf-8")
                self.handles[p] = h
            for (i, j) in cs:
                h.write(f"{i} {j}\t{line}\n")

    def close(self):
        for h in self.handles.values():
            h.close()
        self.handles.clear()


def segment_cells(points):
    out = set()
    prev = None
    for lat, lon in points:
        if prev is None:
            out.add(cell(lat, lon))
        else:
            i0, j0 = cell(min(prev[0], lat), min(prev[1], lon))
            i1, j1 = cell(max(prev[0], lat), max(prev[1], lon))
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    out.add((i, j))
        prev = (lat, lon)
    return out


def tags_dict(taglist):
    return {t.k: t.v for t in taglist}


def process_roads(path, sink, stats):
    # Pass 1: turn restrictions with a via node -> remember the via node ids.
    restrictions = []
    via_ids = set()
    for rel in osmium.FileProcessor(path, osmium.osm.RELATION):
        tags = tags_dict(rel.tags)
        if tags.get("type") != "restriction":
            continue
        members = [(m.type, m.ref, m.role) for m in rel.members]
        vias = [m for m in members if m[2] == "via"]
        if len(vias) != 1 or vias[0][0] != "n":
            continue
        if not any(m[2] == "from" and m[0] == "w" for m in members) or not any(m[2] == "to" and m[0] == "w" for m in members):
            continue
        keep = {k: v for k, v in tags.items() if k in ("type", "restriction", "except") or k.startswith("restriction:")}
        restrictions.append((rel.id, members, keep, vias[0][1]))
        via_ids.add(vias[0][1])
    via_loc = {}
    # Pass 2: nodes (POIs, via locations) and ways with locations.
    fp = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_locations()
    for obj in fp:
        if obj.is_node():
            if obj.id in via_ids and obj.location.valid():
                via_loc[obj.id] = (obj.location.lat, obj.location.lon)
            if not obj.tags or not obj.location.valid():
                continue
            tags = tags_dict(obj.tags)
            if not is_poi(tags):
                continue
            lat, lon = obj.location.lat, obj.location.lon
            keep = {k: v for k, v in tags.items() if k in NODE_KEYS}
            sink.put([cell(lat, lon)], {"type": "node", "id": obj.id, "lat": r6(lat), "lon": r6(lon), "tags": keep})
            stats["pois"] += 1
        else:
            tags = tags_dict(obj.tags)
            hw = tags.get("highway")
            if hw not in ROAD_HIGHWAYS:
                continue
            if hw == "service" and tags.get("service") in SKIP_SERVICE:
                continue
            if tags.get("area") == "yes":
                continue
            nodes, pts = [], []
            ok = True
            for n in obj.nodes:
                if not n.location.valid():
                    ok = False
                    break
                nodes.append(n.ref)
                pts.append((n.location.lat, n.location.lon))
            if not ok or len(nodes) < 2:
                continue
            keep = {k: v for k, v in tags.items() if k in WAY_KEYS or k.startswith(WAY_PREFIXES)}
            el = {"type": "way", "id": obj.id, "nodes": nodes,
                  "geometry": [{"lat": r6(a), "lon": r6(b)} for a, b in pts], "tags": keep}
            sink.put(segment_cells(pts), el)
            stats["ways"] += 1
    for rid, members, keep, via in restrictions:
        loc = via_loc.get(via)
        if loc is None:
            continue
        el = {"type": "relation", "id": rid,
              "members": [{"type": {"n": "node", "w": "way", "r": "relation"}[t], "ref": ref, "role": role} for t, ref, role in members],
              "tags": keep}
        sink.put([cell(*loc)], el)
        stats["restrictions"] += 1


def ring_json(coords):
    return [{"lat": round(y, 5), "lon": round(x, 5)} for x, y in coords]


def process_areas(path, sink, stats):
    factory = osmium.geom.WKBFactory()
    fp = osmium.FileProcessor(path).with_areas().with_filter(osmium.filter.EntityFilter(osmium.osm.AREA))
    for area in fp:
        tags = tags_dict(area.tags)
        if tags.get("landuse") in AREA_LANDUSE:
            out_tags = {"type": "multipolygon", "landuse": tags["landuse"]}
        elif tags.get("boundary") == "administrative" and tags.get("admin_level") == "8" and not area.from_way():
            out_tags = {"type": "boundary", "boundary": "administrative", "admin_level": "8"}
        else:
            continue
        try:
            geom = shapely_wkb.loads(factory.create_multipolygon(area), hex=True)
        except Exception:
            continue
        if geom.is_empty:
            continue
        geom = geom.simplify(0.00003, preserve_topology=True)
        if not geom.is_valid:
            geom = shapely.make_valid(geom)
        minx, miny, maxx, maxy = geom.bounds
        i0, j0 = cell(miny, minx)
        i1, j1 = cell(maxy, maxx)
        prepared = None
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                b = box(j * CELL - AREA_BUFFER, i * CELL - AREA_BUFFER, (j + 1) * CELL + AREA_BUFFER, (i + 1) * CELL + AREA_BUFFER)
                if (i1 - i0) or (j1 - j0):
                    if prepared is None:
                        shapely.prepare(geom)
                        prepared = True
                    if not geom.intersects(b):
                        continue
                    piece = geom.intersection(b)
                else:
                    piece = geom
                polys = [g for g in getattr(piece, "geoms", [piece]) if g.geom_type == "Polygon" and not g.is_empty]
                if not polys:
                    continue
                members = []
                for poly in polys:
                    members.append({"type": "way", "ref": 0, "role": "outer", "geometry": ring_json(poly.exterior.coords)})
                    for hole in poly.interiors:
                        members.append({"type": "way", "ref": 0, "role": "inner", "geometry": ring_json(hole.coords)})
                # Stable synthetic id: source area id x cell slot (unique within any 64x64 cell window),
                # so an unchanged area keeps its id and unchanged packs keep their sha256.
                el = {"type": "relation", "id": -(area.id * 4096 + (i % 64) * 64 + (j % 64)), "members": members, "tags": out_tags}
                sink.put([(i, j)], el)
                stats["area_pieces"] += 1


def build_packs(tmp, out, data_date, base_url):
    packs = []
    for name in sorted(os.listdir(tmp)):
        if not name.endswith(".lines"):
            continue
        tiles = defaultdict(dict)
        with open(os.path.join(tmp, name), encoding="utf-8") as f:
            for line in f:
                key, js = line.rstrip("\n").split("\t", 1)
                el = json.loads(js)
                tiles[key][(el["type"], el["id"])] = js
        pname = name[:-6]
        zpath = os.path.join(out, pname + ".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for key in sorted(tiles, key=lambda k: tuple(map(int, k.split()))):
                i, j = map(int, key.split())
                els = tiles[key]
                # Ways first, then nodes, then relations (stable order -> stable hash).
                order = {"way": 0, "node": 1, "relation": 2}
                body = '{"elements":[' + ",".join(els[k] for k in sorted(els, key=lambda k: (order[k[0]], k[1]))) + "]}"
                info = zipfile.ZipInfo(f"t{i}_{j}.json", date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, body.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        pi, pj = map(int, pname[1:].split("_"))
        size = os.path.getsize(zpath)
        with open(zpath, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        packs.append({"name": pname + ".zip", "pi": pi, "pj": pj,
                      "bbox": [round(pi * PACK * CELL, 4), round(pj * PACK * CELL, 4), round((pi + 1) * PACK * CELL, 4), round((pj + 1) * PACK * CELL, 4)],
                      "tiles": len(tiles), "size": size, "sha256": sha})
    total = sum(p["size"] for p in packs)
    manifest = {
        "schema": 1,
        "format": "overpass-json-subtiles",
        "cell_deg": CELL,
        "pack_cells": PACK,
        "data_date": data_date,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": base_url,
        "total_size": total,
        "attribution": "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright). Extract: Geofabrik GmbH.",
        "license": "ODbL-1.0",
        "packs": packs,
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roads", required=True, help="pbf filtered to roads/pois/restrictions")
    ap.add_argument("--areas", required=True, help="pbf filtered to landuse/admin boundaries")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tmp", default="tmp_lines")
    ap.add_argument("--data-date", required=True)
    ap.add_argument("--base-url", default="")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if os.path.isdir(a.tmp):
        for n in os.listdir(a.tmp):
            os.remove(os.path.join(a.tmp, n))
    sink = Sink(a.tmp)
    stats = defaultdict(int)
    process_roads(a.roads, sink, stats)
    print("roads done", dict(stats), flush=True)
    process_areas(a.areas, sink, stats)
    sink.close()
    print("areas done", dict(stats), flush=True)
    m = build_packs(a.tmp, a.out, a.data_date, a.base_url)
    sizes = sorted(p["size"] for p in m["packs"])
    print(json.dumps({"stats": stats, "packs": len(m["packs"]), "total_mb": round(m["total_size"] / 1e6, 1),
                      "max_pack_mb": round(sizes[-1] / 1e6, 2) if sizes else 0,
                      "median_pack_mb": round(sizes[len(sizes) // 2] / 1e6, 2) if sizes else 0}))


if __name__ == "__main__":
    main()
