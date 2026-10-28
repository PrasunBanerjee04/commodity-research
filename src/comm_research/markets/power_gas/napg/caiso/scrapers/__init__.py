"""Public API for comm_research.markets.power_gas.napg.caiso.scrapers.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'oasis': ('.oasis', None),
    'CAISOOASISClient': ('.oasis', 'CAISOOASISClient'),
    'CategoryScraper': ('.oasis', 'CategoryScraper'),
    'Report': ('.oasis', 'Report'),
    'NoDataError': ('.oasis', 'NoDataError'),
    'OASISError': ('.oasis', 'OASISError'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
