"""Deprecated shim — the JARZ POS workspace is built by :mod:`setup_workspace`.

This module used to own the workspace: it created it and appended the forecast
shortcut. It no longer defines any content. Everything moved to
``jarz_pos.utils.setup_workspace``, which writes the card sections this module
never wrote and prunes stale entries this module could never remove.

Kept only so an older ``after_migrate`` registration or a site that still has
the old hook cached resolves to the new builder instead of raising. Add new
shortcuts to ``setup_workspace.SHORTCUTS``, not here.
"""

from __future__ import annotations

from jarz_pos.utils.setup_workspace import WORKSPACE_NAME, ensure_jarz_workspace  # noqa: F401

# Historical entry points. Both now build the full workspace.
ensure_jarz_pos_workspace = ensure_jarz_workspace
ensure_forecast_workspace_shortcuts = ensure_jarz_workspace
