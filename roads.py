"""Matches driving log tracks to OpenStreetMap roads."""
import argparse
import geopandas as gpd
import pandas as pd
import tomllib
from pathlib import Path
from shapely.geometry import Point, LineString, MultiLineString
from tqdm import tqdm

from osm import load_osm

with open('config.toml', 'rb') as config_file:
    CONFIG = tomllib.load(config_file)

class RoadCounter():
    """Counts roads that have been traveled on."""
    def __init__(
        self,
        osm_pbf_path: Path,
        tracks_path: Path,
        output_dir: Path,
    ):
        self.osm_pbf_path: Path = osm_pbf_path
        self.tracks_path: Path = tracks_path
        self.output_dir: Path = output_dir
        self.ways: gpd.GeoDataFrame | None = None
        self.way_nodes: dict | None = None
        self.node_ways: dict | None = None
        self.rels: dict | None = None
        self.rel_parents: dict | None = None
        self.rel_routes: dict = {}
        self.way_rels: dict | None = None
        self.ways_sindex: gpd.sindex.SpatialIndex | None = None
        self.tracks: gpd.GeoDataFrame | None = None
        self.routes: dict = {}
        self._route_inc: int = 0
        self.root_route_way_ids: dict = {}
        self.visited_road_count: int = 0
        self.visited_road_way_ids: set = set()
        self.visited_road_records: list = []

    def collect_roads(self):
        """Builds a collection of traveled roads."""
        print(
            f"Loading OSM data from {self.osm_pbf_path}. This may take a "
            "while."
        )
        self._load_osm()
        self._load_tracks()
        print("Processing tracks...")
        with tqdm(
            self.tracks.iterrows(),
            total=len(self.tracks),
            ascii=True,
        ) as prog_bar:
            for track_fid, track in prog_bar:
                for segment in track.geometry.geoms:
                    self._collect_segment(segment, track_fid)
                prog_bar.set_postfix(roads=self.visited_road_count)

    def export_roads(self):
        """Saves road data to a file."""
        if len(self.visited_road_records) == 0:
            print("No roads found.")
            return
        visited_road_gdf = gpd.GeoDataFrame(
            self.visited_road_records,
            geometry='geometry',
            crs=CONFIG['crs']['metric']
        ).to_crs(CONFIG['crs']['output'])
        gpkg_path = self.output_dir / CONFIG['output']['gpkg']
        visited_road_gdf.to_file(gpkg_path, layer='roads', driver='GPKG')
        print(f"Exported GeoPackage to {gpkg_path}.")


    def _add_route(self, rel_id: int, track_fid: int):
        """Creates a numbered route record."""
        route_id = self._route_inc
        self._route_inc += 1
        route_way_ids = self._get_route_way_ids(rel_id, route_id)
        self.routes[route_id] = {
            'way_ids': route_way_ids
        }
        self.rel_routes[rel_id] = route_id
        self.visited_road_count += 1
        mutual_way_ids = self.ways.index.intersection(route_way_ids)
        record = {
            'visit_order': self.visited_road_count,
            'name': format_numbered_route(self.rels[rel_id]),
            'is_numbered_route': True,
            'track_fid': track_fid,
            'track_utc_start': self.tracks.loc[track_fid]['utc_start'],
            'origin_way': None,
            'origin_rel': rel_id,
            'geometry': MultiLineString(
                self.ways['geometry'].loc[mutual_way_ids].to_list()
            ),
        }
        self.visited_road_records.append(record)
        return route_id

    def _collect_segment(self, segment: LineString, track_fid: int):
        """Collects roads for a given driving track segment."""
        seg_ways = self._get_segment_ways(segment).to_frame()
        seg_ways = seg_ways.join(
            self.ways[['road_name', 'route_ref', 'formatted_name']],
            on='way_id',
        )
        for _, seg_way in seg_ways.iterrows():
            self._trace_road(seg_way, track_fid)

    def _get_closest_way(self, coords: tuple) -> int:
        """Looks up the closest OSM way to a given coordinate."""
        closest_idx = list(self.ways_sindex.nearest(
            Point(coords),
            max_distance=CONFIG['search']['max_dist'],
        ))[1]
        if len(closest_idx) == 0:
            return None
        return self.ways.index[closest_idx[0]]

    def _get_named_road_way_ids(
        self,
        seg_road_ways: dict,
        way_id: int,
        road_name: str,
    ):
        """Traces adjacent ways with the same name to build a road."""
        stack = [way_id]
        while stack:
            current_way_id = stack.pop()
            if current_way_id in seg_road_ways:
                continue
            way = self.ways.loc[current_way_id]
            self.visited_road_way_ids.add(current_way_id)
            seg_road_ways[current_way_id] = way.geometry

            for node in self.way_nodes[current_way_id]:
                for adj_way_id in self.node_ways[node]:
                    if adj_way_id in seg_road_ways:
                        continue
                    adj_way = self.ways.loc[adj_way_id]
                    if pd.isna(adj_way.road_name):
                        continue
                    if adj_way.road_name == road_name:
                        stack.append(adj_way_id)

    def _get_segment_ways(self, segment: LineString) -> pd.Series:
        """Gets a Series of way IDs the segment traverses."""
        points_gdf = gpd.GeoDataFrame(
            geometry=gpd.points_from_xy(*zip(*segment.coords)),
            crs=CONFIG['crs']['metric'],
        )
        # Get the closest way for every point.
        closest_way_ids = points_gdf.geometry.apply(
            lambda r: self._get_closest_way((r.x, r.y))
        )
        points_gdf['closest_way_id'] = pd.array(
            # Handles cases where all values are null.
            [pd.NA if pd.isna(c) else int(c) for c in closest_way_ids],
            dtype="Int64",
        )
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

    def _get_route_way_ids(self, rel_id: int, route_id: int) -> set:
        """Gets relations and ways for a route from a given rel_id."""
        stack = [rel_id]
        checked_rel_ids = set()
        route_way_ids = set()
        rel = self.rels[rel_id]
        ref = rel['ref']
        network = rel['network']
        while stack:
            cur_rel_id = stack.pop()
            if cur_rel_id in checked_rel_ids:
                continue
            checked_rel_ids.add(cur_rel_id)
            cur_rel = self.rels.get(cur_rel_id)
            if cur_rel is None:
                continue
            if cur_rel['ref'] != ref or cur_rel['network'] != network:
                continue
            # Store rel and way ids.
            self.rel_routes[cur_rel_id] = route_id
            route_way_ids.update(cur_rel['ways'])
            # Find parents and children to check.
            parents = self.rel_parents.get(cur_rel_id)
            if parents is not None:
                stack.extend(parents)
            children = cur_rel['child_relations']
            if children is not None:
                stack.extend(children)
        return route_way_ids

    def _load_osm(self):
        """Loads OSM PBF data."""
        osm = load_osm(self.osm_pbf_path)
        self.ways = osm['ways']
        self.way_nodes = osm['way_nodes']
        self.node_ways = osm['node_ways']
        self.rels = osm['routes']
        self.rel_parents = osm['rel_parents']
        self.way_rels = osm['way_rels']
        self.ways_sindex = osm['ways_sindex']

    def _load_tracks(self):
        """Loads GeoPackage driving track data."""
        print("Loading tracks...", end=" ", flush=True)
        tracks = gpd.read_file(
            self.tracks_path,
            layer='driving_tracks',
            fid_as_index=True,
            columns=['utc_start'],
        )
        tracks = tracks.sort_values('utc_start')
        print("done.")
        self.tracks = tracks.to_crs(CONFIG['crs']['metric'])

    def _trace_road(self, way: pd.Series, track_fid: int):
        """Creates a road record starting with a given way."""
        has_valid_rels = way.way_id in self.way_rels
        route_refs = []
        route_way_sets = []

        # Find numbered routes.
        if has_valid_rels:
            # This way is part of at least one valid relation. Build
            # numbered routes from relations.
            for rel_id in self.way_rels[way.way_id]:
                if rel_id not in self.rel_routes:
                    self._add_route(rel_id, track_fid)
                # Get route ref and geometry:
                if self.rels[rel_id]['ref'] is not None:
                    route_refs.append(self.rels[rel_id]['ref'])
                route = self.routes[self.rel_routes[rel_id]]
                route_way_sets.append(route['way_ids'])

        # Find named road.
        if way.way_id not in self.visited_road_way_ids:
            if pd.isna(way.road_name):
                return
            if any(ref in way.road_name for ref in route_refs):
                # Name matches a numbered route we're already using.
                return
            seg_road_ways = {}
            self._get_named_road_way_ids(
                seg_road_ways,
                way.way_id,
                way.road_name,
            )
            # Check that named road is distinct enough from any numbered
            # routes sharing the same way.
            named_road_way_ids = set(seg_road_ways.keys())
            for route_way_ids in route_way_sets:
                road_way_count = len(named_road_way_ids)
                unique_way_count = len(named_road_way_ids - route_way_ids)
                ratio = unique_way_count/road_way_count
                if ratio < CONFIG['search']['distinctness_ratio']:
                    return
            # Create named road record.
            self.visited_road_count += 1
            self.visited_road_records.append({
                'visit_order': self.visited_road_count,
                'name': way.road_name,
                'is_numbered_route': False,
                'track_fid': track_fid,
                'track_utc_start': self.tracks.loc[track_fid]['utc_start'],
                'origin_way': way.way_id,
                'origin_rel': None,
                'geometry': MultiLineString(seg_road_ways.values()),
            })


def format_numbered_route(route: dict) -> str:
    """Formats a numbered route identifier."""
    network = route['network'].split(":")
    if network[0] == "US":
        name = "-".join(
            [n for n in [network[1], route['ref']] if n is not None]
        )
        if len(network) > 2:
            return f"{name} {" ".join(network[2:])}"
        return name
    if network[0] == "CA":
        if network[1] == "transcanada":
            return f"Trans-Canada Highway {route['ref']}"
        if network[1] == "ON":
            return f"Ontario Highway {route['ref']}"
    return " ".split(
        [n for n in [route['network'], route['ref']] if n is not None]
    )


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

    rc = RoadCounter(args.osm, args.tracks, args.output_dir)
    rc.collect_roads()
    rc.export_roads()
