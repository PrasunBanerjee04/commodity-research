"""Incrementally refresh the CAISO OASIS lake. Configure globals below, then run()."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time
from pathlib import Path
from typing import IO
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import polars as pl

from comm_research.infra.scrapers.caiso.oasis import (
    SCRAPERS,
    CAISOOASISClient,
    NoDataError,
    OASISError,
)
from comm_research.infra.scrapers.caiso.oasis.models import Report
from comm_research.infra.tools.caiso_normalizer import DEFAULT_LAKE_ROOT, normalize_csvs
from comm_research.infra.tools.lake_methods import _clear_raw

# Running this module with no arguments refreshes every registered report.
CATEGORIES = tuple(SCRAPERS)
DATASETS: tuple[str, ...] | None = None 
START_DATE: date | None = None 
END_DATE: date | None = None

IGNORE_END_DATE = True

RETENTION_MONTHS = 39

NODES = ("TH_SP15_GEN-APND", "TH_NP15_GEN-APND")
REPORT_PARAMETERS: dict[str, dict[str, str]] = {}
PROJECT_ROOT = Path(__file__).resolve().parents[6]
RAW_ROOT = PROJECT_ROOT / "data/raw/caiso"
LAKE_ROOT = DEFAULT_LAKE_ROOT
STATE_ROOT = PROJECT_ROOT / "data/state/caiso_oasis"
LOG_LEVEL = logging.INFO

logger = logging.getLogger(
    "comm_research.markets.power_gas.napg.caiso.caiso_oasis_pipeline"
)


@dataclass
class RunSummary:
    downloaded: int = 0
    resumed: int = 0
    skipped: int = 0
    adopted: int = 0
    no_data: int = 0
    incomplete: int = 0
    failed: int = 0
    unavailable: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _configure_logging(stream: IO[str] | None) -> None:
    parent = logging.getLogger("comm_research")
    for handler in list(parent.handlers):
        if getattr(handler, "_caiso_pipeline", False):
            parent.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(stream or sys.stdout)  # emit() flushes each record.
    handler._caiso_pipeline = True
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    parent.addHandler(handler)
    parent.setLevel(LOG_LEVEL)
    parent.propagate = False


def _subtract_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _day(value: date | str) -> date:
    if isinstance(value, datetime):
        raise TypeError("Use calendar dates, not datetimes, for pipeline boundaries")
    return date.fromisoformat(value) if isinstance(value, str) else value


def _bounds(
    report: Report, start: date | None, end: date, today: date
) -> tuple[date, date]:
    floor = _subtract_months(today, min(RETENTION_MONTHS, report.retention_months))
    if report.available_from:
        floor = max(floor, report.available_from)
    lower = max(start or floor, floor)
    if start and start < floor:
        logger.warning(
            "%s: start clamped from %s to retention bound %s",
            report.dataset_name,
            start,
            floor,
        )
    publication_cutoff = today - timedelta(days=report.publication_lag_days)
    if report.publication_lag_days == 0:
        publication_cutoff += timedelta(days=1)
    upper = min(end, publication_cutoff)
    if report.publication_lag_quarters:
        quarter_start = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
        upper = min(
            upper, _subtract_months(quarter_start, report.publication_lag_quarters * 3)
        )
    if report.latest_only:
        # The current-usage service explicitly rejects historical queries.
        if end < today or lower > today:
            return lower, lower
        return today, today + timedelta(days=1)
    return lower, upper


def _query_bounds(report: Report, day: date) -> tuple[datetime, datetime]:
    # All pipeline dates are Pacific trading days; lake partitions stay UTC.
    tz = ZoneInfo("America/Los_Angeles")
    return (
        datetime.combine(day, datetime_time(), tz),
        datetime.combine(day + timedelta(days=1), datetime_time(), tz),
    )


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".json", delete=False
    ) as output:
        temporary = Path(output.name)
        json.dump(value, output, default=str, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError) as error:
        logger.warning("Ignoring unreadable checkpoint %s: %s", path, error)
        return {}


def _fingerprint(report: Report, params: dict[str, str]) -> str:
    specification = {
        "report": asdict(report),
        "parameters": params,
        "normalizer_version": 3,
    }
    return hashlib.sha256(
        json.dumps(specification, default=str, sort_keys=True).encode()
    ).hexdigest()[:20]


def _partition(lake: Path, report: Report, day: date) -> Path:
    return (
        lake
        / report.dataset_name
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
        / "data.parquet"
    )


def _verified_outputs(state: dict, lake: Path) -> bool:
    if state.get("status") != "complete" or not state.get("outputs"):
        return False
    for entry in state["outputs"]:
        try:
            path = lake / entry["path"]
            if not path.resolve().is_relative_to(lake.resolve()):
                return False
            # Reading the footer checks that the partition is still a valid table.
            table = pl.scan_parquet(path, hive_partitioning=False)
            if table.select(pl.len()).collect().item() < entry["rows"]:
                return False
        except (OSError, ValueError, KeyError, TypeError, pl.exceptions.PolarsError):
            return False
    return True


def _output_records(paths: Iterable[Path], lake: Path) -> list[dict]:
    return [
        {
            "path": str(path.relative_to(lake)),
            "rows": pl.scan_parquet(path, hive_partitioning=False)
            .select(pl.len())
            .collect()
            .item(),
        }
        for path in paths
    ]


def _legacy_complete(path: Path, report: Report, day: date, node: str | None) -> bool:
    """Adopt existing price data only when every requested interval is present."""
    if not path.exists() or report.interval_minutes is None or node is None:
        return False
    try:
        # A Pacific trading day crosses two UTC lake partitions.
        next_path = _partition(path.parents[4], report, day + timedelta(days=1))
        paths = [item for item in (path, next_path) if item.exists()]
        table = pl.concat(
            [pl.read_parquet(item, hive_partitioning=False) for item in paths],
            how="diagonal_relaxed",
        )
        if not {"node", "interval_start_time_gmt", "interval_end_time_gmt"} <= set(
            table.columns
        ):
            return False
        table = table.filter(pl.col("node") == node)
        if "market_run_id" in table.columns and report.market_run_id:
            table = table.filter(pl.col("market_run_id") == report.market_run_id)
        lower, upper = _query_bounds(report, day)
        lower, upper = lower.astimezone(timezone.utc), upper.astimezone(timezone.utc)
        table = table.filter(
            (pl.col("interval_start_time_gmt") >= lower)
            & (pl.col("interval_start_time_gmt") < upper)
        )
        components = set(table["lmp_type"]) if "lmp_type" in table.columns else set()
        if not set(report.required_components) <= components:
            return False
        measurements = {"mw", "value", "lmp_prc"} & set(table.columns)
        if not measurements or any(
            table[column].null_count() for column in measurements
        ):
            return False
        step = timedelta(minutes=report.interval_minutes)
        expected = {
            lower + index * step for index in range(int((upper - lower) / step))
        }
        parts = (
            table.partition_by("lmp_type") if "lmp_type" in table.columns else [table]
        )
        if "lmp_type" in table.columns and "LMP" not in set(table["lmp_type"]):
            return False
        if not parts or any(
            set(part["interval_start_time_gmt"]) != expected for part in parts
        ):
            return False
        return all(
            end - start == step
            for start, end in table.select(
                "interval_start_time_gmt", "interval_end_time_gmt"
            ).iter_rows()
        )
    except (OSError, ValueError, pl.exceptions.PolarsError):
        return False


@contextmanager
def _run_lock(state_root: Path):
    # Linux/macOS advisory lock survives crashes without stale lock ownership.
    import fcntl

    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "Another OASIS refresh is using this state directory"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run(
    *,
    categories: Iterable[str] | None = None,
    datasets: Iterable[str] | None = None,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    ignore_end_date: bool | None = None,
    nodes: Iterable[str] | None = None,
    report_parameters: dict[str, dict[str, str]] | None = None,
    raw_root: Path | str | None = None,
    lake_root: Path | str | None = None,
    state_root: Path | str | None = None,
    client: CAISOOASISClient | None = None,
    log_stream: IO[str] | None = None,
) -> RunSummary:
    """Refresh selected datasets, fill gaps, and return explicit outcome counts.

    Completed days are skipped per report/filter fingerprint. Failed/no-data days
    remain eligible for later runs. Raw data is checkpointed before normalization
    and retained for recovery on failure. No bulk backfill is launched on import.
    """
    _configure_logging(log_stream)
    chosen = tuple(CATEGORIES if categories is None else categories)
    unknown = set(chosen) - set(SCRAPERS)
    if unknown:
        raise ValueError(f"Unknown categories: {sorted(unknown)}")
    selected = DATASETS if datasets is None else tuple(datasets)
    reports = [
        (SCRAPERS[category], report)
        for category in dict.fromkeys(chosen)
        for report in SCRAPERS[category].reports
        if selected is None or report.dataset_name in selected
    ]
    if selected is not None:
        unknown = set(selected) - {report.dataset_name for _, report in reports}
        if unknown:
            raise ValueError(
                f"Datasets not present in selected categories: {sorted(unknown)}"
            )
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    lower = start_date if start_date is not None else START_DATE
    lower = _day(lower) if lower is not None else None
    ignore = IGNORE_END_DATE if ignore_end_date is None else ignore_end_date
    end = end_date if end_date is not None else END_DATE
    if not ignore and end is None:
        raise ValueError("Set END_DATE/end_date when IGNORE_END_DATE is false")
    end = today if ignore else _day(end)
    if lower is not None and lower >= end:
        raise ValueError("Start date must precede the exclusive end date")
    if RETENTION_MONTHS < 1:
        raise ValueError("RETENTION_MONTHS must be positive")
    node_list = tuple(dict.fromkeys(NODES if nodes is None else nodes))
    if any(report.requires_node for _, report in reports) and not node_list:
        raise ValueError("Configure at least one LMP node")
    params_by_report = (
        REPORT_PARAMETERS if report_parameters is None else report_parameters
    )
    raw = Path(raw_root or RAW_ROOT).absolute()
    lake = Path(lake_root or LAKE_ROOT).absolute()
    state = Path(state_root or STATE_ROOT).absolute()
    for left, right in ((raw, lake), (raw, state), (lake, state)):
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError("Raw, lake, and state roots must be separate directories")
    downloader = client or CAISOOASISClient(raw_root=raw)
    if Path(downloader.raw_root).absolute() != raw:
        raise ValueError("Client and pipeline raw roots must match")
    summary = RunSummary()
    logger.info(
        "OASIS refresh: %d reports, start=%s end=%s (exclusive), nodes=%s",
        len(reports),
        lower or "retention bound",
        end,
        node_list,
    )

    with _run_lock(state):
        # Preserve unknown pre-existing files rather than discard unrecovered data.
        keep = {
            path.absolute()
            for path in raw.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        try:
            for category in chosen:
                for item in SCRAPERS[category].unavailable:
                    message = f"{category}/{item}: current API definition not verified"
                    summary.unavailable.append(message)
                    logger.warning("Unavailable: %s", message)
            for scraper, report in reports:
                overrides = params_by_report.get(report.dataset_name, {})
                for node in node_list if report.requires_node else (None,):
                    label = report.dataset_name + (f" node={node}" if node else "")
                    try:
                        params = report.request_parameters(overrides, node=node)
                    except ValueError as error:
                        summary.failed += 1
                        summary.failures.append(f"{label}: {error}")
                        logger.error("Skipping %s: %s", label, error)
                        continue
                    report_today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
                    begin, stop = _bounds(
                        report, lower, report_today if ignore else end, report_today
                    )
                    fingerprint = _fingerprint(report, params)
                    logger.info(
                        "Report %s: %s [%s, %s)", scraper.name, label, begin, stop
                    )
                    if begin >= stop:
                        logger.info("No published dates eligible for %s", label)
                    day = begin
                    while day < stop:
                        checkpoint = (
                            state
                            / report.dataset_name
                            / fingerprint
                            / f"{day.isoformat()}.json"
                        )
                        saved = _read_state(checkpoint)
                        staged: list[Path] = []
                        try:
                            if not report.latest_only and _verified_outputs(
                                saved, lake
                            ):
                                summary.skipped += 1
                                logger.info("Skip complete: %s %s", label, day)
                            elif not report.latest_only and _legacy_complete(
                                _partition(lake, report, day), report, day, node
                            ):
                                targets = [
                                    _partition(lake, report, value)
                                    for value in (day, day + timedelta(days=1))
                                ]
                                _atomic_json(
                                    checkpoint,
                                    {
                                        "status": "complete",
                                        "parameters": params,
                                        "outputs": _output_records(
                                            [
                                                target
                                                for target in targets
                                                if target.exists()
                                            ],
                                            lake,
                                        ),
                                    },
                                )
                                summary.adopted += 1
                                logger.info(
                                    "Adopt existing complete data: %s %s", label, day
                                )
                            else:
                                staged = [
                                    Path(path) for path in saved.get("raw_files", [])
                                ]
                                if staged and not all(
                                    path.is_relative_to(raw) and path.is_file()
                                    for path in staged
                                ):
                                    staged = []
                                if staged and saved.get("status") == "staged":
                                    summary.resumed += 1
                                    logger.info("Resume staged CSVs: %s %s", label, day)
                                else:
                                    start, finish = _query_bounds(report, day)
                                    logger.info(
                                        "Pull %s %s [%s, %s)",
                                        label,
                                        day,
                                        start.isoformat(),
                                        finish.isoformat(),
                                    )
                                    staged = scraper.download(
                                        downloader,
                                        report,
                                        start,
                                        finish,
                                        node=node,
                                        parameters=overrides,
                                    )
                                    keep.update(staged)
                                    _atomic_json(
                                        checkpoint,
                                        {
                                            "status": "staged",
                                            "parameters": params,
                                            "raw_files": [str(path) for path in staged],
                                        },
                                    )
                                outputs = normalize_csvs(
                                    staged,
                                    report.dataset_name,
                                    lake_root=lake,
                                    report=report,
                                    request_day=day,
                                    cleanup=False,
                                )
                                complete = not (
                                    report.requires_node and report.interval_minutes
                                ) or _legacy_complete(
                                    _partition(lake, report, day), report, day, node
                                )
                                _atomic_json(
                                    checkpoint,
                                    {
                                        "status": "complete"
                                        if complete
                                        else "incomplete",
                                        "parameters": params,
                                        "outputs": _output_records(outputs, lake),
                                    },
                                )
                                keep.difference_update(staged)
                                summary.downloaded += 1
                                if complete:
                                    logger.info(
                                        "Complete %s %s: %d partition(s)",
                                        label,
                                        day,
                                        len(outputs),
                                    )
                                else:
                                    summary.incomplete += 1
                                    logger.warning(
                                        "Partial price coverage: %s %s; saved rows, retry on next run",
                                        label,
                                        day,
                                    )
                        except NoDataError as error:
                            summary.no_data += 1
                            logger.warning(
                                "No published data: %s %s: %s; retry on a later run",
                                label,
                                day,
                                error,
                            )
                        except Exception as error:  # noqa: BLE001 - isolate failures to one report/day.
                            keep.update(staged)
                            summary.failed += 1
                            message = f"{label} {day}: {type(error).__name__}: {error}"
                            summary.failures.append(message)
                            logger.error(
                                "Failed %s; continuing with next day/report", message
                            )
                            if isinstance(error, HTTPError) and error.code in (
                                401,
                                403,
                            ):
                                logger.error(
                                    "Access denied for %s; skipping remaining dates for this report",
                                    label,
                                )
                                break
                            if isinstance(error, OASISError) and error.code in {
                                "1001",
                                "1002",
                                "1004",
                                "1005",
                                "1011",
                                "1016",
                                "1017",
                                "1018",
                                "1019",
                                "1020",
                            }:
                                logger.error(
                                    "Invalid API configuration for %s; skipping remaining dates",
                                    label,
                                )
                                break
                        day += timedelta(days=1)
        finally:
            _clear_raw(raw, keep=keep)
            logger.info("Refresh finished: %s", asdict(summary))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="Inclusive date, YYYY-MM-DD")
    parser.add_argument(
        "--end", help="Exclusive date; supplying this disables IGNORE_END_DATE"
    )
    parser.add_argument("--categories", nargs="+", choices=tuple(SCRAPERS))
    parser.add_argument(
        "--datasets", nargs="+", help="Dataset names from the category modules"
    )
    parser.add_argument("--nodes", nargs="+", help="LMP nodes")
    args = parser.parse_args()
    summary = run(
        start_date=args.start,
        end_date=args.end,
        ignore_end_date=False if args.end else None,
        categories=args.categories,
        datasets=args.datasets,
        nodes=args.nodes,
    )
    if summary.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
