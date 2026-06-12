# 20k_roads

A tool to compare GPS driving tracks to OSM roads.

## Usage

### Build Visited Roads

```
python roads.py --osm region.osm.pbf --states tl_2023_us_state.zip --tracks tracks.gpkg --output-dir /path-to-dir
```

Arguments:

- **`--osm`:** An OpenStreetMap PBF file containing roads for the region(s) that all of the driving tracks are in.
- **`--tracks`:** A GeoPackage file that contains a `driving_tracks` MultiLineString layer with a `utc_start` datetime column.
- **`--output-dir`:** A directory to store the outputs from this script.

> [!IMPORTANT]
>
> Preprocessing the OSM PBF data into a format that Python can use takes a long time for large regions. Because of this, the script will store preprocessed OSM data in cache files based on the `--osm` file with the final `.pbf` extension changed to `.ways.feather` (for way geometry data) and `.pickle` (for other data). (For example, processing `roads.osm.pbf` would create `roads.osm.ways.feather` and `roads.osm.pickle` in the same directory).
>
> If these files are both present, then the script will use them instead of the original `.pbf` data. If you update the data in the original `.pbf` file, be sure to delete its corresponding `.ways.feather` and `.pickle` files to force the script to use your updated data.

### Chart Cumulative Roads

Creates a simple chart of cumulative roads visited over time.

```
python chart_cumulative_roads.py 20k_roads.gpkg
```

Arguments:

- **`roads_path`:** The output file from roads.py.

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
  w/highway=living_street \
  r/type=route,route=road \
  r/type=superroute,route=road \
  -o region-roads-drivable.osm.pbf
```

## Lexicon

As this project uses [OpenStreetMap](https://www.openstreetmap.org/) data, it uses OSM's basic data structures of **nodes** (points at a specific geographic location), **ways** (sequences of nodes forming a line), and **relations** (collections of nodes, ways, and other relations which represent a map feature).

For the purposes of this project, a **road** is a continuous drivable collection of ways which has a single identity for its length. These are divided into two types:

- A **numbered route** is a road like an Interstate (I-80), US Route (US-1), or State Route (OH-4) which belongs to a network of similar roads and is identified primarily by number. (This does not include roads like 1st Street or 5th Avenue; even though they are numbered, they doesn't belong to a network.) Numbered routes are represented by OSM relations that include a `network` and `ref` tag, and the specific networks this project will use are defined in `config.toml`.
- A **named road** is a road that is known primarily by name. These are made up of adjacent ways whose `name` tags are the same.

