"""Public API for comm_research.infra.tools.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'preproc': ('.preproc', None),
    'vis': ('.vis', None),
    'plot_timeseries': ('.vis', 'plot_timeseries'),
    'plot_locations': ('.vis', 'plot_locations'),
    'style_figure': ('.vis', 'style_figure'),
    'plot_style': ('.vis', 'plot_style'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
