"""One change of a Sales Invoice's kanban state, with the moment it happened.

``custom_sales_invoice_state`` only holds the state an order is in *now*, so
the time an order spent in "In Progress" (the prep time) could never be
measured. This record is the missing history: one row per transition.

It is written only through ``jarz_pos.services.state_log.record_state_change``,
which never raises, so a logging problem can not stop an order from moving.
"""

from __future__ import annotations

from frappe.model.document import Document


class InvoiceStateLog(Document):
	pass
