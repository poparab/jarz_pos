"""Lead-side schema for settlement terms agreed during a B2B deal.

WHY a Lead field and not a ``Jarz Settlement Terms`` record
----------------------------------------------------------
``Jarz Settlement Terms`` is named after its Customer (``autoname:
field:customer``) and the daily reminder pass, the collections list and the
approvals badge all key on that. A Lead is not a Customer yet and owes nothing,
so it needs no reminders -- it only needs somewhere to keep what the rep agreed
("pays every Thursday, returns accepted") until the first order converts it.
One JSON column on the Lead is exactly that, and it keeps the live DocType's
naming and every reader of it untouched.

When the Lead becomes a Customer, ``services/settlement_lead_terms`` copies the
JSON into a real ``Jarz Settlement Terms`` record (Customer ``after_insert``,
plus a lazy fallback in ``api/settlement_terms.get_settlement_terms``). The JSON
stays on the Lead as history.

WHY an after_migrate seeder and not ``fixtures/custom_field.json``
-----------------------------------------------------------------
Same reasoning as ``setup/credit_terms.py``: fixtures sync at the very END of a
migrate while the freshly deployed code is already serving, and the save
endpoint writes this column on the first call. ``create_custom_fields`` also
runs ``updatedb`` so the column is queryable in the same migrate.

CAUTION for whoever next runs ``bench export-fixtures``: ``Lead`` IS listed in
the ``fixtures`` filter in ``hooks.py``, so an export will scoop this field into
``fixtures/custom_field.json`` and it will then be imported twice per migrate,
last-writer-wins. If that happens, delete the exported entry; this seeder owns
it.

Idempotent and safe on every ``bench migrate``. Never raises. This module must
import cleanly with NO top-level frappe calls.
"""

from __future__ import annotations

from typing import Any, Dict, List

import frappe

LOGGER_NAME = "settlement_terms_leads_setup"

#: The Lead column holding the agreed terms as JSON. Mirrored by
#: ``services/settlement_lead_terms.LEAD_FIELD``.
LEAD_FIELD = "custom_settlement_terms"

#: Anchored on ``company_name``, a core Lead field present on every site, for
#: the same reason ``credit_terms`` anchors on ``customer_group``.
_ANCHOR = "company_name"


def _json_fieldtype() -> str:
    """``JSON`` where this Frappe knows the fieldtype, else ``Long Text``.

    Frappe v14+ ships ``JSON`` (``frappe.model.data_fieldtypes``); the check
    keeps the seeder correct on an older bench rather than failing the field.
    """
    try:
        from frappe.model import data_fieldtypes

        if "JSON" in data_fieldtypes:
            return "JSON"
    except Exception:
        pass
    return "Long Text"


def lead_field_spec() -> Dict[str, Any]:
    return {
        "fieldname": LEAD_FIELD,
        "label": "Agreed Settlement Terms",
        "fieldtype": _json_fieldtype(),
        "read_only": 1,
        "no_copy": 1,
        "insert_after": _ANCHOR,
        "module": "jarz pos",
        "description": (
            "Payment terms agreed with this lead in the B2B app (cycle, days, "
            "reminders, notes). Copied to the customer's Settlement Terms when the "
            "lead is converted; kept here as history. Edited from the app only."
        ),
    }


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def ensure_lead_settlement_terms_field() -> Dict[str, List[str]]:
    """Idempotently seed ``Lead.custom_settlement_terms``. Never raises."""
    log: Dict[str, List[str]] = {"created": [], "existing": [], "skipped": [], "warnings": []}
    logger = _logger()
    try:
        if not frappe.db.exists("DocType", "Lead"):
            log["skipped"].append("Lead: DocType not present")
        else:
            from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

            try:
                already = bool(
                    frappe.db.exists("Custom Field", {"dt": "Lead", "fieldname": LEAD_FIELD})
                )
            except Exception:
                already = False
            try:
                create_custom_fields({"Lead": [lead_field_spec()]})
                (log["existing"] if already else log["created"]).append(f"Lead.{LEAD_FIELD}")
            except Exception:
                logger.error(f"Failed to ensure Custom Field Lead.{LEAD_FIELD}", exc_info=True)

            try:
                present = bool(frappe.db.has_column("Lead", LEAD_FIELD))
            except Exception:
                present = False
            if not present:
                log["warnings"].append(
                    f"Lead.{LEAD_FIELD} has no database column; terms cannot be saved on "
                    f"a Lead until the next successful migrate"
                )

        if log["created"]:
            frappe.db.commit()

        # ERROR level on purpose: .info() is discarded on staging/production.
        logger.error(
            "settlement_terms_leads_setup: created=%s existing=%s skipped=%s"
            % (log["created"], log["existing"], log["skipped"])
        )
        for warning in log["warnings"]:
            logger.error("settlement_terms_leads_setup WARNING: " + warning)
    except Exception:
        try:
            logger.error("ensure_lead_settlement_terms_field failed unexpectedly", exc_info=True)
        except Exception:
            pass
    return log
