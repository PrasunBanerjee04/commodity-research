"""Public API for comm_research.markets.weather.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'scripts': ('.scripts', None),
    'ecmwf': ('.scripts.ecmwf', None),
    "gefs": (".scripts.gefs", None),
    'models': ('.models', None),
    'pipelines': ('comm_research.markets.weather.pipelines', None),
    'scrapers': ('comm_research.markets.weather.ingestion', None),
    'transforms': ('comm_research.markets.weather.transforms', None),
    'era5': ('.scripts.era5', None),
    'WeatherGrid': ('.models', 'WeatherGrid'),
    'ERA5Scraper': ('comm_research.markets.weather.ingestion', 'ERA5Scraper'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
