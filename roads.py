import argparse
import geopandas as gpd
import osmium
from pathlib import Path

OSM_CRS = 'EPSG:4326'
METRIC_CRS = 'EPSG:5070' # CONUS Albers Metric

def find_roads(osm_data: Path, state_data: Path, track_file: Path) -> None:
    """Matches tracks to unique OSM roads."""

    print("Loading OSM roads...", end=" ")
    roads = build_roads(osm_data, state_data)
    print("done.")
    print(roads)
    
    print("Loading tracks...", end=" ")
    tracks = build_tracks(track_file)
    print("done.")
    

def build_roads(osm_data: Path, state_data: Path) -> gpd.GeoDataFrame:
    """Creates a road GeoDataFrame with state labels."""
    states = gpd.read_file(state_data)
    states = states[['STUSPS', 'NAME', 'geometry']].rename(columns={
        'STUSPS': 'state_abbr',
        'NAME': 'state_name',
    }).to_crs(OSM_CRS)
    fp = (
        osmium.FileProcessor(osm_data)
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter('highway'))
        .with_filter(osmium.filter.GeoInterfaceFilter(tags=[
            'highway', 'name', 'ref', 'network', 'state'
        ]))
    )
    roads = gpd.GeoDataFrame.from_features(fp, crs=OSM_CRS)
    # Spatially join U.S. states onto roads.
    roads = gpd.sjoin(roads, states, how='left', predicate='within')
    return roads.to_crs(METRIC_CRS)

def build_tracks(track_file: Path) -> gpd.GeoDataFrame:
    tracks = gpd.read_file(
        track_file,
        layer='driving_tracks',
        fid_as_index=True,
        columns=['utc_start'],
    )
    tracks = tracks.sort_values('utc_start')
    return tracks.to_crs(METRIC_CRS)


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