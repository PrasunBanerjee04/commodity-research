"""Public API for comm_research.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from pathlib import Path as _Path

_checkout = _Path(__file__).resolve().parents[2]
if (_checkout / "infra").is_dir():
    __path__.append(str(_checkout))

from ._compat import install as _install_legacy_imports

_install_legacy_imports()

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'models': ('.models', None),
    'common': ('.models', None),
    'setup': ('.setup', None),
    'infra': ('.infra', None),
    'markets': ('.markets', None),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
