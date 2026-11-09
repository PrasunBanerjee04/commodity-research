"""General mathematical and statistical research models."""
from comm_research._exports import lazy_exports
_EXPORTS = {'models': ('comm_research.models', None), 'math': ('.math', None), 'curves': ('.curves', None), 'regression': ('.regression', None), 'kernels': ('.kernels', None)}
__all__ = list(_EXPORTS)
__getattr__, __dir__ = lazy_exports(__name__, globals(), _EXPORTS)
