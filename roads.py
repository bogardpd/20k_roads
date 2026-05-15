import argparse
import geopandas as gpd
import osmium
from pathlib import Path

def find_roads(osm_data):
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="find_roads",
        description="Matches GPS tracks to roads",
    )
    parser.add_argument("--osm", type=Path, required=True)
    args = parser.parse_args()
    find_roads(args.osm)