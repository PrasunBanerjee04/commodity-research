"""CAISO OASIS CSV downloads, without transformation or persistence to the lake.

All clients in this process share a request gate. Separate processes sharing an
egress IP must coordinate externally to respect CAISO's IP-wide rate limit.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import threading
import time
from typing import Callable, TypeVar
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4
from zipfile import BadZipFile, ZipFile


ENDPOINT = "http://oasis.caiso.com/oasisapi/SingleZip"
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data/raw/caiso"


class OASISError(RuntimeError):
    """An OASIS application error or invalid download payload."""


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
                        detail = archive.read(member).decode("utf-8", errors="replace")[:2000]
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
        for lower, upper in query_windows(start, end, report.max_days):
            params: dict[str, str | int] = {
                "queryname": report.queryname,
                "market_run_id": report.market_run_id,
                "version": report.version,
                "resultformat": 6,
                "startdatetime": lower.strftime("%Y%m%dT%H:%M-0000"),
                "enddatetime": upper.strftime("%Y%m%dT%H:%M-0000"),
            }
            if node is not None:
                params["node"] = node
            staged.extend(self._stage(self._fetch(params), report))
        return staged


def main() -> None:
    """Download and partition a three-day DAM LMP sample for SP15."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--start", default="2025-01-01", help="UTC start date (YYYY-MM-DD)")
    args = parser.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    from comm_research.infra.tools.caiso_normalizer import normalize_csvs

    raw_files = CAISOOASISClient().download(
        DAM_LMP, start, start + timedelta(days=3), node="TH_SP15_GEN-APND"
    )
    for path in normalize_csvs(raw_files, DAM_LMP.dataset_name):
        print(path)


if __name__ == "__main__":
    main()
