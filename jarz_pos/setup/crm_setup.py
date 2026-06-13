"""CRM config seeding (Phase 3).

Idempotent, guarded configuration for the ERPNext-native CRM pipeline. Called
from ``after_migrate`` so it must be safe to run on every ``bench migrate``:

  - Every DocType is checked with ``frappe.db.exists("DocType", ...)`` first.
  - Records are only created when absent; nothing existing is overwritten.
  - v15/v16 safe: missing doctypes/roles never raise.
  - Each unit is wrapped; one failure logs and the rest continue.

This module imports cleanly with NO top-level frappe calls.
"""

import frappe

LOGGER_NAME = "crm_setup"

_ASSIGNMENT_RULE_NAME = "Jarz Lead Round Robin"
_WORKFLOW_NAME = "Jarz Opportunity Pipeline"
_WORKFLOW_STATE_FIELD = "status"

# Preferred -> fallback roles for transitions.
_PREFERRED_MANAGER_ROLE = "Sales Manager"
_PREFERRED_USER_ROLE = "Sales User"
_FALLBACK_ROLE = "System Manager"


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _doctype_exists(name):
    try:
        return bool(frappe.db.exists("DocType", name))
    except Exception:
        return False


def _resolve_role(preferred):
    """Return ``preferred`` if it exists as a Role, else the fallback role."""
    try:
        if frappe.db.exists("Role", preferred):
            return preferred
    except Exception:
        pass
    return _FALLBACK_ROLE


def _resolve_assignees():
    """Return a list of enabled Users holding a sales-ish role.

    Returns an empty list when none can be safely resolved (rule stays disabled).
    """
    try:
        roles = [r for r in (_PREFERRED_MANAGER_ROLE, _PREFERRED_USER_ROLE) if r]
        roles = [r for r in roles if frappe.db.exists("Role", r)]
        if not roles:
            return []
        has_role = frappe.get_all(
            "Has Role",
            filters={"role": ["in", roles], "parenttype": "User"},
            fields=["parent"],
            limit_page_length=0,
        )
        users = []
        for row in has_role:
            user = row.get("parent")
            if not user or user in users:
                continue
            try:
                if frappe.db.get_value("User", user, "enabled") and user not in (
                    "Administrator",
                    "Guest",
                ):
                    users.append(user)
            except Exception:
                continue
        return users
    except Exception:
        _logger().error("_resolve_assignees failed", exc_info=True)
        return []


def _ensure_assignment_rule(log):
    """Create a Lead round-robin Assignment Rule if absent."""
    try:
        if not _doctype_exists("Assignment Rule"):
            return
        if frappe.db.exists("Assignment Rule", _ASSIGNMENT_RULE_NAME):
            log["existing"].append(f"Assignment Rule: {_ASSIGNMENT_RULE_NAME}")
            return

        assignees = _resolve_assignees()
        disabled = 0 if assignees else 1

        data = {
            "doctype": "Assignment Rule",
            "name": _ASSIGNMENT_RULE_NAME,
            "document_type": "Lead",
            "priority": 0,
            "disabled": disabled,
            "rule": "Round Robin",
            "assign_condition": "status in ('Lead', 'Open', 'Replied')",
        }
        if disabled:
            data["description"] = (
                "Auto-created disabled: no Sales Manager/Sales User assignees "
                "could be resolved. Add users and enable to activate round-robin "
                "Lead assignment."
            )

        # Child tables: assignment days (all days) + users.
        days = [
            "Sunday",
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
        ]
        data["assignment_days"] = [{"day": d} for d in days]
        if assignees:
            data["users"] = [{"user": u} for u in assignees]

        doc = frappe.get_doc(data)
        doc.insert(ignore_permissions=True)
        state = "disabled" if disabled else "enabled"
        log["created"].append(f"Assignment Rule: {_ASSIGNMENT_RULE_NAME} ({state})")
    except Exception:
        _logger().error("_ensure_assignment_rule failed", exc_info=True)


def _ensure_opportunity_workflow(log):
    """Create the Opportunity pipeline Workflow if none exists for Opportunity."""
    try:
        if not (
            _doctype_exists("Workflow")
            and _doctype_exists("Workflow State")
            and _doctype_exists("Workflow Action")
        ):
            return
        if not _doctype_exists("Opportunity"):
            return

        # If ANY workflow already exists on Opportunity, do nothing.
        try:
            existing = frappe.get_all(
                "Workflow",
                filters={"document_type": "Opportunity"},
                fields=["name"],
                limit_page_length=1,
            )
            if existing:
                log["existing"].append(
                    f"Workflow on Opportunity: {existing[0].get('name')}"
                )
                return
        except Exception:
            # If we cannot determine existing workflows, do not risk creating one.
            _logger().error(
                "Could not check existing Opportunity workflows; skipping",
                exc_info=True,
            )
            return

        # Validate that 'status' is a real field with the states we need.
        try:
            field = frappe.get_meta("Opportunity").get_field(_WORKFLOW_STATE_FIELD)
            if not field or not field.options:
                _logger().info(
                    "Opportunity.status has no options; skipping workflow creation"
                )
                return
            valid_statuses = {
                o.strip()
                for o in (field.options or "").split("\n")
                if o.strip()
            }
        except Exception:
            _logger().error(
                "Could not read Opportunity.status options; skipping workflow",
                exc_info=True,
            )
            return

        manager_role = _resolve_role(_PREFERRED_MANAGER_ROLE)
        user_role = _resolve_role(_PREFERRED_USER_ROLE)

        # Desired pipeline states mapped to Opportunity.status values.
        # Some (Qualification/Quotation/Negotiation) may not be valid statuses on
        # this schema; only include states whose status value actually exists.
        desired_states = [
            ("Open", "Open", "Primary", 0),
            ("Qualification", "Open", "Warning", 0),
            ("Quotation", "Quotation", "Info", 0),
            ("Negotiation", "Quotation", "Warning", 0),
            ("Converted", "Converted", "Success", 1),
            ("Lost", "Lost", "Danger", 1),
        ]
        # Ensure required Workflow State master records exist; keep only states
        # whose mapped Opportunity status is valid.
        states = []
        for state_label, status_value, style, doc_status in desired_states:
            if status_value not in valid_statuses:
                continue
            try:
                if not frappe.db.exists("Workflow State", state_label):
                    frappe.get_doc(
                        {
                            "doctype": "Workflow State",
                            "workflow_state_name": state_label,
                            "style": style,
                        }
                    ).insert(ignore_permissions=True)
            except Exception:
                _logger().error(
                    f"Failed to ensure Workflow State '{state_label}'",
                    exc_info=True,
                )
                continue
            states.append((state_label, status_value, style, doc_status))

        if not states:
            _logger().info("No valid workflow states resolved; skipping workflow")
            return

        # Ensure the Workflow Action master records exist.
        actions = {"Submit", "Approve", "Reject"}
        for action in actions:
            try:
                if not frappe.db.exists("Workflow Action Master", action):
                    frappe.get_doc(
                        {
                            "doctype": "Workflow Action Master",
                            "workflow_action_name": action,
                        }
                    ).insert(ignore_permissions=True)
            except Exception:
                _logger().error(
                    f"Failed to ensure Workflow Action Master '{action}'",
                    exc_info=True,
                )

        state_labels = [s[0] for s in states]

        # Build linear transitions across the available states.
        transitions = []
        for idx in range(len(state_labels) - 1):
            transitions.append(
                {
                    "state": state_labels[idx],
                    "action": "Approve",
                    "next_state": state_labels[idx + 1],
                    "allowed": manager_role,
                }
            )
        # Allow moving any non-terminal state to Lost (re-using user role).
        if "Lost" in state_labels:
            for label in state_labels:
                if label == "Lost":
                    continue
                transitions.append(
                    {
                        "state": label,
                        "action": "Reject",
                        "next_state": "Lost",
                        "allowed": user_role,
                    }
                )

        workflow_data = {
            "doctype": "Workflow",
            "workflow_name": _WORKFLOW_NAME,
            "document_type": "Opportunity",
            "workflow_state_field": _WORKFLOW_STATE_FIELD,
            "is_active": 0,  # disabled by default to avoid disrupting existing flows
            "send_email_alert": 0,
            "states": [
                {
                    "state": label,
                    "doc_status": str(doc_status),
                    "allow_edit": manager_role,
                }
                for (label, _status, _style, doc_status) in states
            ],
            "transitions": transitions,
        }

        frappe.get_doc(workflow_data).insert(ignore_permissions=True)
        log["created"].append(f"Workflow: {_WORKFLOW_NAME} (inactive)")
    except Exception:
        _logger().error("_ensure_opportunity_workflow failed", exc_info=True)


def ensure_crm_setup():
    """Idempotently seed CRM config. Safe to run on every migrate. Never raises."""
    log = {"created": [], "existing": []}
    logger = _logger()

    try:
        _ensure_assignment_rule(log)
        _ensure_opportunity_workflow(log)

        try:
            frappe.db.commit()
        except Exception:
            pass

        if log["created"]:
            logger.info("CRM setup created: " + "; ".join(log["created"]))
        else:
            logger.info("CRM setup: nothing new to create")
        if log["existing"]:
            logger.info("CRM setup already present: " + "; ".join(log["existing"]))
    except Exception:
        logger.error("ensure_crm_setup failed unexpectedly", exc_info=True)

    return log
