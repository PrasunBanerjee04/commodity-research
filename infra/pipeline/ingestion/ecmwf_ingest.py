import os
from datetime import date
from pathlib import Path


CDS_DATASET = "seasonal-monthly-single-levels"
CDS_URL = "https://cds.climate.copernicus.eu/api"


def form_request(
    date,
    time=0,
    model="ifs",
    resol="0p25",
    params=None,
    target=None,
    type="hourly",
    step=None,
    *,
    stream="oper",
    forecast_type="fc",
):
    if type != "hourly":
        raise ValueError("Use the Copernicus CDS pipeline for seasonal forecasts")
    if not params or target is None:
        raise ValueError("params and target are required")

    return {
        "date": date,
        "time": time,
        "model": model,
        "resol": resol,
        "stream": stream,
        "type": forecast_type,
        "step": step,
        "param": params,
        "levtype": "sfc",
        "target": str(target),
    }


def ingest_ecmwf(request):
    from ecmwf.opendata import Client

    request = dict(request)
    client = Client(source="ecmwf", model=request.get("model"))
    target = Path(request.pop("target", "data.grib2")).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        client.retrieve(request=request, target=str(partial))
        with partial.open("rb") as stream:
            if stream.read(4) != b"GRIB":
                raise ValueError("ECMWF download does not begin with a GRIB header")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def retrieve_monthly_forecast(
    forecast_date: date,
    leadtime_month: int,
    target: Path,
    *,
    system: str,
    variable: str,
    product_type: str,
) -> Path:
    """Download an ECMWF seasonal field from Copernicus CDS."""
    if forecast_date.day != 1:
        raise ValueError("The seasonal forecast date must be the first day of a month")
    if not 1 <= leadtime_month <= 6:
        raise ValueError("CDS leadtime_month must be between 1 and 6")

    key = os.environ.get("CDSAPI_KEY")
    if not key:
        raise ValueError("Set CDSAPI_KEY in the repository's .env file")

    import cdsapi

    request = {
        "originating_centre": "ecmwf",
        "system": system,
        "variable": [variable],
        "product_type": [product_type],
        "year": [forecast_date.strftime("%Y")],
        "month": [forecast_date.strftime("%m")],
        "leadtime_month": [str(leadtime_month)],
        "data_format": "grib",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        client = cdsapi.Client(url=CDS_URL, key=key, progress=False)
        client.retrieve(CDS_DATASET, request, str(partial))
        with partial.open("rb") as stream:
            if stream.read(4) != b"GRIB":
                raise ValueError("CDS download does not begin with a GRIB header")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target
