"""POS -> CRM bridge (Phase 3).

Links a submitted B2B Sales Invoice back to its originating Opportunity and
schedules a post-sale follow-up. This runs on EVERY Sales Invoice ``on_submit``,
so it is engineered to be completely inert and non-blocking:

  - The entire body is wrapped in a single try/except that logs and returns. It
    must NEVER raise, because raising here would block invoicing/accounting.
  - It fast-exits immediately for non-B2B ("Standard") orders.
  - It uses ``frappe.db.set_value`` / ``doc.db_set`` with update_modified=False
    and never calls ``doc.save()`` (we are inside submit).
  - It is v15/v16 safe: every DocType/field/status access is guarded.

This module must import cleanly with NO top-level frappe calls. It does not
touch any accounting/settlement/invoice-creation logic.
"""

import frappe

LOGGER_NAME = "crm_pos_bridge"

# Opportunity statuses we will NOT touch / will not treat as "open".
_CLOSED_OPP_STATUSES = ("Converted", "Closed", "Lost")

_FOLLOWUP_DAYS = 7


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _doctype_exists(name):
    try:
        return bool(frappe.db.exists("DocType", name))
    except Exception:
        return False


def _has_field(doctype, fieldname):
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


def _status_is_valid(doctype, fieldname, value):
    """True if ``value`` is a valid Select option for doctype.fieldname."""
    try:
        field = frappe.get_meta(doctype).get_field(fieldname)
        if not field or not field.options:
            return False
        options = [o.strip() for o in (field.options or "").split("\n") if o.strip()]
        return value in options
    except Exception:
        return False


def link_b2b_sale_to_opportunity(doc, method=None):
    """On Sales Invoice submit: link B2B sale to its Opportunity. NEVER raises."""
    try:
        # --- Fast exit for non-B2B ("Standard") orders -------------------
        order_purpose = getattr(doc, "custom_order_purpose", "Standard")

        customer = getattr(doc, "customer", None)
        customer_type = None
        if customer:
            try:
                customer_type = frappe.db.get_value(
                    "Customer", customer, "customer_type"
                )
            except Exception:
                customer_type = None

        is_company = customer_type == "Company"
        is_b2b_purpose = order_purpose not in ("", "Standard", None)

        if not is_b2b_purpose and not is_company:
            # Not a B2B-ish order; nothing to do.
            return

        if not customer:
            return

        if not _doctype_exists("Opportunity"):
            return

        # --- Find an open Opportunity for this customer ------------------
        opp = None
        try:
            if _has_field("Opportunity", "party_name") and _has_field(
                "Opportunity", "status"
            ):
                candidates = frappe.get_all(
                    "Opportunity",
                    filters={
                        "party_name": customer,
                        "status": ["not in", list(_CLOSED_OPP_STATUSES)],
                    },
                    fields=["name", "owner", "status"],
                    order_by="modified desc",
                    limit_page_length=1,
                )
                if candidates:
                    opp = candidates[0]
        except Exception:
            _logger().error(
                "link_b2b_sale_to_opportunity: opportunity lookup failed",
                exc_info=True,
            )
            opp = None

        opp_owner = opp.get("owner") if opp else None

        # --- Convert the opportunity + stamp the invoice ----------------
        if opp:
            opp_name = opp.get("name")

            # Set status -> Converted only if it is a valid option.
            try:
                if _status_is_valid("Opportunity", "status", "Converted"):
                    frappe.db.set_value(
                        "Opportunity",
                        opp_name,
                        "status",
                        "Converted",
                        update_modified=False,
                    )
            except Exception:
                _logger().error(
                    "link_b2b_sale_to_opportunity: failed to set Opportunity status",
                    exc_info=True,
                )

            # Stamp the invoice with the source opportunity.
            try:
                if _has_field("Sales Invoice", "custom_source_opportunity"):
                    doc.db_set(
                        "custom_source_opportunity",
                        opp_name,
                        update_modified=False,
                    )
            except Exception:
                _logger().error(
                    "link_b2b_sale_to_opportunity: failed to stamp invoice",
                    exc_info=True,
                )

        # --- Post-sale follow-up ToDo (deduped) -------------------------
        try:
            from jarz_pos.crm.follow_ups import _ensure_todo

            from frappe.utils import add_days, today

            followup_date = add_days(today(), _FOLLOWUP_DAYS)

            # Prefer linking the ToDo to the Opportunity; fall back to Customer.
            if opp:
                ref_type, ref_name = "Opportunity", opp.get("name")
            else:
                ref_type, ref_name = "Customer", customer

            owner = opp_owner
            if not owner:
                owner = getattr(doc, "owner", None) or frappe.session.user

            _ensure_todo(
                ref_type,
                ref_name,
                owner,
                f"Post-sale follow-up for {customer}",
                date=followup_date,
            )
        except Exception:
            _logger().error(
                "link_b2b_sale_to_opportunity: follow-up ToDo creation failed",
                exc_info=True,
            )
    except Exception:
        # Absolute safety net: this hook must never block invoice submission.
        try:
            _logger().error(
                "link_b2b_sale_to_opportunity failed unexpectedly", exc_info=True
            )
        except Exception:
            pass
        return
