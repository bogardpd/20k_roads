# 20k_roads

A tool to compare GPS driving tracks to OSM roads.

## Usage

```
python roads.py --osm region.osm.pbf --tracks tracks.gpkg
```

### Arguments

- **`--osm`:** An OpenStreetMap PBF file containing roads for the region(s) that all of the driving tracks are in.
- **`--tracks`:** A GeoPackage file that contains a `driving_tracks` MultiLineString layer.

## Notes

OpenStreetMap PBF extracts can be downloaded from [Geofabrik downloads](https://download.geofabrik.de/). Once they're downloaded, use [Osmium Tool](https://osmcode.org/osmium-tool/) to filter them to only roads with the following command:

```
osmium tags-filter region-latest.osm.pbf w/highway -o region-roads.osm.pbf
```
