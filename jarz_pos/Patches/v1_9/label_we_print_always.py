"""Every customer label is printed by JARZ: set ``we_print = 1`` on every row.

The owner confirmed customers never print their own labels, so the flag is now
hidden and forced to 1 by the controller. Rows saved earlier with 0 would stay
"Not Tracked" (and be skipped by consumption) until somebody re-saved them, so
this flips them once. Raw SQL, ``modified`` untouched. Idempotent.
"""

import frappe

DOCTYPE = "Jarz Customer Label"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return
	if not frappe.db.has_column(DOCTYPE, "we_print"):
		return
	frappe.db.sql(
		"update `tabJarz Customer Label` set `we_print` = 1 where ifnull(`we_print`, 0) != 1"
	)
