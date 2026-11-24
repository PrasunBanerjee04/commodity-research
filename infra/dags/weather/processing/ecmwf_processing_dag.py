import re
from datetime import datetime, timezone
from pathlib import Path

from airflow.sdk import Param, dag, get_current_context, task


## global variables
PATH = None
REGION = None

@dag(
    dag_id="ecmwf_processing",
    schedule=None,
)
def ecmwf_seasonal_ingest():
    @task
    def extract():
        ## pulls data from the specified global var defined path 
        pass

    @task
    def transform():
        ## filters data to the desired region, structures according to specified schema
        pass

    @task
    def load():
        ## pushes to appropriate location in the duckDB database
        pass
    