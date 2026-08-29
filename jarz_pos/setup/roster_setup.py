"""Schema and configuration the shift-distribution screen needs.

Three custom fields and one auto-mapping, all idempotent, all safe on a bench
without HRMS. Runs on ``after_migrate`` so staging and production end up with
the same shape without anybody editing a server by hand.

The auto-mapping is the interesting part. Branch scoping for the roster works
by joining a manager's POS Profiles to HR Shift Locations, and that join has to
exist before a line manager can see anything at all. Asking somebody to fill in
a Link field they have never heard of, on a DocType they rarely open, is how a
feature ships dead -- so this seeds the obvious matches by name and leaves only
genuine ambiguities for a human. It only ever fills an EMPTY field, so a
deliberate mapping is never overwritten by a name that happens to look close.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe

from jarz_pos.services.roster import (
    EMPLOYEE_STANDARD_HOURS_FIELD,
    POS_PROFILE_LOCATION_FIELD,
)
from jarz_pos.events.employee_checkin import EMPLOYEE_EXEMPT_FIELD


def _logger():
    return frappe.logger("jarz_pos.roster_setup")


def _hrms_present() -> bool:
    try:
        return bool(frappe.db.exists("DocType", "Shift Location"))
    except Exception:
        return False


def _custom_field_exists(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}))
    except Exception:
        return False


def _ensure_fields(doctype: str, specs: List[Dict[str, Any]], log: Dict[str, List[str]]) -> None:
    if not frappe.db.exists("DocType", doctype):
        log.setdefault("skipped", []).append(f"{doctype}: DocType not present")
        return
    try:
        from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    except Exception:
        _logger().error("Could not import create_custom_fields", exc_info=True)
        return

    for spec in specs:
        fieldname = spec["fieldname"]
        try:
            already = _custom_field_exists(doctype, fieldname)
            # One call per field: a batched call lets one bad spec take the
            # whole group down, and the check-in gate depends on these existing.
            create_custom_fields({doctype: [dict(spec)]})
            log["existing" if already else "created"].append(f"{doctype}.{fieldname}")
        except Exception:
            _logger().error(f"Failed to ensure Custom Field {doctype}.{fieldname}", exc_info=True)
            log.setdefault("failed", []).append(f"{doctype}.{fieldname}")


def _normalise(text: Any) -> str:
    """Loose key for matching a POS Profile name to a Shift Location name.

    Strips case, punctuation and the words that appear on one side of the join
    but not the other ("branch", "pos", "store"), so "6 October" matches
    "6th of October Branch" without matching "Nasr City".
    """
    lowered = str(text or "").lower()
    for noise in ("branch", "store", "pos", "profile", "location", "the", "of", "-", "_", ".", ","):
        lowered = lowered.replace(noise, " ")
    # Ordinal suffixes: "6th" and "6" must land on the same key.
    for suffix in ("st", "nd", "rd", "th"):
        lowered = lowered.replace(f"{suffix} ", " ")
    return " ".join(lowered.split())


def ensure_roster_setup() -> Dict[str, List[str]]:
    """Idempotent. Safe to run on every migrate, with or without HRMS."""
    log: Dict[str, List[str]] = {"created": [], "existing": [], "mapped": []}

    _ensure_fields(
        "Employee",
        [
            {
                "fieldname": EMPLOYEE_STANDARD_HOURS_FIELD,
                "label": "Roster Standard Hours",
                "fieldtype": "Float",
                "insert_after": "default_shift",
                "precision": "2",
                "description": (
                    "Length of a normal working day for this person, used as the "
                    "overtime baseline. Leave at 0 to derive it from their shift schedule."
                ),
            },
            {
                "fieldname": EMPLOYEE_EXEMPT_FIELD,
                "label": "Exempt From Roster Check-in Gate",
                "fieldtype": "Check",
                "insert_after": EMPLOYEE_STANDARD_HOURS_FIELD,
                "default": "0",
                "description": (
                    "Let this employee check in even on a day the roster leaves empty. "
                    "For fixing a rota mistake without switching the gate off company-wide."
                ),
            },
        ],
        log,
    )

    if _hrms_present():
        _ensure_fields(
            "POS Profile",
            [
                {
                    "fieldname": POS_PROFILE_LOCATION_FIELD,
                    "label": "HR Shift Location",
                    "fieldtype": "Link",
                    "options": "Shift Location",
                    "insert_after": "warehouse",
                    "description": (
                        "The HR branch this POS branch corresponds to. Decides which "
                        "employees a line manager of this branch can roster."
                    ),
                }
            ],
            log,
        )
        _auto_map_profiles(log)
    else:
        log.setdefault("skipped", []).append("POS Profile.custom_shift_location: HRMS not installed")

    return log


def _auto_map_profiles(log: Dict[str, List[str]]) -> None:
    """Fill empty POS Profile -> Shift Location links where the name is obvious.

    Only unambiguous matches are taken: a normalised name that matches exactly
    one Shift Location. Anything matching zero or several is left blank for a
    human, because guessing which branch a manager may roster is precisely the
    decision that must not be made by a string heuristic.
    """
    try:
        locations = frappe.get_all("Shift Location", pluck="name")
        profiles = frappe.get_all(
            "POS Profile",
            filters={POS_PROFILE_LOCATION_FIELD: ("is", "not set")},
            pluck="name",
        )
    except Exception:
        _logger().error("Could not read POS Profiles / Shift Locations", exc_info=True)
        return

    by_key: Dict[str, List[str]] = {}
    for location in locations:
        by_key.setdefault(_normalise(location), []).append(location)

    for profile in profiles:
        matches = by_key.get(_normalise(profile)) or []
        if len(matches) != 1:
            log.setdefault("unmapped", []).append(profile)
            continue
        try:
            frappe.db.set_value("POS Profile", profile, POS_PROFILE_LOCATION_FIELD, matches[0])
            log["mapped"].append(f"{profile} -> {matches[0]}")
        except Exception:
            _logger().error(f"Could not map POS Profile {profile}", exc_info=True)
