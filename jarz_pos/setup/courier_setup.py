"""Courier app setup seeding (COURIER_CONTRACTS §6).

Idempotent and **create-only**: every record is checked with
``frappe.db.exists`` before insert and no existing record is ever overwritten.
That matters more than usual here — the labels are operator-facing Arabic and
English strings that dispatch is expected to tune in production, and a seeder
that rewrote them on every migrate would silently undo that work each deploy.

Wired on ``after_migrate``, and **every failure is swallowed**. This runs inside
the shared ``bench migrate`` that the whole bench depends on; a raising seeder
does not fail its own feature, it aborts the migrate for every app on the site.
Modelled on ``setup/production_setup.py``.

This module must import cleanly with NO top-level frappe calls.
"""

from __future__ import annotations

from typing import Any, Dict, List

import frappe

LOGGER_NAME = "courier_setup"

DOCTYPE = "Delivery Failure Reason"

#: The frozen seed set. Codes are the document names and therefore the values
#: stored on ``Sales Invoice.custom_delivery_failure_reason`` — do not rename one
#: without a patch that repoints the invoices.
FAILURE_REASONS: List[Dict[str, Any]] = [
    {
        "code": "CUSTOMER_UNREACHABLE",
        "label_en": "Customer unreachable",
        "label_ar": "العميل لا يرد",
        "next_action": "Reschedule",
    },
    {
        "code": "CUSTOMER_REFUSED",
        "label_en": "Customer refused the order",
        "label_ar": "العميل رفض الطلب",
        "next_action": "Return",
    },
    {
        "code": "WRONG_ADDRESS",
        "label_en": "Wrong or incomplete address",
        "label_ar": "العنوان خطأ أو ناقص",
        "next_action": "Reschedule",
    },
    {
        "code": "POSTPONED_BY_CUSTOMER",
        "label_en": "Customer asked to postpone",
        "label_ar": "العميل طلب التأجيل",
        "next_action": "Reschedule",
    },
    {
        "code": "AREA_INACCESSIBLE",
        "label_en": "Area inaccessible",
        "label_ar": "المنطقة يصعب الوصول إليها",
        "next_action": "Reschedule",
    },
    {
        "code": "PAYMENT_UNAVAILABLE",
        "label_en": "Customer could not pay",
        "label_ar": "العميل لا يستطيع الدفع",
        "next_action": "Return",
    },
]


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _ensure_failure_reasons(log: Dict[str, List[str]]) -> None:
    """Insert any missing seed reason. Never touches one that already exists."""
    if not frappe.db.exists("DocType", DOCTYPE):
        # Migrate order put the seeder ahead of the doctype (or the app is being
        # uninstalled). Not an error — the next migrate picks it up.
        _logger().warning(f"Skipping seed: DocType '{DOCTYPE}' not found")
        return

    for reason in FAILURE_REASONS:
        code = reason["code"]
        try:
            if frappe.db.exists(DOCTYPE, code):
                log["existing"].append(code)
                continue
            doc = frappe.get_doc(
                {
                    "doctype": DOCTYPE,
                    "code": code,
                    "label_en": reason["label_en"],
                    "label_ar": reason["label_ar"],
                    "next_action": reason["next_action"],
                    "is_active": 1,
                }
            )
            doc.insert(ignore_permissions=True)
            log["created"].append(code)
        except Exception:
            # One bad row must not stop the other five.
            _logger().error(f"Failed to seed {DOCTYPE} '{code}'", exc_info=True)


def ensure_courier_setup() -> Dict[str, List[str]]:
    """Idempotently seed the courier app's master data."""
    log: Dict[str, List[str]] = {"created": [], "existing": []}
    logger = _logger()

    try:
        _ensure_failure_reasons(log)

        # Logged at ERROR level on purpose, not INFO. Frappe's default log level
        # off a dev server is ERROR, so `.info()` and `.warning()` are discarded
        # entirely on staging and production — a seeder summary that only exists
        # at INFO is a summary nobody will ever read where it matters.
        if log["created"]:
            logger.error("Courier setup created: " + ", ".join(log["created"]))
        else:
            logger.error("Courier setup: nothing new to create")
    except Exception:
        # Never let setup seeding break a migrate for the whole bench.
        logger.error("ensure_courier_setup failed unexpectedly", exc_info=True)

    return log
