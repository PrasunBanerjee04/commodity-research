"""Measure filtered reads of a temporary synthetic Parquet table."""

import argparse
import json
import resource
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl
from datalake import DataLake


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=2_000_000)
    args = parser.parse_args()
    if args.rows <= 0:
        parser.error("--rows must be positive")
    with TemporaryDirectory() as directory:
        root = Path(directory) / "lake"
        frame = pl.DataFrame({"time": range(args.rows), "value": range(args.rows)}).with_columns(
            pl.col("time").cast(pl.Datetime("us", "UTC"))
        )
        DataLake(root).write("synthetic", frame)
        with DataLake(root) as lake:
            for label in ("first_open", "warm"):
                began = time.perf_counter()
                selected = lake.read("synthetic", filters={"value": [10, 20]})
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                print(
                    json.dumps(
                        {
                            "run": label,
                            "elapsed_ms": (time.perf_counter() - began) * 1000,
                            "selected_rows": selected.height,
                            "file_bytes": lake.describe("synthetic")["size_bytes"],
                            "peak_process_rss_bytes": rss
                            if sys.platform == "darwin"
                            else rss * 1024,
                            "cold_cache": "not controlled",
                        }
                    )
                )


if __name__ == "__main__":
    main()
