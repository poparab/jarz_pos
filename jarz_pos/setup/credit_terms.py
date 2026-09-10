"""Customer-side schema for the "order on credit / on account" path.

WHY this is code and not three Custom Fields made by hand on the server
----------------------------------------------------------------------
Credit is decided PER ORDER, but *permission* to take an order on credit is a
property of the shop: ``services/invoice_creation`` refuses a Credit order
outright unless ``Customer.custom_credit_allowed`` is on, and
``api/credit.get_customer_credit_profile`` is what the POS asks before it will
even offer the button. A field that exists on staging and not on production
would therefore not degrade quietly — it would make every credit order on
production impossible while the same build worked in test, which is the exact
shape of the Cash Over/Short incident recorded in ``setup/accounts_setup.py``.

Deriving the schema from a committed module means staging and production get
the same three columns from the same commit, and they come back on the next
migrate if anyone deletes them.

WHY these live here and not in ``fixtures/custom_field.json``
------------------------------------------------------------
Two reasons, and the second is the load-bearing one:

* ``sync_fixtures`` runs at the very END of a migrate, long after the freshly
  deployed code is already serving requests. The invoice-creation gate reads
  these columns on the first credit order placed after a deploy; an
  after-migrate seeder is what makes them exist by then.
* The three fields carry DEFAULTS (``0``/``0``/``0``) and a Check field with no
  column is not merely absent, it reads back as ``None`` and every
  ``if customer.custom_credit_allowed`` silently answers "no". Seeding through
  ``create_custom_fields`` also runs ``frappe.db.updatedb``, so the column is
  queryable inside the SAME migrate rather than one migrate later.

The Sales Invoice side of this feature (``custom_payment_method`` gaining the
``Credit`` option, and ``custom_credit_terms_days``) DOES live in the fixture
file — those are read only after the invoice exists, and the Select option has
to be part of the exported field definition or Desk refuses the value.

CAUTION for whoever next runs ``bench export-fixtures``: ``Customer`` IS listed
in the ``fixtures`` filter in ``hooks.py``, so an export will scoop these three
fields into ``fixtures/custom_field.json`` and they will then be imported twice
per migrate, last-writer-wins. If that happens, delete the exported entries;
this seeder owns them. (Same caution, same reason, as
``setup/employee_link_setup.py``.)

Idempotent and safe on every ``bench migrate``: each unit is wrapped so one
failure logs and the rest continue, and the whole routine is wrapped so a
seeder can never abort the shared migrate for every app on the site.

This module must import cleanly with NO top-level frappe calls.
"""

from __future__ import annotations

from typing import Any, Dict, List

import frappe

LOGGER_NAME = "credit_terms_setup"

#: The system-wide fallback when a customer is allowed credit but nobody typed a
#: number of days. Mirrored by ``invoice_creation.DEFAULT_CREDIT_DAYS``; the two
#: constants are deliberately independent so a bench missing this seeder still
#: freezes a sane figure onto the invoice instead of a zero-day term that reads
#: as "overdue on delivery".
DEFAULT_CREDIT_DAYS = 30

#: ``custom_credit_allowed`` is the gate, the other two are its terms — so all
#: three are anchored in a chain off ``customer_group``, a core Customer field
#: that exists on every site. Anchoring on one of this app's own fixture fields
#: would drop the whole block to the bottom of the form on a site where that
#: fixture has not imported yet.
CUSTOMER_FIELDS: List[Dict[str, Any]] = [
    {
        "fieldname": "custom_credit_allowed",
        "label": "Allow orders on credit",
        "fieldtype": "Check",
        "default": "0",
        "insert_after": "customer_group",
        "module": "jarz pos",
        "description": (
            "When on, the POS may take an order from this customer on credit "
            "(goods delivered, nothing paid at the door). Decided per order at "
            "checkout; this only says it is permitted at all."
        ),
    },
    {
        "fieldname": "custom_credit_days",
        "label": "Credit Days",
        "fieldtype": "Int",
        "default": "0",
        "non_negative": 1,
        "insert_after": "custom_credit_allowed",
        "depends_on": "eval:doc.custom_credit_allowed",
        "module": "jarz pos",
        "description": (
            f"Days until a credit order from this customer falls due. "
            f"0 means use the system default of {DEFAULT_CREDIT_DAYS} days. "
            "The value in force at order time is frozen onto the invoice as "
            "custom_credit_terms_days and never re-read."
        ),
    },
    {
        "fieldname": "custom_credit_limit_amount",
        "label": "Credit Limit",
        "fieldtype": "Currency",
        "default": "0",
        "non_negative": 1,
        "insert_after": "custom_credit_days",
        "depends_on": "eval:doc.custom_credit_allowed",
        "module": "jarz pos",
        "description": (
            "Most this customer may owe across all open credit orders at once. "
            "0 means no limit. Checked at order creation against the running "
            "balance plus the new order."
        ),
    },
]


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _custom_field_exists(doctype: str, fieldname: str) -> bool:
    """True when the Custom Field row is already there.

    ``create_custom_fields`` returns nothing, so created-vs-existing has to be
    observed before the call rather than read off its result.
    """
    try:
        return bool(frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}))
    except Exception:
        return False


def _ensure_fields(doctype: str, specs: List[Dict[str, Any]], log: Dict[str, List[str]]) -> None:
    """Create (or bring in line) the given Custom Fields on ``doctype``.

    Uses ``frappe.custom.doctype.custom_field.custom_field.create_custom_fields``
    rather than hand-inserting Custom Field docs, because it also clears the
    DocType cache and runs ``frappe.db.updatedb`` so the new column is actually
    queryable in the same migrate rather than one migrate later.
    """
    if not frappe.db.exists("DocType", doctype):
        # Migrate ordering put this seeder ahead of the DocType, or the owning
        # app is not installed. Not an error — the next migrate picks it up.
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
            # One call per field on purpose: a batched call means one bad spec
            # takes the whole group down, and the gate field is the one that
            # decides whether credit orders are possible at all.
            create_custom_fields({doctype: [dict(spec)]})
            if already:
                log["existing"].append(f"{doctype}.{fieldname}")
            else:
                log["created"].append(f"{doctype}.{fieldname}")
        except Exception:
            _logger().error(
                f"Failed to ensure Custom Field {doctype}.{fieldname}", exc_info=True
            )


def _verify_columns(log: Dict[str, List[str]]) -> None:
    """Say out loud whether the three columns are actually queryable.

    ``create_custom_fields`` can succeed as a document write and still leave the
    physical column missing if ``updatedb`` was skipped, and the failure mode
    downstream is silent: ``frappe.db.get_value("Customer", x, "custom_credit_allowed")``
    raises, the caller's ``except`` swallows it, and every customer looks like
    "credit not allowed". Cheaper to name it here, at migrate time.
    """
    for spec in CUSTOMER_FIELDS:
        fieldname = spec["fieldname"]
        try:
            present = bool(frappe.db.has_column("Customer", fieldname))
        except Exception:
            present = False
        if not present:
            log.setdefault("warnings", []).append(
                f"Customer.{fieldname} has no database column; credit orders will be "
                f"refused for every customer until the next successful migrate"
            )


def ensure_credit_terms_fields() -> Dict[str, List[str]]:
    """Idempotently seed the customer credit schema. Safe on every migrate.

    Never raises. This runs inside the shared ``bench migrate`` that the whole
    bench depends on; a raising seeder does not fail its own feature, it aborts
    the migrate for every app on the site.
    """
    log: Dict[str, List[str]] = {
        "created": [],
        "existing": [],
        "skipped": [],
        "warnings": [],
    }
    logger = _logger()

    try:
        _ensure_fields("Customer", CUSTOMER_FIELDS, log)
        _verify_columns(log)

        if log["created"]:
            frappe.db.commit()

        # Logged at ERROR level on purpose. Frappe's default log level off a dev
        # server is ERROR, so .info() and .warning() are discarded entirely on
        # staging and production — a seeder summary that exists only at INFO is
        # a summary nobody will ever read where it matters.
        logger.error(
            "credit_terms_setup: created=%s existing=%s skipped=%s"
            % (log["created"], log["existing"], log["skipped"])
        )
        for warning in log["warnings"]:
            logger.error("credit_terms_setup WARNING: " + warning)
    except Exception:
        # Never let schema seeding break a migrate for the whole bench.
        logger.error("ensure_credit_terms_fields failed unexpectedly", exc_info=True)

    return log
