"""Public API for comm_research.common.models.regression.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'elastic_net': ('.elastic_net', None),
    'lasso': ('.lasso', None),
    'ols': ('.ols', None),
    'ridge': ('.ridge', None),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
