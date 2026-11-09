"""Reshape parquet tables stored in nested lake directories"""

import os
import tempfile
from pathlib import Path

import polars as pl


def scan_parquet(dataset_dir: Path) -> pl.LazyFrame:
    """Lazily scan all Parquet partitions under a dataset directory."""
    files = sorted(dataset_dir.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No Parquet files found under {dataset_dir}")
    return pl.scan_parquet([str(path) for path in files], hive_partitioning=True)


def write_atomic(frame: pl.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, suffix=".parquet", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        frame.write_parquet(temporary_path, compression="zstd")
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
