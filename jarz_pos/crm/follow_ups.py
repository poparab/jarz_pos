"""CRM follow-up reminders (Phase 3).

Scheduled (daily) task that creates ToDo reminders + Notification logs for:
  1. Leads due for follow-up (custom_next_followup_date <= today, not done).
  2. Stalled open Opportunities (no modification in > 7 days).
  3. Lost Leads/Opportunities with a re-engagement follow-up date due today.

Hard requirements:
- ERPNext v15 AND v16 safe: every DocType/field access is guarded; a missing
  doctype/field never raises.
- Every pass is wrapped so one failing pass never aborts the others.
- This module never raises and imports cleanly with NO top-level frappe calls.
"""

import frappe

LOGGER_NAME = "crm_follow_ups"

_STALLED_OPP_DAYS = 7


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


def _today():
    try:
        from frappe.utils import today

        return today()
    except Exception:
        return None


def _add_days(date, days):
    try:
        from frappe.utils import add_days

        return add_days(date, days)
    except Exception:
        return None


def _ensure_todo(reference_type, reference_name, owner, description, date=None):
    """Create a ToDo for the reference if no OPEN ToDo already references it.

    Dedups against existing open ToDos on (reference_type, reference_name).
    Returns the ToDo name if created, else None. Never raises.
    """
    try:
        if not _doctype_exists("ToDo"):
            return None
        if not reference_name:
            return None

        # Dedup: any open ToDo already pointing at this reference?
        existing_filters = {
            "reference_type": reference_type,
            "reference_name": reference_name,
            "status": "Open",
        }
        try:
            if frappe.db.exists("ToDo", existing_filters):
                return None
        except Exception:
            # If the dedup query fails, fall through and try to create — a
            # duplicate ToDo is preferable to a silent miss, but guard the create.
            pass

        todo_data = {
            "doctype": "ToDo",
            "description": description,
            "reference_type": reference_type,
            "reference_name": reference_name,
            "status": "Open",
        }

        # Assign owner/allocated_to only when resolvable to a valid User.
        if owner and frappe.db.exists("User", owner):
            todo_data["allocated_to"] = owner
            todo_data["owner"] = owner

        if date and _has_field("ToDo", "date"):
            todo_data["date"] = date

        if _has_field("ToDo", "priority"):
            todo_data["priority"] = "Medium"

        doc = frappe.get_doc(todo_data)
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        _logger().error(
            f"_ensure_todo failed for {reference_type}:{reference_name}",
            exc_info=True,
        )
        return None


def _notify(owner, subject, document_type=None, document_name=None):
    """Create a Notification Log entry. Never raises."""
    try:
        if not _doctype_exists("Notification Log"):
            return
        if not owner or not frappe.db.exists("User", owner):
            return
        data = {
            "doctype": "Notification Log",
            "subject": subject,
            "for_user": owner,
            "type": "Alert",
        }
        if document_type and document_name:
            data["document_type"] = document_type
            data["document_name"] = document_name
        frappe.get_doc(data).insert(ignore_permissions=True)
    except Exception:
        _logger().error("_notify failed", exc_info=True)


def _pass_lead_followups(summary):
    """Pass 1: Leads due for follow-up."""
    try:
        if not _doctype_exists("Lead"):
            return
        if not (
            _has_field("Lead", "custom_next_followup_date")
            and _has_field("Lead", "custom_followup_done")
        ):
            return

        today = _today()
        if not today:
            return

        filters = {
            "custom_next_followup_date": ["<=", today],
            "custom_followup_done": 0,
        }
        select = ["name", "owner"]
        if _has_field("Lead", "lead_name"):
            select.append("lead_name")

        leads = frappe.get_all(
            "Lead", filters=filters, fields=select, limit_page_length=0
        )
        for lead in leads:
            try:
                label = lead.get("lead_name") or lead.get("name")
                todo = _ensure_todo(
                    "Lead",
                    lead.get("name"),
                    lead.get("owner"),
                    f"Follow up with lead {label}",
                    date=today,
                )
                if todo:
                    summary["lead_followups"] += 1
                    _notify(
                        lead.get("owner"),
                        f"Follow-up due for lead {label}",
                        "Lead",
                        lead.get("name"),
                    )
            except Exception:
                _logger().error(
                    f"lead follow-up failed for {lead.get('name')}", exc_info=True
                )
    except Exception:
        _logger().error("_pass_lead_followups failed", exc_info=True)


def _pass_stalled_opportunities(summary):
    """Pass 2: open Opportunities with no modification in > 7 days."""
    try:
        if not _doctype_exists("Opportunity"):
            return
        if not _has_field("Opportunity", "status"):
            return

        cutoff = _add_days(_today(), -_STALLED_OPP_DAYS)
        if not cutoff:
            return

        filters = {
            "status": "Open",
            "modified": ["<", cutoff],
        }
        select = ["name", "owner"]
        if _has_field("Opportunity", "party_name"):
            select.append("party_name")

        opps = frappe.get_all(
            "Opportunity", filters=filters, fields=select, limit_page_length=0
        )
        for opp in opps:
            try:
                label = opp.get("party_name") or opp.get("name")
                todo = _ensure_todo(
                    "Opportunity",
                    opp.get("name"),
                    opp.get("owner"),
                    f"Stalled opportunity {label} - follow up",
                    date=_today(),
                )
                if todo:
                    summary["stalled_opps"] += 1
                    _notify(
                        opp.get("owner"),
                        f"Opportunity {label} has stalled",
                        "Opportunity",
                        opp.get("name"),
                    )
            except Exception:
                _logger().error(
                    f"stalled opp failed for {opp.get('name')}", exc_info=True
                )
    except Exception:
        _logger().error("_pass_stalled_opportunities failed", exc_info=True)


def _pass_reengagement(summary):
    """Pass 3: Lost Leads/Opportunities with re-engagement date due today."""
    today = _today()
    if not today:
        return

    # Lost Leads with a follow-up date due today.
    try:
        if (
            _doctype_exists("Lead")
            and _has_field("Lead", "custom_next_followup_date")
            and _has_field("Lead", "status")
        ):
            leads = frappe.get_all(
                "Lead",
                filters={
                    "status": "Lost Quotation",
                    "custom_next_followup_date": ["<=", today],
                },
                fields=["name", "owner"],
                limit_page_length=0,
            )
            for lead in leads:
                try:
                    todo = _ensure_todo(
                        "Lead",
                        lead.get("name"),
                        lead.get("owner"),
                        f"Re-engage lost lead {lead.get('name')}",
                        date=today,
                    )
                    if todo:
                        summary["reengagement"] += 1
                except Exception:
                    _logger().error(
                        f"reengage lead failed for {lead.get('name')}", exc_info=True
                    )
    except Exception:
        _logger().error("_pass_reengagement (leads) failed", exc_info=True)

    # Lost Opportunities with a follow-up date due today (if field exists).
    try:
        if (
            _doctype_exists("Opportunity")
            and _has_field("Opportunity", "custom_next_followup_date")
            and _has_field("Opportunity", "status")
        ):
            opps = frappe.get_all(
                "Opportunity",
                filters={
                    "status": "Lost",
                    "custom_next_followup_date": ["<=", today],
                },
                fields=["name", "owner"],
                limit_page_length=0,
            )
            for opp in opps:
                try:
                    todo = _ensure_todo(
                        "Opportunity",
                        opp.get("name"),
                        opp.get("owner"),
                        f"Re-engage lost opportunity {opp.get('name')}",
                        date=today,
                    )
                    if todo:
                        summary["reengagement"] += 1
                except Exception:
                    _logger().error(
                        f"reengage opp failed for {opp.get('name')}", exc_info=True
                    )
    except Exception:
        _logger().error("_pass_reengagement (opps) failed", exc_info=True)


def run_followup_reminders():
    """Scheduled daily task. Never raises. Returns a summary dict."""
    summary = {
        "lead_followups": 0,
        "stalled_opps": 0,
        "reengagement": 0,
    }
    logger = _logger()

    try:
        _pass_lead_followups(summary)
        _pass_stalled_opportunities(summary)
        _pass_reengagement(summary)

        try:
            frappe.db.commit()
        except Exception:
            pass

        logger.info(
            "run_followup_reminders summary: "
            f"lead_followups={summary['lead_followups']} "
            f"stalled_opps={summary['stalled_opps']} "
            f"reengagement={summary['reengagement']}"
        )
    except Exception:
        logger.error("run_followup_reminders failed unexpectedly", exc_info=True)

    return summary
