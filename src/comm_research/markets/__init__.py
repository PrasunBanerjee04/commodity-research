"""Public API for comm_research.markets.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'ags': ('.ags', None),
    'oil': ('.oil', None),
    'power_gas': ('.power_gas', None),
    'weather': ('.weather', None),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
