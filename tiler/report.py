#!/usr/bin/env python3
"""Markdown size report for a manifest.json."""
import json, sys
m = json.load(open(sys.argv[1]))
packs = sorted(m["packs"], key=lambda p: p["size"])
total = m["total_size"]
print(f"**Data date:** {m['data_date']}  ")
print(f"**Packs:** {len(packs)} (0.5° each), **sub-tiles:** {sum(p['tiles'] for p in packs)} (0.025°)  ")
print(f"**Total:** {total/1e6:.1f} MB, **median pack:** {packs[len(packs)//2]['size']/1e6:.2f} MB, **largest:** {packs[-1]['name']} {packs[-1]['size']/1e6:.2f} MB  ")
print()
print("| pack | bbox (lat0, lon0, lat1, lon1) | tiles | MB |")
print("|---|---|---:|---:|")
for p in sorted(packs, key=lambda p: -p["size"])[:15]:
    print(f"| {p['name']} | {p['bbox']} | {p['tiles']} | {p['size']/1e6:.2f} |")
