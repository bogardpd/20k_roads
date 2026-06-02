"""OSM processing helper functions."""
import geopandas as gpd
import hashlib
import json
import numpy as np
import osmium
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
        self.way_nodes = defaultdict(set)
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
        if w.tags.get('junction') in ["circular", "roundabout"]:
            # Get all nodes for roundabouts.
            way_nodes = [n.ref for n in w.nodes]
        else:
            # Get only first and last nodes for other ways.
            way_nodes = [w.nodes[0].ref, w.nodes[-1].ref]
        self.rows.append({
            'geometry': geom,
            'id': w.id,
            'highway': w.tags.get('highway'),
            'road_name': w.tags.get('name'),
            'route_ref': w.tags.get('ref'),
            'junction': w.tags.get('junction'),
        })
        self.way_nodes[w.id] = set(way_nodes)
        for way_node in way_nodes:
            self.node_ways[way_node].add(w.id)

    def relation(self, r):
        """Processing for each relation in OSM data."""
        tags = dict(r.tags)
        if tags.get('network') not in CONFIG['networks']:
            return
        if (
            tags.get('ref') is None
            and tags.get('network') not in CONFIG['networks_unsigned']
        ):
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
    def ways_gdf(self) -> gpd.GeoDataFrame:
        """Creates a GeoDataFrame of ways."""
        ways = gpd.GeoDataFrame(
            self.rows,
            crs=CONFIG['crs']['osm'],
        ).set_index('id')
        ways['road_name'] = ways['road_name'].astype("string")
        ways['route_ref'] = ways['route_ref'].astype("string")
        return ways.to_crs(CONFIG['crs']['metric'])

def load_osm(osm_data_path):
    """Loads data from OSM PBF file."""
    cache_path = _cache_path(osm_data_path, 'pickle')
    cache_path_ways = _cache_path(osm_data_path, 'geoparquet')
    checksum_path = _checksum_path(osm_data_path)

    if (
        checksum_path.is_file()
        and cache_path.is_file()
        and cache_path_ways.is_file()
    ):
        with open(checksum_path, 'r', encoding='utf-8') as csf:
            osm_cache_checksum = json.load(csf)['checksum']
        if osm_cache_checksum == _checksum(osm_data_path):
            # Load cached data.
            print(f"{datetime.now()} Loading pickle...")
            with open(cache_path, 'rb') as cf:
                data = pickle.load(cf)
            print(f"{datetime.now()} done.")
            print(f"{datetime.now()} Loading ways from geoparquet...")
            ways_gdf = gpd.read_parquet(cache_path_ways)
            print(f"{datetime.now()} done.")
        else:
            data, ways_gdf = _process_osm(osm_data_path)
    else:
        data, ways_gdf = _process_osm(osm_data_path)
    if len(data['routes']) == 0 or len(data['way_rels']) == 0:
        print(
            "No routes were found. Did you remember to include relations "
            "in your filter?"
        )
        sys.exit(1)
    print(f"{datetime.now()} Creating ways dict...")
    data['ways'] = ways_gdf \
        .astype(object) \
        .replace({np.nan: None}) \
        .to_dict(orient='index')
    print(f"{datetime.now()} done.")

    return data

def _cache_path(osm_data_path, cache_type):
    suffixes = {
        'pickle': ".pickle",
        'geoparquet': ".ways.parquet",
    }
    return osm_data_path.with_suffix(suffixes[cache_type])

def _checksum(osm_data_path):
    h = hashlib.sha256()
    with open(osm_data_path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

def _checksum_path(osm_data_path):
    return osm_data_path.with_suffix('.checksum.json')

def _process_osm(osm_data_path) -> tuple:
    """Processes the provided OSM PBF file."""
    print(f"{datetime.now()} No cache available. Processing OSM PBF...")
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
        'node_ways': handler.node_ways,
        'way_nodes': handler.way_nodes,
        'routes': handler.routes,
        'rel_parents': handler.rel_parents,
        'way_rels': handler.way_rels,
        'ways_sindex': handler.ways_gdf.sindex, # Build spatial index
    }
    # Cache processed data.
    cache_path = _cache_path(osm_data_path, 'pickle')
    print(f"{datetime.now()} Writing geoparquet...")
    handler.ways_gdf.to_parquet(_cache_path(osm_data_path, 'geoparquet'))
    print(f"{datetime.now()} done.")
    print(f"{datetime.now()} Writing pickle...")
    with open(cache_path, 'wb') as f:
        pickle.dump(data, f)
    print(f"{datetime.now()} done.")
    return (data, handler.ways_gdf)
