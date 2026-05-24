"""Matches driving log tracks to OpenStreetMap roads."""
import argparse
import geopandas as gpd
import hashlib
import json
import osmium
import pandas as pd
import pickle
import re
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from shapely.geometry import Point, LineString, MultiLineString
from shapely.wkb import loads as wkb_loads

with open('config.toml', 'rb') as config_file:
    CONFIG = tomllib.load(config_file)

class OSMDataContainer():
    """Holds OSM data."""
    def __init__(self, osm_data_path):
        self.osm_data_path: Path = osm_data_path
        self.osm_cache_path: Path = self.osm_data_path.with_suffix('.pickle')
        self.osm_checksum = self._osm_checksum()
        self.osm_checksum_path = self.osm_data_path.with_suffix(
            '.checksum.json'
        )
        self.ways: gpd.GeoDataFrame | None = None
        self.ways_sindex = None
        self.node_ways: dict | None = None
        self.load_osm()

    def load_osm(self):
        """Loads OSM data."""
        if self.osm_checksum_path.is_file() and self.osm_cache_path.is_file():
            with open(self.osm_checksum_path, 'r', encoding='utf-8') as csf:
                osm_cache_checksum = json.load(csf)['checksum']
            if osm_cache_checksum == self.osm_checksum:
                # Load cached data.
                print("Loading OSM from cache...", end=" ", flush=True)
                with open(self.osm_cache_path, 'rb') as cf:
                    data = pickle.load(cf)
            else:
                print(
                    "OSM PBF has changed since last cache. Processing...",
                    end=" ",
                    flush=True,
                )
                data = self._process_osm()
        else:
            print(
                "No cache available. Processing OSM PBF...",
                end=" ",
                flush=True,
            )
            data = self._process_osm()
        print("done.")

        self.ways = data['ways']
        self.node_ways = data['node_ways']
        self.numbered_routes = data['numbered_routes']
        self.way_routes = data['way_routes']
        self.ways_sindex = data['ways_sindex']

    def _osm_checksum(self):
        h = hashlib.sha256()
        with open(self.osm_data_path, 'rb') as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()

    def _process_osm(self) -> dict:
        """Processes the provided OSM PBF file."""
        handler = RoadHandler()
        handler.apply_file(self.osm_data_path, locations=True)
        metadata = {
            'source': str(self.osm_data_path),
            'checksum': self.osm_checksum,
            'processed_at': datetime.now(timezone.utc).isoformat(),
        }
        with open(self.osm_checksum_path, 'w', encoding='utf-8') as f:
            # Store checksum of OSM PBF file.
            json.dump(metadata, f, indent=2)
        data = {
            'ways': handler.ways,
            'node_ways': handler.node_ways,
            'numbered_routes': handler.numbered_routes,
            'way_routes': handler.way_routes,
            'ways_sindex': handler.ways.sindex, # Build spatial index
        }
        # Cache processed data.
        with open(self.osm_cache_path, 'wb') as f:
            pickle.dump(data, f)
        return data


class RoadHandler(osmium.SimpleHandler):
    """Processes OSM roads."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.node_ways = defaultdict(set)
        self.numbered_routes = {}
        self.way_routes = defaultdict(set)
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

    def relation(self, r):
        """Processing for each relation in OSM data."""
        tags = dict(r.tags)
        if tags.get('type') != "route" or tags.get('route') != "road":
            return
        if tags.get('network') not in CONFIG['networks']:
            return
        members = [m.ref for m in r.members if m.type == "w"]
        if len(members) == 0:
            return
        self.numbered_routes[r.id] = {
            'network': tags.get('network'),
            'ref': tags.get('ref'),
            'ways': members,
        }
        for member in members:
            self.way_routes[member].add(r.id)

    @property
    def ways(self) -> gpd.GeoDataFrame:
        """Creates a GeoDataFrame of ways."""
        ways = gpd.GeoDataFrame(
            self.rows,
            crs=CONFIG['crs']['osm'],
        ).set_index('id')
        ways['formatted_name'] = ways.apply(format_road_name, axis=1)
        return ways.to_crs(CONFIG['crs']['metric'])


def count_roads(
    osm_data: Path,
    track_file: Path,
    output_dir: Path,
) -> None:
    """Matches tracks to unique OSM roads."""

    print(f"Loading OSM data from {osm_data}. This may take a while.")
    osmdc = OSMDataContainer(osm_data)
    ways = osmdc.ways
    nodes = osmdc.node_ways
    numbered_routes = osmdc.numbered_routes
    way_routes = osmdc.way_routes
    ways_sindex = osmdc.ways_sindex

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
                        route = numbered_routes[r_id]
                        visited_road_way_ids.update(route['ways'])
                        visited_road_count += 1
                        visited_road_records.append({
                            'visit_order': visited_road_count,
                            'name': format_numbered_route(route),
                            'is_numbered_route': True,
                            'track_fid': track_fid,
                            'track_utc_start': track.utc_start,
                            'geometry': MultiLineString(
                                ways['geometry'].loc[route['ways']].to_list()
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
