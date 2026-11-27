"""Weather research."""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    "plot": (".vis", "plot"),
    "ZONAL_MAP": (".vis.map", "ZONAL_MAP"),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
