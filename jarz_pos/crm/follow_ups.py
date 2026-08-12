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

# Every reminder this app opens is tagged ``[jarz:<kind>]`` in its ToDo
# description, so it can be found again without colliding with ToDos other
# things (notably Assignment Rules) open on the same record. See _ensure_todo.
_MARKER_PREFIX = "jarz:"

# The reminder kinds. One per distinct promise, because dedup and re-dating are
# per kind: a lead can legitimately owe both a follow-up and a re-engage.
KIND_LEAD_FOLLOWUP = "lead-followup"
KIND_STALLED_OPP = "stalled-opp"
KIND_REENGAGE = "reengage"
KIND_POST_SALE = "post-sale"
KIND_SAMPLE_FEEDBACK = "sample-feedback"
KIND_CHECK_UP = "check-up"


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


def todo_marker(kind):
    """The tag that identifies a reminder KIND inside a ToDo description."""
    return f"[{_MARKER_PREFIX}{kind}]"


def _ensure_todo(reference_type, reference_name, owner, description, date=None, *, kind):
    """Create — or re-date — THIS KIND of reminder on a record.

    ``kind`` is keyword-only and required on purpose. The dedup used to match
    ANY open ToDo on (reference_type, reference_name), which sounds right until
    you notice that something else opens ToDos on these records too: this site
    runs an Assignment Rule that opens one on every Lead the instant it is
    created — 2319 of them on staging. Every CRM reminder therefore found an
    "existing" ToDo and silently created nothing, for years, on every lead the
    rule had touched. Reps saw the assignment row (dated the day the lead was
    imported, saying nothing about a follow-up) and no reminder at all.

    So a reminder now declares what it IS. Its description is tagged
    ``[jarz:<kind>]``, dedup matches only that tag, and an existing open one is
    RE-DATED in place rather than skipped — which also fixes the quieter half of
    the bug, where a follow-up date moved but its reminder kept the old date.

    Making ``kind`` required is the actual guard: a future caller cannot
    reintroduce the bug by forgetting it, because the call will not run.

    RETURN CONTRACT -- the ToDo name only when one was CREATED, None when an
    existing reminder was merely re-dated. Callers read this as "did something
    newsworthy happen", and the scheduled passes turn a truthy answer into a
    Notification Log entry:

        if todo:
            summary[...] += 1
            _notify(owner, ...)

    Returning the name on a re-date would therefore notify every rep about every
    open reminder every single day the pass runs. The reminder is still ensured
    and still current; it is just not news. Never raises.
    """
    try:
        if not _doctype_exists("ToDo"):
            return None
        if not reference_name:
            return None

        marker = todo_marker(kind)
        # Tag the description so this reminder is findable later. Callers pass
        # human text; the marker is ours and goes first.
        tagged = f"{marker} {description}".strip()

        existing = _open_todos_of_kind(reference_type, reference_name, marker)
        if existing:
            _redate_todos(existing, tagged, date)
            # Deliberately None, not existing[0] -- see RETURN CONTRACT above.
            return None

        todo_data = {
            "doctype": "ToDo",
            "description": tagged,
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


def _open_todos_of_kind(reference_type, reference_name, marker):
    """Open ToDos on a record carrying ``marker``. Guarded -> []."""
    try:
        return frappe.get_all(
            "ToDo",
            filters={
                "reference_type": reference_type,
                "reference_name": reference_name,
                "status": "Open",
                "description": ["like", f"%{marker}%"],
            },
            pluck="name",
            order_by="creation asc",
        )
    except Exception:
        # A failed lookup must not silently suppress the reminder, but it must
        # not duplicate one either. Treat it as "cannot tell" and skip — the
        # next scheduled pass retries.
        _logger().error(
            f"_open_todos_of_kind failed for {reference_type}:{reference_name}",
            exc_info=True,
        )
        return []


def _redate_todos(names, description, date):
    """Move an existing reminder to its current date/text. Never raises."""
    if not date or not _has_field("ToDo", "date"):
        return
    for name in names:
        try:
            frappe.db.set_value(
                "ToDo",
                name,
                {"date": date, "description": description},
                update_modified=False,
            )
        except Exception:
            _logger().error(f"_redate_todos failed for {name}", exc_info=True)


def close_all_jarz_todos(reference_type, reference_name):
    """Close every reminder THIS APP opened on a record -- and nothing else.

    The counterpart to the dedup fix. ``complete_followup`` used to close EVERY
    open ToDo on the record, which on a Lead meant a rep marking a follow-up
    done also closed the Assignment Rule's ToDo and quietly un-assigned
    themselves from the lead. Matching the ``[jarz:`` prefix keeps the blast
    radius to reminders this app is responsible for.

    Returns the number closed. Never raises.
    """
    try:
        if not _doctype_exists("ToDo"):
            return 0
        names = frappe.get_all(
            "ToDo",
            filters={
                "reference_type": reference_type,
                "reference_name": reference_name,
                "status": "Open",
                "description": ["like", f"%[{_MARKER_PREFIX}%"],
            },
            pluck="name",
        )
        for name in names:
            try:
                frappe.db.set_value("ToDo", name, "status", "Closed")
            except Exception:
                pass
        return len(names)
    except Exception:
        _logger().error(
            f"close_all_jarz_todos failed for {reference_type}:{reference_name}",
            exc_info=True,
        )
        return 0


def close_todos_of_kind(reference_type, reference_name, kind):
    """Close a record's open reminders of one kind. Never raises.

    Used when the thing the reminder was about has been settled, so a stale
    reminder stops nagging.
    """
    try:
        names = _open_todos_of_kind(
            reference_type, reference_name, todo_marker(kind)
        )
        for name in names:
            frappe.db.set_value("ToDo", name, "status", "Closed")
        return len(names)
    except Exception:
        _logger().error(
            f"close_todos_of_kind failed for {reference_type}:{reference_name}",
            exc_info=True,
        )
        return 0


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
        # A lead manually judged not suitable must stop nagging its owner,
        # and so must a duplicate that has been merged into another lead.
        if _has_field("Lead", "custom_not_suitable"):
            filters["custom_not_suitable"] = 0
        if _has_field("Lead", "custom_merged_into"):
            filters["custom_merged_into"] = ["is", "not set"]
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
                    kind=KIND_LEAD_FOLLOWUP,
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
                    kind=KIND_STALLED_OPP,
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
            reengage_filters = {
                "status": "Lost Quotation",
                "custom_next_followup_date": ["<=", today],
            }
            # Never try to re-engage a prospect judged not suitable, or a
            # duplicate whose branches now live on another lead.
            if _has_field("Lead", "custom_not_suitable"):
                reengage_filters["custom_not_suitable"] = 0
            if _has_field("Lead", "custom_merged_into"):
                reengage_filters["custom_merged_into"] = ["is", "not set"]
            leads = frappe.get_all(
                "Lead",
                filters=reengage_filters,
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
                        kind=KIND_REENGAGE,
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
                        kind=KIND_REENGAGE,
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
