"""Deprecated shim — the JARZ POS workspace is built by :mod:`setup_workspace`.

This module used to append the Production SOPs shortcut to the workspace after
the fact. That shortcut is now declared in ``setup_workspace.SHORTCUTS`` along
with everything else, so there is nothing left to append.

Kept only so an older ``after_migrate`` registration or a site that still has
the old hook cached resolves to the new builder instead of raising.
"""

from __future__ import annotations

from jarz_pos.utils.setup_workspace import WORKSPACE_NAME, ensure_jarz_workspace  # noqa: F401

# Historical entry point. Now builds the full workspace.
ensure_production_workspace_shortcuts = ensure_jarz_workspace
