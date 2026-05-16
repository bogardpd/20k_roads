import argparse
import geopandas as gpd
import osmium
from pathlib import Path

def find_roads(osm_data, track_file):
    print("Loading OSM roads...")
    fp = (
        osmium.FileProcessor(osm_data)
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter('highway'))
        .with_filter(osmium.filter.GeoInterfaceFilter(tags=[
            'highway', 'name', 'ref'
        ]))
    )
    roads = gpd.GeoDataFrame.from_features(fp, crs='EPSG:4326')
    print(roads)
    print("Loading tracks...")
    tracks = gpd.read_file(track_file,
        layer='driving_tracks',
        fid_as_index=True,
        columns=['utc_start'],
    )
    tracks = tracks.sort_values('utc_start')
    print(tracks)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="find_roads",
        description="Matches GPS tracks to roads",
    )
    parser.add_argument("--osm",
        type=Path,
        required=True,
        help="OpenStreetMap PBF file covering the region of the tracks",
    )
    parser.add_argument("--tracks",
        type=Path,
        required=True,
        help="GeoPackage file containing driving tracks",
    )
    args = parser.parse_args()
    find_roads(args.osm, args.tracks)