"""Public API for comm_research.dashboard.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'config': ('.config', None),
    'data': ('.data', None),
    'ui': ('.ui', None),
    'app': ('.app', None),
    'lake_api': ('.lake_api', None),
    'server': ('.server', None),
    'create_app': ('.server', 'create_app'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
