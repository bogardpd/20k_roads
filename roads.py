import argparse
import geopandas as gpd
import pandas as pd
import osmium
import re
import tomllib
from collections import defaultdict
from pathlib import Path
from shapely import Point
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
            'name': w.tags.get('name'),
            'ref': w.tags.get('ref'),
            'network': w.tags.get('network'),
            'state': w.tags.get('state'),
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
    roads = processed_osm['roads']
    node_ways = processed_osm['nodes']
    roads_sindex = roads.sindex # Build spatial index
    print("done.")

    print("Loading tracks...", end=" ")
    tracks = build_tracks(track_file)
    print("done.")

    # Temporarily filter to a small subset of tracks.
    tracks = tracks[tracks['utc_start'] < "2010-01-16"]

    unique_roads = {}
    for track_fid, track in tracks.iterrows():
        print(f"Processing track {track_fid} ({track.utc_start})")
        for segment in track.geometry.geoms:
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
            streaks = streaks[
                streaks['streak_length'] >= CONFIG['search']['consec_pts']
            ]
            streaks = streaks.join(roads['unique_name'], on='closest_way_id')
            streaks = streaks.dropna(subset='unique_name')
            for way_idx, way in streaks.iterrows():
                for way_name in way.unique_name.split(";"):
                    if not way_name in unique_roads:
                        unique_roads[way_name] = track_fid
    
    print("\nROADS WENT DOWN:")
    for i, (k, v) in enumerate(unique_roads.items()):
        print(f"{i+1}: {k}")

    records_df = pd.DataFrame([
        {'road': k, 'track_fid': v}
        for k, v in unique_roads.items()
    ])
    records_df = records_df.join(tracks['utc_start'], on='track_fid')
    records_df = records_df[['utc_start','track_fid','road']]
    csv_path = output_dir / CONFIG['output']['csv']
    records_df.to_csv(csv_path, index=False)
    print(f"Saved data to {csv_path}.")


def build_osm(osm_data: Path, state_data: Path) -> dict:
    """Creates a road GeoDataFrame and a node lookup table."""

    # Load U.S. states.
    states = gpd.read_file(state_data)
    states = states[['STUSPS', 'NAME', 'geometry']].rename(columns={
        'STUSPS': 'state_abbr',
        'NAME': 'state_name',
    }).to_crs(CONFIG['crs']['osm'])

    # Process OSM roads.
    handler = RoadHandler()
    handler.apply_file(osm_data, locations=True)
    roads = gpd.GeoDataFrame(
        handler.rows,
        crs=CONFIG['crs']['osm'],
    ).set_index('id')

    # Spatially join U.S. states onto roads.
    roads = gpd.sjoin(roads, states, how='left', predicate='within')
    roads['unique_name'] = roads.apply(unique_road_name, axis=1)

    return {
        'roads': roads.to_crs(CONFIG['crs']['metric']),
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

def unique_road_name(row: pd.Series) -> str:
    """Formats a road name for an OSM way."""
    if pd.isna(row.ref):
        return row['name']
    if re.match(r'SR ', row['ref']):
        # Prepend state abbreviation to state route.
        return f"{row['state_abbr']} {row['ref']}"
    return row['ref']


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