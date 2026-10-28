"""Public API for comm_research.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from ._compat import install as _install_legacy_imports

_install_legacy_imports()

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'common': ('.common', None),
    'dashboard': ('.dashboard', None),
    'infra': ('.infra', None),
    'markets': ('.markets', None),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
