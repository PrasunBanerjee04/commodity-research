#!/usr/bin/env python3
"""Download and normalize CAISO OASIS data for local research.

All clients in this process share a request gate. Separate processes sharing an
egress IP must coordinate externally to respect CAISO's IP-wide rate limit.
"""

from __future__ import annotations

import argparse
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import sys
import threading
import time
from typing import Callable, TypeVar
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo


ENDPOINT = "http://oasis.caiso.com/oasisapi/SingleZip"
PACIFIC_ZONE = ZoneInfo("America/Los_Angeles")
SINGLEZIP_HISTORY_MONTHS = 39
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data/raw/caiso"
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT / "src"))


class OASISError(RuntimeError):
    """An OASIS application error or invalid download payload."""


class NoDataAvailable(OASISError):
    """OASIS reports no data for a valid query window."""


@dataclass(frozen=True)
class Report:
    queryname: str
    market_run_id: str
    version: int
    max_days: int
    dataset_name: str
    requires_node: bool = False


DAM_LMP = Report("PRC_LMP", "DAM", 12, 15, "dam_lmp", True)
RTM_LMP = Report("PRC_LMP", "RTM", 12, 1, "rtm_lmp", True)
DAM_LOAD_FORECAST = Report("SLD_FCST", "DAM", 1, 15, "dam_load_forecast")
DEFAULT_START = datetime(2025, 1, 1, tzinfo=PACIFIC_ZONE).astimezone(timezone.utc)
DEFAULT_NODES = ("TH_SP15_GEN-APND", "TH_NP15_GEN-APND")

T = TypeVar("T")


class RequestGate:
    """Serialize requests and space their starts by at least five seconds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request: float | None = None

    def run(self, request: Callable[[], T]) -> T:
        with self._lock:
            if self._last_request is not None:
                remaining = 5.0 - (time.monotonic() - self._last_request)
                while remaining > 0:
                    time.sleep(remaining)
                    remaining = 5.0 - (time.monotonic() - self._last_request)
            self._last_request = time.monotonic()
            return request()


_REQUEST_GATE = RequestGate()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's automatic redirects would issue a second, unpaced request.
        return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Query boundaries must be timezone-aware datetimes")
    value = value.astimezone(timezone.utc)
    if value.second or value.microsecond:
        raise ValueError("OASIS query boundaries must have minute precision")
    return value


def query_windows(start: datetime, end: datetime, max_days: int):
    """Yield contiguous UTC windows; end is exclusive."""
    start, end = _utc(start), _utc(end)
    if start >= end or not 1 <= max_days <= 15:
        raise ValueError("Require start < end and a window size of 1–15 days")
    while start < end:
        stop = min(start + timedelta(days=max_days), end)
        yield start, stop
        start = stop


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def earliest_singlezip_date(today: date | None = None) -> date:
    """Return the earliest date in the rolling SingleZip history window."""
    if today is None:
        today = datetime.now(timezone.utc).astimezone(PACIFIC_ZONE).date()
    return _subtract_months(today, SINGLEZIP_HISTORY_MONTHS)


def _trade_day_start(day: date) -> datetime:
    """Return Pacific midnight for a CAISO trade date as a UTC instant."""
    return datetime.combine(day, datetime.min.time(), tzinfo=PACIFIC_ZONE).astimezone(
        timezone.utc
    )


def _trade_day_windows(start: datetime, end: datetime, max_days: int):
    """Yield CAISO local-calendar windows as UTC instants, preserving DST."""
    start, end = _utc(start), _utc(end)
    local_start = start.astimezone(PACIFIC_ZONE)
    local_end = end.astimezone(PACIFIC_ZONE)
    if (local_start.time() != datetime.min.time()
            or local_end.time() != datetime.min.time()):
        raise ValueError("CAISO query boundaries must be Pacific trade-day midnights")
    if start >= end or not 1 <= max_days <= 15:
        raise ValueError("Require start < end and a window size of 1–15 days")

    current = local_start.date()
    end_day = local_end.date()
    while current < end_day:
        stop = min(current + timedelta(days=max_days), end_day)
        yield _trade_day_start(current), _trade_day_start(stop)
        current = stop


def _split_trade_window(start: datetime, end: datetime) -> datetime:
    start_day = start.astimezone(PACIFIC_ZONE).date()
    end_day = end.astimezone(PACIFIC_ZONE).date()
    days = (end_day - start_day).days
    if days <= 1:
        raise ValueError("Cannot split a one-day CAISO trade window")
    return _trade_day_start(start_day + timedelta(days=days // 2))


def _existing_partition_dates(dataset_dir: Path, node: str) -> set[date]:
    """Return Pacific trade dates already stored for a node."""
    files = sorted(dataset_dir.rglob("data.parquet"))
    if not files:
        return set()

    import polars as pl

    dates = (
        pl.scan_parquet([str(path) for path in files], hive_partitioning=False)
        .filter(pl.col("node") == node)
        .select(
            pl.col("interval_start_time_gmt")
            .dt.convert_time_zone(str(PACIFIC_ZONE))
            .dt.date()
            .alias("trade_date")
        )
        .unique()
        .collect()
        .get_column("trade_date")
        .to_list()
    )
    return set(dates)


def _missing_date_ranges(
    start: date,
    end: date,
    existing: set[date],
) -> list[tuple[date, date]]:
    """Return contiguous missing dates as half-open date ranges.

    The latest stored date is included for refresh; all other existing dates
    are skipped, while gaps between stored dates remain eligible for backfill.
    """
    if start >= end:
        return []
    latest = max(existing) if existing else None
    missing: list[date] = []
    for offset in range((end - start).days):
        day = start + timedelta(days=offset)
        if day not in existing or day == latest:
            missing.append(day)
    ranges: list[tuple[date, date]] = []
    for day in missing:
        if ranges and ranges[-1][1] == day:
            ranges[-1] = (ranges[-1][0], day + timedelta(days=1))
        else:
            ranges.append((day, day + timedelta(days=1)))
    return ranges


class CAISOOASISClient:
    def __init__(
        self,
        raw_root: Path | str = DEFAULT_RAW_ROOT,
        *,
        timeout: float = 120.0,
        max_retries: int = 4,
        backoff_seconds: float = 5.0,
    ) -> None:
        if timeout <= 0 or max_retries < 0 or backoff_seconds <= 0:
            raise ValueError("Require positive timeout/backoff and nonnegative retries")
        self.raw_root = Path(raw_root)
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._opener = build_opener(_NoRedirect())

    def _fetch(self, params: dict[str, str | int]) -> bytes:
        url = f"{ENDPOINT}?{urlencode(params)}"

        def request() -> bytes:
            with self._opener.open(url, timeout=self.timeout) as response:
                return response.read()

        attempt = 0
        redirects = 0
        while True:
            try:
                return _REQUEST_GATE.run(request)
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                location = error.headers.get("Location") if error.headers else None
                error.close()
                if status in (301, 302, 303, 307, 308) and location:
                    destination = urljoin(url, location)
                    parsed = urlsplit(destination)
                    if (redirects >= 5 or parsed.scheme not in ("http", "https")
                            or parsed.hostname != "oasis.caiso.com"
                            or parsed.username or parsed.password):
                        raise OASISError("Unsafe or excessive OASIS redirects") from error
                    url = destination
                    redirects += 1
                    continue
                if status not in (429, 503) or attempt == self.max_retries:
                    raise
                delay = self.backoff_seconds * 2**attempt
                if retry_after and retry_after.isdecimal():
                    delay = max(delay, float(retry_after))
                time.sleep(delay)
                attempt += 1

    def _stage(self, payload: bytes, report: Report) -> list[Path]:
        # Validate every member before creating any raw files. Never extract ZIP
        # paths, so malicious or unexpected member names cannot escape staging.
        try:
            with ZipFile(BytesIO(payload)) as archive:
                members = [member for member in archive.infolist() if not member.is_dir()]
                for member in members:
                    if "ERR" in member.filename.upper() or member.filename.lower().endswith(".xml"):
                        content = archive.read(member)
                        detail = content.decode("utf-8", errors="replace")[:2000]
                        if member.filename.lower().endswith(".xml"):
                            try:
                                root = ElementTree.fromstring(content)
                            except ElementTree.ParseError:
                                root = None
                            fields = {
                                element.tag.rsplit("}", 1)[-1]: (element.text or "").strip()
                                for element in root.iter()
                            } if root is not None else {}
                            if (
                                fields.get("ERR_CODE") == "1000"
                                and fields.get("ERR_DESC", "")
                                .rstrip(".")
                                .casefold()
                                == "no data returned for the specified selection"
                            ):
                                raise NoDataAvailable(
                                    f"OASIS has no data for {report.dataset_name}: {detail}"
                                )
                        raise OASISError(f"OASIS returned {member.filename}: {detail}")
                csvs = [(member.filename, archive.read(member)) for member in members
                        if member.filename.lower().endswith(".csv")]
                if not csvs or any(not content.strip() for _, content in csvs):
                    raise OASISError("OASIS ZIP contains no usable CSV data")
                if any(content.lstrip().startswith(b"<") for _, content in csvs):
                    raise OASISError("OASIS returned XML disguised as a CSV member")
        except BadZipFile as error:
            raise OASISError("OASIS response is not a valid ZIP archive") from error

        folder = self.raw_root / report.queryname
        folder.mkdir(parents=True, exist_ok=True)
        staged: list[Path] = []
        batch = uuid4().hex
        try:
            for index, (_, content) in enumerate(csvs):
                path = folder / f"{report.market_run_id}_{batch}_{index}.csv"
                staged.append(path)
                with path.open("xb") as output:
                    output.write(content)
        except Exception:
            for path in staged:
                path.unlink(missing_ok=True)
            raise
        return staged

    def _download_window(
        self,
        report: Report,
        start: datetime,
        end: datetime,
        node: str | None,
    ) -> list[Path]:
        params: dict[str, str | int] = {
            "queryname": report.queryname,
            "market_run_id": report.market_run_id,
            "version": report.version,
            "resultformat": 6,
            "startdatetime": start.strftime("%Y%m%dT%H:%M-0000"),
            "enddatetime": end.strftime("%Y%m%dT%H:%M-0000"),
        }
        if node is not None:
            params["node"] = node

        print(
            f"Requesting {report.dataset_name} for {node or 'all nodes'}: "
            f"{start.astimezone(PACIFIC_ZONE).date()} to "
            f"{(end - timedelta(microseconds=1)).astimezone(PACIFIC_ZONE).date()}",
            flush=True,
        )
        try:
            return self._stage(self._fetch(params), report)
        except NoDataAvailable as error:
            duration = end.astimezone(PACIFIC_ZONE).date() - start.astimezone(
                PACIFIC_ZONE
            ).date()
            if duration.days <= 1:
                print(
                    f"No CAISO data for {start.isoformat()}–{end.isoformat()}"
                    f" for {node or report.dataset_name}",
                    file=sys.stderr,
                )
                return []

            midpoint = _split_trade_window(start, end)
            print(
                f"CAISO returned no data for {start.isoformat()}–{end.isoformat()}; "
                "retrying in smaller date windows",
                file=sys.stderr,
            )
            return (
                self._download_window(report, start, midpoint, node)
                + self._download_window(report, midpoint, end, node)
            )

    def download(
        self,
        report: Report,
        start: datetime,
        end: datetime,
        *,
        node: str | None = None,
    ) -> list[Path]:
        """Stage all CSV members for [start, end); retain raw data on failure.

        LMP queries require an explicit node to avoid accidental all-node pulls.
        Each successful chunk remains staged if a later request fails.
        """
        if report not in (DAM_LMP, RTM_LMP, DAM_LOAD_FORECAST):
            raise ValueError("Unsupported OASIS report")
        if report.requires_node and (not node or not node.strip()):
            raise ValueError("An explicit node is required for LMP reports")
        if node is not None and not report.requires_node:
            raise ValueError("Node filtering is only supported for LMP reports")
        staged: list[Path] = []
        for lower, upper in _trade_day_windows(start, end, report.max_days):
            staged.extend(self._download_window(report, lower, upper, node))
        return staged


def run_backfill(
    start: datetime = DEFAULT_START,
    *,
    end: datetime | None = None,
    nodes: tuple[str, ...] = DEFAULT_NODES,
    raw_root: Path | str = DEFAULT_RAW_ROOT,
    lake_root: Path | str | None = None,
) -> list[Path]:
    """Fill missing DAM LMP dates and refresh each node's latest stored date.

    Existing dates are skipped except for the latest stored date, which is
    re-fetched. Missing gaps are still fetched. The end boundary is exclusive.
    """
    from comm_research.infra.tools.caiso_normalizer import (
        DEFAULT_LAKE_ROOT,
        normalize_csvs,
    )

    if not nodes or any(not node.strip() for node in nodes):
        raise ValueError("At least one non-empty node is required")

    lake_root = Path(lake_root) if lake_root is not None else DEFAULT_LAKE_ROOT
    dataset_dir = lake_root / DAM_LMP.dataset_name
    if end is None:
        today = datetime.now(timezone.utc).astimezone(PACIFIC_ZONE).date()
        end = _trade_day_start(today + timedelta(days=1))
    else:
        end = _utc(end)

    client = CAISOOASISClient(raw_root)
    outputs: list[Path] = []
    start_day = _utc(start).astimezone(PACIFIC_ZONE).date()
    end_day = end.astimezone(PACIFIC_ZONE).date()
    earliest_day = earliest_singlezip_date()
    if start_day < earliest_day:
        raise ValueError(
            f"CAISO SingleZip history starts at {earliest_day}; "
            f"backfill requested {start_day}"
        )

    for node in dict.fromkeys(nodes):
        existing_dates = _existing_partition_dates(dataset_dir, node)
        missing_ranges = _missing_date_ranges(start_day, end_day, existing_dates)
        if not missing_ranges:
            print(
                f"{node}: no missing dates to fetch in "
                f"{start_day} through {end_day - timedelta(days=1)}",
                flush=True,
            )
            continue
        print(
            f"{node}: found {len(existing_dates)} stored dates; "
            f"fetching {len(missing_ranges)} missing range(s)",
            flush=True,
        )
        for range_start, range_end in missing_ranges:
            raw_files = client.download(
                DAM_LMP,
                _trade_day_start(range_start),
                _trade_day_start(range_end),
                node=node,
            )
            if raw_files:
                outputs.extend(
                    normalize_csvs(
                        raw_files, DAM_LMP.dataset_name, lake_root=lake_root
                    )
                )
            else:
                print(
                    f"{node}: no new files returned for "
                    f"{range_start} through {range_end - timedelta(days=1)}",
                    flush=True,
                )
    return outputs


def main() -> None:
    """Backfill DAM LMP by default, or download an explicit date range."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        help="inclusive CAISO Pacific trade date (YYYY-MM-DD; default: 2025-01-01)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        help="inclusive CAISO Pacific trade date (YYYY-MM-DD; default: today)",
    )
    parser.add_argument(
        "--node",
        action="append",
        dest="nodes",
        help="node to pull; repeat for multiple nodes (default: SP15 and NP15)",
    )
    args = parser.parse_args()
    nodes = tuple(args.nodes) if args.nodes else DEFAULT_NODES

    if args.start is None and args.end is None:
        for path in run_backfill(nodes=nodes):
            print(path)
        return

    start_day = args.start or DEFAULT_START.date()
    end_day = args.end or datetime.now(timezone.utc).astimezone(PACIFIC_ZONE).date()
    earliest_day = earliest_singlezip_date()
    if start_day < earliest_day:
        parser.error(
            f"CAISO SingleZip supports a rolling {SINGLEZIP_HISTORY_MONTHS}-month "
            f"window; earliest available date is {earliest_day}"
        )
    if start_day > end_day:
        parser.error("--start must be on or before --end")
    start = _trade_day_start(start_day)
    end = _trade_day_start(end_day + timedelta(days=1))
    for path in run_backfill(start, end=end, nodes=nodes):
        print(path)


if __name__ == "__main__":
    main()
