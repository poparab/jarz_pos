"""Task Board setup seeding (after_migrate).

Seeds ``Jarz Task Settings.full_access_users`` with the owner account -- but
ONLY while the list is empty and only if that User exists. Once the list has
any row, the seeder leaves it alone: an operator's choice is never overwritten
by a deploy. (Emptying the list completely does re-seed on the next migrate.)

Wired on ``after_migrate``, and every failure is swallowed: this runs inside
the shared ``bench migrate``, where a raising seeder aborts the migrate for
every app on the site. Must import cleanly with no top-level frappe calls.
"""

from __future__ import annotations

from typing import Dict

import frappe

from jarz_pos.services import task_board as tb

LOGGER_NAME = "task_board_setup"


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def ensure_task_board_settings() -> Dict[str, str]:
    """Idempotently seed the full-access list. Never raises."""
    result = {"status": "skipped"}
    try:
        if not frappe.db.exists("DocType", tb.SETTINGS_DOCTYPE) or not frappe.db.exists(
            "DocType", tb.SETTINGS_USER_DOCTYPE
        ):
            result["status"] = "skipped_no_doctype"
            return result

        existing = frappe.db.sql(
            """
            SELECT COUNT(*) FROM `tabJarz Task Settings User`
            WHERE parent = %s AND parenttype = %s
            """,
            (tb.SETTINGS_DOCTYPE, tb.SETTINGS_DOCTYPE),
        )
        if existing and int(existing[0][0] or 0) > 0:
            result["status"] = "exists"
            return result

        if not frappe.db.exists("User", tb.SEED_FULL_ACCESS_USER):
            result["status"] = "skipped_no_user"
            return result

        settings = frappe.get_single(tb.SETTINGS_DOCTYPE)
        settings.append("full_access_users", {"user": tb.SEED_FULL_ACCESS_USER})
        settings.save(ignore_permissions=True)
        result["status"] = "seeded"
        # ERROR level on purpose: .info() is discarded on the servers, and a
        # seeder summary nobody can read is not worth writing.
        _logger().error(f"Task Board: seeded full access for {tb.SEED_FULL_ACCESS_USER}")
    except Exception:
        result["status"] = "failed"
        try:
            _logger().error("ensure_task_board_settings failed", exc_info=True)
        except Exception:
            pass
    return result
