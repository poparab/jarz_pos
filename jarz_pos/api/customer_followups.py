"""Follow-up screen API (post-delivery messages and calls).

Methods (all under ``jarz_pos.api.customer_followups``):

* ``get_followups_today()`` -> ``{items: [...], summary: {due, overdue, calls, messages}}``
* ``log_touch(name, reached=None, outcome=None, notes=None)`` -> the updated item
* ``skip_touch(name, reason=None)`` -> the updated item
* ``get_followup_stats(from_date=None, to_date=None)`` -> outcome numbers
* ``get_sent_report(from_date=None, to_date=None)`` -> WhatsApp messages sent, read, replied, reordered (manager)
* ``run_generator_now()`` -> ``{enabled, invoices, touches, errors}`` (manager)
* ``get_followup_settings()`` -> the settings the screen needs (no secrets)
* ``draft_followup_message(name, refresh=0)`` -> Claude-written WhatsApp text (template fallback)
* ``get_call_brief(name, refresh=0)`` -> Claude-written briefing for a call (code fallback)

Business rules live in ``jarz_pos.services.customer_followups``.
Access: JARZ Manager, line managers, System Manager.
"""

import frappe

from jarz_pos.constants import ROLES

_ALLOWED_ROLES = (
	ROLES.JARZ_MANAGER,
	ROLES.JARZ_LINE_MANAGER,
	ROLES.JARZ_LINE_MANAGER_ALT,
	"System Manager",
)
_MANAGER_ROLES = (ROLES.JARZ_MANAGER, "System Manager")


def _gate(roles=_ALLOWED_ROLES) -> None:
	if frappe.session.user == "Administrator":
		return
	frappe.only_for(list(roles))


@frappe.whitelist()
def get_followups_today():
	"""Pending touches due by the end of today, oldest first, plus a summary."""
	_gate()
	from jarz_pos.services import customer_followups as svc

	return svc.get_due_touches()


@frappe.whitelist(methods=["POST"])
def log_touch(name, reached=None, outcome=None, notes=None):
	"""Record a result. ``reached`` (Reached / Not Reached) is required for calls."""
	_gate()
	from jarz_pos.services import customer_followups as svc

	return svc.log_touch(name, reached=reached, outcome=outcome, notes=notes)


@frappe.whitelist(methods=["POST"])
def skip_touch(name, reason=None):
	"""Drop a touch without contacting the customer."""
	_gate()
	from jarz_pos.services import customer_followups as svc

	return svc.skip_touch(name, reason=reason)


@frappe.whitelist()
def get_followup_stats(from_date=None, to_date=None):
	"""Reach rate, outcomes and 30-day reorder rate (default: last 30 days)."""
	_gate(_MANAGER_ROLES)
	from jarz_pos.services import customer_followups as svc

	return svc.get_followup_stats(from_date, to_date)


@frappe.whitelist()
def get_sent_report(from_date=None, to_date=None):
	"""Messages the sender sent (default: last 7 days): delivery, replies, orders, codes and cost vs revenue."""
	_gate(_MANAGER_ROLES)
	from jarz_pos.services import customer_followups as svc

	return svc.get_sent_report(from_date, to_date)


@frappe.whitelist(methods=["POST"])
def run_generator_now():
	"""Run the hourly generator immediately (manager)."""
	_gate(_MANAGER_ROLES)
	from jarz_pos.services import customer_followups as svc

	return svc.generate_touches()


@frappe.whitelist()
def get_followup_settings():
	"""Settings the screen shows: switch, start date, hours, offer."""
	_gate()
	from jarz_pos.services import customer_followups as svc

	s = svc.load_settings()
	return {
		"enabled": s["enabled"],
		"start_date": str(s["start_date"]),
		"offer_text": s["offer_text"],
		"quiet_start": str(s["quiet_start"]),
		"quiet_end": str(s["quiet_end"]),
		"call_window_start": str(s["call_window_start"]),
		"call_window_end": str(s["call_window_end"]),
		"apology_discount_percent": s["apology_discount_percent"],
		"high_value_order_min": s["high_value_order_min"],
	}


@frappe.whitelist(methods=["POST"])
def draft_followup_message(name, refresh=0):
	"""Claude-written WhatsApp text for a message touch (cached; template on failure)."""
	_gate()
	from jarz_pos.services import customer_followups as svc

	return svc.draft_message(name, refresh=frappe.utils.cint(refresh) == 1)


@frappe.whitelist(methods=["POST"])
def get_call_brief(name, refresh=0):
	"""Claude-written briefing for a call touch (cached; code-built text on failure)."""
	_gate()
	from jarz_pos.services import customer_followups as svc

	return svc.call_brief(name, refresh=frappe.utils.cint(refresh) == 1)
