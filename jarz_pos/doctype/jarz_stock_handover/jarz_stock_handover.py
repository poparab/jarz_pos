# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""A stock movement one side has declared and the other side has not confirmed yet.

A branch or the factory says "I received X from them" / "they took X from me";
the other side confirms, changes the quantities (which hands it back) or
rejects. Only a confirmation posts the Material Transfer Stock Entry, at the
moment of confirmation. Every rule lives in ``jarz_pos.services.stock_handover``
-- the API never goes through Desk permissions -- so the controller only keeps
the record internally consistent. The DocType is read-only in Desk (no role has
write), and a closed row can only be saved by the service, which sets
``flags.from_handover_service``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class JarzStockHandover(Document):
	def validate(self):
		self._guard_closed_rows()
		if (self.kind or "Handover") == "Handover":
			if not self.to_warehouse:
				frappe.throw(_("A stock handover needs a To warehouse."))
		elif self.to_warehouse:
			frappe.throw(_("A sent-out declaration (Dispatch) has no destination."))
		if self.from_warehouse and self.from_warehouse == self.to_warehouse:
			frappe.throw(_("From and To warehouses must be different."))
		if not self.items:
			frappe.throw(_("A stock handover needs at least one item."))
		for index, row in enumerate(self.items, start=1):
			row.idx = index

	def _guard_closed_rows(self):
		"""A Confirmed / Rejected / Cancelled row is history: only the service may touch it."""
		if self.is_new() or self.flags.get("from_handover_service"):
			return
		before = self.get_doc_before_save()
		if before and before.status != "Pending":
			frappe.throw(
				_("{0} is already {1} and can no longer be edited.").format(self.name, _(before.status)),
				frappe.PermissionError,
			)
