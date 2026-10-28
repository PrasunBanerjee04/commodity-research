"""Public API for comm_research.markets.power_gas.eupg.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'cwe': ('.cwe', None),
    'ipex': ('.ipex', None),
    'nordics': ('.nordics', None),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
