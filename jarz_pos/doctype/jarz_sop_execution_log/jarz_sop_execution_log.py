"""Append-only record of what an operator actually captured.

Deliberately behaviour-free: validation belongs in ``api/sop.py``, where the
SOP step that defines the acceptable range is already in hand.  Rows are
written by ``record_sop_step_capture`` and never edited afterwards — this is
the audit trail, so a row that changes later is worth nothing.
"""

from __future__ import annotations

from frappe.model.document import Document


class JarzSOPExecutionLog(Document):
    pass
