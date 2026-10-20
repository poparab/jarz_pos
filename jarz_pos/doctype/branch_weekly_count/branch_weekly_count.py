from __future__ import annotations

from frappe.model.document import Document


class BranchWeeklyCount(Document):
	"""One branch's blind weekly count of everything it holds (Saturday to Friday).

	Created and driven by ``jarz_pos.api.branch_weekly_count`` (submit, approve,
	reject), which writes with ``ignore_permissions``. The document is not
	submittable: ``status`` carries the lifecycle, and the Stock Reconciliation
	it posts is the accounting record.
	"""
