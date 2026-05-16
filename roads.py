import argparse
import geopandas as gpd
import osmium
from pathlib import Path
from shapely import Point

OSM_CRS = 'EPSG:4326'
METRIC_CRS = 'EPSG:5070' # CONUS Albers Metric
MAX_DIST = 50 # Max meters (metric CRS) to search for nearby road
CONSEC_PTS = 5 # Min number of consecutive points to count as road match

def find_roads(osm_data: Path, state_data: Path, track_file: Path) -> None:
    """Matches tracks to unique OSM roads."""

    print("Loading OSM roads...", end=" ")
    roads = build_roads(osm_data, state_data)
    roads_sindex = roads.sindex # Build spatial index
    print("done.")

    print("Loading tracks...", end=" ")
    tracks = build_tracks(track_file)
    print("done.")

    # Temporarily filter to a small subset of tracks.
    tracks = tracks[tracks['utc_start'] < "2010-01-16"]
    print(tracks)

    unique_roads = {}
    for idx, track in tracks.iterrows():
        print(f"Processing track {track.utc_start}")
        for segment in track.geometry.geoms:
            points_gdf = gpd.GeoDataFrame(
                geometry=gpd.points_from_xy(*zip(*segment.coords)),
                crs=METRIC_CRS,
            )
            # Get the closest way for every point.
            points_gdf['closest_way_id'] = points_gdf.geometry.apply(
                lambda r: get_closest_way(roads, roads_sindex, (r.x, r.y))
            ).astype("Int64")
            points_gdf = points_gdf.dropna(subset=['closest_way_id'])

            # Find streaks of consecutive points having same closest
            # OSM way.
            points_gdf['streak_id'] = (
                points_gdf['closest_way_id'] \
                != points_gdf['closest_way_id'].shift()
            ).cumsum().fillna(0)
            points_gdf['streak_length'] = (points_gdf
                .groupby('streak_id')['closest_way_id']
                .transform("count")
            )
            streaks = (
                points_gdf.groupby(
                    ['closest_way_id', 'streak_id'],
                    sort=False,
                )['streak_length']
                .first()
                .reset_index()
                .drop_duplicates(subset='closest_way_id', keep='first')
            )
            streaks = streaks[streaks['streak_length'] >= CONSEC_PTS]
            print(points_gdf)
            print(streaks)



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
    """Creates a track GeoDataFrame."""
    tracks = gpd.read_file(
        track_file,
        layer='driving_tracks',
        fid_as_index=True,
        columns=['utc_start'],
    )
    tracks = tracks.sort_values('utc_start')
    return tracks.to_crs(METRIC_CRS)

def get_closest_way(
    roads: gpd.GeoDataFrame,
    sindex: gpd.sindex.SpatialIndex,
    coords,
) -> int:
    """Looks up the closest OSM way to a given coordinate."""
    closest_idx = list(sindex.nearest(Point(coords), max_distance=MAX_DIST))[1]
    if len(closest_idx) == 0:
        return None
    return closest_idx[0]


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