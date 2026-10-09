"""CAISO OASIS CSV downloads, without transformation or persistence to the lake.

All clients in this process share a request gate. Separate processes sharing an
egress IP must coordinate externally to respect CAISO's IP-wide rate limit.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import TypeVar
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

from .models import Report

ENDPOINT = "http://oasis.caiso.com/oasisapi/SingleZip"
PROJECT_ROOT = Path(__file__).resolve().parents[6]
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data/raw/caiso"


class OASISError(RuntimeError):
    """An OASIS application error or invalid download payload."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class NoDataError(OASISError):
    """A valid query has no published rows yet; it must be retried later."""


def _raise_payload_error(detail: str, filename: str) -> None:
    try:
        root = ElementTree.fromstring(detail)
        fields = {
            element.tag.rsplit("}", 1)[-1].upper(): (element.text or "").strip()
            for element in root.iter()
        }
    except ElementTree.ParseError:
        fields = {}
    code = fields.get("ERR_CODE", "")
    description = fields.get("ERR_DESC", detail)
    error_type = NoDataError if code == "1000" else OASISError
    raise error_type(
        f"OASIS {filename}: code={code or 'unknown'} {description[:1000]}", code=code
    )


logger = logging.getLogger(__name__)


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
                    logger.info("Rate limit: waiting %.1f seconds", remaining)
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


def trading_day_windows(start: datetime, end: datetime, max_days: int = 1):
    """Whole Pacific trading days, including DST days; end is exclusive."""
    tz = ZoneInfo("America/Los_Angeles")
    start, end = _utc(start).astimezone(tz), _utc(end).astimezone(tz)
    if (
        start >= end
        or start.time() != datetime.min.time()
        or end.time() != datetime.min.time()
        or not 1 <= max_days <= 15
    ):
        raise ValueError(
            "GroupZip boundaries must be Pacific midnights with start < end"
        )
    while start < end:
        stop = min(start + timedelta(days=max_days), end)
        yield start.astimezone(timezone.utc), stop.astimezone(timezone.utc)
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

    def _fetch(
        self, params: dict[str, str | int], *, endpoint: str = "SingleZip"
    ) -> bytes:
        url = f"{ENDPOINT.removesuffix('SingleZip')}{endpoint}?{urlencode(params)}"

        def request() -> bytes:
            with self._opener.open(url, timeout=self.timeout) as response:
                content = response.read()
                logger.info("Received %d bytes", len(content))
                return content

        attempt = 0
        redirects = 0
        while True:
            try:
                logger.info("Request attempt %d: %s", attempt + 1, url)
                return _REQUEST_GATE.run(request)
            except HTTPError as error:
                status = error.code
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                location = error.headers.get("Location") if error.headers else None
                error.close()
                if status in (301, 302, 303, 307, 308) and location:
                    destination = urljoin(url, location)
                    parsed = urlsplit(destination)
                    if (
                        redirects >= 5
                        or parsed.scheme not in ("http", "https")
                        or parsed.hostname != "oasis.caiso.com"
                        or parsed.username
                        or parsed.password
                    ):
                        raise OASISError(
                            "Unsafe or excessive OASIS redirects"
                        ) from error
                    url = destination
                    redirects += 1
                    continue
                if status not in (429, 503) or attempt == self.max_retries:
                    raise
                delay = self.backoff_seconds * 2**attempt
                if retry_after and retry_after.isdecimal():
                    delay = max(delay, float(retry_after))
                logger.warning(
                    "HTTP %s: retry %d/%d in %.1fs",
                    status,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
                attempt += 1

    def _stage(self, payload: bytes, report: Report) -> list[Path]:
        # Validate every member before creating any raw files. Never extract ZIP
        # paths, so malicious or unexpected member names cannot escape staging.
        try:
            with ZipFile(BytesIO(payload)) as archive:
                members = [
                    member for member in archive.infolist() if not member.is_dir()
                ]
                for member in members:
                    if (
                        "ERR" in member.filename.upper()
                        or member.filename.lower().endswith(".xml")
                    ):
                        detail = archive.read(member).decode("utf-8", errors="replace")
                        _raise_payload_error(detail, member.filename)
                csvs = [
                    (member.filename, archive.read(member))
                    for member in members
                    if member.filename.lower().endswith(".csv")
                ]
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
        logger.info("Staged %d CSV(s) in %s", len(staged), folder)
        return staged

    def download(
        self,
        report: Report,
        start: datetime,
        end: datetime,
        *,
        node: str | None = None,
        parameters: dict[str, str] | None = None,
    ) -> list[Path]:
        """Stage all CSV members for [start, end); retain raw data on failure.

        LMP queries require an explicit node to avoid accidental all-node pulls.
        Each successful chunk remains staged if a later request fails.
        """
        if report.requires_node and (not node or not node.strip()):
            raise ValueError("An explicit node is required for LMP reports")
        if node is not None and not report.requires_node:
            raise ValueError("Node filtering is only supported for LMP reports")
        report.request_parameters(parameters, node=node)
        staged: list[Path] = []
        pacific = ZoneInfo("America/Los_Angeles")
        aligned = all(
            _utc(value).astimezone(pacific).time() == datetime.min.time()
            for value in (start, end)
        )
        windows = (
            trading_day_windows(
                start, end, 1 if report.endpoint == "GroupZip" else report.max_days
            )
            if report.endpoint == "GroupZip" or aligned
            else query_windows(start, end, report.max_days)
        )
        for lower, upper in windows:
            params: dict[str, str | int] = {
                (
                    "groupid" if report.endpoint == "GroupZip" else "queryname"
                ): report.queryname,
                "version": report.version,
                "resultformat": 6,
                "startdatetime": lower.strftime("%Y%m%dT%H:%M-0000"),
                "enddatetime": (lower if report.same_day_end else upper).strftime(
                    "%Y%m%dT%H:%M-0000"
                ),
            }
            params.update(report.request_parameters(parameters, node=node))
            logger.info(
                "Downloading %s [%s, %s)",
                report.dataset_name,
                lower.isoformat(),
                upper.isoformat(),
            )
            for attempt in range(self.max_retries + 1):
                try:
                    payload = (
                        self._fetch(params)
                        if report.endpoint == "SingleZip"
                        else self._fetch(params, endpoint=report.endpoint)
                    )
                    staged.extend(self._stage(payload, report))
                    break
                except OASISError as error:
                    if error.code != "1015" or attempt == self.max_retries:
                        raise
                    delay = self.backoff_seconds * 2**attempt
                    logger.warning(
                        "GroupZip is processing: retry %d/%d in %.1fs",
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    time.sleep(delay)
        return staged
