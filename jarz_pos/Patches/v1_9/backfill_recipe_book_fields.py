"""Give every existing Jarz SOP its Recipe Book fields.

The Recipe Book added ``stage``, ``track`` and ``recipe_title`` to Jarz SOP.
Frappe does not apply a new Select default to rows that already exist, so an
SOP written before this patch would show a blank stage and drop out of the
book.  Every existing record was an approved procedure on the Production Board,
so:

* blank ``stage``        -> ``Approved``
* blank ``track``        -> ``Standard line``
* blank ``recipe_title`` -> ``item_name`` (else the item code)

Only blank values are written, so the patch is re-runnable and never undoes an
edit.  ``update_modified=False`` keeps the SOP's own history untouched.
"""

import frappe

DOCTYPE = "Jarz SOP"
DEFAULT_STAGE = "Approved"
DEFAULT_TRACK = "Standard line"


def execute():
	if not frappe.db.has_column(DOCTYPE, "stage"):
		return

	rows = frappe.get_all(
		DOCTYPE,
		fields=["name", "item_code", "item_name", "stage", "track", "recipe_title"],
		limit_page_length=0,
	)
	for row in rows:
		values = {}
		if not (row.get("stage") or "").strip():
			values["stage"] = DEFAULT_STAGE
		if not (row.get("track") or "").strip():
			values["track"] = DEFAULT_TRACK
		if not (row.get("recipe_title") or "").strip():
			title = (row.get("item_name") or row.get("item_code") or "").strip()
			if title:
				values["recipe_title"] = title
		if values:
			frappe.db.set_value(DOCTYPE, row["name"], values, update_modified=False)
