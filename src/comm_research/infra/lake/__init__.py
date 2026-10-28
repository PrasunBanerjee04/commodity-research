"""Public API for comm_research.infra.lake.

Edit _EXPORTS to expose additional modules or helpers; imports load on demand.
"""

from comm_research._exports import lazy_exports as _lazy_exports
from .writer_lock import writer_lock

# Bind this stdlib-only helper eagerly: its name matches its submodule, which
# Python otherwise installs on the package when other code imports that module.

_EXPORTS = {
    'cleanup': ('.cleanup', None),
    'parquet': ('.parquet', None),
    'schema': ('.schema', None),
    'writer_lock': ('.writer_lock', 'writer_lock'),
    'scan_parquet': ('.parquet', 'scan_parquet'),
    'write_atomic': ('.parquet', 'write_atomic'),
    'utc_expression': ('.schema', 'utc_expression'),
    '_clear_raw': ('.cleanup', '_clear_raw'),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
