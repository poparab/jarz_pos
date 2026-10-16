from frappe import _

from jarz_pos.services.meta_capi import daily_rows

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
