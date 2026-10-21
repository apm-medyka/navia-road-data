#!/usr/bin/env bash
# Usage: filter.sh input.osm.pbf  -> roads.pbf, areas.pbf
set -euo pipefail
IN="$1"
osmium tags-filter --overwrite -o roads.pbf "$IN" \
  w/highway=motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link,residential,unclassified,service,living_street,track,road,construction \
  n/highway=speed_camera,stop,give_way,traffic_signals,crossing,rest_area \
  n/traffic_calming n/railway=level_crossing n/amenity=fuel,school n/barrier=gate,lift_gate,bollard \
  n/enforcement=maxspeed n/traffic_sign n/city_limit r/type=restriction
osmium tags-filter --overwrite -o areas.pbf "$IN" \
  wr/landuse=residential,commercial,retail,industrial r/boundary=administrative \
  wr/place=city,town,village,hamlet,suburb,quarter,neighbourhood
ls -la roads.pbf areas.pbf
