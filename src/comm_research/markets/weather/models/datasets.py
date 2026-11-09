"""Point temperature extraction from the weather lake."""
from pathlib import Path

import polars as pl

from comm_research.infra.lake.datasets import DATA_DIR, load_data, save
from comm_research.infra.lake.parquet import scan_parquet

__all__ = ["socal_temp_2m"]


def socal_temp_2m(dataset_dir, lon, lat):
    """Collect temperature rows for a single coordinate."""
    df = scan_parquet(Path(dataset_dir)).collect()
    return df.filter((pl.col("latitude") == lat) & (pl.col("longitude") == lon))


def main():
    lat, lon = 32.5, -121.0
    data_dir = Path(DATA_DIR, "lake/weather/era5/t_2m")
    save_dir = Path(DATA_DIR, f"clean/socal_temp_2m_{lat}_{lon}.csv")
    return save(save_dir, socal_temp_2m(data_dir, lon, lat))


if __name__ == "__main__":
    main()
