import pandas as pd
import polars as pl
import matplotlib.pyplot as plt
from pathlib import Path

from comm_research.infra.paths import PROJECT_ROOT
from comm_research.infra.lake.parquet import scan_parquet

DATA_DIR = dataset_dir = PROJECT_ROOT / "data/"

def load_data(data_field):

    path = Path(DATA_DIR, data_field)

    return scan_parquet(path).collect()

def socal_temp_2m(dataset_dir, lon, lat):
    table = scan_parquet(Path(dataset_dir))
    df = table.collect()

    df = df.filter(
        (pl.col('latitude') == lat) &
        (pl.col('longitude') == lon)
    )
    return df

def save(filepath, df):
    try:
        df.to_csv(filepath)
    except Exception as e:
        print(e)
        return -1
    return 1



if __name__ == "__main__":

    lat = 32.5
    lon = -121.0

    data_dir = Path(DATA_DIR, 'lake/weather/t_2m')
    save_dir = f'data/clean/socal_temp_2m_{lat}_{lon}.csv'

    df = socal_temp_2m(data_dir, lon, lat)

    save(save_dir, df)
