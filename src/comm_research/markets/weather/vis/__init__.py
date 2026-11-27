"""Weather visualization and map resources."""

from comm_research._exports import lazy_exports as _lazy_exports

_EXPORTS = {
    "SpatialMap": (".spatial_map", "SpatialMap"),
    "plot": ("._plot", "plot"),
}

__all__ = list(_EXPORTS)
__getattr__, __dir__ = _lazy_exports(__name__, globals(), _EXPORTS)
