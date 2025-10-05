from __future__ import annotations

import frappe


@frappe.whitelist()
def ping() -> dict:
    """Lightweight health check for jarz_pos API.

    Returns basic runtime info to confirm the server picked up latest code.
    Access via: /api/method/jarz_pos.api.health.ping
    """
    try:
        site = getattr(frappe.local, "site", None)
    except Exception:
        site = None
    try:
        now_ts = frappe.utils.now()
    except Exception:
        now_ts = None
    return {
        "ok": True,
        "app": "jarz_pos",
        "site": site,
        "time": now_ts,
        "message": "pong",
    }
