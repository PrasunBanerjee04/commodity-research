"""Authenticated ECMWF Web API requests with atomic, request-keyed caching."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def request_for(reference, hdates, area, perturbed, lead_days):
    """Request seven daily mean fields, not instantaneous temperature samples."""
    request = dict(
        dataset="s2s", **{"class": "s2"}, origin="ecmf", stream="enfh",
        expver="prod", model="glob", levtype="sfc", param="167",
        date=reference.isoformat(), hdate="/".join(d.isoformat() for d in hdates),
        time="00:00:00", type="pf" if perturbed else "cf",
        grid="1.5/1.5", area="/".join(map(str, area)), format="grib",
        step="/".join(f"{24*d}-{24*(d+1)}" for d in range(lead_days, lead_days + 7)),
    )
    if perturbed:
        request["number"] = "/".join(map(str, range(1, 11)))
    return request


class Client:
    """One server per retrieval; no credentials are copied into manifests."""

    def retrieve(self, request, directory: Path, *, refresh=False):
        from ecmwfapi import ECMWFDataServer

        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        destination = directory / f"{key}.grib"
        if destination.is_file() and destination.stat().st_size and not refresh:
            return destination
        fd, name = tempfile.mkstemp(dir=directory, suffix=".part")
        os.close(fd)
        try:
            # The official client handles API polling and transient request retries.
            ECMWFDataServer().retrieve({**request, "target": name})
            if not Path(name).stat().st_size:
                raise RuntimeError("ECMWF returned an empty GRIB file")
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)
        return destination
