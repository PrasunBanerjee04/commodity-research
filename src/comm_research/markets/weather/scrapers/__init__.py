"""Public API for comm_research.markets.weather.scrapers.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'era5_global': ('.era5_global', None),
    'ecmwf': ('.ecmwf', None),
    'era5': ('.era5', None),
    'ERA5Scraper': ('.era5', 'ERA5Scraper'),
    'RunSummary': ('.era5', 'RunSummary'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
