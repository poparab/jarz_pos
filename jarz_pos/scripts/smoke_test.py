"""Jarz POS API smoke test.

Run with:
  bench --site <site> execute jarz_pos.scripts.smoke_test.run

This calls a curated set of safe, read-only API endpoints to verify runtime.
"""
from __future__ import annotations

import importlib
import traceback
from typing import Any

import frappe


def _call(module: str, func: str, kwargs: dict[str, Any] | None = None) -> tuple[bool, Any]:
    try:
        mod = importlib.import_module(module)
        fn = getattr(mod, func)
        return True, fn(**(kwargs or {}))
    except Exception as e:
        return False, {"error": str(e), "trace": traceback.format_exc(limit=3)}


def run() -> dict[str, Any]:
    results: dict[str, Any] = {}

    def rec(mod: str, fn: str, kwargs: dict[str, Any] | None = None):
        key = f"{mod}.{fn}"
        ok, val = _call(mod, fn, kwargs)
        results[key] = {"ok": ok, "result": val if ok else None, "error": None if ok else val}

    # Base connectivity and info
    rec("jarz_pos.api.test_connection", "ping")
    rec("jarz_pos.api.test_connection", "health_check")
    rec("jarz_pos.api.test_connection", "get_backend_info")

    # User/session
    rec("jarz_pos.api.user", "get_current_user_roles")

    # POS basics
    rec("jarz_pos.api.pos", "get_pos_profiles")

    # Manager states (kanban columns)
    rec("jarz_pos.api.manager", "get_manager_states")

    # Couriers (safe reads)
    rec("jarz_pos.api.couriers", "get_active_couriers")
    rec("jarz_pos.api.couriers", "get_courier_balances")

    # Debug bundle data (read-only)
    rec("jarz_pos.api.test_endpoints", "debug_bundle_data")

    # Follow-ups requiring POS Profile: call only if a profile exists
    first_profile: str | None = None
    try:
        pos_profiles = results.get("jarz_pos.api.pos.get_pos_profiles", {}).get("result") or []
        if isinstance(pos_profiles, list) and pos_profiles:
            first_profile = pos_profiles[0]
    except Exception:
        first_profile = None

    if first_profile:
        rec("jarz_pos.api.pos", "get_profile_products", {"profile": first_profile})
        rec("jarz_pos.api.pos", "get_profile_bundles", {"profile": first_profile})
        rec("jarz_pos.api.manager", "get_manager_orders", {"branch": first_profile, "limit": 5})

    # Summaries
    ok = sum(1 for v in results.values() if v.get("ok"))
    fail = sum(1 for v in results.values() if not v.get("ok"))
    return {"success": fail == 0, "ok": ok, "fail": fail, "site": frappe.local.site, "results": results}
