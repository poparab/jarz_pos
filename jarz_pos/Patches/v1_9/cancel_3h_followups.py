"""Cancel every Pending 3-hour follow-up.

The 3-hour thank-you is sent by WooCommerce, so the step was dropped from the
follow-up plan (owner, 2026-10-24). Rows already generated would still show up
on the screen, so any ``Pending`` touch with step ``3h`` (message or call) is
closed the way a superseded touch is: status ``Cancelled`` with a
``cancel_reason``. The ``3h`` option stays in the Touch ``step`` Select so old
rows keep validating.

Re-runnable: a second run finds nothing Pending.
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
        WHERE status = 'Pending' AND step = '3h'
        """,
		{"reason": REASON},
	)
	frappe.db.commit()
