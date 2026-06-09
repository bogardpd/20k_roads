"""Matches driving log tracks to OpenStreetMap roads."""
import argparse
import geopandas as gpd
import numpy as np
import tomllib
import colorama
from itertools import groupby
from pathlib import Path
from shapely.geometry import Point, LineString, MultiLineString
from tqdm import tqdm

from osm import OSMDataContainer

with open('config.toml', 'rb') as config_file:
    CONFIG = tomllib.load(config_file)
colorama.init()

class RoadCounter():
    """Counts roads that have been traveled on."""
    def __init__(
        self,
        osm_pbf_path: Path,
        tracks_path: Path,
        output_path: Path,
    ):
        self.osm_pbf_path: Path = osm_pbf_path
        self.tracks_path: Path = tracks_path
        self.output_path: Path = output_path
        self.ways: dict | None = None
        self.ways_gdf: gpd.GeoDataFrame | None = None
        self.ways_index: list | None = None
        self.rels: dict | None = None
        self.rel_parents: dict | None = None
        self.way_rels: dict | None = None
        self.ways_sindex: gpd.sindex.SpatialIndex | None = None
        self.rel_routes: dict = {}
        self.tracks: gpd.GeoDataFrame | None = None
        self.routes: dict = {}
        self._route_inc: int = 0
        self.visited_road_count: int = 0
        self.visited_road_way_ids: set = set()
        self.visited_road_records: list = []

    def collect_roads(self):
        """Builds a collection of traveled roads."""
        self._load_osm()
        self._load_tracks()
        print("Processing driving tracks...")
        with tqdm(
            self.tracks.iterrows(),
            total=len(self.tracks),
            ascii=True,
        ) as prog_bar:
            for track_fid, track in prog_bar:
                for segment in track.geometry.geoms:
                    for seg_way_id in self._get_segment_ways(segment):
                        self._trace_road(seg_way_id, track_fid)
                prog_bar.set_postfix(roads=self.visited_road_count)

    def export_roads(self):
        """Saves road data to a file."""
        if not self.visited_road_records:
            print("No roads found.")
            return
        visited_road_gdf = gpd.GeoDataFrame(
            self.visited_road_records,
            geometry='geometry',
            crs=CONFIG['crs']['metric']
        ).to_crs(CONFIG['crs']['output'])
        gpkg_path = self.output_path
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
        mutual_way_ids = route_way_ids & self.ways.keys()
        record = {
            'visit_order': self.visited_road_count,
            'name': format_numbered_route(self.rels[rel_id]),
            'is_numbered_route': True,
            'track_fid': track_fid,
            'track_utc_start': self.tracks.loc[track_fid]['utc_start'],
            'origin_way': None,
            'origin_rel': rel_id,
            'geometry': MultiLineString(
                [self.ways[w]['geometry'] for w in mutual_way_ids]
            ),
        }
        self.visited_road_records.append(record)
        return route_id

    def _all_equal(self, iterable):
        """Returns true if all elements are equal."""
        g = groupby(iterable)
        return next(g, True) and not next(g, False)

    def _get_named_road_way_ids(
        self,
        seg_road_geoms: dict,
        way_id: int,
        road_name: str,
    ):
        """Traces adjacent ways with the same name to build a road."""
        stack = [way_id]
        checked_ways = set()
        while stack:
            current_way_id= stack.pop()
            if current_way_id in checked_ways:
                continue
            checked_ways.add(current_way_id)
            way = self.ways[current_way_id]
            if way['road_name'] == road_name:
                # Road name matches, so store its way.
                seg_road_geoms[current_way_id] = way['geometry']
                self.visited_road_way_ids.add(current_way_id)
                # Check for nearby ways with same name.
                nearby_idx = self.ways_sindex.query(
                    way['geometry'],
                    predicate='dwithin',
                    distance=CONFIG['search']['max_dist_gap'],
                    output_format='indices'
                )
                nearby = [self.ways_index[n] for n in nearby_idx]
                stack.extend(nearby)

    def _get_segment_ways(self, segment: LineString) -> list[int]:
        """Gets a list of way IDs the segment traverses."""
        coords = np.array(segment.coords)
        if len(coords) < CONFIG['search']['consec_pts']:
            # The segment is not long enough to match any ways.
            return []

        # Get the closest way for every point.
        input_idx, result_idx = self.ways_sindex.nearest(
            [Point(x, y) for x, y in coords],
            max_distance=CONFIG['search']['max_dist_track'],
            return_all=False,
        )
        closest_way_ids = [None] * len(coords)
        for i, r in zip(input_idx, result_idx):
            closest_way_ids[i] = self.ways_index[r]
        closest_way_ids = [w for w in closest_way_ids if w is not None]
        if not closest_way_ids:
            return []

        closest_ways = [{
            'id': wid,
            'road_name': self.ways[wid]['road_name'],
            'way_rels': self.way_rels[wid]
        } for wid in closest_way_ids]

        # Find rolling windows where a certain number of points in a row
        # have the same way ID, have the same name, or share at least
        # one relation.
        streaks = []
        for i in range(len(closest_ways) - CONFIG['search']['consec_pts'] + 1):
            current_id = closest_ways[i]['id']
            if closest_ways[i]['id'] in streaks:
                continue
            window = closest_ways[i:i+CONFIG['search']['consec_pts']]
            if self._all_equal([w['id'] for w in window]):
                # All window points share same way ID.
                streaks.append(current_id)
                continue
            if self._all_equal([w['road_name'] for w in window]):
                # All window points share same name.
                streaks.append(current_id)
                continue
            if set.intersection(*[w['way_rels'] for w in window]):
                # All window points share at least one relation.
                # This assumes way_rels have already been filtered for
                # valid networks in RoadHandler in osm.py.
                streaks.append(current_id)
                continue
        return streaks

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
        osm = OSMDataContainer(self.osm_pbf_path)
        osm.load_data()
        self.ways = osm.ways
        self.ways_gdf = osm.ways_gdf
        self.ways_index = list(osm.ways.keys()) # Positional index
        self.rels = osm.rels
        self.rel_parents = osm.rel_parents
        self.way_rels = osm.way_rels
        osm = None
        print("Building spatial index...")
        self.ways_sindex = self.ways_gdf.sindex

    def _load_tracks(self):
        """Loads GeoPackage driving track data."""
        print("Loading driving tracks...")
        tracks = gpd.read_file(
            self.tracks_path,
            layer='driving_tracks',
            fid_as_index=True,
            columns=['utc_start'],
        )
        tracks = tracks.sort_values('utc_start')
        self.tracks = tracks.to_crs(CONFIG['crs']['metric'])

    def _trace_road(self, way_id: int, track_fid: int):
        """Creates a road record starting with a given way."""
        has_valid_rels = way_id in self.way_rels
        route_refs = []
        route_way_sets = []

        # Find numbered routes.
        if has_valid_rels:
            # This way is part of at least one valid relation. Build
            # numbered routes from relations.
            for rel_id in self.way_rels[way_id]:
                if rel_id not in self.rel_routes:
                    self._add_route(rel_id, track_fid)
                # Get route ref and geometry:
                if self.rels[rel_id]['ref'] is not None:
                    route_refs.append(self.rels[rel_id]['ref'])
                route = self.routes[self.rel_routes[rel_id]]
                route_way_sets.append(route['way_ids'])

        # Find named road.
        if way_id not in self.visited_road_way_ids:
            way = self.ways[way_id]
            if way['road_name'] is None:
                return
            if any(ref in way['road_name'] for ref in route_refs):
                # Name matches a numbered route we're already using.
                return
            seg_road_geoms = {}
            self._get_named_road_way_ids(
                seg_road_geoms,
                way_id,
                way['road_name'],
            )
            # Check that named road is distinct enough from any numbered
            # routes sharing the same way.
            named_road_way_ids = set(seg_road_geoms.keys())
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
                'name': way['road_name'],
                'is_numbered_route': False,
                'track_fid': track_fid,
                'track_utc_start': self.tracks.loc[track_fid]['utc_start'],
                'origin_way': way_id,
                'origin_rel': None,
                'geometry': MultiLineString(seg_road_geoms.values()),
            })


def format_numbered_route(route: dict) -> str:
    """Formats a numbered route identifier."""
    network = route['network'].split(":")
    ref = route['ref']
    if network[0] == "US":
        name = "-".join(
            [n for n in [network[1], ref] if n is not None]
        )
        if len(network) > 2:
            return f"{name} {" ".join(network[2:])}"
        return name
    if network[0] == "AU":
        if network[1] == "WA":
            if network[2] == "NR":
                return f"National Route {ref}"
            if network[2] == "S":
                return f"State Route {ref}"
        return f"Route {ref}"
    if network[0] == "BAB":
        return ref
    if network[0] == "CA":
        if network[1] == "transcanada":
            return f"Trans-Canada Highway {ref}"
        return f"{network[1]} Highway {ref}"
    if network[0] == "JP":
        if network[1] == "national":
            return f"National Route {ref}"
        if network[1] == "E":
            return ref
    if network[0] == "NZ":
        return f"SH {ref}"
    return " ".join(
        [n for n in [route['network'], ref] if n is not None]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="20000 Roads",
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
    parser.add_argument('--output',
        type=Path,
        required=True,
        help="GeoPackage (.gpkg) file to store output data",
    )
    args = parser.parse_args()

    rc = RoadCounter(args.osm, args.tracks, args.output)
    rc.collect_roads()
    print(
        f"Found {colorama.Fore.GREEN}{colorama.Style.BRIGHT}"
        f"{len(rc.visited_road_records)} roads{colorama.Style.RESET_ALL}."
    )
    rc.export_roads()
