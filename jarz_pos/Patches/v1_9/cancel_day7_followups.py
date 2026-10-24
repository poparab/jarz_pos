"""Cancel every Pending day-7 follow-up message.

The day-7 message step was dropped from the follow-up plan (2026-10-24). Rows
already generated would still show up on the screen, so any ``Pending`` touch
with step ``day7`` is closed the same way a superseded touch is: status
``Cancelled`` with a ``cancel_reason``. The ``day7`` option stays in the Touch
``step`` Select so old rows keep validating.

Re-runnable: a second run finds nothing Pending. ``modified`` is refreshed like
the module's own cancel sweeps do.
"""

import frappe

TOUCH = "Jarz Follow-up Touch"
REASON = "Step removed"


def execute():
	if not frappe.db.exists("DocType", TOUCH):
		return
	frappe.db.sql(
		"""
        UPDATE `tabJarz Follow-up Touch`
        SET status = 'Cancelled', cancel_reason = %(reason)s, modified = NOW()
        WHERE status = 'Pending' AND step = 'day7'
        """,
		{"reason": REASON},
	)
	frappe.db.commit()
