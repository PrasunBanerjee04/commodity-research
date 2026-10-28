"""Small PEP 562 helper for explicit, dependency-light package APIs."""

from importlib import import_module


def lazy_exports(package, namespace, exports):
    """Build package attribute hooks from ``name: (module, attribute)`` entries.

    Modules are relative to the package. A null attribute exports the module
    itself. Resolved objects are cached in the package namespace.
    """
    def get_attribute(name):
        if name not in exports:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        module_name, attribute = exports[name]
        module = import_module(module_name, package)
        value = getattr(module, attribute) if attribute else module
        namespace[name] = value
        return value

    def directory():
        return sorted(set(namespace) | set(exports))

    return get_attribute, directory
