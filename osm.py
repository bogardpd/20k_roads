"""OSM processing helper functions."""
import geopandas as gpd
import hashlib
import json
import osmium
import pandas as pd
import pickle
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from shapely.wkb import loads as wkb_loads

with open('config.toml', 'rb') as config_file:
    CONFIG = tomllib.load(config_file)

class RoadHandler(osmium.SimpleHandler):
    """Processes OSM roads."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.node_ways = defaultdict(set)
        self.routes = {}
        self.rel_parents = defaultdict(set)
        self.way_rels = defaultdict(set)
        self._factory = osmium.geom.WKBFactory()

    def way(self, w):
        """Processing for each way in OSM data."""
        if w.tags.get('highway') not in CONFIG['search']['highway_types']:
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
        if tags.get('network') not in CONFIG['networks']:
            return
        if tags.get('ref') is None:
            return
        if tags.get('route') != "road":
            return
        if tags.get('type') not in ["route", "superroute"]:
            return
        ways = [m.ref for m in r.members if m.type == "w"]
        child_relations = [m.ref for m in r.members if m.type == "r"]
        if len(ways) == 0 and len(child_relations) == 0:
            return
        self.routes[r.id] = {
            'network': tags.get('network'),
            'ref': tags.get('ref'),
            'child_relations': child_relations,
            'ways': ways,
        }
        for cr in child_relations:
            self.rel_parents[cr].add(r.id)
        for w in ways:
            self.way_rels[w].add(r.id)

    @property
    def ways(self) -> gpd.GeoDataFrame:
        """Creates a GeoDataFrame of ways."""
        ways = gpd.GeoDataFrame(
            self.rows,
            crs=CONFIG['crs']['osm'],
        ).set_index('id')
        ways['road_name'] = ways['road_name'].astype("string")
        ways['route_ref'] = ways['route_ref'].astype("string")
        ways['formatted_name'] = ways.apply(format_road_name, axis=1)
        return ways.to_crs(CONFIG['crs']['metric'])

def load_osm(osm_data_path):
    """Loads data from OSM PBF file."""
    cache_path = _cache_path(osm_data_path)
    checksum_path = _checksum_path(osm_data_path)

    if checksum_path.is_file() and cache_path.is_file():
        with open(checksum_path, 'r', encoding='utf-8') as csf:
            osm_cache_checksum = json.load(csf)['checksum']
        if osm_cache_checksum == _checksum(osm_data_path):
            # Load cached data.
            print("Loading OSM from cache...", end=" ", flush=True)
            with open(cache_path, 'rb') as cf:
                data = pickle.load(cf)
        else:
            print(
                "OSM PBF has changed since last cache. Processing...",
                end=" ",
                flush=True,
            )
            data = _process_osm(osm_data_path)
    else:
        print(
            "No cache available. Processing OSM PBF...",
            end=" ",
            flush=True,
        )
        data = _process_osm(osm_data_path)
    print("done.")
    if len(data['routes']) == 0 or len(data['way_rels']) == 0:
        print(
            "No routes were found. Did you remember to include relations "
            "in your filter?"
        )
        sys.exit(1)

    return data

def format_road_name(row: pd.Series) -> str:
    """
    Formats a road name for an OSM way.
    Generally only used for roads that have no ref or whose ref isn't in
    the networks list in config, as those roads will use
    format_numbered_route.
    """
    if pd.isna(row.road_name):
        return row.route_ref
    return row.road_name

def _cache_path(osm_data_path):
    return osm_data_path.with_suffix('.pickle')

def _checksum(osm_data_path):
    h = hashlib.sha256()
    with open(osm_data_path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

def _checksum_path(osm_data_path):
    return osm_data_path.with_suffix('.checksum.json')

def _process_osm(osm_data_path) -> dict:
    """Processes the provided OSM PBF file."""
    handler = RoadHandler()
    handler.apply_file(osm_data_path, locations=True)
    metadata = {
        'source': str(osm_data_path),
        'checksum': _checksum(osm_data_path),
        'processed_at': datetime.now(timezone.utc).isoformat(),
    }
    checksum_path = _checksum_path(osm_data_path)
    with open(checksum_path, 'w', encoding='utf-8') as f:
        # Store checksum of OSM PBF file.
        json.dump(metadata, f, indent=2)
    data = {
        'ways': handler.ways,
        'node_ways': handler.node_ways,
        'routes': handler.routes,
        'rel_parents': handler.rel_parents,
        # 'numbered_routes': handler.numbered_routes,
        'way_rels': handler.way_rels,
        # 'superroutes': handler.superroutes,
        # 'route_superroutes': handler.route_superroutes,
        'ways_sindex': handler.ways.sindex, # Build spatial index
    }
    # Cache processed data.
    cache_path = _cache_path(osm_data_path)
    with open(cache_path, 'wb') as f:
        pickle.dump(data, f)
    return data
