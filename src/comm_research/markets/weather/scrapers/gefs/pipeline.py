"""Resumable, grid-aware GEFSv12 temperature ingestion."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile

import polars as pl

from comm_research.infra.lake.writer_lock import writer_lock
from .catalogue import MEMBERS, forecast_hours, key_for, plan, targets_from_csv
from .client import Client, atomic_bytes
from .normalizer import normalize

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
COLUMNS = ["control", *(f"perturbed_{i}" for i in range(1, 11))]


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(2**20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_parquet(frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".partial")
    os.close(fd)
    try:
        frame.write_parquet(temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verified(manifest, fingerprint, weekly, samples):
    if not manifest.exists() or not weekly.exists() or not samples.exists():
        return False
    try:
        info = json.loads(manifest.read_text())
        return (info["fingerprint"] == fingerprint and info["weekly_sha256"] == checksum(weekly)
                and info["samples_sha256"] == checksum(samples))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run(*, targets, output, raw, state, start=None, end=None, lead_days=26, workers=4,
        retries=3, timeout=60, min_free_gb=2, keep_raw=False, refresh=False,
        refresh_catalogue=False, dry_run=True):
    """Plan exact target weeks or fetch all eleven members using indexed ranges.

    Native six-hourly temperatures are sampled onto the requested coordinates.
    Weekly means use those same 28 instantaneous samples; they are not hourly
    means. Raw global slices are removed only after weekly and sample outputs
    are committed, unless keep_raw=True. Source details survive in manifests.
    """
    steps = forecast_hours(lead_days)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError("workers must be an integer from 1 through 16")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 8:
        raise ValueError("retries must be an integer from 0 through 8")
    if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(min_free_gb) or min_free_gb < 0:
        raise ValueError("timeout must be positive and min_free_gb nonnegative")
    if start and end and start >= end:
        raise ValueError("start must precede the exclusive end date")
    frame = targets_from_csv(targets, start, end)
    output, raw, state = Path(output), Path(raw), Path(state)
    if len({p.resolve() for p in (output, raw, state)}) != 3:
        raise ValueError("output, raw and state must be distinct directories")
    client = Client(raw, workers=workers, retries=retries, timeout=timeout, min_free_gb=min_free_gb)
    try:
        coverage = plan(frame, client, lead_days, workers=workers, refresh=refresh_catalogue)
        logger.info("GEFS planned %d/%d target weeks; all eleven member fields are validated during download",
                    coverage["matched"].sum(), coverage.height)
        if dry_run:
            return coverage
        if not coverage["matched"].any():
            raise ValueError("No available GEFS extended runs match the requested target weeks")
        paths, downloaded, skipped, transferred, rows_written = [], 0, 0, 0, 0
        # Also lock raw: regional runs may share source files which are cleaned up.
        with writer_lock(output), writer_lock(raw), ThreadPoolExecutor(max_workers=workers) as pool:
            atomic_parquet(coverage, state / "coverage.parquet")
            atomic_parquet(pl.DataFrame({"weekly_path": [], "samples_path": []},
                                        schema={"weekly_path": pl.String, "samples_path": pl.String}),
                           state / "files.parquet")
            for week, initialization in coverage.filter(pl.col("matched")).select("timestamp", "initialization_date").rows():
                timestamp = datetime.combine(week, time(), timezone.utc)
                selected = frame.filter(pl.col("timestamp") == timestamp)
                points = selected.select("lat", "lon").rows()
                identity = json.dumps([SCHEMA_VERSION, initialization.isoformat(), steps, points])
                fingerprint = hashlib.sha256(identity.encode()).hexdigest()
                directory = (output / f"year={initialization.year}" / f"month={initialization.month:02d}"
                             / f"initialization={initialization:%Y%m%d}" / fingerprint[:20])
                weekly, samples = directory / "weekly.parquet", directory / "samples.parquet"
                manifest = directory / "manifest.json"
                complete = not refresh and verified(manifest, fingerprint, weekly, samples)
                if complete:
                    skipped += 1
                else:
                    def member_job(member):
                        path, source = client.download(key_for(initialization, member), steps, refresh=refresh)
                        mean, values, grid = normalize(path, initialization, member, points, steps)
                        return mean, values, grid, path, source
                    # A single pool limits combined network and decode concurrency.
                    futures = [pool.submit(member_job, member) for member in MEMBERS]
                    try:
                        results = [future.result() for future in futures]
                    finally:
                        for future in futures:
                            future.cancel()
                    if any(item[2] != results[0][2] for item in results):
                        raise ValueError("GEFS member grids differ")
                    weekly_frame = selected.with_columns(
                        pl.lit(initialization).alias("initialization_date"),
                        pl.lit(timestamp+timedelta(days=7)).alias("valid_end"),
                        pl.lit("gefs_v12").alias("model"), pl.lit(lead_days).alias("lead_days"),
                        pl.lit("K").alias("units"), pl.lit(6).alias("sampling_hours"),
                        pl.lit("mean_of_6h_instantaneous_samples").alias("aggregation"),
                        pl.lit("bilinear").alias("spatial_method"),
                        *(pl.Series(name, item[0]) for name, item in zip(COLUMNS, results)))
                    sample_frame = pl.concat([item[1] for item in results]).with_columns(
                        pl.lit(initialization).alias("initialization_date"),
                        pl.lit("gefs_v12").alias("model"))
                    atomic_parquet(sample_frame, samples)
                    atomic_parquet(weekly_frame, weekly)
                    info = dict(schema_version=SCHEMA_VERSION, fingerprint=fingerprint,
                                initialization_date=initialization.isoformat(), target_week=week.isoformat(),
                                forecast_hours=steps, source_grid=results[0][2],
                                weekly_sha256=checksum(weekly), samples_sha256=checksum(samples),
                                sources=[item[4] for item in results])
                    atomic_bytes(manifest, json.dumps(info, sort_keys=True).encode())
                    downloaded += 1
                    transferred += sum(item[4]["bytes"] for item in results if not item[4].get("cached"))
                    rows_written += weekly_frame.height
                    logger.info("GEFS saved %s: %d grid points, 11 members, 28 samples/member", week, len(points))
                    if not keep_raw:
                        for _, _, _, path, _ in results:
                            path.unlink(missing_ok=True)
                            path.with_suffix(".json").unlink(missing_ok=True)
                paths.append(dict(weekly_path=str(weekly.resolve()), samples_path=str(samples.resolve())))
                coverage = coverage.with_columns(
                    pl.when(pl.col("timestamp") == week).then(pl.lit("completed"))
                    .otherwise(pl.col("status")).alias("status"))
                atomic_parquet(coverage, state / "coverage.parquet")
                atomic_parquet(pl.DataFrame(paths), state / "files.parquet")
        return dict(files=paths, coverage=coverage, downloaded=downloaded, skipped=skipped,
                    bytes_downloaded=transferred, rows_written=rows_written)
    finally:
        client.close()
