"""Reshape parquet tables stored in nested lake directories"""

from pathlib import Path
import polars as pl

def scan_parquet(dataset_dir: Path) -> pl.LazyFrame:
    """Lazily scan all Parquet partitions under a dataset directory."""
    files = sorted(dataset_dir.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No Parquet files found under {dataset_dir}")
    return pl.scan_parquet([str(path) for path in files], hive_partitioning=True)