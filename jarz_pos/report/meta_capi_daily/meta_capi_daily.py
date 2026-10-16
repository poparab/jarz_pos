from frappe import _

from jarz_pos.services.meta_capi import daily_rows, skipped_by_reason

VIEW_BY_DAY = "By day"
VIEW_SKIPPED = "Skipped by reason"

STATUS_COLUMNS = (
	("pending", "Pending"),
	("held", "Held"),
	("approved", "Approved"),
	("rejected", "Rejected"),
	("sent", "Sent"),
	("failed", "Failed"),
	("expired", "Expired"),
	("skipped", "Skipped"),
)


def execute(filters=None):
	filters = filters or {}
	if filters.get("view") == VIEW_SKIPPED:
		return _skipped_view(filters)
	columns = [
		{"fieldname": "day", "label": _("Order Date"), "fieldtype": "Date", "width": 110},
		{"fieldname": "orders", "label": _("Orders"), "fieldtype": "Int", "width": 80},
	]
	columns += [
		{"fieldname": key, "label": _(label), "fieldtype": "Int", "width": 90}
		for key, label in STATUS_COLUMNS
	]
	columns.append(
		{"fieldname": "sent_value", "label": _("Sent Value (EGP)"), "fieldtype": "Currency", "width": 140}
	)
	data = daily_rows(from_date=filters.get("from_date"), to_date=filters.get("to_date"))
	return columns, data


def _skipped_view(filters):
	columns = [
		{"fieldname": "reason", "label": _("Skip Reason"), "fieldtype": "Data", "width": 280},
		{"fieldname": "orders", "label": _("Orders"), "fieldtype": "Int", "width": 90},
		{"fieldname": "order_value", "label": _("Order Value (EGP)"), "fieldtype": "Currency", "width": 150},
		{"fieldname": "last_day", "label": _("Last Seen"), "fieldtype": "Date", "width": 110},
	]
	data = skipped_by_reason(from_date=filters.get("from_date"), to_date=filters.get("to_date"))
	return columns, data
