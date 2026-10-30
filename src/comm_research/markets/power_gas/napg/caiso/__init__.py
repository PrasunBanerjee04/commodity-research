"""Public API for comm_research.markets.power_gas.napg.caiso.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'analytics': ('.analytics', None),
    'models': ('.models', None),
    'pipelines': ('.pipelines', None),
    'scrapers': ('.scrapers', None),
    'transforms': ('.transforms', None),
    'CAISOOASISClient': ('.scrapers.oasis', 'CAISOOASISClient'),
    'Report': ('.scrapers.oasis', 'Report'),
    'normalize_csvs': ('.transforms', 'normalize_csvs'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
