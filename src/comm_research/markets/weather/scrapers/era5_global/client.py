"""Read global temperature chunks from Google's public ARCO ERA5 mirror.

The store's name ends in ``zarr-v3`` but its consolidated metadata uses the
Zarr v2 protocol. Pinning zarr<3 is intentional. Missing data objects must fail
the request, rather than becoming Zarr's implicit fill values.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

SOURCE_URL = (
    "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/"
    "full_37-1h-0p25deg-chunk-1.zarr-v3"
)
VARIABLE = "2m_temperature"
logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """A provider request, availability check, or source validation failed."""


class HTTPStore(MutableMapping):
    """Read-only Zarr mapping using a thread-safe HTTP connection pool."""

    def __init__(self, url: str, *, workers: int, retries: int, timeout: float):
        import httpx

        self.url = url.rstrip("/")
        self.retries = retries
        self.client = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=workers, max_keepalive_connections=workers
            ),
            follow_redirects=True,
        )

    def __getitem__(self, key: str) -> bytes:
        import httpx

        for attempt in range(self.retries + 1):
            try:
                response = self.client.get(f"{self.url}/{key}")
            except httpx.TransportError as error:
                # A proxy policy rejection is not cured by repeated retries.
                if "403" in str(error):
                    raise SourceError(
                        "ERA5 host denied by proxy/network policy: "
                        "allow storage.googleapis.com"
                    ) from error
                if attempt == self.retries:
                    raise SourceError(f"ERA5 transport failed for {key}: {error}") from error
                delay = min(30.0, 2**attempt + random.random())
            else:
                if response.status_code == 200:
                    return response.content
                if response.status_code not in (408, 429, 500, 502, 503, 504):
                    raise SourceError(
                        f"ERA5 object {key}: HTTP {response.status_code}. "
                        "Check source availability and network policy."
                    )
                if attempt == self.retries:
                    raise SourceError(
                        f"ERA5 object {key}: HTTP {response.status_code} after retries"
                    )
                delay = min(30.0, 2**attempt + random.random())
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(
                                delay,
                                (parsedate_to_datetime(retry_after)
                                 - datetime.now(timezone.utc)).total_seconds(),
                            )
                        except (ValueError, TypeError):
                            pass
            logger.warning("Retrying %s in %.1fs", key, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def __setitem__(self, key, value):
        raise TypeError("ERA5 source is read-only")

    def __contains__(self, key):
        # Zarr checks membership before fetching each chunk. Fetching here
        # would download every object twice. Only metadata-declared objects
        # are read; __getitem__ raises on absence instead of returning fill.
        return True

    def __delitem__(self, key):
        raise TypeError("ERA5 source is read-only")

    def __iter__(self) -> Iterator[str]:
        raise NotImplementedError("Read objects through consolidated metadata")

    def __len__(self):
        raise NotImplementedError("Remote object listing is not supported")

    def close(self):
        self.client.close()


def utc_timestamp(value):
    """Convert an ISO timestamp to a timezone-free numpy UTC timestamp."""
    import numpy as np

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return np.datetime64(parsed, "us")


@dataclass(frozen=True)
class SourceInfo:
    url: str
    latitude: object
    longitude: object
    times: object
    first_day: date
    end_day: date  # exclusive; only complete, finalized UTC days
    valid_time_stop: str
    last_updated: str

    @property
    def cells(self) -> int:
        return len(self.latitude) * len(self.longitude)


class ERA5Client:
    """Fetch only 2 m temperature, with bounded concurrent hourly reads."""

    def __init__(self, *, workers=4, retries=4, timeout=120.0, url=SOURCE_URL):
        self.store = HTTPStore(url, workers=workers, retries=retries, timeout=timeout)
        try:
            self._open(url)
        except (KeyError, ValueError) as error:
            self.close()
            raise SourceError(f"Invalid ERA5 source metadata: {error}") from error
        except BaseException:
            self.close()
            raise

    def _open(self, url):
        import numpy as np
        import zarr

        group = zarr.open_consolidated(self.store, mode="r")
        self.temperature = group[VARIABLE]
        if self.temperature.attrs.get("_ARRAY_DIMENSIONS") != [
            "time", "latitude", "longitude"
        ]:
            raise SourceError("Unexpected temperature dimensions; refusing to remap silently")
        if self.temperature.attrs.get("units") != "K":
            raise SourceError("Expected ERA5 temperature in kelvin")
        if self.temperature.dtype != np.dtype("float32"):
            raise SourceError("Expected float32 ERA5 temperature; source schema changed")
        latitude = np.asarray(group["latitude"][:])
        longitude = np.asarray(group["longitude"][:])
        if (
            latitude.shape != (721,)
            or longitude.shape != (1440,)
            or not np.array_equal(np.sort(latitude), np.linspace(-90, 90, 721))
            or not np.array_equal(np.sort(longitude), np.arange(1440) * 0.25)
        ):
            raise SourceError("Expected complete global 0.25 degree coordinates")
        time_array = group["time"]
        calendar = time_array.attrs.get("calendar", "proleptic_gregorian")
        if calendar not in ("standard", "gregorian", "proleptic_gregorian"):
            raise SourceError(f"Unsupported ERA5 calendar: {calendar}")
        units = time_array.attrs.get("units", "")
        match = re.fullmatch(r"(hours|days|minutes|seconds|microseconds|nanoseconds) since (.+)", units)
        if not match:
            raise SourceError(f"Unsupported ERA5 time encoding: {units}")
        unit = dict(hours="h", days="D", minutes="m", seconds="s",
                    microseconds="us", nanoseconds="ns")[match[1]]
        offsets = np.asarray(time_array[:])
        if not np.isfinite(offsets).all() or not np.equal(offsets, np.floor(offsets)).all():
            raise SourceError("ERA5 time coordinates must contain integer offsets")
        times = (utc_timestamp(match[2]) + offsets.astype(f"timedelta64[{unit}]")).astype("datetime64[us]")
        if not len(times) or not np.all(np.diff(times) == np.timedelta64(1, "h")):
            raise SourceError("ERA5 time coordinates are not contiguous hourly UTC values")
        if self.temperature.shape != (len(times), 721, 1440):
            raise SourceError("Temperature shape does not match coordinate arrays")
        attrs = group.attrs.asdict()
        try:
            first = max(utc_timestamp(attrs["valid_time_start"]), times[0])
            last = min(utc_timestamp(attrs["valid_time_stop"]), times[-1])
        except KeyError as error:
            raise SourceError("Source lacks finalized-data availability metadata") from error
        if np.isnat(first) or np.isnat(last) or first > last:
            raise SourceError("Invalid finalized-data availability metadata")
        if first != first.astype("datetime64[h]") or last != last.astype("datetime64[h]"):
            raise SourceError("Source availability timestamps are not aligned to UTC hours")
        # Round the start up and the stop down to retain only complete UTC days.
        first_day = first.astype("datetime64[D]")
        if first > first_day:
            first_day += np.timedelta64(1, "D")
        end_day = (last + np.timedelta64(1, "h")).astype("datetime64[D]")
        self.info = SourceInfo(
            url=url, latitude=latitude, longitude=longitude, times=times,
            first_day=first_day.item(), end_day=end_day.item(),
            valid_time_stop=str(attrs["valid_time_stop"]),
            last_updated=str(attrs.get("last_updated", "unknown")),
        )

    def read_hour(self, timestamp):
        import numpy as np

        index = int(np.searchsorted(self.info.times, timestamp))
        if index >= len(self.info.times) or self.info.times[index] != timestamp:
            raise SourceError(f"Hour {timestamp} is absent from ERA5")
        return np.asarray(self.temperature[index, :, :])

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
