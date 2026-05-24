"""Matches driving log tracks to OpenStreetMap roads."""
import argparse
import geopandas as gpd
import pandas as pd
import re
import tomllib
from pathlib import Path
from shapely.geometry import Point, LineString, MultiLineString
from shapely.wkb import loads as wkb_loads

from osm import load_osm

with open('config.toml', 'rb') as config_file:
    CONFIG = tomllib.load(config_file)

def count_roads(
    osm_data: Path,
    track_file: Path,
    output_dir: Path,
) -> None:
    """Matches tracks to unique OSM roads."""

    print(f"Loading OSM data from {osm_data}. This may take a while.")
    osm = load_osm(osm_data)
    ways = osm['ways']
    nodes = osm['node_ways']
    numbered_routes = osm['numbered_routes']
    way_routes = osm['way_routes']
    superroutes = osm['superroutes']
    route_superroutes = osm['route_superroutes']
    ways_sindex = osm['ways_sindex']

    tracks = load_tracks(track_file)

    # Temporarily filter to a small subset of tracks.
    tracks = tracks[tracks['utc_start'] < "2010-06-19"]

    visited_road_way_ids = set()
    visited_road_records = []
    visited_road_count = 0
    for track_fid, track in tracks.iterrows():
        print(f"Processing track {track_fid} ({track.utc_start})")
        for segment in track.geometry.geoms:
            seg_ways = get_segment_ways(ways, ways_sindex, segment).to_frame()
            seg_ways = seg_ways.join(
                ways[['road_name', 'route_ref', 'formatted_name']],
                on='way_id',
            )
            seg_ways = seg_ways.dropna(subset='formatted_name')
            for _, seg_way in seg_ways.iterrows():
                if seg_way.way_id in visited_road_way_ids:
                    continue
                if seg_way.way_id in way_routes:
                    # This way is part of at least one numbered route.
                    # Get associated ways from relations index.
                    for r_id in way_routes[seg_way.way_id]:
                        visited_road_count += 1
                        route_all_ways = set()
                        if r_id in route_superroutes:
                            # Route belongs to a superroute. Get ways
                            # from all sibling routes too.
                            for superroute_id in route_superroutes[r_id]:
                                superroute = superroutes[superroute_id]
                                print("superroute", superroute)
                                name = format_numbered_route(superroute)
                                for subroute_id in superroute['routes']:
                                    subroute = numbered_routes.get(subroute_id)
                                    if subroute is not None:
                                        route_all_ways.update(subroute['ways'])
                        else:
                            # Route does not belong to a superroute.
                            # Just use it as is.
                            route = numbered_routes[r_id]
                            name = format_numbered_route(route)
                            route_all_ways.update(route['ways'])
                        visited_road_way_ids.update(route_all_ways)
                        mutual_way_ids = ways.index.intersection(route_all_ways)
                        visited_road_records.append({
                            'visit_order': visited_road_count,
                            'name': name,
                            'is_numbered_route': True,
                            'track_fid': track_fid,
                            'track_utc_start': track.utc_start,
                            'geometry': MultiLineString(
                                ways['geometry'].loc[mutual_way_ids].to_list()
                            ),
                        })
                else:
                    # Follow ways by road name.
                    seg_road_ways = dict()
                    get_road_way_ids(
                        ways,
                        nodes,
                        visited_road_way_ids,
                        seg_road_ways,
                        seg_way.way_id,
                        road_name=seg_way.road_name,
                    )
                    visited_road_count += 1
                    visited_road_records.append({
                        'visit_order': visited_road_count,
                        'name': seg_way.formatted_name,
                        'is_numbered_route': False,
                        'track_fid': track_fid,
                        'track_utc_start': track.utc_start,
                        'geometry': MultiLineString(seg_road_ways.values()),
                    })

    if len(visited_road_records) == 0:
        print("No roads found.")
        return
    visited_road_gdf = gpd.GeoDataFrame(
        visited_road_records,
        geometry='geometry',
        crs=CONFIG['crs']['metric']
    ).to_crs(CONFIG['crs']['output'])
    gpkg_path = output_dir / CONFIG['output']['gpkg']
    visited_road_gdf.to_file(gpkg_path, layer='roads', driver='GPKG')
    print(f"Exported GeoPackage to {gpkg_path}")

def load_tracks(track_file: Path) -> gpd.GeoDataFrame:
    """Creates a track GeoDataFrame."""
    print("Loading tracks...", end=" ", flush=True)
    tracks = gpd.read_file(
        track_file,
        layer='driving_tracks',
        fid_as_index=True,
        columns=['utc_start'],
    )
    tracks = tracks.sort_values('utc_start')
    print("done.")
    return tracks.to_crs(CONFIG['crs']['metric'])

def format_numbered_route(route: dict) -> str:
    """Formats a numbered route identifier."""
    if route['network'] == "US:I":
        return f"I-{route['ref']}"
    if route['network'] == "US:US":
        return f"US-{route['ref']}"
    match = re.search(r"^US:(?!US)([A-Z]{2})$", route['network'])
    if match:
        return f"{match.group(1)}-{route['ref']}"
    return f"{route['network']}"

def format_road_name(row: pd.Series) -> str:
    """Formats a road name for an OSM way."""
    if pd.isna(row.route_ref):
        return row.road_name
    return row.route_ref

def get_closest_way(
    roads: gpd.GeoDataFrame,
    sindex: gpd.sindex.SpatialIndex,
    coords: tuple,
) -> int:
    """Looks up the closest OSM way to a given coordinate."""
    closest_idx = list(sindex.nearest(
        Point(coords),
        max_distance=CONFIG['search']['max_dist'],
    ))[1]
    if len(closest_idx) == 0:
        return None
    return roads.index[closest_idx[0]]

def get_road_way_ids(
    ways: gpd.GeoDataFrame,
    nodes: dict,
    visited_road_way_ids: set,
    seg_road_ways: dict,
    way_id: int,
    road_name: str | None = None,
    route_ref: str | None = None,
):
    """Traces a road."""
    stack = [way_id]
    while stack:
        current_way_id = stack.pop()
        if current_way_id in seg_road_ways:
            continue
        way = ways.loc[current_way_id]
        visited_road_way_ids.add(current_way_id)
        seg_road_ways[current_way_id] = way.geometry

        for node in [way.first_node, way.last_node]:
            for adj_way_id in nodes[node]:
                if adj_way_id in seg_road_ways:
                    continue
                adj_way = ways.loc[adj_way_id]
                if route_ref is None and adj_way.road_name == road_name:
                    stack.append(adj_way_id)
                elif route_ref in str(adj_way.route_ref).split(";"):
                    stack.append(adj_way_id)

def get_segment_ways(
    roads: gpd.GeoDataFrame,
    ways_sindex: gpd.sindex.SpatialIndex,
    segment: LineString,
) -> pd.Series:
    """Gets a Series of way IDs the segment traverses."""
    points_gdf = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(*zip(*segment.coords)),
        crs=CONFIG['crs']['metric'],
    )
    # Get the closest way for every point.
    points_gdf['closest_way_id'] = points_gdf.geometry.apply(
        lambda r: get_closest_way(roads, ways_sindex, (r.x, r.y))
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
    return streaks[
        streaks['streak_length'] >= CONFIG['search']['consec_pts']
    ]['closest_way_id'].rename('way_id')




if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="20k_roads",
        description="Matches GPS tracks to roads",
    )
    parser.add_argument('--osm',
        type=Path,
        required=True,
        help="OpenStreetMap PBF file covering the region of the tracks",
    )
    parser.add_argument('--tracks',
        type=Path,
        required=True,
        help="GeoPackage file containing driving tracks",
    )
    parser.add_argument('--output-dir',
        type=Path,
        required=True,
        help="Directory to store output data",
    )
    args = parser.parse_args()
    count_roads(args.osm, args.tracks, args.output_dir)
