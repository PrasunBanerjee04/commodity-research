"""Public API for comm_research.dashboard.data.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    'loader': ('.loader', None),
    'Dataset': ('.loader', 'Dataset'),
    'Signal': ('.loader', 'Signal'),
    'Metadata': ('.loader', 'Metadata'),
    'SeriesBundle': ('.loader', 'SeriesBundle'),
    'LakeError': ('.loader', 'LakeError'),
    'discover_lake': ('.loader', 'discover_lake'),
    'inspect_dataset': ('.loader', 'inspect_dataset'),
    'load_series': ('.loader', 'load_series'),
    'load_raw_page': ('.loader', 'load_raw_page'),
    'raw_row_count': ('.loader', 'raw_row_count'),
    'dimension_options': ('.loader', 'dimension_options'),
    'clear_caches': ('.loader', 'clear_caches'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
