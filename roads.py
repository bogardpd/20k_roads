import argparse
import geopandas as gpd
import pandas as pd
import osmium
import re
import tomllib
from collections import defaultdict
from pathlib import Path
from shapely.geometry import Point, LineString, MultiLineString
from shapely.wkb import loads as wkb_loads

with open('config.toml', 'rb') as f:
    CONFIG = tomllib.load(f)

class RoadHandler(osmium.SimpleHandler):
    """Processes OSM roads."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.node_ways = defaultdict(set)
        self._factory = osmium.geom.WKBFactory()

    def way(self, w):
        """Processing for each way in OSM data."""
        if 'highway' not in w.tags:
            return
        try:
            geom = wkb_loads(self._factory.create_linestring(w), hex=True)
        except osmium.InvalidLocationError:
            return
        self.rows.append({
            'geometry': geom,
            'id': w.id,
            'highway': w.tags.get('highway'),
            'road_name': w.tags.get('name'),
            'route_ref': w.tags.get('ref'),
            'first_node': w.nodes[0].ref,
            'last_node': w.nodes[-1].ref,
        })
        for node in [w.nodes[0], w.nodes[-1]]:
            self.node_ways[node.ref].add(w.id)

def find_roads(
    osm_data: Path,
    state_data: Path,
    track_file: Path,
    output_dir: Path,
) -> None:
    """Matches tracks to unique OSM roads."""

    print("Loading OSM data...", end=" ")
    processed_osm = build_osm(osm_data, state_data)
    ways = processed_osm['ways']
    nodes = processed_osm['nodes']
    roads_sindex = ways.sindex # Build spatial index
    print("done.")

    print("Loading tracks...", end=" ")
    tracks = build_tracks(track_file)
    print("done.")

    # Temporarily filter to a small subset of tracks.
    tracks = tracks[tracks['utc_start'] < "2010-01-16"]

    unique_roads = {}
    visited_road_way_ids = set()
    visited_road_records = []
    visited_road_count = 0
    for track_fid, track in tracks.iterrows():
        print(f"Processing track {track_fid} ({track.utc_start})")
        for segment in track.geometry.geoms:
            seg_ways = get_segment_ways(ways, roads_sindex, segment).to_frame()
            seg_ways = seg_ways.join(
                ways[['road_name', 'route_ref', 'unique_name']],
                on='way_id',
            )
            seg_ways = seg_ways.dropna(subset='unique_name')
            for _, seg_way in seg_ways.iterrows():
                if seg_way.way_id in visited_road_way_ids:
                    continue
                if pd.isna(seg_way.route_ref):
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
                        'name': seg_way.unique_name,
                        'geometry': MultiLineString(seg_road_ways.values()),
                    })
                else:
                    for route_ref in seg_way.route_ref.split(";"):
                        seg_road_ways = dict()
                        get_road_way_ids(
                            ways,
                            nodes,
                            visited_road_way_ids,
                            seg_road_ways,
                            seg_way.way_id,
                            route_ref=route_ref,
                        )
                        visited_road_count += 1
                        visited_road_records.append({
                            'visit_order': visited_road_count,
                            'name': route_ref,
                            'geometry': MultiLineString(
                                seg_road_ways.values()
                            ),
                        })
    
    visited_road_gdf = gpd.GeoDataFrame(
        visited_road_records,
        geometry='geometry',
        crs=CONFIG['crs']['metric']
    ).to_crs(CONFIG['crs']['output'])
    gpkg_path = output_dir / CONFIG['output']['gpkg']
    visited_road_gdf.to_file(gpkg_path, layer='roads', driver='GPKG')
    print(f"Exported GeoPackage to {gpkg_path}")


def build_osm(osm_data: Path, state_data: Path) -> dict:
    """Creates a road GeoDataFrame and a node lookup table."""

    # Load U.S. states.
    states = gpd.read_file(state_data)
    states = states[['STUSPS', 'NAME', 'geometry']].rename(columns={
        'STUSPS': 'state_abbr',
        'NAME': 'state_name',
    }).to_crs(CONFIG['crs']['osm'])

    # Process OSM ways.
    handler = RoadHandler()
    handler.apply_file(osm_data, locations=True)
    ways = gpd.GeoDataFrame(
        handler.rows,
        crs=CONFIG['crs']['osm'],
    ).set_index('id')

    # Spatially join U.S. states onto ways.
    ways = gpd.sjoin(ways, states, how='left', predicate='within')
    ways['unique_name'] = ways.apply(unique_road_name, axis=1)

    return {
        'ways': ways.to_crs(CONFIG['crs']['metric']),
        'nodes': handler.node_ways
    }

def build_tracks(track_file: Path) -> gpd.GeoDataFrame:
    """Creates a track GeoDataFrame."""
    tracks = gpd.read_file(
        track_file,
        layer='driving_tracks',
        fid_as_index=True,
        columns=['utc_start'],
    )
    tracks = tracks.sort_values('utc_start')
    return tracks.to_crs(CONFIG['crs']['metric'])

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
    roads_sindex: gpd.sindex.SpatialIndex,
    segment: LineString,
) -> pd.Series:
    """Gets a Series of way IDs the segment traverses."""
    points_gdf = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(*zip(*segment.coords)),
        crs=CONFIG['crs']['metric'],
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
    return streaks[
        streaks['streak_length'] >= CONFIG['search']['consec_pts']
    ]['closest_way_id'].rename('way_id')

def unique_road_name(row: pd.Series) -> str:
    """Formats a road name for an OSM way."""
    if pd.isna(row.route_ref):
        return row.road_name
    if re.match(r'SR ', row.route_ref):
        # Prepend state abbreviation to state route.
        return f"{row.state_abbr} {row.route_ref}"
    return row.route_ref


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="find_roads",
        description="Matches GPS tracks to roads",
    )
    parser.add_argument('--osm',
        type=Path,
        required=True,
        help="OpenStreetMap PBF file covering the region of the tracks",
    )
    parser.add_argument('--states',
        type=Path,
        required=True,
        help="Census TIGER data for U.S. state boundaries",
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
    find_roads(args.osm, args.states, args.tracks, args.output_dir)