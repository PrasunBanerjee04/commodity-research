import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from airflow.providers.standard.sensors.date_time import DateTimeSensor
from airflow.sdk import CronTriggerTimetable, Param, dag, get_current_context, task
from botocore.exceptions import ClientError

from pipeline.ingestion.ecmwf_ingest import retrieve_monthly_forecast
from pipeline.storage.s3_client import S3Client
from setup.paths import DATA_DIR

SYSTEM = "51"
PRODUCT_TYPE = "monthly_mean"
PARAMS = {"2t": "2m_temperature"}
FCMONTHS = (1, 2, 3, 4, 5, 6)
EASTERN = ZoneInfo("America/New_York")
START_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUCKET = "commodities-raw"
LOGGER = logging.getLogger(__name__)


def has_grib_markers(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"GRIB":
                return False
            stream.seek(-4, 2)
            return stream.read(4) == b"7777"
    except FileNotFoundError:
        return False


@dag(
    dag_id="ecmwf_seasonal_ingest",
    schedule=CronTriggerTimetable("0 0 1 * *", timezone=EASTERN.key),
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    params={
        "forecast_date": Param(
            None,
            type=["null", "string"],
            pattern=r"^[0-9]{6}01$",
            title="Forecast date (YYYYMM01)",
            description="Leave blank to use the run's month in Eastern time.",
        ),
    },
    tags=["ecmwf", "seasonal", "ingestion"],
)
def ecmwf_seasonal_ingest():

    @task(multiple_outputs=True)
    def resolve_forecast() -> dict[str, str]:
        dag_run = get_current_context()["dag_run"]
        run_date = (dag_run.conf or {}).get("forecast_date")
        if run_date is None and dag_run.logical_date is not None:
            run_date = dag_run.logical_date.astimezone(EASTERN).strftime("%Y%m01")
        if not isinstance(run_date, str) or not re.fullmatch(r"[0-9]{6}01", run_date):
            raise ValueError("Provide forecast_date in YYYYMM01 format")
        forecast_date = datetime.strptime(run_date, "%Y%m%d")
        release_time = forecast_date.replace(day=6, hour=12, tzinfo=timezone.utc)
        return {"forecast_date": run_date, "release_time": release_time.isoformat()}

    @task
    def ingest_data(run_date: str) -> dict[str, str]:
        forecast_date = datetime.strptime(run_date, "%Y%m%d").date()

        s3 = S3Client()
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
                grib_path = DATA_DIR / "ecmwf-raw" / key
                object_key = key.as_posix()
                LOGGER.info("Checking seasonal forecast %s in Garage", object_key)
                try:
                    stored = s3.read_from_lake(BUCKET, object_key)
                except ClientError as error:
                    if error.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                        raise
                    stored = None
                if stored and stored.startswith(b"GRIB") and stored.endswith(b"7777"):
                    LOGGER.info("Skipping stored seasonal forecast %s", object_key)
                    grib_path.unlink(missing_ok=True)
                    continue

                if has_grib_markers(grib_path):
                    LOGGER.info("Reusing local seasonal forecast %s", object_key)
                else:
                    LOGGER.info("Requesting seasonal forecast %s from Copernicus", object_key)
                    retrieve_monthly_forecast(
                        forecast_date,
                        month,
                        grib_path,
                        system=SYSTEM,
                        variable=variable,
                        product_type=PRODUCT_TYPE,
                    )
                    if not has_grib_markers(grib_path):
                        raise ValueError(f"Incomplete GRIB download for {object_key}")

                s3.write_to_lake(grib_path, BUCKET, object_key)
                LOGGER.info("Stored seasonal forecast %s in Garage", object_key)
                grib_path.unlink()
        return {}

    @task
    def store_data(paths: dict[str, str]):
        if not paths:
            return
        s3 = S3Client()
        for filepath, key in paths.items():
            s3.write_to_lake(filepath, BUCKET, key)
        for filepath in paths:
            Path(filepath).unlink(missing_ok=True)

    forecast = resolve_forecast()
    wait_for_release = DateTimeSensor(
        task_id="wait_for_release",
        target_time=forecast["release_time"],
        mode="reschedule",
        poke_interval=1800,
        timeout=timedelta(days=7),
    )
    paths = ingest_data(forecast["forecast_date"])
    wait_for_release >> paths
    store_data(paths)


ecmwf_seasonal_ingest()
