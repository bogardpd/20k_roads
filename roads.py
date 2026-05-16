import argparse
import geopandas as gpd
import osmium
from pathlib import Path

METRIC_CRS = 'EPSG:5070' # CONUS Albers Metric

def find_roads(osm_data, state_data, track_file):
    print("Loading states...")
    states = gpd.read_file(state_data)
    states = states[['STUSPS', 'NAME', 'geometry']]rename(columns={
        'STUSPS': 'state_abbr',
        'NAME': 'state_name',
    })

    print("Loading OSM roads...")
    fp = (
        osmium.FileProcessor(osm_data)
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter('highway'))
        .with_filter(osmium.filter.GeoInterfaceFilter(tags=[
            'highway', 'name', 'ref', 'network', 'state'
        ]))
    )
    roads = gpd.GeoDataFrame.from_features(fp, crs='EPSG:4326')
    # Spatially join onto state boundaries.
    roads = gpd.sjoin(roads, states, how='left', predicate='within')
    print(roads)

    print("Loading tracks...")
    tracks = gpd.read_file(track_file,
        layer='driving_tracks',
        fid_as_index=True,
        columns=['utc_start'],
    )
    tracks = tracks.sort_values('utc_start')

    print("Reprojecting to metric CRS...")
    roads_proj = roads.to_crs(METRIC_CRS)
    tracks_proj = tracks.to_crs(METRIC_CRS)

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
    parser.add_argument("--states",
        type=Path,
        required=True,
        help="Census TIGER data for U.S. state boundaries",
    )
    parser.add_argument("--tracks",
        type=Path,
        required=True,
        help="GeoPackage file containing driving tracks",
    )
    args = parser.parse_args()
    find_roads(args.osm, args.states, args.tracks)