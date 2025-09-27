"""Compatibility shim package.

This package allows imports of the form `jarz_pos.jarz_pos.api.<module>` to resolve
to the actual top-level API modules `jarz_pos.api.<module>`.

It is safe to keep and works in both local and production setups.
"""

from importlib import import_module as _import_module
import sys as _sys

# Expose the top-level API package as this package to support attribute access
_base = _import_module("jarz_pos.api")

# Ensure submodule discovery works by borrowing the base package path
try:
    __path__ = _base.__path__  # type: ignore[attr-defined]
except Exception:
    pass

# Optional: export common names from the base package (not strictly required)
for _name in getattr(_base, "__all__", []):
    try:
        globals()[_name] = getattr(_base, _name)
    except Exception:
        pass

def __getattr__(name: str):
    """Lazily import submodules (e.g., user, couriers) under this shim.

    This lets `import jarz_pos.jarz_pos.api.user` work by mapping to
    `jarz_pos.api.user` under the hood and caching the alias in sys.modules.
    """
    mod = _import_module(f"jarz_pos.api.{name}")
    _sys.modules[__name__ + "." + name] = mod
    return mod
