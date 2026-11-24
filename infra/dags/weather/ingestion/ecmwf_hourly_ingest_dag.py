import re
from datetime import datetime, timezone
from pathlib import Path

from airflow.sdk import Param, dag, get_current_context, task

from pipeline.ingestion.ecmwf_ingest import form_request, ingest_ecmwf
from pipeline.storage.s3_client import S3Client
from setup.paths import DATA_DIR

MODEL = "aifs-ens"
RESOLUTION = "0p25"
STREAM = "enfo"
FORECAST_TYPE = "pf"
PARAMS = ("tp", "2t", "10u", "10v")
ECMWF_CYCLES = (0, 12)
FCAST_HORIZONS_HOURLY = (24, 48, 72)
START_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUCKET = "commodities-raw"


@dag(
    dag_id="ecmwf_hourly_ingest",
    schedule=None,
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    params={
        "forecast_date": Param(
            type="string",
            pattern=r"^[0-9]{8}$",
            title="Forecast date (YYYYMMDD)",
        ),
    },
    tags=["ecmwf", "hourly", "ingestion"],
)
def ecmwf_hourly_ingest():

    @task
    def ingest_data() -> dict[str, str]:
        context = get_current_context()
        conf = context["dag_run"].conf or {}
        run_date = conf.get("forecast_date")
        if not isinstance(run_date, str) or not re.fullmatch(r"[0-9]{8}", run_date):
            raise ValueError("Provide forecast_date in YYYYMMDD format")
        datetime.strptime(run_date, "%Y%m%d")

        paths = {}
        steps = "-".join(f"{hour:03d}" for hour in FCAST_HORIZONS_HOURLY)
        for param in PARAMS:
            for cycle in ECMWF_CYCLES:
                key = Path(
                    "ecmwf",
                    "source=opendata",
                    f"model={MODEL}",
                    f"resolution={RESOLUTION}",
                    f"stream={STREAM}",
                    f"type={FORECAST_TYPE}",
                    f"param={param}",
                    f"date={run_date}",
                    f"cycle={cycle:02d}",
                    f"steps={steps}",
                    "forecast.grib2",
                )
                request = form_request(
                    date=run_date,
                    time=cycle,
                    model=MODEL,
                    resol=RESOLUTION,
                    params=[param],
                    target=DATA_DIR / "ecmwf-raw" / key,
                    step=FCAST_HORIZONS_HOURLY,
                    stream=STREAM,
                    forecast_type=FORECAST_TYPE,
                )
                grib_path = ingest_ecmwf(request)
                paths[str(grib_path)] = key.as_posix()
        return paths

    @task
    def store_data(paths: dict[str, str]):
        s3 = S3Client()
        for filepath, key in paths.items():
            s3.write_to_lake(filepath, BUCKET, key)
        for filepath in paths:
            Path(filepath).unlink(missing_ok=True)

    store_data(ingest_data())


ecmwf_hourly_ingest()
