# 20k_roads

A tool to compare GPS driving tracks to OSM roads.

## Usage

```
python roads.py --osm region.osm.pbf --states tl_2023_us_state.zip --tracks tracks.gpkg --output-dir /path-to-dir
```

### Arguments

- **`--osm`:** An OpenStreetMap PBF file containing roads for the region(s) that all of the driving tracks are in.
- **`--tracks`:** A GeoPackage file that contains a `driving_tracks` MultiLineString layer with a `utc_start` datetime column.
- **`--output-dir`:** A directory to store the outputs from this script.

## Data Sources

OpenStreetMap PBF extracts can be downloaded from [Geofabrik downloads](https://download.geofabrik.de/). Once they're downloaded, use [Osmium Tool](https://osmcode.org/osmium-tool/) to filter them to only drivable roads and road relations with the following command:

```
osmium tags-filter region-latest.osm.pbf \
  w/highway=motorway \
  w/highway=trunk \
  w/highway=primary \
  w/highway=secondary \
  w/highway=tertiary \
  w/highway=unclassified \
  w/highway=residential \
  w/highway=motorway_link \
  w/highway=trunk_link \
  w/highway=primary_link \
  w/highway=secondary_link \
  r/type=route,route=road \
  r/type=superroute,route=road \
  -o region-roads-drivable.osm.pbf
```
