"""Controller for ``Delivery Failure Reason``.

Deliberately minimal. This is setup master data: it holds no money, drives no
posting, and its name *is* its ``code`` (``autoname: field:code``), which is the
value stored on ``Sales Invoice.custom_delivery_failure_reason``.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

#: Uppercase, digits and underscores only. The code is the primary key and the
#: link target on thousands of invoices; keeping it to a strict shape stops a
#: stray space or casing difference from creating a near-duplicate reason that
#: fragments every failure report built on top of it.
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class DeliveryFailureReason(Document):
    def validate(self):
        code = str(self.code or "").strip().upper()
        if not code:
            frappe.throw(_("Code is required"))
        if not _CODE_RE.match(code):
            frappe.throw(
                _(
                    "Code must be uppercase letters, digits and underscores only "
                    "(got '{0}')."
                ).format(self.code)
            )
        self.code = code

        if not str(self.label_en or "").strip():
            frappe.throw(_("Label (English) is required"))
