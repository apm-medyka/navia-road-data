# navia-road-data

Offline road data for the **Navia FYT Launcher** (speed limits, signs, junctions, turn
restrictions, built-up areas) for all of Poland, rebuilt every week by GitHub Actions from the
[Geofabrik](https://download.geofabrik.de/europe/poland.html) OpenStreetMap extract.

## Format

* `manifest.json` – data date, per-pack `sha256`, `size`, `bbox`, number of sub-tiles, attribution.
  Always available at `https://github.com/apm-medyka/navia-road-data/releases/latest/download/manifest.json`.
* `p{PI}_{PJ}.zip` – one pack per 0.5° × 0.5° (`PI = floor(lat / 0.5)`, `PJ = floor(lon / 0.5)`), holding
  sub-tiles `t{I}_{J}.json` of 0.025° (`I = floor(lat / 0.025)`, `J = floor(lon / 0.025)`).
* Each sub-tile is `{"elements": [...]}` in the format of an Overpass `out body geom` reply, limited to the
  tags the launcher reads (see `tiler/filter.sh` and `tiler/build_tiles.py`): drivable `highway` ways with
  `nodes` + `geometry`, traffic-POI nodes, `type=restriction` relations (via node), and landuse /
  `admin_level=8` areas and (format_version 2) `place` settlement polygons clipped to the sub-tile as
  synthetic multipolygon relations (negative ids), plus D-42 / D-43 (`traffic_sign=city_limit`, `PL:D-42`,
  `PL:D-43`) sign nodes. Format changes are additive: older launchers ignore what they do not know.
  A way is repeated in every sub-tile it crosses; the app deduplicates by id.
* Zips are deterministic: an unchanged area keeps its `sha256`, so the app downloads only changed packs.

## Build locally

```bash
sudo apt-get install osmium-tool && pip install osmium shapely
curl -LO https://download.geofabrik.de/europe/poland-latest.osm.pbf
tiler/filter.sh poland-latest.osm.pbf
python3 tiler/build_tiles.py --roads roads.pbf --areas areas.pbf --out out --data-date 2026-10-18 \
  --base-url https://github.com/apm-medyka/navia-road-data/releases/download/data-20261018/
python3 tiler/report.py out/manifest.json
```

## License / attribution

The data is © **OpenStreetMap contributors** and available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/);
see <https://www.openstreetmap.org/copyright>. The release files are a derived database of
OpenStreetMap and are distributed under the same license. Extract provided by Geofabrik GmbH.
The scripts in `tiler/` and `.github/` are MIT licensed.
