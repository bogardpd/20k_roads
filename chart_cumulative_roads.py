"""Creates a chart of cumulative roads over time."""
import argparse
import geopandas as gpd
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path

def chart_cumulative_roads(roads_path):
    """Create a chart of cumulative roads over time."""
    gdf = gpd.read_file(roads_path, layer='roads')
    df = pd.DataFrame(gdf[['visit_order','track_utc_start']])
    df = df.groupby('track_utc_start').count().sort_index().reset_index()
    df = df.rename(columns={'visit_order': 'new_roads'})
    df['cumulative_roads'] = df['new_roads'].cumsum()
    plt.scatter(df['track_utc_start'], df['cumulative_roads'],
        s=2,
    )
    plt.show()
    print(df)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="Chart Cumulative Roads",
        description="Charts cumulative roads over time"
    )
    parser.add_argument('roads_path',
        type=Path,
        help="Path for 20000 Roads GeoPackage output file"
    )
    args = parser.parse_args()
    chart_cumulative_roads(args.roads_path)