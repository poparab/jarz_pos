# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""One opening of a shared material link.

Written by an anonymous visitor, so the controller stays empty on purpose: the
row is built and updated through narrow helpers in
``jarz_pos.services.materials`` that set an explicit field list, never through a
full ``save()`` from a guest request.
"""

from frappe.model.document import Document


class JarzMaterialView(Document):
    pass
