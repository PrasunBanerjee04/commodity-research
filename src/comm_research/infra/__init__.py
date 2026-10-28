"""Public API for comm_research.infra.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'http': ('.http', None),
    'lake': ('.lake', None),
    'tools': ('.tools', None),
    'paths': ('.paths', None),
    'PROJECT_ROOT': ('.paths', 'PROJECT_ROOT'),
    'project_root': ('.paths', 'project_root'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
