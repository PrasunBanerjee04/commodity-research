from pathlib import Path

import polars as pl

from comm_research.infra.lake.parquet import scan_parquet
from comm_research.setup.paths import PROJECT_ROOT

DATA_DIR = dataset_dir = PROJECT_ROOT / "data/"


def load_data(data_field):

    path = Path(DATA_DIR, data_field)

    return scan_parquet(path).collect()


def save(filepath, df):
    """Save a Polars or pandas-style table; retain the existing 1/-1 status API."""
    try:
        if isinstance(df, pl.DataFrame):
            df.write_csv(filepath)
        else:
            df.to_csv(filepath)
    except Exception as e:  # noqa: BLE001 - preserve this utility's status-code API.
        print(e)
        return -1
    return 1


def __getattr__(name):
    from comm_research._compat import legacy_attribute
    return legacy_attribute(__name__, name)


if __name__ == "__main__":
    from comm_research._compat import legacy_main
    legacy_main("comm_research.infra.lake.datasets")
