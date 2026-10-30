"""Discover IRI's published S2S issuance coordinates, without downloading fields.

IRI is the catalogue/mirror used by the paper. Only issuance dates listed for
both control and perturbed temperature enter the plan. Coordinate presence is
not proof that every historical member/lead exists; GRIB validation does that.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = "https://iridl.ldeo.columbia.edu/SOURCES/.ECMWF/.S2S/.ECMF/"
MAX_BYTES = 8 * 1024 * 1024


def fetch(url, cache, refresh=False):
    """Cache metadata for 24 hours; preserve TLS verification and old good files."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / hashlib.sha256(url.encode()).hexdigest()
    if path.exists() and not refresh and time.time() - path.stat().st_mtime < 86400:
        return path.read_bytes()
    try:
        with urlopen(url, timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
    except (URLError, OSError) as error:
        raise RuntimeError(
            "Cannot read IRI S2S catalogue. Allow iridl.ldeo.columbia.edu and "
            "check IRI access/terms. No dates were guessed. Alternatively pass "
            "explicit reference_dates from the ECMWF catalogue."
        ) from error
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f"Invalid or oversized catalogue response: {url}")
    return data


def cache_validated(url, data, cache):
    """Publish only metadata which has passed its format/coordinate validation."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / hashlib.sha256(url.encode()).hexdigest()
    if (destination.exists() and time.time() - destination.stat().st_mtime < 86400
            and destination.read_bytes() == data):
        return
    fd, name = tempfile.mkstemp(dir=cache, suffix=".part")
    os.close(fd)
    try:
        Path(name).write_bytes(data)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


class CycleLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cycles = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    match = re.search(r"(?:^|/)\.(CY\d+(?:-\d+)?)(?:/|$)", value)
                    if match:
                        self.cycles.add(match[1])


def cycles_from_html(data):
    parser = CycleLinks()
    parser.feed(data.decode("utf-8"))
    if not parser.cycles:
        raise ValueError("IRI catalogue did not list model cycles; check access/terms or pass catalogue_cycles")
    return sorted(parser.cycles)


def dates_from_netcdf(data):
    from netCDF4 import Dataset, num2date

    try:
        with Dataset("iri-coordinate", memory=data) as dataset:
            coordinate = dataset.variables["S"]
            if coordinate.ndim != 1 or len(coordinate) == 0:
                raise ValueError("Expected a nonempty one-dimensional issuance coordinate S")
            values = coordinate[:]
            if any(not math.isfinite(float(v)) for v in values) or bool(values.mask.any()):
                raise ValueError("Missing/nonfinite issuance coordinates")
            decoded = num2date(values, coordinate.units,
                              calendar=getattr(coordinate, "calendar", "standard"))
            if any(d.hour or d.minute or d.second for d in decoded):
                raise ValueError("Expected 00 UTC issuance dates")
            return {date(d.year, d.month, d.day) for d in decoded}
    except (OSError, KeyError, AttributeError) as error:
        raise ValueError("Invalid IRI issuance NetCDF; check access/terms and catalogue structure") from error


def discover(*, cache, as_of, workers=2, refresh=False, cycles=None):
    """Return reference_date -> model_cycle from common control/perturbed axes.

    The Ingrid S/data.nc transform selects only the issuance coordinate, not
    global weather fields. NetCDF parsing is serial (libnetcdf is not thread safe).
    Network reads use bounded parallelism. Fail closed if any cycle cannot load.
    """
    index_url = ROOT + "index.html"
    if cycles is None:
        data = fetch(index_url, cache, refresh)
        cycles = cycles_from_html(data)
        cache_validated(index_url, data, cache)
    cycles = sorted(set(cycles))
    if not cycles or any(not re.fullmatch(r"CY\d+(?:-\d+)?", c) for c in cycles):
        raise ValueError("catalogue_cycles must contain IRI cycle names such as CY48")
    selections = [(cycle, member) for cycle in cycles for member in ("control", "perturbed")]
    urls = [f"{ROOT}.{cycle}/.reforecast/.{member}/.2m_above_ground/.2t/S/data.nc"
            for cycle, member in selections]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        payloads = list(pool.map(lambda url: fetch(url, cache, refresh), urls))
    dates = {}
    for selection, url, data in zip(selections, urls, payloads):
        dates[selection] = dates_from_netcdf(data)
        cache_validated(url, data, cache)
    references = {}
    for cycle in cycles:
        for reference in dates[cycle, "control"] & dates[cycle, "perturbed"]:
            if reference <= as_of:
                if reference in references and references[reference] != cycle:
                    raise ValueError(f"Ambiguous model cycle for {reference}; restrict catalogue_cycles")
                references[reference] = cycle
    if not references:
        raise ValueError("No common control/perturbed issuance dates on or before as_of")
    return references
