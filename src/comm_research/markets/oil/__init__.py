"""Public API for comm_research.markets.oil.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'scripts': ('.scripts', None),
    'analytics': ('comm_research.markets.oil.analytics', None),
    'models': ('.models', None),
    'pipelines': ('comm_research.markets.oil.pipelines', None),
    'scrapers': ('comm_research.markets.oil.scrapers', None),
    'transforms': ('comm_research.markets.oil.transforms', None),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
