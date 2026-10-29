"""ERA5 ingestion: regional multi-variable reads and global temperature streams.

The existing ERA5Scraper/RunSummary API describes regional ingestion. Global
temperature ingestion is explicit through global_temperature or run_global.
Optional provider dependencies load only when the corresponding API is used.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    "regional": (".regional", None),
    "global_temperature": (".global_temperature", None),
    "ERA5Scraper": (".regional", "ERA5Scraper"),
    "RunSummary": (".regional", "RunSummary"),
    "GlobalRunSummary": (".global_temperature.pipeline", "RunSummary"),
    "run_global": (".global_temperature.pipeline", "run"),
    "WeatherGrid": ("...models.grid", "WeatherGrid"),
    "DEFAULT_LAKE_ROOT": (".regional", "DEFAULT_LAKE_ROOT"),
    "DEFAULT_RAW_ROOT": (".regional", "DEFAULT_RAW_ROOT"),
    "CDS_URL": (".regional", "CDS_URL"),
    "DATASET": (".regional", "DATASET"),
    "ARCO_ZARR_STORE": (".regional", "ARCO_ZARR_STORE"),
    "VARIABLES": (".regional", "VARIABLES"),
    "VARIABLE_DIRECTORIES": (".regional", "VARIABLE_DIRECTORIES"),
    "SCHEMA": (".regional", "SCHEMA"),
    "KEYS": (".regional", "KEYS"),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
