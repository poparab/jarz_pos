"""CRM lead scoring (Phase 3).

Computes a 0-100 lead score for active Leads from a configurable set of
weighted signals. Designed to run as a scheduled (daily) task.

Hard requirements:
- ERPNext v15 (staging/prod) AND v16 (local) safe: every DocType/field access is
  guarded with existence checks so a missing doctype/field never raises.
- Batch-safe: each lead is scored inside its own try/except so one bad row
  never aborts the run.
- This module must import cleanly with NO top-level frappe calls.
"""

import frappe

LOGGER_NAME = "crm_lead_scoring"

# Configurable default weights. Total raw max = 90; score is clamped to 100.
SCORE_WEIGHTS = {
    "has_mobile": 10,
    "has_email": 10,
    "qualified_status": 25,
    "has_company": 15,
    "recent_activity": 20,
    "source_known": 10,
}

# Statuses considered "active" for scoring.
_ACTIVE_STATUSES = ("Lead", "Open", "Replied")

# Days within which a modification counts as "recent activity".
_RECENT_ACTIVITY_DAYS = 14


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _lead_has_field(fieldname):
    """True if Lead exposes ``fieldname`` (standard or custom)."""
    try:
        return bool(frappe.get_meta("Lead").get_field(fieldname))
    except Exception:
        return False


def _score_lead(lead, weights, fields_present):
    """Return a clamped 0-100 score for a single lead row (dict)."""
    score = 0

    if fields_present.get("mobile_no") and (lead.get("mobile_no") or lead.get("phone")):
        score += weights.get("has_mobile", 0)

    if fields_present.get("email_id") and lead.get("email_id"):
        score += weights.get("has_email", 0)

    # "Qualified-ish" statuses signal sales-readiness.
    status = (lead.get("status") or "") if fields_present.get("status") else ""
    if status in ("Replied", "Opportunity", "Quotation", "Converted"):
        score += weights.get("qualified_status", 0)

    if fields_present.get("company_name") and lead.get("company_name"):
        score += weights.get("has_company", 0)

    # Recent activity based on last modification.
    if lead.get("modified"):
        try:
            from frappe.utils import date_diff, now_datetime

            if date_diff(now_datetime(), lead.get("modified")) <= _RECENT_ACTIVITY_DAYS:
                score += weights.get("recent_activity", 0)
        except Exception:
            pass

    if fields_present.get("source") and lead.get("source"):
        score += weights.get("source_known", 0)

    if score > 100:
        score = 100
    if score < 0:
        score = 0
    return score


def compute_lead_scores():
    """Scheduled daily task: recompute ``custom_lead_score`` for active Leads.

    Never raises. Returns a summary dict.
    """
    summary = {"scored": 0, "skipped": 0, "errors": 0}
    logger = _logger()

    try:
        if not frappe.db.exists("DocType", "Lead"):
            logger.info("compute_lead_scores: Lead DocType not present; nothing to do")
            return summary

        # The target field must exist to write anything.
        if not _lead_has_field("custom_lead_score"):
            logger.info(
                "compute_lead_scores: Lead.custom_lead_score missing; nothing to do"
            )
            return summary

        # Determine which optional fields exist for safe selection/scoring.
        candidate_fields = [
            "name",
            "status",
            "mobile_no",
            "phone",
            "email_id",
            "company_name",
            "source",
            "modified",
        ]
        fields_present = {f: _lead_has_field(f) for f in candidate_fields}
        # ``name`` and ``modified`` always exist on any doctype.
        fields_present["name"] = True
        fields_present["modified"] = True

        select_fields = [f for f, present in fields_present.items() if present]

        weights = dict(SCORE_WEIGHTS)

        # Build status filter only if the field exists.
        filters = {}
        if fields_present.get("status"):
            filters["status"] = ["in", list(_ACTIVE_STATUSES)]

        try:
            leads = frappe.get_all(
                "Lead",
                filters=filters or None,
                fields=select_fields,
                limit_page_length=0,
            )
        except Exception:
            logger.error("compute_lead_scores: failed to query Leads", exc_info=True)
            return summary

        for lead in leads:
            try:
                new_score = _score_lead(lead, weights, fields_present)
                frappe.db.set_value(
                    "Lead",
                    lead.get("name"),
                    "custom_lead_score",
                    new_score,
                    update_modified=False,
                )
                summary["scored"] += 1
            except Exception:
                summary["errors"] += 1
                logger.error(
                    f"compute_lead_scores: failed to score Lead '{lead.get('name')}'",
                    exc_info=True,
                )

        try:
            frappe.db.commit()
        except Exception:
            pass

        logger.info(
            "compute_lead_scores summary: "
            f"scored={summary['scored']} errors={summary['errors']}"
        )
    except Exception:
        # Never let lead scoring break the scheduler.
        logger.error("compute_lead_scores failed unexpectedly", exc_info=True)

    return summary
