# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""One approved allocation of a received item to a transit send (or a direct sender).

Written only by ``jarz_pos.services.stock_handover.approve_receipt``: which receipt,
which send (empty for a direct allocation), from where to where, how much, which
Stock Entry posted it, who approved it, and whether the manager kept the engine's
recommendation. The history of these rows feeds the matcher's "usual supplier".
"""

from frappe.model.document import Document


class JarzStockTransitMatch(Document):
	pass
