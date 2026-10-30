"""Plan exact ERA5 week matches and run bounded, resumable reforecast jobs."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

from comm_research.infra.lake.writer_lock import writer_lock
from .catalogue import discover
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


def plan(targets, reference_dates, lead_days, *, as_of=None):
    """Assign each week once, to the newest eligible associated issuance.

    This is a retrospective selection, not a historical real-time backtest.
    Catalogue axes establish listed issuances; exact field availability is only
    verified after retrieval. Explicit manual reference dates remain supported.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    references = (reference_dates if isinstance(reference_dates, dict)
                  else {d: None for d in reference_dates})
    by_month_day = {}
    for reference in sorted(references, reverse=True):
        if reference <= as_of:
            by_month_day.setdefault((reference.month, reference.day), []).append(reference)
    jobs, rows = {}, []
    for week in sorted({ts.date() for ts in targets["timestamp"]}):
        initialization = week - timedelta(days=lead_days)
        candidates = by_month_day.get((initialization.month, initialization.day), [])
        eligible = [r for r in candidates if 1 <= r.year - initialization.year <= 20]
        reference = eligible[0] if eligible else None
        if reference:
            jobs.setdefault(reference, []).append(initialization)
        rows.append(dict(timestamp=week, initialization_date=initialization,
                         reference_date=reference,
                         model_cycle=references[reference] if reference else None,
                         matched=reference is not None,
                         status="planned" if reference else "unmatched",
                         reason=("" if reference else
                                 "No listed issuance with matching month/day and preceding-20-year coverage"),
                         as_of=as_of))
    coverage = pl.DataFrame(rows, schema_overrides={"reference_date": pl.Date, "model_cycle": pl.String})
    return sorted(jobs.items()), coverage


def atomic_parquet(frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".part")
    os.close(fd)
    try:
        frame.write_parquet(name)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(*, targets, output, raw, reference_dates=None, start=None, end=None,
        lead_days=26, workers=2, refresh=False, dry_run=False, as_of=None,
        catalogue_cycles=None, refresh_catalogue=False):
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
    as_of = as_of or datetime.now(timezone.utc).date()
    if type(as_of) is not date:
        raise ValueError("as_of must be a datetime.date")
    references = None if reference_dates is None else list(reference_dates)
    if references is not None and (not references or any(type(d) is not date for d in references)):
        raise ValueError("reference_dates must be None (automatic) or a nonempty list of dates")
    if start and end and start >= end:
        raise ValueError("start must precede exclusive end")
    target_frame = targets_from_csv(targets, start, end)
    if references is None:
        references = discover(cache=Path(raw) / "catalogue", as_of=as_of, workers=workers,
                              refresh=refresh_catalogue, cycles=catalogue_cycles)
    else:
        references = {d: None for d in references if d <= as_of}
    jobs, coverage = plan(target_frame, references, lead_days, as_of=as_of)
    logger.info("Planned %d/%d target weeks across %d associated issuances (not yet downloaded)",
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
        identity = json.dumps([requests, selected.to_dicts(), references[reference]], sort_keys=True, default=str)
        key = hashlib.sha256(("latest-vintage-v2:" + identity).encode()).hexdigest()[:20]
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
                             reference_date=reference, model_cycle=references[reference],
                             selection_policy="latest_eligible_retrospective",
                             initialization_date=initialization,
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
        paths = []
        # The manifest identifies only the current selection, including partial
        # progress. Older vintage files stay on disk but are not globbed in.
        atomic_parquet(pl.DataFrame({"path": []}, schema={"path": pl.String}),
                       output / "files.parquet")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(pull, job): job[0] for job in jobs}
            try:
                for future in as_completed(futures):
                    paths.append(future.result())
                    coverage = coverage.with_columns(
                        pl.when(pl.col("reference_date") == futures[future])
                        .then(pl.lit("completed")).otherwise(pl.col("status")).alias("status"))
                    atomic_parquet(coverage, output / "coverage.parquet")
                    atomic_parquet(pl.DataFrame({"path": sorted(str(p.resolve()) for p in paths)}),
                                   output / "files.parquet")
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        paths.sort()
    return dict(files=paths, coverage=coverage, matched_weeks=int(coverage["matched"].sum()),
                unmatched_weeks=int((~coverage["matched"]).sum()))
