"""Plan exact UTC target weeks against NOAA's listed eleven-member runs."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

from .client import byte_range

logger = logging.getLogger(__name__)

MEMBERS = ("c00", *(f"p{i:02d}" for i in range(1, 11)))


def key_for(initialization, member):
    return (f"GEFSv12/reforecast/{initialization:%Y/%Y%m%d}00/{member}/Days:10-35/"
            f"tmp_2m_{initialization:%Y%m%d}00_{member}.grib2")


def forecast_hours(lead_days):
    if isinstance(lead_days, bool) or not isinstance(lead_days, int) or not 16 <= lead_days <= 28:
        raise ValueError("GEFS extended-range lead_days must be an integer from 16 through 28")
    return list(range(24*lead_days, 24*(lead_days+7), 6))


def targets_from_csv(directory, start=None, end=None):
    paths = sorted(Path(directory).glob("*.csv"))
    if not paths:
        raise ValueError(f"No target CSV files in {directory}")
    frame = pl.concat([pl.read_csv(p, columns=["timestamp", "lat", "lon"]) for p in paths])
    frame = frame.with_columns(pl.col("timestamp").str.to_datetime(time_zone="UTC", strict=True))
    if start:
        frame = frame.filter(pl.col("timestamp") >= datetime.combine(start, time(), timezone.utc))
    if end:
        frame = frame.filter(pl.col("timestamp") < datetime.combine(end, time(), timezone.utc))
    frame = frame.unique().sort(["timestamp", "lat", "lon"])
    if frame.is_empty() or frame.null_count().row(0) != (0, 0, 0):
        raise ValueError("Empty target selection or null target keys")
    if frame.filter(~pl.col("lat").is_finite() | ~pl.col("lon").is_finite()
                    | ~pl.col("lat").is_between(-90, 90) | ~pl.col("lon").is_between(-180, 180, closed="left")).height:
        raise ValueError("Invalid target coordinates")
    if any(ts.time() != time() for ts in frame["timestamp"]):
        raise ValueError("Target weeks must begin at 00:00 UTC")
    return frame


def plan(targets, client, lead_days, *, workers=4, refresh=False):
    steps = forecast_hours(lead_days)
    weeks = sorted({ts.date() for ts in targets["timestamp"]})
    def inspect(week):
        initialization = week-timedelta(days=lead_days)
        reason = ""
        if not date(2000, 1, 1) <= initialization < date(2020, 1, 1):
            reason = "Initialization outside the GEFSv12 2000-2019 reforecast archive"
        elif not set(MEMBERS).issubset(client.members(initialization, refresh=refresh)):
            reason = "No listed eleven-member extended run for this initialization"
        else:
            records = client.index(key_for(initialization, "c00"), refresh=refresh)
            if records is None:
                reason = "No listed extended-range control temperature index"
            else:
                try:
                    byte_range(records, steps)
                except ValueError as error:
                    # Metadata alone cannot rule out actual GRIB fields. The
                    # downloader checks the full file before classifying a gap.
                    logger.warning("GEFS control index for %s needs full-file verification: %s", initialization, error)
        return dict(timestamp=week, initialization_date=initialization,
                    matched=not reason, status="unmatched" if reason else "planned", reason=reason)
    rows, remaining = [], iter(weeks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque(pool.submit(inspect, week) for week in islice(remaining, workers))
        try:
            while pending:
                rows.append(pending.popleft().result())
                if len(rows) % 100 == 0:
                    logger.info("GEFS inspected %d/%d target weeks", len(rows), len(weeks))
                week = next(remaining, None)
                if week is not None:
                    pending.append(pool.submit(inspect, week))
        finally:
            for future in pending:
                future.cancel()
    return pl.DataFrame(rows)
