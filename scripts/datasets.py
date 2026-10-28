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
    return df

def save(filepath, df):
    try:
        df.to_csv(filepath)
    except Exception as e:
        print(e)
        return -1
    return 1



if __name__ == "__main__":
    pass