"""Matches driving log tracks to OpenStreetMap roads."""
import argparse
import geopandas as gpd
import pandas as pd
import re
import tomllib
from pathlib import Path
from shapely.geometry import Point, LineString, MultiLineString

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
        self.node_ways: dict | None = None
        self.routes: dict | None = None
        self.way_routes: dict | None = None
        self.superroutes: dict | None = None
        self.route_superroutes: dict | None = None
        self.ways_sindex: gpd.sindex.SpatialIndex | None = None
        self.tracks: gpd.GeoDataFrame | None = None
        self.visited_road_count: int = 0
        self.visited_road_way_ids: set = set()
        self.visited_road_records: list = []

    def collect_roads(self):
        """Builds a collection of traveled roads."""
        print(f"Loading OSM data from {self.osm_pbf_path}. This may take a while.")
        self._load_osm()
        self._load_tracks()
        for track_fid, track in self.tracks.iterrows():
            print(f"Processing track {track_fid} ({track.utc_start})")
            for segment in track.geometry.geoms:
                self._collect_segment(segment, track_fid)

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


    def _collect_segment(self, segment, track_fid):
        """Collects roads for a given driving track segment."""
        seg_ways = self._get_segment_ways(segment).to_frame()
        seg_ways = seg_ways.join(
            self.ways[['road_name', 'route_ref', 'formatted_name']],
            on='way_id',
        )
        seg_ways = seg_ways.dropna(subset='formatted_name')
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

            for node in [way.first_node, way.last_node]:
                for adj_way_id in self.node_ways[node]:
                    if adj_way_id in seg_road_ways:
                        continue
                    adj_way = self.ways.loc[adj_way_id]
                    if adj_way.road_name == road_name:
                        stack.append(adj_way_id)

    def _get_segment_ways(self, segment: LineString) -> pd.Series:
        """Gets a Series of way IDs the segment traverses."""
        points_gdf = gpd.GeoDataFrame(
            geometry=gpd.points_from_xy(*zip(*segment.coords)),
            crs=CONFIG['crs']['metric'],
        )
        # Get the closest way for every point.
        points_gdf['closest_way_id'] = points_gdf.geometry.apply(
            lambda r: self._get_closest_way((r.x, r.y))
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

    def _load_osm(self):
        """Loads OSM PBF data."""
        osm = load_osm(self.osm_pbf_path)
        self.ways = osm['ways']
        self.node_ways = osm['node_ways']
        self.routes = osm['numbered_routes']
        self.way_routes = osm['way_routes']
        self.superroutes = osm['superroutes']
        self.route_superroutes = osm['route_superroutes']
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

        # Temporarily filter to a small subset of tracks.
        tracks = tracks[tracks['utc_start'] < "2010-06-19"]

        self.tracks = tracks.to_crs(CONFIG['crs']['metric'])

    def _trace_road(self, way: pd.Series, track_fid: int):
        """Creates a road record starting with a given way."""
        if way.way_id in self.visited_road_way_ids:
            return
        if way.way_id in self.way_routes:
            # This way is part of at least one numbered route.
            # Get associated ways from relations index.
            for r_id in self.way_routes[way.way_id]:
                self.visited_road_count += 1
                route_all_ways = set()
                if r_id in self.route_superroutes:
                    # Route belongs to a superroute. Get ways
                    # from all sibling routes too.
                    for superroute_id in self.route_superroutes[r_id]:
                        superroute = self.superroutes[superroute_id]
                        print("superroute", superroute)
                        name = format_numbered_route(superroute)
                        for subroute_id in superroute['routes']:
                            subroute = self.routes.get(subroute_id)
                            if subroute is not None:
                                route_all_ways.update(subroute['ways'])
                else:
                    # Route does not belong to a superroute.
                    # Just use it as is.
                    route = self.routes[r_id]
                    name = format_numbered_route(route)
                    route_all_ways.update(route['ways'])
                self.visited_road_way_ids.update(route_all_ways)
                mutual_way_ids = self.ways.index.intersection(route_all_ways)
                self.visited_road_records.append({
                    'visit_order': self.visited_road_count,
                    'name': name,
                    'is_numbered_route': True,
                    'track_fid': track_fid,
                    'track_utc_start': self.tracks.loc[track_fid]['utc_start'],
                    'geometry': MultiLineString(
                        self.ways['geometry'].loc[mutual_way_ids].to_list()
                    ),
                })
        else:
            # Follow ways by road name.
            seg_road_ways = {}
            self._get_named_road_way_ids(
                seg_road_ways,
                way.way_id,
                way.road_name,
            )
            self.visited_road_count += 1
            self.visited_road_records.append({
                'visit_order': self.visited_road_count,
                'name': way.formatted_name,
                'is_numbered_route': False,
                'track_fid': track_fid,
                'track_utc_start': self.tracks.loc[track_fid]['utc_start'],
                'geometry': MultiLineString(seg_road_ways.values()),
            })


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
