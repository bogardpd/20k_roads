"""OSM processing helper functions."""
import geopandas as gpd
import osmium
import pickle
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from shapely.wkb import loads as wkb_loads

with open('config.toml', 'rb') as config_file:
    CONFIG = tomllib.load(config_file)

class RoadHandler(osmium.SimpleHandler):
    """Processes OSM roads."""
    def __init__(self):
        super().__init__()
        self.rows = []
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
        self.rows.append({
            'geometry': geom,
            'id': w.id,
            'highway': w.tags.get('highway'),
            'road_name': w.tags.get('name'),
            'route_ref': w.tags.get('ref'),
            'junction': w.tags.get('junction'),
        })

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
        self.rels: dict | None = None
        self.rel_parents: dict | None = None
        self.way_rels: dict | None = None
        self.ways_gdf: gpd.GeoDataFrame | None = None
        self._osm_data_path: Path = osm_data_path

    def load_data(self):
        """Loads data from OSM PBF file."""
        cache_path = self._cache_path('pickle')
        cache_path_ways = self._cache_path('feather')

        if (cache_path.is_file() and cache_path_ways.is_file()):
            print(
                f"Loading preprocessed OSM data for {self._osm_data_path}..."
            )
            self._read_cache_pickle()
            self._read_cache_feather()
        else:
            self._preprocess_osm()

        print(
            f"Loaded {len(self.ways_gdf.index)} OSM ways, "
            f"{len(self.rels)} OSM relations."
        )
        cols = self.ways_gdf.columns.to_list()
        data = self.ways_gdf.to_numpy(dtype=object, na_value=None)
        self.ways = {
            idx: dict(zip(cols, row))
            for idx, row in zip(self.ways_gdf.index, data)
        }

    def _cache_path(self, cache_type):
        suffixes = {
            'pickle': ".pickle",
            'feather': ".ways.feather",
        }
        return self._osm_data_path.with_suffix(suffixes[cache_type])

    def _preprocess_osm(self) -> None:
        """Processes the provided OSM PBF file."""
        print(f"No preprocessed data available for {self._osm_data_path}.")
        print("Processing OSM PBF (this may take a while)...")
        handler = RoadHandler()
        handler.apply_file(self._osm_data_path, locations=True)

        self.rels = handler.rels
        self.rel_parents = handler.rel_parents
        self.way_rels = handler.way_rels
        self.ways_gdf = handler.ways_gdf
        handler = None

        if not self.rels or not self.way_rels:
            print(
                "No route relations were found. Did you remember to include "
                "relations in your filter?"
            )
            sys.exit(1)

        # Write preprocessed data.
        print("Saving preprocessed OSM data...")
        self._write_cache_feather()
        self._write_cache_pickle()

    def _read_cache_feather(self) -> None:
        """Reads data from feather file."""
        self.ways_gdf = gpd.read_feather(self._cache_path('feather'))

    def _read_cache_pickle(self) -> None:
        """Reads data from pickle file."""
        with open(self._cache_path('pickle'), 'rb') as f:
            data = pickle.load(f)
        self.rels = data['rels']
        self.rel_parents = data['rel_parents']
        self.way_rels = data['way_rels']

    def _write_cache_feather(self) -> None:
        """Stores data as feather file."""
        self.ways_gdf.to_feather(self._cache_path('feather'))

    def _write_cache_pickle(self) -> None:
        """Stores data as pickle file."""
        data = {
            'rels': self.rels,
            'rel_parents': self.rel_parents,
            'way_rels': self.way_rels,
        }
        with open(self._cache_path('pickle'), 'wb') as f:
            pickle.dump(data, f)
