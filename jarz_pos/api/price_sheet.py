"""Staff API for the generated B2B price sheet: preview, read template, save template.

Three endpoints, three different gates, each the first statement of its
function:

* ``preview_price_sheet``      B2B access -- a rep may look at what a customer
                               will receive, nothing more.
* ``get_price_sheet_template`` pricing READ -- managers (including the line
                               manager) and B2B reps may open the editor
                               read-only, exactly like the Pricing page.
* ``save_price_sheet_template`` FULL manager only. The template decides what
                               every customer is shown as the Jarz price list,
                               so it is gated like the prices themselves.

The customer-facing side -- minting a share and serving it -- lives in
``api/materials.py``; this module never talks to a guest.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint

from jarz_pos.api.crm import _can_access_b2b, _ensure_b2b_access
from jarz_pos.api.materials import _ensure_reference
from jarz_pos.api.pos import _has_manager_pricing_access
from jarz_pos.api.price_lists import (
	_ensure_full_manager_pricing_access,
	_ensure_pricing_read_access,
)
from jarz_pos.services import price_sheet as sheet

MATERIAL_DOCTYPE = sheet.MATERIAL_DOCTYPE

#: The keys ``save_price_sheet_template`` accepts, mapped to the DocType field
#: they land on. Anything else in ``data`` is ignored rather than written: this
#: endpoint must not become a back door onto ``attachment`` or ``source_type``.
_SCALAR_FIELDS = {
	"title": "title",
	"title_ar": "title_ar",
	"enabled": "enabled",
	"is_default": "is_default",
	"sheet_heading": "sheet_heading",
	"sheet_price_list": "sheet_price_list",
	"sheet_show_deals": "sheet_show_deals",
	"sheet_footer": "sheet_footer",
}


def _can_edit_sheets() -> bool:
	"""Full manager pricing access. Same predicate as ``_ensure_full_manager_pricing_access``."""
	return bool(_has_manager_pricing_access() and _can_access_b2b())


def _generated_material(name: str):
	"""The material doc, refusing anything that is not a generated price list."""
	name = (name or "").strip()
	if not name or not frappe.db.exists(MATERIAL_DOCTYPE, name):
		frappe.throw(f"Material '{name}' not found.")
	doc = frappe.get_doc(MATERIAL_DOCTYPE, name)
	if (doc.get("source_type") or "") != sheet.SOURCE_GENERATED:
		frappe.throw(f"'{doc.title}' is an uploaded file, not a generated price list.")
	return doc


@frappe.whitelist()
def preview_price_sheet(
	material: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> dict[str, Any]:
	"""Render the sheet for one customer (or generically) and return its pages.

	With no reference the sheet is the generic one: the base B2B list, no deals,
	no "Prepared for" line -- what an unknown lead would be sent. Building it
	writes a snapshot directory like a real send does, which is what makes the
	preview identical to the link a rep then mints (same digest, same files).

	Returns ``{"pdf_url", "pages": [{thumb, screen, full, w, h}], "sheet"}``.
	"""
	_ensure_b2b_access()
	doc = _generated_material(material)
	if reference_name:
		_ensure_reference(reference_doctype or "", reference_name)
	snapshot = sheet.ensure_snapshot(doc, reference_doctype, reference_name)
	return {
		"pdf_url": snapshot["pdf_url"],
		"pages": snapshot["pages"],
		"sheet": snapshot["sheet"],
	}


@frappe.whitelist()
def get_price_sheet_template(material: str) -> dict[str, Any]:
	"""Everything the template editor needs in one round trip."""
	_ensure_pricing_read_access()
	doc = _generated_material(material)
	return sheet.template_payload(doc, can_edit=_can_edit_sheets())


@frappe.whitelist(methods=["POST"])
def save_price_sheet_template(material: str, data: Any) -> dict[str, Any]:
	"""Replace the editable parts of a generated material and return it re-read.

	``data`` is a JSON string (the app form-encodes; Frappe would flatten a
	list to its last element) or a dict. ``rows`` is a FULL replacement: the
	editor owns the whole list, order included, so a row dropped in the app is
	dropped here. Validation lives on the DocType (``validate_sheet_config``),
	so the Desk form and this endpoint cannot disagree about what is valid.
	"""
	_ensure_full_manager_pricing_access()
	doc = _generated_material(material)

	payload = data
	if isinstance(payload, str):
		try:
			payload = json.loads(payload or "{}")
		except ValueError:
			frappe.throw("The template data is not valid JSON.")
	if not isinstance(payload, dict):
		frappe.throw("The template data must be an object.")

	for key, fieldname in _SCALAR_FIELDS.items():
		if key not in payload:
			continue
		value = payload[key]
		if fieldname in ("enabled", "is_default", "sheet_show_deals"):
			value = 1 if cint(value) else 0
		elif isinstance(value, str):
			value = value.strip()
		if fieldname in ("title_ar", "sheet_price_list"):
			value = value or None
		doc.set(fieldname, value)

	if "sheet_sizes" in payload:
		doc.sheet_sizes = ",".join(sheet.parse_sizes(payload["sheet_sizes"]))
	if "sheet_terms" in payload:
		terms = payload["sheet_terms"]
		if isinstance(terms, str):
			terms = terms.splitlines()
		doc.sheet_terms = "\n".join(str(t).strip() for t in (terms or []) if str(t or "").strip())
	if "rows" in payload:
		rows = payload["rows"]
		if not isinstance(rows, list):
			frappe.throw("'rows' must be a list.")
		doc.set("sheet_rows", [])
		for row in rows:
			if not isinstance(row, dict):
				continue
			flavour = str(row.get("flavour") or "").strip()
			if not flavour:
				continue
			doc.append(
				"sheet_rows",
				{
					"flavour": flavour,
					"label": str(row.get("label") or "").strip() or None,
					"enabled": 1 if cint(row.get("enabled", 1)) else 0,
				},
			)

	# The caller is already a full manager; DocType write permission is a
	# second, different question (a custom role may hold one and not the other).
	doc.save(ignore_permissions=True)
	return sheet.template_payload(doc, can_edit=True)
