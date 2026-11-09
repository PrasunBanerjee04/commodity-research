"""Public API for comm_research.infra.http.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'rate_limit': ('.rate_limit', None),
    'RequestGate': ('.rate_limit', 'RequestGate'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
