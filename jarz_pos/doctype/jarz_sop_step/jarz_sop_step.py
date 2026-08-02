"""Child row of a Jarz SOP.

No behaviour of its own: ``step_no`` is renumbered by the parent's
``validate`` so the numbering is decided in one place, and the capture rules
are enforced in ``api/sop.py`` where the reading actually arrives.
"""

from __future__ import annotations

from frappe.model.document import Document


class JarzSOPStep(Document):
    pass
