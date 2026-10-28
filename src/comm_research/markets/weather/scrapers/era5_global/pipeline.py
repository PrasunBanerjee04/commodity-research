"""Resumable global hourly temperature ingestion into UTC daily partitions."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from comm_research.infra.lake.writer_lock import writer_lock
from comm_research.infra.paths import PROJECT_ROOT

from .client import ERA5Client, SOURCE_URL
from .normalizer import HourNormalizer, SCHEMA_VERSION, parquet_schema

DEFAULT_LAKE_ROOT = PROJECT_ROOT / "data/lake"
DEFAULT_STATE_ROOT = PROJECT_ROOT / "data/state/weather/era5/t_2m"
DATASET = Path("weather/era5/t_2m/single_levels")
logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    downloaded: int = 0
    skipped: int = 0
    rows_written: int = 0
    bytes_written: int = 0


def partition(lake: Path, day: date) -> Path:
    return (lake / DATASET / f"year={day.year:04d}" / f"month={day.month:02d}"
            / f"day={day.day:02d}" / "data.parquet")


def _manifest_path(state: Path, day: date) -> Path:
    return state / f"{day:%Y/%m/%d}.json"


def _fingerprint(info) -> str:
    digest = hashlib.sha256()
    digest.update(f"{SCHEMA_VERSION}:{info.url}:2m_temperature:finalized".encode())
    for coordinate in (info.latitude, info.longitude):
        digest.update(coordinate.tobytes())
    return digest.hexdigest()


def _atomic_json(target: Path, contents: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=target.parent, prefix=".era5-", suffix=".partial", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(contents, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _verified(target, day, schema, cells) -> bool:
    """Verify the footer and per-hour row counts without rescanning old history."""
    import pyarrow.parquet as pq

    try:
        with pq.ParquetFile(target) as parquet:
            stored = parquet.schema_arrow
            if (stored.metadata or {}).get(b"fingerprint") != schema.metadata[b"fingerprint"]:
                return False
            if not stored.remove_metadata().equals(schema.remove_metadata()):
                return False
            if parquet.metadata.num_rows != cells * 24:
                return False
            hours = Counter()
            for i in range(parquet.metadata.num_row_groups):
                group = parquet.metadata.row_group(i)
                timestamp = group.column(0).statistics
                if not timestamp or not timestamp.has_min_max or timestamp.min != timestamp.max:
                    return False
                when = timestamp.min
                if (when.date() != day or when.minute or when.second or when.microsecond):
                    return False
                if any(group.column(j).statistics is None
                       or group.column(j).statistics.null_count != 0
                       for j in range(4)):
                    return False
                hours[when.hour] += group.num_rows
            return hours == Counter({hour: cells for hour in range(24)})
    except (OSError, ValueError):
        return False


def _hour_batches(source, hours, executor, workers):
    """Keep at most workers downloads in flight; yield in timestamp order."""
    timestamps = iter(hours)
    pending = deque()
    try:
        for _ in range(min(workers, len(hours))):
            hour = next(timestamps)
            pending.append((hour, executor.submit(source.read_hour, hour)))
        while pending:
            hour, future = pending.popleft()
            values = future.result()
            next_hour = next(timestamps, None)
            if next_hour is not None:
                pending.append((next_hour, executor.submit(source.read_hour, next_hour)))
            yield hour, values
    finally:
        for _, future in pending:
            future.cancel()


def _check_space(target: Path, required_bytes: int, reserve_bytes: int):
    free = shutil.disk_usage(target).free
    if free < required_bytes + reserve_bytes:
        raise OSError(
            f"Insufficient lake storage: {free / 2**30:.2f} GiB free; need "
            f"{(required_bytes + reserve_bytes) / 2**30:.2f} GiB including reserve. "
            "Use --lake on a larger volume or reduce --min-free-gb."
        )


def _manifest(target, day, schema, info):
    import pyarrow.parquet as pq

    # Recovery after a crash must preserve the original file's provenance,
    # even if the upstream availability watermark has advanced since then.
    metadata = pq.read_schema(target).metadata
    return {
        "status": "complete", "day": day.isoformat(),
        "fingerprint": schema.metadata[b"fingerprint"].decode(),
        "rows": info.cells * 24, "bytes": target.stat().st_size,
        "source_url": metadata[b"source_url"].decode(), "source_status": "finalized",
        "source_valid_time_stop": metadata[b"source_valid_time_stop"].decode(),
        "source_last_updated": metadata[b"source_last_updated"].decode(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def _state_matches(manifest, target, schema, info):
    try:
        contents = json.loads(manifest.read_text())
        return (
            contents.get("status") == "complete"
            and contents.get("fingerprint") == schema.metadata[b"fingerprint"].decode()
            and contents.get("rows") == info.cells * 24
            and contents.get("bytes") == target.stat().st_size
        )
    except (OSError, ValueError, AttributeError):
        return False


def run(
    start: date = date(2000, 1, 1),
    end: date | None = None,
    *,
    lake: Path = DEFAULT_LAKE_ROOT,
    state: Path | None = None,
    workers: int = 4,
    retries: int = 4,
    timeout: float = 120.0,
    min_free_gb: float = 2.0,
    refresh: bool = False,
    source_url: str = SOURCE_URL,
) -> RunSummary:
    """Fetch [start, end); omitted end means the latest complete finalized day.

    Processes days in order, so a full-history run can resume after interruption
    or storage exhaustion. Existing partitions are verified before skipping.
    """
    import math

    if (isinstance(start, datetime) or not isinstance(start, date)
            or (end is not None and (isinstance(end, datetime) or not isinstance(end, date)))):
        raise ValueError("start/end must be calendar dates")
    if end is not None and end <= start:
        raise ValueError("end must be after start (exclusive end date)")
    if (not isinstance(workers, int) or workers < 1 or not isinstance(retries, int)
            or retries < 0 or not math.isfinite(timeout) or timeout <= 0
            or not math.isfinite(min_free_gb) or min_free_gb < 0):
        raise ValueError("Invalid workers, retries, timeout, or free-space reserve")
    try:
        import numpy as np
        import pyarrow.parquet as pq
        import httpx  # noqa: F401
        import zarr  # noqa: F401
    except ImportError as error:
        raise RuntimeError("Install ERA5 dependencies: python -m pip install -e '.[era5]'") from error
    lake = Path(lake).expanduser().resolve()
    # Custom lakes keep state beside their own data unless explicitly overridden.
    state = (Path(state).expanduser().resolve() if state is not None
             else lake / ".state/weather/era5/t_2m" if lake != DEFAULT_LAKE_ROOT.resolve()
             else DEFAULT_STATE_ROOT)
    summary = RunSummary()
    with writer_lock(lake), ERA5Client(
        workers=workers, retries=retries, timeout=timeout, url=source_url
    ) as source:
        info = source.info
        stop = end or info.end_day
        if start < info.first_day or stop > info.end_day or start >= stop:
            raise ValueError(
                f"Requested dates are outside finalized complete days: "
                f"[{info.first_day}, {info.end_day}); end is exclusive"
            )
        schema = parquet_schema(info, _fingerprint(info))
        normalizer = HourNormalizer(info, schema)
        reserve_bytes = int(min_free_gb * 2**30)
        # Conservative allowance for one uncompressed daily table. Output is
        # compressed, but relying on a guessed compression ratio risks ENOSPC.
        required_bytes = info.cells * 24 * (8 + 4 + 4 + 4)
        logger.info("ERA5 finalized window [%s, %s); requested [%s, %s); %s cells/hour",
                    info.first_day, info.end_day, start, stop, f"{info.cells:,}")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="era5") as executor:
            day = start
            while day < stop:
                target = partition(lake, day)
                manifest = _manifest_path(state, day)
                if target.exists() and not refresh:
                    if _verified(target, day, schema, info.cells):
                        # Also repairs a crash between output and state publication.
                        if not _state_matches(manifest, target, schema, info):
                            _atomic_json(manifest, _manifest(target, day, schema, info))
                        summary.skipped += 1
                        logger.info("SKIP %s: verified complete partition", day)
                        day += timedelta(days=1)
                        continue
                    # Never silently replace an unrelated file from another source.
                    try:
                        with pq.ParquetFile(target) as existing:
                            fingerprint = (existing.schema_arrow.metadata or {}).get(b"fingerprint")
                    except (OSError, ValueError):
                        fingerprint = None
                    if fingerprint != schema.metadata[b"fingerprint"]:
                        raise ValueError(f"Unrecognized/corrupt partition {target}; use --refresh to replace")
                    logger.warning("Rebuilding incomplete partition %s", target)
                target.parent.mkdir(parents=True, exist_ok=True)
                _check_space(target.parent, required_bytes, reserve_bytes)
                hours = np.datetime64(day, "us") + np.arange(24) * np.timedelta64(1, "h")
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=target.parent, prefix=".era5-", suffix=".partial", delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                    logger.info("FETCH %s: 24 global hours with %d workers", day, workers)
                    with pq.ParquetWriter(
                        temporary, schema, compression="zstd", use_dictionary=False,
                        write_statistics=True,
                    ) as writer:
                        for hour, values in _hour_batches(source, hours, executor, workers):
                            _check_space(target.parent, 0, reserve_bytes)
                            table = normalizer.table(hour, values)
                            writer.write_table(table, row_group_size=131_072)
                    if not _verified(temporary, day, schema, info.cells):
                        raise ValueError(f"Daily Parquet verification failed for {day}")
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                    _atomic_json(manifest, _manifest(target, day, schema, info))
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                summary.downloaded += 1
                summary.rows_written += info.cells * 24
                summary.bytes_written += target.stat().st_size
                logger.info("SAVED %s: %s rows, %.1f MiB", day, f"{info.cells * 24:,}",
                            target.stat().st_size / 2**20)
                day += timedelta(days=1)
    logger.info("Finished: %s", summary)
    return summary
