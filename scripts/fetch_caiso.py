#!/usr/bin/env python3
"""Focused, resumable CAISO LMP backfill CLI; dates are inclusive Pacific days."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

# Permit direct checkout execution without an editable package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import polars as pl

from comm_research.infra.scrapers.caiso.oasis.client import (
    CAISOOASISClient,
    NoDataError,
    OASISError,
)
from comm_research.infra.scrapers.caiso.oasis.models import Report
from comm_research.infra.scrapers.caiso.oasis.prices import REPORTS
from comm_research.infra.tools.caiso_normalizer import DEFAULT_LAKE_ROOT, normalize_csvs
from comm_research.infra.tools.caiso_schema import (
    COMPONENT_ALIASES,
    normalize_schema,
    utc_expression,
)
from comm_research.infra.tools.lake_methods.writer_lock import writer_lock

MARKETS = {"DAM": REPORTS[0], "RTM": REPORTS[1], "FMM": REPORTS[2]}
PACIFIC = ZoneInfo("America/Los_Angeles")
LOG = logging.getLogger("fetch_caiso")


@dataclass
class FetchSummary:
    downloaded: int = 0
    skipped: int = 0
    incomplete: int = 0
    no_data: int = 0
    failed: int = 0


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, datetime.min.time(), PACIFIC).astimezone(timezone.utc),
        datetime.combine(
            day + timedelta(days=1), datetime.min.time(), PACIFIC
        ).astimezone(timezone.utc),
    )


def partition_path(lake: Path, dataset: str, day: date) -> Path:
    return (
        lake
        / dataset
        / f"year={day.year:04d}/month={day.month:02d}/day={day.day:02d}/data.parquet"
    )


def complete_day(lake: Path, report: Report, day: date, node: str) -> bool:
    """Skip only verified interval/component coverage, never just a directory."""
    lower, upper = day_bounds(day)
    paths = [
        partition_path(lake, report.dataset_name, value)
        for value in (lower.date(), upper.date())
    ]
    paths = list(dict.fromkeys(path for path in paths if path.is_file()))
    if not paths:
        return False
    try:
        frames = [
            normalize_schema(
                pl.scan_parquet(path, hive_partitioning=False), report.dataset_name
            )
            for path in paths
        ]
        frame = pl.concat(frames, how="diagonal_relaxed")
        if not {"timestamp", "interval_end_time_gmt", "node", "lmp_type"} <= set(
            frame.collect_schema()
        ):
            return False
        frame = frame.filter(
            (pl.col("node") == node)
            & (pl.col("timestamp") >= lower)
            & (pl.col("timestamp") < upper)
        )
        if "market_run_id" in frame.collect_schema():
            frame = frame.filter(pl.col("market_run_id") == report.market_run_id)
        value = next(
            (
                column
                for column in ("value", "mw", "lmp_prc", "price")
                if column in frame.collect_schema()
            ),
            None,
        )
        if value is None:
            return False
        step = timedelta(minutes=report.interval_minutes)
        rows = (
            frame.filter(
                pl.col(value).is_finite()
                & (
                    (
                        utc_expression(frame, "interval_end_time_gmt")
                        - pl.col("timestamp")
                    )
                    == step
                )
            )
            .select("timestamp", "lmp_type")
            .unique()
            .collect()
        )
        expected = {
            lower + index * step for index in range(int((upper - lower) / step))
        }
        return all(
            set(
                rows.filter(pl.col("lmp_type") == COMPONENT_ALIASES[component])[
                    "timestamp"
                ]
            )
            == expected
            for component in report.required_components
        )
    except (ValueError, OSError, pl.exceptions.PolarsError):
        return False


def fetch(
    *,
    market: str,
    nodes: Sequence[str],
    start: date,
    end: date,
    lake: Path = DEFAULT_LAKE_ROOT,
    raw: Path | None = None,
    force: bool = False,
    client: CAISOOASISClient | None = None,
) -> FetchSummary:
    """Download one node/day at a time; retain failed CSVs and other nodes."""
    if market not in MARKETS or start > end:
        raise ValueError("Choose DAM/RTM/FMM and start <= inclusive end")
    nodes = tuple(dict.fromkeys(nodes))
    if not nodes or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", node) or node.upper() == "ALL"
        for node in nodes
    ):
        raise ValueError("Supply explicit PNode identifiers (not ALL)")
    lake = lake.resolve()
    raw = (raw or Path(__file__).resolve().parents[1] / "data/raw/caiso").resolve()
    if raw.is_relative_to(lake) or lake.is_relative_to(raw):
        raise ValueError("Raw and lake directories must be separate")
    downloader = client or CAISOOASISClient(raw_root=raw)
    if Path(downloader.raw_root).resolve() != raw:
        raise ValueError("Client and CLI raw directories must match")
    report = MARKETS[market]
    summary = FetchSummary()
    LOG.info(
        "%s [%s, %s] inclusive Pacific days; nodes=%s",
        market,
        start,
        end,
        ",".join(nodes),
    )
    with writer_lock(lake):
        day = start
        while day <= end:
            for node in nodes:
                if not force and complete_day(lake, report, day, node):
                    summary.skipped += 1
                    LOG.info("SKIP complete %s %s %s", market, day, node)
                    continue
                staged: list[Path] = []
                try:
                    LOG.info("FETCH %s %s %s", market, day, node)
                    staged = downloader.download(report, *day_bounds(day), node=node)
                    outputs = normalize_csvs(
                        staged,
                        report.dataset_name,
                        lake_root=lake,
                        report=report,
                        cleanup=True,
                    )
                    summary.downloaded += 1
                    complete = complete_day(lake, report, day, node)
                    summary.incomplete += not complete
                    LOG.info(
                        "SAVED %s %s %s: %d UTC partitions, %s",
                        market,
                        day,
                        node,
                        len(outputs),
                        "complete" if complete else "partial; rerun to refresh",
                    )
                except NoDataError as error:
                    summary.no_data += 1
                    LOG.warning("NO DATA %s %s: %s", day, node, error)
                except (
                    OASISError,
                    OSError,
                    ValueError,
                    pl.exceptions.PolarsError,
                ) as error:
                    summary.failed += 1
                    LOG.error(
                        "FAILED %s %s: %s; raw retained=%s", day, node, error, staged
                    )
                    if isinstance(error, HTTPError) and error.code in (401, 403):
                        LOG.error("Access denied; stopping remaining requests")
                        return summary
            day += timedelta(days=1)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", type=str.upper, choices=MARKETS, required=True)
    parser.add_argument("--nodes", nargs="+", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=datetime.now(PACIFIC).date(),
        help="Inclusive Pacific day (default today)",
    )
    parser.add_argument(
        "--lake",
        type=Path,
        default=DEFAULT_LAKE_ROOT,
        help="CAISO dataset root, not the whole lake",
    )
    parser.add_argument(
        "--raw",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data/raw/caiso",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch completed days; replace matching prices",
    )
    args = parser.parse_args(argv)
    if args.start > args.end or args.end > datetime.now(PACIFIC).date():
        parser.error("Require start <= end <= today's Pacific date")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        summary = fetch(
            market=args.market,
            nodes=args.nodes,
            start=args.start,
            end=args.end,
            lake=args.lake,
            raw=args.raw,
            force=args.force,
        )
    except (ValueError, RuntimeError, OSError) as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted; persisted partitions and failed raw files remain")
        return 130
    LOG.info("Finished: %s", summary)
    return 1 if summary.failed else 2 if summary.no_data or summary.incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
