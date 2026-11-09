"""Discover IRI's published S2S issuance coordinates, without downloading fields.

IRI is the catalogue/mirror used by the paper. Only issuance dates listed for
both control and perturbed temperature enter the plan. Coordinate presence is
not proof that every historical member/lead exists; GRIB validation does that.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import re
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

ROOT = "https://iridl.ldeo.columbia.edu/SOURCES/.ECMWF/.S2S/.ECMF/"
MAX_BYTES = 8 * 1024 * 1024


AUTH_HELP = (
    "IRI redirected the catalogue request to its login/S2S terms page. "
    "Sign in at https://iridl.ldeo.columbia.edu/auth/login and accept the S2S terms. "
    "Browser login alone does not authenticate Python. Configure the paper's "
    "~/.pycpt_dlauth JSON file using ecmwf.setup_iri_auth(email) (or cptdl.setup_dlauth), or set IRI_DLAUTH_COOKIE "
    "securely in the Python process environment. Never put cookie values in the notebook. "
    "This IRI login is separate from ECMWF Web API authentication."
)


def auth_cookie():
    """Reuse PyCPT's credential file or a process binding; never log its value."""
    key = os.environ.get("IRI_DLAUTH_COOKIE")
    if key is None:
        path = Path.home() / ".pycpt_dlauth"
        if not path.exists():
            return None
        try:
            key = json.loads(path.read_text())["key"]
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError("Cannot read IRI ~/.pycpt_dlauth; regenerate it with cptdl.setup_dlauth") from None
    if not isinstance(key, str) or not key or re.search(r"[\x00-\x20\x7f;,]", key):
        raise RuntimeError("Invalid IRI authentication cookie; regenerate it securely")
    return key


class IRIRedirects(HTTPRedirectHandler):
    """Keep authentication on the exact HTTPS IRI host, including redirects."""
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.netloc != "iridl.ldeo.columbia.edu":
            raise RuntimeError("IRI redirected outside its HTTPS host; authentication was not forwarded")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def iri_opener(key=None):
    jar = CookieJar()
    if key:
        jar.set_cookie(Cookie(
            version=0, name="__dlauth_id", value=key, port=None, port_specified=False,
            domain="iridl.ldeo.columbia.edu", domain_specified=False, domain_initial_dot=False,
            path="/", path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False))
    return build_opener(IRIRedirects(), HTTPCookieProcessor(jar))


def open_catalogue(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "iridl.ldeo.columbia.edu":
        raise ValueError("Catalogue requests must use the HTTPS IRI host")
    return iri_opener(auth_cookie()).open(url, timeout=30)


def setup_iri_auth(email, *, overwrite=False):
    """PyCPT-compatible key setup using IRI's login/genkey flow.

    Prompts privately for a password, writes only the generated key (mode 0600),
    and returns its path, never the credential. Accept S2S terms in the browser
    first. No attempt is made to accept terms automatically.
    """
    path = Path.home() / ".pycpt_dlauth"
    if path.exists() and not overwrite:
        raise FileExistsError("~/.pycpt_dlauth exists; use overwrite=True to replace an expired key")
    if not isinstance(email, str) or not email.strip():
        raise ValueError("Provide your registered IRI email address")
    password = getpass.getpass("IRI password (hidden): ")
    opener = iri_opener()
    request = Request(
        "https://iridl.ldeo.columbia.edu/auth/login/local/submit/login",
        data=urlencode({"email": email.strip(), "password": password,
                        "redirect": "https://iridl.ldeo.columbia.edu/auth"}).encode())
    del password
    try:
        with opener.open(request, timeout=30):
            pass
        with opener.open("https://iridl.ldeo.columbia.edu/auth/genkey", timeout=30) as response:
            payload = json.loads(response.read(32768))
        key = payload["key"]
        if not isinstance(key, str) or not key or re.search(r"[\x00-\x20\x7f;,]", key):
            raise ValueError("Invalid key")
    except (URLError, OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("IRI key setup failed; check your IRI login and account. No credentials were saved") from None
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".iri-auth-")
    try:
        with os.fdopen(fd, "w") as destination:
            json.dump({"key": key}, destination)
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic no-clobber publication, including concurrent setup attempts.
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def fetch(url, cache, refresh=False):
    """Cache metadata for 24 hours; preserve TLS verification and old good files."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / hashlib.sha256(url.encode()).hexdigest()
    if path.exists() and not refresh and time.time() - path.stat().st_mtime < 86400:
        return path.read_bytes()
    try:
        with open_catalogue(url) as response:
            data = response.read(MAX_BYTES + 1)
            if (urlparse(response.geturl()).path.startswith("/auth/")
                    or re.search(rb"<title[^>]*>\s*dlauth\s*</title>", data[:8192], re.I)):
                raise RuntimeError(AUTH_HELP)
            if url.endswith("data.nc") or "/dods." in url:
                reject_html(data, response.headers.get("Content-Type", ""))
    except (URLError, OSError) as error:
        if getattr(error, "code", None) == 401:
            raise RuntimeError(AUTH_HELP) from None
        raise RuntimeError(
            "Cannot read IRI S2S catalogue. Allow iridl.ldeo.columbia.edu and "
            "check IRI access/terms. No dates were guessed. Alternatively pass "
            "explicit reference_dates from the ECMWF catalogue."
        ) from error
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f"Invalid or oversized catalogue response: {url}")
    return data


def reject_html(data, content_type=""):
    """IRI can label an HTTP-200 HTML error page as application/x-netcdf."""
    prefix = data[:8192].lstrip().lower()
    if ("html" in content_type.lower() or prefix.startswith((b"<html", b"<!doctype html"))):
        match = re.search(rb"<title[^>]*>(.*?)</title>", data[:8192], re.I | re.S)
        title = ""
        if match:
            title = unescape(re.sub(r"<[^>]+>", "", match[1].decode(errors="replace")))
            title = " ".join(title.split())[:120]
        raise RuntimeError(
            "IRI returned HTML instead of catalogue data" + (f" ({title})" if title else "")
            + "; the catalogue endpoint/access must be checked. HTTP status and Content-Type are insufficient.")


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


def dates_from_dap(data, attributes):
    """Decode a DAP2 projection containing only Float32/Float64 S[S=N].

    Numeric DAP2 arrays are XDR: two big-endian length words followed by values.
    Reject ignored projections, truncated arrays, HTML errors and nonfinite
    values. Reading S directly avoids Ingrid's grid-to-NetCDF exporter.
    """
    from netCDF4 import num2date

    reject_html(data)
    reject_html(attributes)
    delimiter = re.search(rb"\r?\nData:\r?\n", data)
    if delimiter is None:
        raise ValueError("IRI response lacks the DAP2 Data delimiter")
    descriptor = data[:delimiter.start()].decode("ascii")
    coordinate = re.fullmatch(
        r"\s*Dataset\s*\{\s*(Float32|Float64)\s+S\s*\[\s*S\s*=\s*(\d+)\s*\]\s*;"
        r"\s*\}\s*[^;]+;\s*", descriptor)
    if coordinate is None:
        raise ValueError("IRI ignored the ?S projection or returned an unexpected coordinate schema")
    dtype, count = coordinate[1], int(coordinate[2])
    payload = data[delimiter.end():]
    width, code = (4, "f") if dtype == "Float32" else (8, "d")
    if count < 1 or len(payload) != 8 + count * width:
        raise ValueError("Incomplete or oversized IRI DAP2 issuance array")
    if struct.unpack(">II", payload[:8]) != (count, count):
        raise ValueError("IRI DAP2 array lengths disagree with its descriptor")
    values = struct.unpack(f">{count}{code}", payload[8:])
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Missing/nonfinite issuance coordinates")
    # S is a top-level coordinate in IRI's DDS/DAS, separate from the 2t grid.
    metadata = attributes.decode("utf-8")
    block = re.search(r"(?:^|[{};])\s*S\s*\{([^{}]*)\}", metadata, re.S)
    if block is None:
        raise ValueError("IRI DAS metadata does not describe coordinate S")
    units = re.search(r'String\s+units\s+("(?:[^"\\]|\\.)*")\s*;', block[1])
    calendar = re.search(r'String\s+calendar\s+("(?:[^"\\]|\\.)*")\s*;', block[1])
    if units is None:
        raise ValueError("IRI S coordinate has no time units")
    decoded = num2date(values, json.loads(units[1]),
                      calendar=json.loads(calendar[1]) if calendar else "standard")
    if any(d.hour or d.minute or d.second or d.microsecond for d in decoded):
        raise ValueError("Expected 00 UTC issuance dates")
    return {date(d.year, d.month, d.day) for d in decoded}


def discover(*, cache, as_of, workers=2, refresh=False, cycles=None):
    """Return reference_date -> model_cycle from common control/perturbed axes.

    DAP2 ?S projects only the standalone issuance array exposed by IRI's DDS;
    dods.das supplies its units. No global temperature array is downloaded.
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
    bases = [f"{ROOT}.{cycle}/.reforecast/.{member}/.2m_above_ground/.2t/"
             for cycle, member in selections]
    urls = [base + suffix for base in bases for suffix in ("dods.dods?S", "dods.das")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        payloads = list(pool.map(lambda url: fetch(url, cache, refresh), urls))
    dates = {}
    for index, selection in enumerate(selections):
        data, attributes = payloads[2*index:2*index+2]
        dates[selection] = dates_from_dap(data, attributes)
        for url, payload in zip(urls[2*index:2*index+2], (data, attributes)):
            cache_validated(url, payload, cache)
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
