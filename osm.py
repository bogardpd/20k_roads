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
from pathlib import Path
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
        self.rels = {}
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
        self.rels[r.id] = {
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

class OSMDataContainer():
    """Holds processed OSM data."""

    def __init__(self, osm_data_path):
        self.ways: dict | None = None
        self.ways_index: list | None = None
        self.way_nodes: dict | None = None
        self.node_ways: dict | None = None
        self.rels: dict | None = None
        self.rel_parents: dict | None = None
        self.way_rels: dict | None = None
        self.ways_sindex: gpd.sindex.SpatialIndex | None = None
        self._osm_data_path: Path = osm_data_path
        self._ways_gdf: gpd.GeoDataFrame | None = None

    def load_data(self):
        """Loads data from OSM PBF file."""
        cache_path = self._cache_path('pickle')
        cache_path_ways = self._cache_path('feather')
        checksum_path = self._checksum_path()

        if (
            checksum_path.is_file()
            and cache_path.is_file()
            and cache_path_ways.is_file()
        ):
            with open(checksum_path, 'r', encoding='utf-8') as csf:
                osm_cache_checksum = json.load(csf)['checksum']
            if osm_cache_checksum == self._checksum():
                # Load cached data.
                self._read_cache_pickle()
                self._read_cache_feather()
            else:
                self._process_osm()
        else:
            self._process_osm()

        print(f"{datetime.now()} Creating ways dict...")
        self.ways = self._ways_gdf \
            .astype(object) \
            .replace({np.nan: None}) \
            .to_dict(orient='index')
        print(f"{datetime.now()} done.")

    def _cache_path(self, cache_type):
        suffixes = {
            'pickle': ".pickle",
            'feather': ".ways.feather",
        }
        return self._osm_data_path.with_suffix(suffixes[cache_type])

    def _checksum(self):
        h = hashlib.sha256()
        with open(self._osm_data_path, 'rb') as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()

    def _checksum_path(self):
        return self._osm_data_path.with_suffix('.checksum.json')

    def _process_osm(self) -> None:
        """Processes the provided OSM PBF file."""
        print(f"{datetime.now()} No cache available. Processing OSM PBF...")
        handler = RoadHandler()
        handler.apply_file(self._osm_data_path, locations=True)
        metadata = {
            'source': str(self._osm_data_path),
            'checksum': self._checksum(),
            'processed_at': datetime.now(timezone.utc).isoformat(),
        }
        checksum_path = self._checksum_path()
        with open(checksum_path, 'w', encoding='utf-8') as f:
            # Store checksum of OSM PBF file.
            json.dump(metadata, f, indent=2)

        self.node_ways = handler.node_ways
        self.way_nodes = handler.way_nodes
        self.rels = handler.rels
        self.rel_parents = handler.rel_parents
        self.way_rels = handler.way_rels
        self.ways_sindex = handler.ways_gdf.sindex # Build spatial index
        self._ways_gdf = handler.ways_gdf

        if not self.rels or not self.way_rels:
            print(
                "No route relations were found. Did you remember to include "
                "relations in your filter?"
            )
            sys.exit(1)

        # Cache processed data.
        self._write_cache_feather()
        self._write_cache_pickle()

    def _read_cache_feather(self) -> None:
        """Reads data from feather file."""
        print(f"{datetime.now()} Reading ways from feather...")
        self._ways_gdf = gpd.read_feather(self._cache_path('feather'))
        print(f"{datetime.now()} done.")

    def _read_cache_pickle(self) -> None:
        """Reads data from pickle file."""
        print(f"{datetime.now()} Reading pickle...")
        with open(self._cache_path('pickle'), 'rb') as f:
            data = pickle.load(f)
        self.node_ways = data['node_ways']
        self.way_nodes = data['way_nodes']
        self.rels = data['rels']
        self.rel_parents = data['rel_parents']
        self.way_rels = data['way_rels']
        self.ways_sindex = data['ways_sindex']
        print(f"{datetime.now()} done.")

    def _write_cache_feather(self) -> None:
        """Stores data as feather file."""
        print(f"{datetime.now()} Writing feather...")
        self._ways_gdf.to_feather(self._cache_path('feather'))
        print(f"{datetime.now()} done.")

    def _write_cache_pickle(self) -> None:
        """Stores data as pickle file."""
        print(f"{datetime.now()} Writing pickle...")
        data = {
            'node_ways': self.node_ways,
            'way_nodes': self.way_nodes,
            'rels': self.rels,
            'rel_parents': self.rel_parents,
            'way_rels': self.way_rels,
            'ways_sindex': self._ways_gdf.sindex,
        }
        with open(self._cache_path('pickle'), 'wb') as f:
            pickle.dump(data, f)
        print(f"{datetime.now()} done.")
