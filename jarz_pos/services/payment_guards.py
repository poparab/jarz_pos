"""Payment Entry guards that ERPNext itself does not provide under concurrency.

ERPNext checks a Payment Entry's allocation against the invoice's outstanding
amount during validate, with plain reads and no lock. Two submits for the same
invoice seconds apart both read the full outstanding, both pass, and both post:
on 2026-10-28 one 14,128 EGP purchase invoice was paid three times in 16 s, and
on 2026-06-03 two sales invoices were each paid twice. The invoice still showed
outstanding 0, because each submit recomputed it from its own older snapshot.

``guard_invoice_overallocation`` closes that window. It locks every referenced
invoice row with a LOCKING read, so a second submit waits for the first to
commit and then reads the outstanding amount the first one left behind.
"""

import frappe
from frappe import _

EPSILON = 0.01
INVOICE_DOCTYPES = ("Sales Invoice", "Purchase Invoice")


def guard_invoice_overallocation(doc, method=None) -> None:
	"""``before_submit`` on Payment Entry.

	Refuses an allocation larger than the invoice's outstanding amount as it
	stands after any concurrent payment has committed. Returns before any lock
	when the entry allocates nothing to an invoice. Exceptions propagate on
	purpose: this is a money guard and must fail closed.
	"""
	wanted: dict[tuple[str, str], float] = {}
	for ref in doc.get("references") or []:
		doctype = ref.get("reference_doctype")
		allocated = float(ref.get("allocated_amount") or 0)
		if doctype not in INVOICE_DOCTYPES or allocated <= EPSILON:
			continue
		key = (doctype, ref.get("reference_name"))
		wanted[key] = wanted.get(key, 0.0) + allocated
	if not wanted:
		return

	# Sorted so two entries touching the same invoices lock them in one order.
	for doctype, name in sorted(wanted):
		row = frappe.db.sql(
			f"SELECT outstanding_amount, docstatus FROM `tab{doctype}` WHERE name = %s FOR UPDATE",
			(name,),
			as_dict=True,
		)
		if not row or int(row[0].docstatus or 0) != 1:
			continue
		outstanding = float(row[0].outstanding_amount or 0)
		# A return/credit note carries a negative outstanding and is paid
		# the other way; only an ordinary payable/receivable is guarded.
		if outstanding < -EPSILON:
			continue
		allocated = wanted[(doctype, name)]
		# Rounded so 100.01 against 100.00 is the one-piastre tolerance, not 0.0100000001.
		if round(allocated - outstanding, 2) > EPSILON:
			frappe.throw(
				_(
					"{0} {1} has only {2} outstanding, so {3} cannot be allocated to it. "
					"It may have just been paid by another entry; refresh and check before paying again."
				).format(_(doctype), name, f"{outstanding:,.2f}", f"{allocated:,.2f}"),
				title=_("Invoice already paid"),
			)
