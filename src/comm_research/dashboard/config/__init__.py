"""Public API for comm_research.dashboard.config.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'taxonomy': ('.taxonomy', None),
    'display_name': ('.taxonomy', 'display_name'),
    'metric_unit': ('.taxonomy', 'metric_unit'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
