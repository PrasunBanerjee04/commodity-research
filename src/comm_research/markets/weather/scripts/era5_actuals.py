"""Build daily-starting seven-day ERA5 actuals, preserving existing grid IDs."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys
from typing import NamedTuple

import polars as pl

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from datalake import DataLake
from comm_research.setup.paths import CLEAN

LOG = logging.getLogger(__name__)
__all__ = ["run", "existing_grid_ids", "matched_era5_weeks", "export_matched_actuals"]

import os
import tempfile
from datetime import datetime, time, timedelta, timezone


def existing_grid_ids(directory, region):
    rows = []
    for path in sorted(Path(directory).glob(f"{region}_temp_2m_*.csv")):
        coordinates = pl.read_csv(path, columns=["lat", "lon"]).unique()
        if coordinates.height != 1 or coordinates.null_count().row(0) != (0, 0):
            raise ValueError(f"Expected one coordinate pair in {path}")
        grid_id = int(path.stem.rsplit("_", 1)[1])
        rows.append({"grid_id": grid_id, **coordinates.row(0, named=True), "filename": path.name})
    if not rows:
        raise ValueError(f"No existing grid-ID CSVs found in {directory}")
    grid = pl.DataFrame(rows).sort("grid_id")
    if grid["grid_id"].n_unique() != grid.height or grid.unique(subset=["lat", "lon"]).height != grid.height:
        raise ValueError("Grid IDs and coordinate pairs must both be unique")
    if grid.filter(~pl.col("lat").is_finite() | ~pl.col("lon").is_finite()
                   | ~pl.col("lat").is_between(-90, 90)
                   | ~pl.col("lon").is_between(-180, 180, closed="left")).height:
        raise ValueError("Invalid coordinates in existing ID files")
    return grid


def matched_era5_weeks(hourly, grid, start=None, end=None):
    """Daily-starting forward seven-day means, with a complete daily calendar."""
    if start and end and start >= end:
        raise ValueError("START must precede exclusive END")
    hourly = hourly.lazy() if isinstance(hourly, pl.DataFrame) else hourly
    schema = hourly.collect_schema()
    value_columns = [name for name in ("value", "temperature_2m") if name in schema]
    if len(value_columns) != 1:
        raise ValueError("Select one hourly dataset with value or temperature_2m, not mixed schemas")
    dtype = schema["timestamp"]
    if not isinstance(dtype, pl.Datetime):
        raise ValueError("Hourly Parquet timestamp must have a Datetime dtype")
    stamp = pl.col("timestamp")
    stamp = stamp.dt.convert_time_zone("UTC") if dtype.time_zone else stamp.dt.replace_time_zone("UTC")
    if "variable" in schema:
        hourly = hourly.filter(pl.col("variable") == "2m_temperature")
    hourly = hourly.select(
        stamp.alias("timestamp"), pl.col("latitude").cast(pl.Float64).alias("lat"),
        pl.col("longitude").cast(pl.Float64).alias("lon"),
        pl.col(value_columns[0]).cast(pl.Float64).alias("value"),
        (pl.col("unit") == "K").alias("unit_ok") if "unit" in schema else pl.lit(True).alias("unit_ok"))
    if start:
        hourly = hourly.filter(pl.col("timestamp") >= datetime.combine(start, time(), timezone.utc))
    if end:
        hourly = hourly.filter(pl.col("timestamp") < datetime.combine(end, time(), timezone.utc))
    selected = (hourly.filter(
        pl.col("lat").is_between(grid["lat"].min(), grid["lat"].max())
        & pl.col("lon").is_between(grid["lon"].min(), grid["lon"].max())
        & pl.col("timestamp").dt.hour().is_in([0, 6, 12, 18])
        & (pl.col("timestamp").dt.minute() == 0) & (pl.col("timestamp").dt.second() == 0)
        & (pl.col("timestamp").dt.nanosecond() == 0))
        .join(grid.select("grid_id", "lat", "lon").lazy(), on=["lat", "lon"], how="inner")
        .with_columns(pl.col("timestamp").dt.truncate("1d").alias("day")))
    daily = (selected.group_by("grid_id", "day").agg(
        pl.len().alias("n_samples"), pl.col("timestamp").n_unique().alias("n_unique"),
        ((~pl.col("value").is_finite()) | (~pl.col("unit_ok"))).fill_null(True).sum().alias("invalid_samples"),
        pl.col("value").sum().alias("sum_k"),
        pl.col("value").max().alias("2m_max_6h"), pl.col("value").min().alias("2m_min_6h"))
        .collect(engine="streaming"))
    if daily.is_empty():
        raise ValueError("No matching 00/06/12/18 UTC ERA5 samples; check the source dataset, dates and grid IDs")
    last_day = daily["day"].max()
    calendar = pl.DataFrame({"day": pl.datetime_range(
        daily["day"].min(), last_day + timedelta(days=6), interval="1d", eager=True)})
    complete_calendar = (grid.join(calendar, how="cross")
        .join(daily, on=["grid_id", "day"], how="left")
        .with_columns(pl.col("n_samples", "n_unique", "invalid_samples", "sum_k").fill_null(0))
        .sort("grid_id", "day"))
    # Shift a trailing seven-day rolling result back six days to its start date.
    coverage = (complete_calendar.with_columns(
        *[pl.col(c).rolling_sum(window_size=7).shift(-6).over("grid_id").alias(c)
          for c in ("n_samples", "n_unique", "invalid_samples", "sum_k")],
        pl.col("2m_max_6h").rolling_max(window_size=7, min_samples=1).shift(-6).over("grid_id"),
        pl.col("2m_min_6h").rolling_min(window_size=7, min_samples=1).shift(-6).over("grid_id"))
        .filter(pl.col("day") <= last_day)
        .with_columns((pl.col("sum_k") / pl.col("n_samples")).alias("2m_mean"))
        .with_columns(pl.when(pl.col("invalid_samples") > 0).then(pl.lit("invalid"))
            .when(pl.col("n_samples") != pl.col("n_unique")).then(pl.lit("duplicate"))
            .when(pl.col("n_samples") == 0).then(pl.lit("missing"))
            .when(pl.col("n_samples") == 28).then(pl.lit("complete"))
            .otherwise(pl.lit("incomplete")).alias("status"))
        .drop("sum_k").rename({"day": "timestamp"}).sort(["grid_id", "timestamp"]))
    return coverage


def atomic_csv(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".partial")
    os.close(fd)
    try:
        frame.write_csv(temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_matched_actuals(coverage, grid, output, id_directory, *, clean_root=None):
    output = Path(output).resolve()
    if not output.is_relative_to(Path(clean_root or CLEAN).resolve()):
        raise ValueError("Output must stay under data/clean")
    atomic_csv(coverage, output / "_audit/coverage.csv")
    if coverage.filter(pl.col("status").is_in(["invalid", "duplicate"])).height:
        raise ValueError("Invalid or duplicate source samples; inspect _audit/coverage.csv before exporting")
    complete = coverage.filter(pl.col("status") == "complete")
    if complete.is_empty():
        raise ValueError("No complete 28-sample weeks; inspect _audit/coverage.csv")
    exported = complete.select(
        "grid_id", "timestamp", "lat", "lon", "2m_mean", "2m_max_6h", "2m_min_6h", "n_samples"
    ).with_columns(pl.col("timestamp").dt.offset_by("7d").alias("valid_end"),
                   pl.lit(6).alias("sampling_hours"), pl.lit("K").alias("units"),
                   pl.lit("mean_of_6h_instantaneous_samples").alias("aggregation"))
    for (grid_id,), frame in exported.group_by("grid_id", maintain_order=True):
        filename = grid.filter(pl.col("grid_id") == grid_id)["filename"].item()
        atomic_csv(frame, output / filename)
    atomic_csv(grid, output / "_audit/grid_ids.csv")
    return exported


class ActualsResult(NamedTuple):
    grid_ids: pl.DataFrame
    coverage: pl.DataFrame
    actuals: pl.DataFrame


def run(hourly=None, *, region="socal", source="weather/era5/t_2m", start=None,
        end=None, output=None):
    """Build and save actuals using the existing clean-data coordinate mapping.

    output is an optional dataset name relative to data/clean. By default the
    region's existing CSVs are replaced, matching the original notebook workflow.
    Pass a lazy hourly table to preserve any notebook research filters.
    """
    if not region or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in region):
        raise ValueError("region must be a simple name, e.g. socal")
    clean = DataLake(area="clean")
    ids = clean.root / "weather/era5/t_2m" / region
    destination = (clean.root / output).resolve() if output is not None else ids.resolve()
    if not destination.is_relative_to(clean.root) or destination == clean.root:
        raise ValueError("Output must be a dataset name inside data/clean")
    if hourly is None:
        hourly = DataLake().scan(source)
    LOG.info("ERA5 actuals: source=%s region=%s start=%s end=%s output=%s", source, region, start, end, destination)
    grid = existing_grid_ids(ids, region)
    LOG.info("Loaded %d existing grid IDs", grid.height)
    coverage = matched_era5_weeks(hourly, grid, start=start, end=end)
    LOG.info("Coverage by status: %s", coverage.group_by("status").len().to_dicts())
    actuals = export_matched_actuals(coverage, grid, destination, ids, clean_root=clean.root)
    LOG.info("Saved %d complete grid-windows for %d grids", actuals.height, grid.height)
    return ActualsResult(grid, coverage, actuals)


def main(argv=None):
    from datetime import date
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="socal")
    parser.add_argument("--source", default="weather/era5/t_2m")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--output", help="Optional output dataset name relative to data/clean")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(region=args.region, source=args.source, start=args.start, end=args.end, output=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
