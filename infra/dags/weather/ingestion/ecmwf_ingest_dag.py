from datetime import datetime, timezone
from setup.paths import DATA_DIR
from pathlib import Path

from pipeline.ingestion.ecmwf_ingest import ingest_ecmwf, form_request
from pipeline.storage.s3_client import (
    init_client,
    write_to_lake,
)
from airflow.sdk import dag, get_current_context, task


MODEL = "aifs-ens"
TYPE = "seasonal"
ECMWF_CYCLES = (0, 12)
FCAST_HORIZONS_HOURLY = (24, 48, 72)
PARAMS = ["tp", "2t", "10u", "10v"]
RESOLUTION = "0p25"
START_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dag(schedule="@daily", start_date=START_DATE, catchup=False)
def ecmwf_ingest():
    @task
    def ingest_data():
        paths = {}
        run_date = get_current_context()["data_interval_start"].strftime("%Y%m%d")

        for param in PARAMS:
            for cycle in ECMWF_CYCLES:
                partitions = [
                    f"model={MODEL}",
                    f"param={param}",
                    f"cycle={cycle:02d}",
                    f"date={run_date}",
                ]
                key = Path(*partitions, "forecast.grib2")
                target = DATA_DIR / "ecmwf-raw" / key
                request = form_request(
                    date=run_date,
                    time=cycle,
                    step=FCAST_HORIZONS_HOURLY,
                    model=MODEL,
                    resol=RESOLUTION,
                    params=[param],
                    target=target,
                    type=TYPE,
                )

                grib_path = ingest_ecmwf(request)
                paths[str(grib_path)] = key.as_posix()

        return paths

    @task
    def store_data(paths: dict[str, str]):
        s3 = init_client()

        for filepath, key in paths.items():
            write_to_lake(
                s3,
                filepath,
                "ecmwf-raw",
                key,
            )

    paths = ingest_data()
    store_data(paths)


@dag(dag_id="ecmwf_ingest_xcom_test", schedule=None, catchup=False)
def ecmwf_ingest_xcom_test():
    @task
    def make_sample_paths() -> dict[str, str]:
        return {
            "local/forecast.grib2": "model=aifs-ens/param=2t/cycle=00/date=20261123/forecast.grib2"
        }

    @task
    def test_xcom_paths(paths: dict[str, str]) -> str:
        if not paths or not all(
            isinstance(filepath, str) and isinstance(key, str)
            for filepath, key in paths.items()
        ):
            raise ValueError("XCom path payload must be a non-empty str-to-str mapping")
        return f"XCom path payload passed ({len(paths)} file)"

    test_xcom_paths(make_sample_paths())


def main():
    return ecmwf_ingest(), ecmwf_ingest_xcom_test()


ecmwf_ingest_dag, ecmwf_ingest_xcom_test_dag = main()

