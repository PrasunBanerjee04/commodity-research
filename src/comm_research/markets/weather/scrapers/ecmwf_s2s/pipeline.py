"""Plan exact ERA5 week matches and run bounded, resumable reforecast jobs."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

from comm_research.infra.lake.writer_lock import writer_lock
from .client import Client, request_for
from .normalize import weekly

logger = logging.getLogger(__name__)
MEMBERS = ["control", *(f"perturbed_{i}" for i in range(1, 11))]


def targets_from_csv(directory, start=None, end=None):
    """Read unique grid/week keys; end is exclusive. Values stay in ERA5 files."""
    paths = sorted(Path(directory).glob("*.csv"))
    if not paths:
        raise ValueError(f"No weekly CSV files found in {directory}")
    targets = pl.concat([pl.read_csv(p, columns=["timestamp", "lat", "lon"])
                         for p in paths]).with_columns(
        pl.col("timestamp").str.to_datetime(time_zone="UTC", strict=True)
    ).unique().sort(["timestamp", "lat", "lon"])
    if start:
        targets = targets.filter(pl.col("timestamp") >= datetime.combine(start, time(), timezone.utc))
    if end:
        targets = targets.filter(pl.col("timestamp") < datetime.combine(end, time(), timezone.utc))
    if targets.is_empty() or targets.null_count().row(0) != (0, 0, 0):
        raise ValueError("No targets selected, or null target keys")
    for ts, lat, lon in targets.iter_rows():
        if ts.time() != time() or ts.weekday() != 0:
            raise ValueError("Expected Monday 00:00 UTC weekly targets")
        if not (-90 <= lat <= 90 and -180 <= lon < 180):
            raise ValueError("Invalid target coordinates")
    return targets


def plan(targets, reference_dates, lead_days):
    """Match exact month/day historical initializations in each preceding 20 years.

    reference_dates are actual associated issuance dates selected from the archive,
    not historical initialization dates. No missing schedule dates are invented.
    """
    jobs = []
    weeks = sorted({ts.date() for ts in targets["timestamp"]})
    covered = set()
    for reference in sorted(set(reference_dates)):
        hdates = [week - timedelta(days=lead_days) for week in weeks
                  if (week - timedelta(days=lead_days)).month == reference.month
                  and (week - timedelta(days=lead_days)).day == reference.day
                  and 1 <= reference.year - (week - timedelta(days=lead_days)).year <= 20]
        if hdates:
            jobs.append((reference, hdates))
            covered.update(h + timedelta(days=lead_days) for h in hdates)
    coverage = pl.DataFrame({"timestamp": weeks, "matched": [w in covered for w in weeks]})
    return jobs, coverage


def atomic_parquet(frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".part")
    os.close(fd)
    try:
        frame.write_parquet(name)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(*, targets, reference_dates, output, raw, start=None, end=None,
        lead_days=26, workers=2, refresh=False, dry_run=False):
    """Pull member-wise weekly means in K, bilinearly mapped to ERA5 coordinates.

    A target week is [historical initialization + lead_days, + lead_days + 7),
    in UTC. lead_days is an elapsed-day offset, never an inclusive day label.
    Outputs partition by associated issuance so model vintages are never merged.
    Unmatched targets are reported, not fabricated or silently shifted.
    """
    if isinstance(lead_days, bool) or not isinstance(lead_days, int) or lead_days < 0 or lead_days + 7 > 46:
        raise ValueError("lead_days must be an integer from 0 through 39")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 through 8")
    references = list(reference_dates)
    if not references or any(type(d) is not date for d in references):
        raise ValueError("Provide actual archive reference_dates as datetime.date objects")
    if start and end and start >= end:
        raise ValueError("start must precede exclusive end")
    target_frame = targets_from_csv(targets, start, end)
    jobs, coverage = plan(target_frame, references, lead_days)
    logger.info("Matched %d/%d target weeks across %d associated issuances",
                coverage["matched"].sum(), coverage.height, len(jobs))
    if dry_run:
        return coverage
    if not jobs:
        raise ValueError("No matching historical weeks; inspect reference dates or use dry_run=True")
    output, raw = Path(output), Path(raw)

    def pull(job):
        reference, hdates = job
        week_starts = [datetime.combine(h + timedelta(days=lead_days), time(), timezone.utc) for h in hdates]
        selected = target_frame.filter(pl.col("timestamp").is_in(week_starts))
        points = selected.select("lat", "lon").unique().sort(["lat", "lon"]).rows()
        # Align outward on the 1.5-degree archive lattice, retaining neighbors.
        lats, lons = zip(*points)
        area = [min(90, math.ceil(max(lats)/1.5)*1.5 + 1.5),
                max(-180, math.floor(min(lons)/1.5)*1.5 - 1.5),
                max(-90, math.floor(min(lats)/1.5)*1.5 - 1.5),
                min(180, math.ceil(max(lons)/1.5)*1.5 + 1.5)]
        requests = [request_for(reference, hdates, area, pf, lead_days) for pf in (False, True)]
        identity = json.dumps([requests, selected.to_dicts()], sort_keys=True, default=str)
        key = hashlib.sha256(identity.encode()).hexdigest()[:20]
        path = output / f"reference_date={reference}" / f"{key}.parquet"
        if path.exists() and not refresh:
            # Read the footer/data so truncated completed files fail visibly.
            existing = pl.read_parquet(path)
            if existing.height != selected.height or not set(MEMBERS).issubset(existing.columns):
                raise ValueError(f"Invalid completed output: {path}")
            return path
        client = Client()
        files = [client.retrieve(r, raw, refresh=refresh) for r in requests]
        values = weekly(files, hdates, points, lead_days)
        point_index = {p: i for i, p in enumerate(points)}
        rows = []
        for ts, lat, lon in selected.iter_rows():
            initialization = ts.date() - timedelta(days=lead_days)
            index = point_index[lat, lon]
            rows.append(dict(timestamp=ts, lat=lat, lon=lon,
                             reference_date=reference, initialization_date=initialization,
                             lead_days=lead_days, valid_end=ts + timedelta(days=7),
                             units="K", spatial_method="bilinear_from_1.5deg",
                             **{name: float(values[initialization, m][index])
                                for m, name in enumerate(MEMBERS)}))
        atomic_parquet(pl.DataFrame(rows), path)
        logger.info("Saved %s (%d grid-week rows)", path, len(rows))
        return path

    # One writer per output root; each worker owns its API client and temporary files.
    with writer_lock(output):
        atomic_parquet(coverage, output / "coverage.parquet")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            paths = list(pool.map(pull, jobs))
    return dict(files=paths, matched_weeks=int(coverage["matched"].sum()),
                unmatched_weeks=int((~coverage["matched"]).sum()))
