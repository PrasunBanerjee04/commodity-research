## pipeline to fetch ecmwf related grid data
from pathlib import Path
from setup.paths import DATA_DIR
from ecmwf.opendata import Client

def form_request(
    date, time, step,
    model, resol, params,
    target, type="hourly"
):
    if type == "hourly":
        request = {
            "date": date,
            "time": time,
            "step": step,
            "stream": "oper",
            "type": "fc",
            "model": model,
            "resol": resol,
            "param": params,
            "levtype": "sfc",
            "target": target
        }

    elif type == "seasonal":
        request = {
            "date": date,
            "time": time,
            "stream": "mmsf",
            "type": "fc",
            "model": model,
            "resol": resol,
            "param": params,
            "levtype": "sfc",
            "fcmonth": [1, 2, 3, 4, 5, 6, 7],
            "target": target
        }

    else:
        raise ValueError("Invalid type. Must be 'hourly' or 'seasonal'.")

    return request
    

def ingest_ecmwf(request):
    request = dict(request)
    client = Client(
        source='ecmwf',
        model=request.get('model')
    )
    target = Path(request.pop("target", "data.grib2")).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    client.retrieve(request=request, target=str(target))
    return target