import re
from datetime import datetime, timezone
from pathlib import Path

from airflow.sdk import Param, dag, get_current_context, task

from pipeline.ingestion.ecmwf_ingest import retrieve_monthly_forecast
from pipeline.storage.s3_client import init_client, write_to_lake
from setup.paths import DATA_DIR

SYSTEM = "51"
PRODUCT_TYPE = "monthly_mean"
PARAMS = {"2t": "2m_temperature"}
FCMONTHS = (1,2,3,4,5)
START_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUCKET = "commodities-raw"


@dag(
    dag_id="ecmwf_seasonal_ingest",
    schedule=None,
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    params={
        "forecast_date": Param(
            type="string",
            pattern=r"^[0-9]{6}01$",
            title="Forecast date (YYYYMM01)",
        ),
    },
    tags=["ecmwf", "seasonal", "ingestion"],
)
def ecmwf_seasonal_ingest():

    @task
    def ingest_data() -> dict[str, str]:
        context = get_current_context()
        conf = context["dag_run"].conf or {}
        run_date = conf.get("forecast_date")
        if not isinstance(run_date, str) or not re.fullmatch(r"[0-9]{8}", run_date):
            raise ValueError("Provide forecast_date in YYYYMMDD format")
        forecast_date = datetime.strptime(run_date, "%Y%m%d").date()

        paths = {}
        product = PRODUCT_TYPE.replace("_", "-")
        for param, variable in PARAMS.items():
            for month in FCMONTHS:
                key = Path(
                    "ecmwf",
                    "source=cds",
                    f"system={SYSTEM}",
                    f"product=seasonal-{product}",
                    f"param={param}",
                    f"date={run_date}",
                    f"fcmonth={month:02d}",
                    "forecast.grib",
                )
                grib_path = retrieve_monthly_forecast(
                    forecast_date,
                    month,
                    DATA_DIR / "ecmwf-raw" / key,
                    system=SYSTEM,
                    variable=variable,
                    product_type=PRODUCT_TYPE,
                )
                paths[str(grib_path)] = key.as_posix()
        return paths

    @task
    def store_data(paths: dict[str, str]):
        s3 = init_client()
        for filepath, key in paths.items():
            write_to_lake(s3, filepath, BUCKET, key)
        for filepath in paths:
            Path(filepath).unlink(missing_ok=True)

    store_data(ingest_data())


ecmwf_seasonal_ingest()
