"""Run the local file API on temporary synthetic tables; no data registration."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl
from datalake import DataLake


def main() -> None:
    with TemporaryDirectory() as directory:
        folder = Path(directory)
        # Simulate an existing ingestion partition; no import/registration step.
        partition = folder / "lake/observations/year=2025/month=01/day=01/data.parquet"
        partition.parent.mkdir(parents=True)
        observations = pl.DataFrame(
            {
                "timestamp": [datetime(2025, 1, 1, tzinfo=timezone.utc)],
                "value": [12.5],
            }
        )
        observations.write_parquet(partition)
        with DataLake(folder / "lake") as lake:
            assert lake.list_datasets() == ["observations"]
            print(lake.search("obs"))
            table = lake.read("observations", start="2025-01-01", end="2025-01-02")
            print(table)
            lake.write("research/example", table)
            lake.write(
                "research/example",
                table.with_columns(pl.lit(99.0).alias("value")),
                mode="upsert",
                keys=["timestamp"],
            )
            assert lake.read("research/example")["value"].to_list() == [99.0]
            lake.export("research/example", destination=folder / "example.csv")
        with DataLake(folder / "lake") as reopened:
            assert reopened.describe("research/example", count_rows=True)["rows"] == 1
        print("Quickstart passed")


if __name__ == "__main__":
    main()
