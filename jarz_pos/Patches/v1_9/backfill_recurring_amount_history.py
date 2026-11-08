"""Seed the dated amount history of every Jarz Recurring Expense.

Until now the registry kept one ``amount`` and used it for every month, so a rent
raise turned already-paid past months into deficits. The amount history
(``amount_history``) fixes that going forward; this patch rebuilds it for rows
that predate it from the ``Version`` log (the DocType has ``track_changes``):

* seed row: the start month at the amount BEFORE the first recorded change
  (or the current amount if the amount never changed);
* one row per recorded amount change: the month of the change at the NEW amount
  (two changes in one month: the later one wins).

Child rows are inserted directly, so ``modified`` and the version log of the
parent are left alone. A row that already has history is skipped (re-runnable).
"""

import json

import frappe
from frappe.utils import now

from jarz_pos.utils.recurring_amount import history_from_versions

PARENT = "Jarz Recurring Expense"
CHILD = "Jarz Recurring Expense Rate"


def _versions(name):
	rows = frappe.get_all(
		"Version",
		filters={"ref_doctype": PARENT, "docname": name},
		fields=["creation", "data"],
		order_by="creation asc",
		limit_page_length=0,
	)
	out = []
	for row in rows:
		try:
			data = json.loads(row.get("data") or "{}")
		except ValueError:
			continue
		out.append({"creation": row.get("creation"), "changed": data.get("changed") or []})
	return out


def execute():
	if not frappe.db.exists("DocType", CHILD) or not frappe.db.table_exists(CHILD):
		return

	stamp = now()
	for parent in frappe.get_all(PARENT, fields=["name", "start_date", "amount"], limit_page_length=0):
		if frappe.db.exists(CHILD, {"parent": parent.name, "parenttype": PARENT}):
			continue
		if not parent.start_date:
			continue
		history = history_from_versions(parent.start_date, parent.amount, _versions(parent.name))
		for idx, entry in enumerate(history, start=1):
			child = frappe.get_doc(
				{
					"doctype": CHILD,
					"parent": parent.name,
					"parenttype": PARENT,
					"parentfield": "amount_history",
					"idx": idx,
					"effective_from": entry["effective_from"],
					"amount": entry["amount"],
				}
			)
			child.name = frappe.generate_hash(length=10)
			child.owner = child.modified_by = "Administrator"
			child.creation = child.modified = stamp
			child.db_insert()
