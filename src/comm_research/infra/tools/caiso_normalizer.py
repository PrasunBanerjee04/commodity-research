"""Transform staged OASIS CSVs into typed, UTC-partitioned Parquet tables."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import polars as pl

from comm_research.infra.scrapers.caiso.oasis.models import Report

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_LAKE_ROOT = PROJECT_ROOT / "data/lake/power_gas/napg/caiso"
logger = logging.getLogger(__name__)
_TIMESTAMPS = ("interval_start_time_gmt", "interval_end_time_gmt")
_ALIASES = {
    "intervalstarttime_gmt": _TIMESTAMPS[0],
    "intervalendtime_gmt": _TIMESTAMPS[1],
    "interval_start_gmt": _TIMESTAMPS[0],
    "interval_end_gmt": _TIMESTAMPS[1],
    "starttime_gmt": _TIMESTAMPS[0],
    "endtime_gmt": _TIMESTAMPS[1],
    "stoptime_gmt": _TIMESTAMPS[1],
    "start_date_gmt": _TIMESTAMPS[0],
    "end_date_gmt": _TIMESTAMPS[1],
    "start_date_time_gmt": _TIMESTAMPS[0],
    "end_date_time_gmt": _TIMESTAMPS[1],
    "start_date_time": _TIMESTAMPS[0],
    "end_date_time": _TIMESTAMPS[1],
    "opr_date": "opr_dt",
}
_INTEGERS = {"opr_hr", "opr_interval", "interval_num", "interval_number"}
_FLOATS = {
    "mw",
    "value",
    "lmp_prc",
    "lmp",
    "mwh",
    "prc",
    "price",
    "quantity",
    "xaxisdata",
    "y1axisdata",
    "y2axisdata",
    "mwquantity",
    "crr_price",
    "selfschedmw",
    "mineohstateofcharge",
    "maxeohstateofcharge",
}


def _column_name(name: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    if not name:
        raise ValueError("CSV contains an empty column name")
    return _ALIASES.get(name, name)


def read_csv(path: Path | str, *, report: Report | None = None) -> pl.DataFrame:
    """Read identifiers as strings and explicitly type OASIS measurement fields.

    Unknown fields stay strings, preserving identifiers and leading zeroes.
    Invalid numeric or timestamp values fail rather than being silently dropped.
    """
    frame = pl.read_csv(path, infer_schema=False, null_values=["", "NULL", "null"])
    names = [_column_name(name) for name in frame.columns]
    if len(set(names)) != len(names):
        raise ValueError(f"Column names collide after normalization: {path}")
    frame.columns = names
    missing = set(_TIMESTAMPS) - set(names)
    if missing and report is None:
        raise ValueError(
            f"Missing required timestamp columns in {path}: {sorted(missing)}"
        )
    if frame.is_empty():
        raise ValueError(f"CSV contains no data rows: {path}")
    timestamp_names = {
        name
        for name in names
        if name in _TIMESTAMPS
        or name.endswith("_gmt")
        or name in {"inventory_date_time", "offerperiodstarttime", "offerperiodendtime"}
    }
    expressions = [
        (
            pl.col(name).cast(pl.Datetime("us", "UTC"))
            if frame[name].null_count() == frame.height
            else pl.col(name).str.to_datetime(time_zone="UTC", strict=True)
        ).alias(name)
        for name in timestamp_names
    ]
    expressions.extend(
        pl.col(name).cast(pl.Int64, strict=True) for name in _INTEGERS & set(names)
    )
    floats = _FLOATS | set(report.numeric_columns if report else ())
    floats |= {
        name
        for name in names
        if name.endswith(("_mw", "_mwh", "_prc", "_price", "_cost"))
    }
    floats |= {
        name for name in names if re.search(r"(^|_)(xaxisdata|y[12]_?axisdata)$", name)
    }
    floats |= {name for name in names if re.fullmatch(r"he\d{2}", name)}
    expressions.extend(
        pl.col(name).cast(pl.Float64, strict=True) for name in floats & set(names)
    )
    dates = {"opr_dt"} | set(report.date_columns if report else ())
    expressions.extend(
        pl.col(name).str.to_date(strict=True) for name in dates & set(names)
    )
    frame = frame.with_columns(expressions)
    if not missing and any(frame[name].null_count() for name in _TIMESTAMPS):
        raise ValueError(f"Null interval timestamps in {path}")
    if (
        not missing
        and frame.filter(pl.col(_TIMESTAMPS[1]) <= pl.col(_TIMESTAMPS[0])).height
    ):
        raise ValueError(f"Interval end must be later than interval start: {path}")
    return frame


def _write_atomic(frame: pl.DataFrame, target: Path) -> None:
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


def normalize_csvs(
    raw_files: Iterable[Path | str],
    dataset_name: str,
    *,
    lake_root: Path | str = DEFAULT_LAKE_ROOT,
    report: Report | None = None,
    request_day: date | None = None,
    cleanup: bool = True,
) -> list[Path]:
    """Merge into daily UTC partitions, then remove successfully ingested CSVs.

    Existing rows (including other nodes) are preserved; exact duplicate rows
    are removed so retries are idempotent. All input is validated before writing.
    Each partition is replaced atomically, but the batch is not a transaction:
    on failure, raw files remain for retry. Use one writer per dataset.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", dataset_name):
        raise ValueError("dataset_name must be a lowercase snake_case identifier")
    paths = list(dict.fromkeys(Path(path) for path in raw_files))
    if not paths:
        raise ValueError("No staged CSVs supplied")
    logger.info("Normalizing %s: %d CSV(s)", dataset_name, len(paths))
    frame = pl.concat(
        [read_csv(path, report=report) for path in paths], how="diagonal_relaxed"
    )
    partition_column = "__caiso_partition_date"
    if partition_column in frame.columns:
        raise ValueError(f"Reserved column: {partition_column}")
    if report and report.snapshot:
        if request_day is None:
            raise ValueError("Snapshot reports require the query's request_day")
        frame = frame.with_columns(pl.lit(request_day).alias("as_of_date"))
        partition = pl.col("as_of_date")
    elif _TIMESTAMPS[0] in frame.columns:
        partition = pl.col(_TIMESTAMPS[0]).dt.date()
    elif report and "opr_dt" in frame.columns:
        partition = pl.col("opr_dt")
    else:
        raise ValueError(
            f"No interval/operating date in {dataset_name}: {frame.columns}"
        )
    frame = frame.with_columns(partition.alias(partition_column))
    outputs: list[Path] = []
    for (day,), partition in frame.partition_by(partition_column, as_dict=True).items():
        target = (
            Path(lake_root)
            / dataset_name
            / f"year={day.year:04d}"
            / f"month={day.month:02d}"
            / f"day={day.day:02d}"
            / "data.parquet"
        )
        clean = partition.drop(partition_column)
        if target.exists():
            clean = pl.concat(
                [pl.read_parquet(target, hive_partitioning=False), clean],
                how="diagonal_relaxed",
            )
        clean = clean.unique(maintain_order=True)
        if _TIMESTAMPS[0] in clean.columns:
            clean = clean.sort(_TIMESTAMPS[0])
        _write_atomic(clean, target)
        logger.info("Wrote %s: %d rows", target, clean.height)
        outputs.append(target)
    # No input is removed until every partition has been successfully written.
    if cleanup:
        for path in paths:
            path.unlink()
    logger.info("Normalized %s into %d partition(s)", dataset_name, len(outputs))
    return sorted(outputs)
