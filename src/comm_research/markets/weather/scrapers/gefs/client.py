"""Anonymous, pooled NOAA S3 reads with indexed GRIB byte-range caching."""
from __future__ import annotations

from collections import Counter
from contextlib import closing

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import quote, urlencode
import xml.etree.ElementTree as ET

ROOT = "https://noaa-gefs-retrospective.s3.amazonaws.com/"
logger = logging.getLogger(__name__)


def atomic_bytes(path, contents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def parse_index(body):
    records = []
    for line in body.decode("utf-8").splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            raise ValueError("Malformed NOAA GRIB index")
        offset = int(parts[1])
        if offset < 0 or (records and offset <= records[-1][0]):
            raise ValueError("Unordered/duplicate NOAA GRIB offsets")
        match = re.fullmatch(r"(\d+) (hours?|hrs?|days?) fcst", parts[5].strip())
        step = (int(match[1]) * (24 if match[2].startswith("day") else 1)
                if match and parts[3] == "TMP" and parts[4] == "2 m above ground" else None)
        records.append((offset, step))
    if not records:
        raise ValueError("Empty NOAA GRIB index")
    return records


def byte_range(records, steps):
    selected = [(i, step) for i, (_, step) in enumerate(records) if step in steps]
    counts = Counter(step for _, step in selected)
    missing = sorted(set(steps)-counts.keys())
    duplicate = sorted(step for step, count in counts.items() if count > 1)
    if missing or duplicate:
        recognized = [step for _, step in records if step is not None]
        extent = f"{min(recognized)}..{max(recognized)}" if recognized else "none"
        raise ValueError(
            "NOAA index lacks exactly one 2m temperature field per requested forecast hour; "
            f"missing={missing}, duplicate={duplicate}, recognized_hours={extent}, "
            f"unrecognized_records={sum(step is None for _, step in records)}")
    first, last = selected[0][0], selected[-1][0]
    return records[first][0], records[last+1][0]-1 if last+1 < len(records) else None


class IncompleteDownload(ValueError):
    """An interrupted transfer can be retried without publishing partial data."""


class Client:
    def __init__(self, raw, *, workers=4, retries=3, timeout=60, min_free_gb=2):
        import httpx

        self.raw = Path(raw)
        self.retries = retries
        self.reserve = int(min_free_gb * 2**30)
        self.http = httpx.Client(timeout=timeout, follow_redirects=False,
                                limits=httpx.Limits(max_connections=workers, max_keepalive_connections=workers))

    def close(self):
        self.http.close()

    def response(self, url, headers=None):
        import httpx

        for attempt in range(self.retries+1):
            try:
                response = self.http.send(self.http.build_request("GET", url, headers=headers), stream=True)
                if response.status_code not in (408, 429, 500, 502, 503, 504):
                    return response
                retry_after = response.headers.get("Retry-After", "0")
                response.close()
                delay = max(2**attempt, float(retry_after) if retry_after.isdigit() else 0)
            except httpx.TransportError as error:
                if "403" in str(error):
                    raise RuntimeError("NOAA host denied by network policy; allow noaa-gefs-retrospective.s3.amazonaws.com") from error
                if attempt == self.retries:
                    raise RuntimeError("NOAA connection failed after retries") from error
                delay = 2**attempt
            if attempt == self.retries:
                raise RuntimeError("NOAA service unavailable after retries")
            logger.warning("Retrying NOAA request in %.1fs", delay)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def metadata(self, url, parser, *, refresh=False):
        path = self.raw / "catalogue" / hashlib.sha256(url.encode()).hexdigest()
        if path.exists() and not refresh and time.time()-path.stat().st_mtime < 86400:
            return parser(path.read_bytes())
        with closing(self.response(url)) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 4*2**20:
                    raise ValueError("NOAA metadata response exceeds 4 MiB")
        result = parser(bytes(data))
        atomic_bytes(path, data)
        return result

    def members(self, initialization, *, refresh=False):
        prefix = f"GEFSv12/reforecast/{initialization:%Y/%Y%m%d}00/"
        url = ROOT + "?" + urlencode({"list-type": 2, "prefix": prefix, "delimiter": "/"})
        def parse(body):
            root = ET.fromstring(body)
            if root.tag.rsplit("}", 1)[-1] != "ListBucketResult":
                raise ValueError("NOAA did not return an S3 listing")
            if root.findtext("{*}IsTruncated") != "false":
                raise ValueError("Unexpected truncated member listing")
            return {node.text.rstrip("/").rsplit("/", 1)[-1]
                    for node in root.findall("{*}CommonPrefixes/{*}Prefix")}
        return self.metadata(url, parse, refresh=refresh) or set()

    def index(self, key, *, refresh=False):
        return self.metadata(ROOT + quote(key, safe="/:") + ".idx", parse_index, refresh=refresh)

    def download(self, key, steps, *, refresh=False):
        import httpx

        for attempt in range(self.retries+1):
            try:
                return self._download(key, steps, refresh=refresh)
            except (httpx.TransportError, IncompleteDownload):
                if attempt == self.retries:
                    raise
                logger.warning("Retrying interrupted GEFS transfer: %s", key)
                time.sleep(2**attempt)

    def _download(self, key, steps, *, refresh=False):
        records = self.index(key, refresh=refresh)
        if records is None:
            raise FileNotFoundError(f"Missing NOAA index: {key}.idx")
        index_issue = None
        try:
            start, stop = byte_range(records, steps)
        except ValueError as error:
            index_issue = str(error)
            start, stop = 0, None
            logger.warning("GEFS index incomplete: %s.idx: %s; checking the full temperature file", key, error)
        identity = json.dumps([key, steps, records], sort_keys=True)
        if index_issue:
            identity += ":full-file-verification"
        path = self.raw / "grib" / (hashlib.sha256(identity.encode()).hexdigest() + ".grib2")
        manifest = path.with_suffix(".json")
        if path.exists() and manifest.exists() and not refresh:
            try:
                info = json.loads(manifest.read_text())
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(2**20), b""):
                        digest.update(chunk)
                if digest.hexdigest() == info["sha256"]:
                    return path, {**info, "cached": True}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            logger.warning("Replacing corrupt raw cache: %s", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        estimate = (stop-start+1) if stop is not None else 32*2**20
        if shutil.disk_usage(path.parent).free < self.reserve + estimate:
            raise OSError("Insufficient disk space for GEFS download and free-space reserve")
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".partial")
        os.close(fd)
        digest, size = hashlib.sha256(), 0
        try:
            url = ROOT + quote(key, safe="/:")
            headers = {} if index_issue else {"Range": f"bytes={start}-{stop if stop is not None else ''}"}
            with closing(self.response(url, headers)) as response:
                response.raise_for_status()
                if index_issue:
                    if response.status_code != 200 or not response.headers.get("Content-Length", "").isdigit():
                        raise ValueError("NOAA did not return a complete temperature-file response")
                    expected = int(response.headers["Content-Length"])
                    actual_end = expected-1
                else:
                    bounds = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if (response.status_code != 206 or not bounds or int(bounds[1]) != start
                            or int(bounds[2]) != (stop if stop is not None else int(bounds[3])-1)):
                        raise ValueError("NOAA ignored or returned an invalid HTTP byte range")
                    actual_end = int(bounds[2])
                    expected = actual_end-start+1
                if expected <= 0 or shutil.disk_usage(path.parent).free < self.reserve + expected:
                    raise OSError("Insufficient disk space for the GEFS temperature file and reserve")
                with open(temporary, "wb") as stream:
                    for chunk in response.iter_bytes():
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if size > expected:
                            raise ValueError("Oversized NOAA byte-range response")
                if size != expected:
                    raise IncompleteDownload("Truncated NOAA byte-range response")
                info = dict(key=key, url=url, etag=response.headers.get("ETag"),
                            byte_range=[start, actual_end], bytes=size,
                            download_mode="full_file" if index_issue else "byte_range",
                            index_issue=index_issue,
                            sha256=digest.hexdigest(), forecast_hours=list(steps), cached=False)
            with open(temporary, "rb") as stream:
                if stream.read(4) != b"GRIB":
                    raise ValueError("NOAA response is not GRIB")
            os.replace(temporary, path)
            atomic_bytes(manifest, json.dumps(info, sort_keys=True).encode())
            return path, info
        finally:
            Path(temporary).unlink(missing_ok=True)
