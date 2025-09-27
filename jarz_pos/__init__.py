"""Jarz POS top-level package.

Backwards-compatibility shims
-----------------------------
Historically some code and clients imported modules using a nested path like
``jarz_pos.jarz_pos.api``. The canonical import path is now ``jarz_pos.api``
and related top-level packages (``services``, ``utils``, ``events``,
``doctype``).

To remain compatible while we remove the duplicate inner package, we alias
``jarz_pos.jarz_pos`` to this top-level package at import time. This is safe
and ensures existing worker tasks and logs that reference the legacy path keep
working.
"""

from __future__ import annotations

__version__ = "0.0.1"
__all__ = ["__version__"]

# Lightweight aliasing of legacy nested package to the canonical one
import sys as _sys
import importlib as _importlib

_LEGACY_ROOT = __name__ + ".jarz_pos"

class _LegacyAliasModule:
	"""Module proxy that forwards attribute access to subpackages.

	This allows imports like ``import jarz_pos.jarz_pos.api.user`` to resolve to
	``jarz_pos.api.user`` and be cached in ``sys.modules``.
	"""

	def __init__(self, base_pkg: str) -> None:
		self.__name__ = base_pkg
		self.__package__ = base_pkg
		# Mirror canonical package's dunder attrs so importlib and callers behave
		base = _importlib.import_module(__name__)
		self._base_mod = base
		# Guarded assignments for optional attributes
		for _attr in ("__file__", "__path__", "__spec__", "__loader__"):
			try:
				setattr(self, _attr, getattr(base, _attr, None))
			except Exception:
				pass

	def __getattr__(self, name: str):
		# Delegate dunder attributes to the base package to avoid import attempts
		if name.startswith("__"):
			try:
				return getattr(self._base_mod, name)
			except Exception as exc:
				raise AttributeError(name) from exc
		# Map: jarz_pos.jarz_pos.<name> -> jarz_pos.<name>
		target_pkg = f"{__name__}.{name}"
		try:
			mod = _importlib.import_module(target_pkg)
		except Exception as exc:
			raise AttributeError(name) from exc
		# Cache alias: jarz_pos.jarz_pos.<name> -> jarz_pos.<name>
		_sys.modules[f"{_LEGACY_ROOT}.{name}"] = mod
		return mod


# Register the legacy alias root module (jarz_pos.jarz_pos) if not already present
if _LEGACY_ROOT not in _sys.modules:
	_sys.modules[_LEGACY_ROOT] = _LegacyAliasModule(_LEGACY_ROOT)

# Ensure both `jarz_pos.patches` and `jarz_pos.Patches` resolve to whichever exists
_patches_mod = None
try:
	_patches_mod = _importlib.import_module(__name__ + ".patches")
except Exception:
	try:
		_patches_mod = _importlib.import_module(__name__ + ".Patches")
	except Exception:
		_patches_mod = None

if _patches_mod is not None:
	_sys.modules[__name__ + ".patches"] = _patches_mod
	_sys.modules[__name__ + ".Patches"] = _patches_mod
