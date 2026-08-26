# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""One shareable item in the B2B sales-material library.

A price list PDF, a sheet of product photos, a certificate. The DocType is the
registry; :mod:`jarz_pos.services.materials` turns whatever is attached into the
JPEG ladder the customer-facing viewer reads.

Two side effects live here rather than in the service, because both are about
what it *means* to put a file in this library:

1. **The attachment is published.** A material exists to be opened by a cafe
   owner who has no ERPNext login, and Frappe serves ``/private/files/...``
   only to authenticated sessions -- so a private attachment produces a link
   that 403s for exactly the audience it was minted for. Uploading here is an
   explicit act of publication, so the file is flipped to public on save and
   the docstring says so rather than the behaviour being a surprise.

2. **Changing the file invalidates the render.** ``source_hash`` is compared on
   every save; a new file resets the render state and re-queues the rasteriser,
   so a corrected price list cannot keep serving yesterday's pixels.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

from jarz_pos.services import materials as materials_service


class JarzSalesMaterial(Document):
    """A price list / photo set / catalogue available to send to a lead."""

    def validate(self):
        self.title = (self.title or "").strip()
        if not self.title:
            frappe.throw("Title is required.")
        self.title_ar = (self.title_ar or "").strip() or None
        if not (self.attachment or "").strip():
            frappe.throw("Attach the price list or photo file.")
        self._publish_attachment()

    def on_update(self):
        previous = self.get_doc_before_save()
        changed = previous is None or (previous.attachment or "") != (self.attachment or "")
        if changed:
            # Not merely stale -- actively wrong, until the rasteriser catches
            # up. Clearing the manifest makes ``manifest_for`` self-heal on the
            # first read instead of serving the old file's pages.
            frappe.db.set_value(
                self.doctype,
                self.name,
                {
                    "render_status": materials_service.RENDER_PENDING,
                    "render_manifest": None,
                    "render_error": None,
                    "source_hash": None,
                    "page_count": 0,
                },
                update_modified=False,
            )
        if changed or self.render_status in (
            materials_service.RENDER_PENDING,
            materials_service.RENDER_FAILED,
        ):
            materials_service.enqueue_build(self.name, force=changed)

    def on_trash(self):
        try:
            materials_service.drop_cache(self.name)
        except Exception:
            # A leftover cache directory is disk we can sweep by hand; a delete
            # that fails because of it is a manager blocked at their desk.
            frappe.logger("jarz_materials", allow_site=True).warning(
                f"drop_cache({self.name}) failed", exc_info=True
            )

    def _publish_attachment(self):
        """Make the attached File public. See the module docstring, point 1."""
        url = (self.attachment or "").strip()
        if not url.startswith("/private/files/"):
            return
        try:
            name = frappe.db.get_value("File", {"file_url": url}, "name")
            if not name:
                return
            file_doc = frappe.get_doc("File", name)
            file_doc.is_private = 0
            file_doc.save(ignore_permissions=True)
            # save() moves the bytes and rewrites file_url, so the material must
            # follow it or the link points at a path that no longer exists.
            self.attachment = file_doc.file_url
        except Exception:
            frappe.logger("jarz_materials", allow_site=True).error(
                f"could not publish attachment {url}", exc_info=True
            )
            frappe.throw(
                "Could not publish this file. Upload it again with the "
                "<b>Private</b> box unticked."
            )
