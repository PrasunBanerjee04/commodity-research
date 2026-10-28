"""Public API for comm_research.markets.power_gas.napg.caiso.dashboard.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'prices': ('.prices', None),
    'research': ('.research', None),
    'CaisoMemoryLake': ('.prices', 'CaisoMemoryLake'),
    'ResearchMemoryLake': ('.research', 'ResearchMemoryLake'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
