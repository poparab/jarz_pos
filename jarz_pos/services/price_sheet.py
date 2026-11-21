"""The generated B2B price sheet: live prices, one customer, one PDF.

A rep used to send the owner's Canva PDF. It was right on the day it was drawn
and wrong the day a rate moved; and it could never carry the one number a
customer on a running deal actually pays. This module builds the same sheet
from the database every time it is sent, so the page the customer opens is the
price list *as of the send*, for *that* customer.

**Pricing is never re-implemented here.** Every cell goes through
``invoice_creation._resolve_item_rate_with_provenance`` -- the resolver the
invoice itself uses -- so the sheet cannot promise a number the order will not
book. The provenance it returns is what drives the sheet:

* ``"deal"``   the customer's running deal. Printed as the deal price with the
               normal price struck through, plus one "valid until" line.
* ``"client"`` nothing was found server-side (the resolver echoed back the
               fallback we handed it). That is *not a price*; the cell prints a
               dash rather than the 0 we passed in.

**Snapshots, not live pages.** ``create_material_share`` builds the sheet once
and stores its digest on the share item. The link a customer opens next week
therefore shows what the rep sent, not whatever the rates are by then -- a
price list that silently changes under a link already forwarded to a partner is
a dispute waiting to happen. The digest is a hash of everything the page
prints, so two sends with identical output share one directory on disk and a
changed rate publishes a new one.

**Why the fonts and the logo are base64 in the HTML.** ``frappe.utils.pdf.
get_pdf`` hard-codes ``--disable-local-file-access`` (it cannot be overridden
through ``options``), so a ``file://`` font silently prints in DejaVu and a
``file://`` logo prints as a hole. Data URIs are the only reference
wkhtmltopdf will follow there.

Derivatives live in their own public root, ``files/jarz_price_sheets/<digest>/``
-- deliberately not under ``jarz_materials`` -- because
``materials._sweep_stale_digests`` prunes everything under a material's
directory except the newest digest, which would delete a snapshot a customer
link still points at.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import re
from typing import Any

import frappe
from frappe.utils import cint, flt, getdate, today

from jarz_pos.services import materials as materials_service

MATERIAL_DOCTYPE = materials_service.MATERIAL_DOCTYPE

SOURCE_UPLOADED = "Uploaded File"
SOURCE_GENERATED = "Generated Price List"

#: Public root holding every snapshot. See the module docstring for why it is
#: not a child of ``jarz_materials``.
SHEET_DIRNAME = "jarz_price_sheets"
PDF_FILENAME = "price-list.pdf"

#: Bumped when the PYTHON side of rendering changes (print options, fonts,
#: rasteriser). Template edits need no bump: its content hash is in the digest.
RENDER_VERSION = 1

#: Item Groups that carry sellable jar sizes. "Meduim" is a real group on the
#: servers (a historic typo) and is listed so its items still appear in the
#: editor's flavour picker.
SIZE_GROUPS = ("Large", "Medium", "Meduim", "Small")

DEFAULT_CURRENCY = "EGP"

_DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")

_BUNDLED_LOGO = ("public", "images", "jarz-logo.png")
_TEMPLATE_PATH = ("templates", "price_sheet", "price_sheet.html")
_FONT_FILES = {
	"inter_regular": "Inter-Regular.ttf",
	"inter_bold": "Inter-Bold.ttf",
	"tajawal_regular": "Tajawal-Regular.ttf",
	"tajawal_bold": "Tajawal-Bold.ttf",
	"display": "DMSerifDisplay-Regular.ttf",
}


def _logger():
	return frappe.logger("jarz_materials", allow_site=True)


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------


def parse_sizes(value: Any) -> list[str]:
	"""``"Medium, Large"`` (or a list) -> ``["Medium", "Large"]``, order kept, no repeats."""
	if value is None:
		return []
	items = value if isinstance(value, (list, tuple)) else str(value).split(",")
	out: list[str] = []
	for item in items:
		name = str(item or "").strip()
		if name and name not in out:
			out.append(name)
	return out


def parse_terms(lines: Any) -> list[dict[str, str | None]]:
	"""One term per line; ``Label: text`` splits on the FIRST colon.

	Only the first colon, because the text itself may contain one (the VAT line
	quotes a percentage and the storage line a duration). A line with no colon,
	or with nothing on one side of it, is all text -- a label with no body is a
	heading the customer cannot act on.
	"""
	if isinstance(lines, str):
		lines = lines.splitlines()
	out: list[dict[str, str | None]] = []
	for raw in lines or []:
		line = str(raw or "").strip()
		if not line:
			continue
		label, sep, text = line.partition(":")
		label, text = label.strip(), text.strip()
		if sep and label and text:
			out.append({"label": label, "text": text})
		else:
			out.append({"label": None, "text": line})
	return out


def format_amount(value: Any) -> str:
	"""``77.0`` -> ``"77"``, ``77.5`` -> ``"77.50"``, ``1250`` -> ``"1,250"``."""
	number = flt(value, 2)
	if number == int(number):
		return f"{int(number):,}"
	return f"{number:,.2f}"


def format_date(value: Any) -> str:
	"""``2026-11-30`` -> ``"30 Nov 2026"``. Locale independent on purpose."""
	parsed = getdate(value)
	return f"{parsed.day} {parsed.strftime('%b')} {parsed.year}"


# ---------------------------------------------------------------------------
# Config validation (shared by the DocType controller and the template editor)
# ---------------------------------------------------------------------------


def validate_sheet_config(doc) -> None:
	"""Refuse a generated material that could not print a useful sheet.

	Runs from the DocType's ``validate`` so the Desk form and the app's editor
	enforce one rule set. It normalises ``sheet_sizes`` in place.
	"""
	sizes = parse_sizes(doc.get("sheet_sizes"))
	if not sizes:
		frappe.throw("Pick at least one size for the price sheet.")
	for size in sizes:
		if not frappe.db.exists("Item Group", size):
			frappe.throw(f"Size '{size}' is not an Item Group.")
	doc.sheet_sizes = ",".join(sizes)

	rows = [r for r in (doc.get("sheet_rows") or []) if cint(r.get("enabled"))]
	if not rows:
		frappe.throw("Enable at least one flavour row.")
	for row in doc.get("sheet_rows") or []:
		row.flavour = (row.get("flavour") or "").strip()
		row.label = (row.get("label") or "").strip() or None
		if not row.flavour:
			frappe.throw("Every row needs a flavour.")

	price_list = (doc.get("sheet_price_list") or "").strip()
	if price_list and not frappe.db.exists("Price List", price_list):
		frappe.throw(f"Price List '{price_list}' not found.")
	doc.sheet_price_list = price_list or None

	doc.sheet_heading = (doc.get("sheet_heading") or "").strip() or "Price List"


# ---------------------------------------------------------------------------
# Data: who is it for, which list, what does each cell cost
# ---------------------------------------------------------------------------


def resolve_customer(reference_doctype: str | None, reference_name: str | None) -> str | None:
	"""The Customer a share is for, or ``None``.

	A Lead or Opportunity that has already converted prices as the Customer it
	became (the rep is often still working the "lead" record after the first
	order). One that has not converted has no deal and no list of its own.
	"""
	name = (reference_name or "").strip()
	if not reference_doctype or not name:
		return None
	if reference_doctype == "Customer":
		return name if frappe.db.exists("Customer", name) else None
	if reference_doctype == "Lead":
		return frappe.db.get_value("Customer", {"lead_name": name}, "name") or None
	if reference_doctype == "Opportunity":
		opp = frappe.db.get_value("Opportunity", name, ["opportunity_from", "party_name"], as_dict=True)
		if not opp or not opp.get("party_name"):
			return None
		if opp.get("opportunity_from") == "Customer":
			return opp.get("party_name")
		if opp.get("opportunity_from") == "Lead":
			return frappe.db.get_value("Customer", {"lead_name": opp.get("party_name")}, "name") or None
	return None


def resolve_price_list(doc, customer: str | None) -> str:
	"""Manager's explicit list, else the customer's own B2B list, else the base list."""
	explicit = (doc.get("sheet_price_list") or "").strip()
	if explicit:
		return explicit
	if customer:
		# Imported lazily: ``api.pos`` is a heavy module and this service is
		# also imported by the DocType controller on every save.
		from jarz_pos.api.pos import resolve_customer_price_list

		found = (resolve_customer_price_list(customer, order_purpose="B2B Supply") or {}).get("price_list")
		if found:
			return found
	from jarz_pos.setup.b2b_pricing import PRICE_LIST

	return PRICE_LIST


def _price_cell(item_code, price_list, customer, include_deals, on_date) -> dict[str, Any]:
	"""One cell: ``rate`` / ``normal_rate`` / ``is_deal`` (+ ``_deal_until``, stripped later)."""
	from jarz_pos.services import customer_deals
	from jarz_pos.services.invoice_creation import _resolve_item_rate_with_provenance

	rate, provenance = _resolve_item_rate_with_provenance(
		item_code,
		price_list,
		fallback_rate=0.0,
		customer=customer,
		on_date=on_date,
		include_deals=include_deals,
	)
	if provenance == "client" or rate is None:
		return {"rate": None, "normal_rate": None, "is_deal": False, "_deal_until": None, "_deal_from": None}
	if provenance != "deal":
		# No deal today, but one agreed for later (a December price set in
		# November) still belongs on the sheet, dated, with today's price struck.
		upcoming = (
			customer_deals.find_upcoming_deal(customer, item_code, on_date=on_date)
			if include_deals and customer
			else None
		)
		if upcoming:
			deal_rate, _deal, starts, ends = upcoming
			return {
				"rate": flt(deal_rate, 2),
				"normal_rate": flt(rate, 2),
				"is_deal": True,
				"_deal_until": str(getdate(ends)) if ends else None,
				"_deal_from": str(getdate(starts)) if starts else None,
			}
		return {
			"rate": flt(rate, 2),
			"normal_rate": None,
			"is_deal": False,
			"_deal_until": None,
			"_deal_from": None,
		}

	normal, normal_provenance = _resolve_item_rate_with_provenance(
		item_code,
		price_list,
		fallback_rate=0.0,
		customer=customer,
		on_date=on_date,
		include_deals=False,
	)
	until = None
	found = customer_deals.find_deal_rate(customer, item_code, on_date=on_date)
	if found:
		until = frappe.db.get_value(customer_deals.DEAL_DOCTYPE, found[1], "valid_upto")
	return {
		"rate": flt(rate, 2),
		"normal_rate": None if normal_provenance == "client" else flt(normal, 2),
		"is_deal": True,
		"_deal_until": str(getdate(until)) if until else None,
		"_deal_from": None,
	}


def build_sheet_data(
	material: Any,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	on_date: Any = None,
) -> dict[str, Any]:
	"""The contract's ``sheet`` dict. Everything the page prints, nothing it does not."""
	doc = frappe.get_doc(MATERIAL_DOCTYPE, material) if isinstance(material, str) else material
	if (doc.get("source_type") or "") != SOURCE_GENERATED:
		frappe.throw(f"'{doc.get('title') or doc.get('name')}' is not a generated price list.")

	as_of = str(getdate(on_date or today()))
	customer = resolve_customer(reference_doctype, reference_name)
	customer_name = (
		(frappe.db.get_value("Customer", customer, "customer_name") or customer) if customer else None
	)
	price_list = resolve_price_list(doc, customer)
	currency = frappe.db.get_value("Price List", price_list, "currency") or DEFAULT_CURRENCY
	sizes = parse_sizes(doc.get("sheet_sizes"))
	include_deals = bool(cint(doc.get("sheet_show_deals")) and customer)

	wanted = [r for r in (doc.get("sheet_rows") or []) if cint(r.get("enabled"))]
	codes = [f"{r.flavour} {size}" for r in wanted for size in sizes]
	live: dict[str, bool] = {}
	if codes:
		for item in frappe.get_all(
			"Item",
			filters={"name": ["in", codes]},
			fields=["name", "disabled"],
			limit_page_length=0,
		):
			live[item.name] = not cint(item.disabled)

	rows: list[dict[str, Any]] = []
	deal_dates: list[str] = []
	deal_starts: list[str] = []
	for row in wanted:
		cells = []
		for size in sizes:
			code = f"{row.flavour} {size}"
			if live.get(code):
				cell = _price_cell(code, price_list, customer, include_deals, as_of)
			else:
				cell = {"rate": None, "normal_rate": None, "is_deal": False, "_deal_until": None}
			until = cell.pop("_deal_until")
			starts = cell.pop("_deal_from", None)
			if until:
				deal_dates.append(until)
			if starts:
				deal_starts.append(starts)
			cells.append({"size": size, **cell})
		rows.append({"flavour": row.flavour, "label": row.label or row.flavour, "cells": cells})
	return _finish(doc, rows, sizes, customer_name, price_list, currency, as_of, deal_dates, deal_starts)


def _finish(
	doc, rows, sizes, customer_name, price_list, currency, as_of, deal_dates=(), deal_starts=()
) -> dict[str, Any]:
	"""Drop dead rows and assemble the dict. Split out so the maths is testable.

	``deal_until`` is the EARLIEST end date among the deal cells: the one line
	under the table must be true of every deal price above it, and a sheet that
	said "until 30 Nov" while one flavour's deal ended on the 15th would be the
	customer's receipt for a price the till refuses.

	``deal_from`` is set only when a deal price on the sheet has not started yet
	(the LATEST start among them, for the same reason): the note then reads
	"from <start> to <end>" so a price agreed for next month is not read as
	today's.
	"""
	rows = [r for r in rows if any(c["rate"] is not None for c in r["cells"])]
	return {
		"heading": (doc.get("sheet_heading") or "").strip() or "Price List",
		"customer_name": customer_name,
		"price_list": price_list,
		"currency": currency,
		"as_of": as_of,
		"sizes": sizes,
		"rows": rows,
		"deal_until": min(deal_dates) if deal_dates else None,
		"deal_from": (max(deal_starts) if deal_starts and max(deal_starts) > str(as_of) else None),
		"terms": parse_terms(doc.get("sheet_terms")),
		"footer": (doc.get("sheet_footer") or "").strip(),
	}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=16)
def _data_uri(path: str, mime: str) -> str:
	# nosemgrep: frappe-security-file-traversal -- path is a bundled app asset or a File row resolved by materials.source_path(), never a request value
	with open(path, "rb") as handle:
		return f"data:{mime};base64,{base64.b64encode(handle.read()).decode('ascii')}"


#: NOT ``font/ttf``, and no ``format('truetype')`` hint in the template: wkhtmltopdf
#: 0.12.6 (Qt 4.8 WebKit) silently ignores a data-URI font declared either way
#: and prints in DejaVu. Verified by inspecting the PDF's embedded fonts; only
#: the bare octet-stream form embeds the face.
_FONT_MIME = "application/octet-stream"


def _font_uris() -> dict[str, str]:
	return {
		key: _data_uri(frappe.get_app_path("jarz_pos", "public", "fonts", filename), _FONT_MIME)
		for key, filename in _FONT_FILES.items()
	}


def _logo(doc) -> tuple[str, str]:
	"""``(data_uri, signature)`` of the logo to print.

	A manager's ``sheet_logo`` wins; anything wrong with it (missing file, not
	an image) falls back to the bundled mark with a warning, because a price
	list that cannot be sent over a broken logo is the wrong trade.
	The signature is the file's content hash, so replacing the logo under the
	same URL still produces a new snapshot.
	"""
	custom = (doc.get("sheet_logo") or "").strip()
	if custom:
		try:
			path = materials_service.source_path(custom)
			mime = {
				".png": "image/png",
				".jpg": "image/jpeg",
				".jpeg": "image/jpeg",
				".gif": "image/gif",
			}.get(os.path.splitext(path)[1].lower())
			if mime:
				return _data_uri(path, mime), materials_service.content_digest(path)
			_logger().warning(f"sheet_logo {custom} is not a png/jpg/gif; using the bundled logo")
		except Exception:
			_logger().warning(f"sheet_logo {custom} unreadable; using the bundled logo", exc_info=True)
	path = frappe.get_app_path("jarz_pos", *_BUNDLED_LOGO)
	return _data_uri(path, "image/png"), "bundled"


def _display_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
	currency = data.get("currency") or DEFAULT_CURRENCY
	rows = []
	for row in data["rows"]:
		cells = []
		for cell in row["cells"]:
			if cell["rate"] is None:
				cells.append({"empty": True})
				continue
			amount = format_amount(cell["rate"])
			was = None
			if cell["is_deal"] and cell.get("normal_rate") is not None:
				was = f"{format_amount(cell['normal_rate'])} {currency}"
			cells.append(
				{
					"empty": False,
					"is_deal": bool(cell["is_deal"]),
					"amount": amount,
					"currency": currency,
					"text": f"{amount} {currency}",
					"was": was,
				}
			)
		rows.append({"label": row["label"], "cells": cells})
	return rows


def render_html(data: dict[str, Any], logo_uri: str) -> str:
	"""Jinja the sheet. Trusted template, untrusted values (escaped in the template)."""
	template = frappe.get_app_path("jarz_pos", *_TEMPLATE_PATH)
	# nosemgrep: frappe-security-file-traversal -- fixed app template path
	with open(template, encoding="utf-8") as handle:
		source = handle.read()
	return frappe.render_template(
		source,
		{
			"heading": data["heading"],
			"customer_name": data.get("customer_name"),
			"as_of_text": format_date(data["as_of"]),
			"sizes": data["sizes"],
			"rows": _display_rows(data),
			"deal_until_text": format_date(data["deal_until"]) if data.get("deal_until") else None,
			"deal_from_text": format_date(data["deal_from"]) if data.get("deal_from") else None,
			"terms": data.get("terms") or [],
			"footer": data.get("footer") or "",
			"fonts": _font_uris(),
			"logo": logo_uri,
		},
		safe_render=False,
	)


def render_pdf(html: str) -> bytes:
	from frappe.utils.pdf import get_pdf

	return get_pdf(
		html,
		options={
			"page-size": "A4",
			# The template pads itself; wkhtmltopdf's own margins would stack on it.
			"margin-top": "0mm",
			"margin-bottom": "0mm",
			"margin-left": "0mm",
			"margin-right": "0mm",
		},
	)


@functools.lru_cache(maxsize=4)
def _template_signature_for(path: str, mtime: float) -> str:
	# nosemgrep: frappe-security-file-traversal -- fixed app template path
	with open(path, "rb") as handle:
		return hashlib.sha256(handle.read()).hexdigest()[:8]


def _template_signature() -> str:
	"""Content hash of the HTML template, so editing it re-renders old sheets.

	Without this a CSS fix would be invisible: the digest only saw the data, so
	"same prices, same day" kept serving the directory built by the old layout.
	"""
	path = frappe.get_app_path("jarz_pos", *_TEMPLATE_PATH)
	try:
		return _template_signature_for(path, os.path.getmtime(path))
	except OSError:
		return "missing"


def sheet_digest(data: dict[str, Any], logo_signature: str = "bundled") -> str:
	"""16 hex chars over everything the page prints, plus the layout version."""
	payload = json.dumps(
		{
			"data": data,
			"v": RENDER_VERSION,
			"logo": logo_signature,
			"tpl": _template_signature(),
		},
		sort_keys=True,
		ensure_ascii=False,
		default=str,
	)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Snapshots on disk
# ---------------------------------------------------------------------------


def _snapshot_dir(digest: str) -> str:
	return frappe.get_site_path("public", "files", SHEET_DIRNAME, digest)


def snapshot_pdf_url(digest: str) -> str:
	return f"/files/{SHEET_DIRNAME}/{digest}/{PDF_FILENAME}"


def pages_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
	"""Manifest -> the ``{thumb, screen, full, w, h}`` page list the app and ``/m`` read."""
	return [
		{
			"thumb": page["tiers"]["thumb"]["u"],
			"screen": page["tiers"]["screen"]["u"],
			"full": page["tiers"]["full"]["u"],
			"w": page["tiers"]["full"]["w"],
			"h": page["tiers"]["full"]["h"],
		}
		for page in manifest.get("pages", [])
		if page.get("tiers", {}).get("full")
	]


def load_snapshot(digest: str | None) -> dict[str, Any] | None:
	"""The stored snapshot ``{"digest","pdf_url","pages"}``, or ``None`` if it is gone.

	The digest is validated before it is allowed anywhere near a path: it comes
	from a database column, but a column is one bad import away from a ``../``.
	"""
	digest = (digest or "").strip()
	if not _DIGEST_RE.match(digest):
		return None
	path = os.path.join(_snapshot_dir(digest), "manifest.json")
	if not os.path.exists(path):
		return None
	manifest = materials_service._read_manifest(path)
	pages = pages_from_manifest(manifest)
	if not pages:
		return None
	return {"digest": digest, "pdf_url": snapshot_pdf_url(digest), "pages": pages}


def _write_snapshot(digest: str, pdf_bytes: bytes) -> None:
	directory = _snapshot_dir(digest)
	os.makedirs(directory, exist_ok=True)
	pdf_path = os.path.join(directory, PDF_FILENAME)
	tmp = pdf_path + ".tmp"
	# nosemgrep: frappe-security-file-traversal -- site public/files/jarz_price_sheets/<validated hex digest>/
	with open(tmp, "wb") as handle:
		handle.write(pdf_bytes)
	os.replace(tmp, pdf_path)

	pages = materials_service._load_pdf_pages(pdf_path)
	manifest: dict[str, Any] = {"digest": digest, "pages": [], "count": 0}
	for index, page in enumerate(pages):
		entry: dict[str, Any] = {"i": index, "tiers": {}}
		for tier_name, long_edge, quality in materials_service.TIERS:
			filename = f"p{index}-{tier_name}.jpg"
			size = materials_service._write_tier(
				page,
				long_edge,
				min(96, quality + materials_service._TEXT_QUALITY_BONUS),
				os.path.join(directory, filename),
			)
			entry["tiers"][tier_name] = {
				"u": f"/files/{SHEET_DIRNAME}/{digest}/{filename}",
				"w": size[0],
				"h": size[1],
			}
		entry["w"] = entry["tiers"]["full"]["w"]
		entry["h"] = entry["tiers"]["full"]["h"]
		manifest["pages"].append(entry)
		page.close()
	manifest["count"] = len(manifest["pages"])
	# The manifest is written LAST: its presence is what "this snapshot is
	# complete" means, so a build that dies half way is rebuilt, not served.
	manifest_path = os.path.join(directory, "manifest.json")
	# nosemgrep: frappe-security-file-traversal -- same server-derived directory as above
	with open(manifest_path + ".tmp", "w", encoding="utf-8") as handle:
		json.dump(manifest, handle)
	os.replace(manifest_path + ".tmp", manifest_path)


def ensure_snapshot(
	material: Any,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	on_date: Any = None,
) -> dict[str, Any]:
	"""Build (or reuse) the sheet for one reference. Idempotent by digest.

	Returns ``{"digest", "pdf_url", "pages", "sheet"}``. Raises (via
	``frappe.throw``) when there is nothing to print or the engine fails: the
	caller must not mint a link to a sheet that does not exist.
	"""
	doc = frappe.get_doc(MATERIAL_DOCTYPE, material) if isinstance(material, str) else material
	data = build_sheet_data(doc, reference_doctype, reference_name, on_date)
	if not data["rows"]:
		frappe.throw("No flavour has a price in this price list, so there is nothing to print.")
	logo_uri, logo_signature = _logo(doc)
	digest = sheet_digest(data, logo_signature)

	stored = load_snapshot(digest)
	if not stored:
		try:
			_write_snapshot(digest, render_pdf(render_html(data, logo_uri)))
		except frappe.ValidationError:
			raise
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), "price sheet render failed")
			frappe.throw(f"Could not render the PDF: {exc}")
		stored = load_snapshot(digest)
		if not stored:
			frappe.throw("The price sheet was rendered but no pages were produced.")
	return {**stored, "sheet": data}


# ---------------------------------------------------------------------------
# Template editor payloads
# ---------------------------------------------------------------------------


def available_catalog() -> tuple[list[str], list[str]]:
	"""``(flavour stems, size groups that exist)`` from the live item master."""
	groups = [g for g in SIZE_GROUPS if frappe.db.exists("Item Group", g)]
	stems: set[str] = set()
	if groups:
		for item in frappe.get_all(
			"Item",
			filters={"item_group": ["in", groups], "disabled": 0, "is_sales_item": 1},
			fields=["name", "item_group"],
			limit_page_length=0,
		):
			suffix = f" {item.item_group}"
			if item.name.endswith(suffix) and len(item.name) > len(suffix):
				stems.add(item.name[: -len(suffix)].strip())
	return sorted(stems, key=str.lower), groups


def template_payload(doc, can_edit: bool) -> dict[str, Any]:
	"""Contract item 5's return value."""
	rows = [
		{"flavour": r.flavour, "label": r.label or r.flavour, "enabled": bool(cint(r.enabled))}
		for r in (doc.get("sheet_rows") or [])
	]
	stems, groups = available_catalog()
	used = [r["flavour"] for r in rows]
	lowered = {u.lower() for u in used}
	flavours = used + [s for s in stems if s.lower() not in lowered]
	price_lists = frappe.get_all(
		"Price List",
		filters={"enabled": 1, "selling": 1},
		pluck="name",
		order_by="name asc",
		limit_page_length=0,
	)
	return {
		"name": doc.name,
		"title": doc.title,
		"title_ar": doc.get("title_ar"),
		"enabled": bool(cint(doc.get("enabled"))),
		"is_default": bool(cint(doc.get("is_default"))),
		"sheet_heading": doc.get("sheet_heading") or "Price List",
		"sheet_price_list": (doc.get("sheet_price_list") or None),
		"sheet_sizes": parse_sizes(doc.get("sheet_sizes")),
		"sheet_show_deals": bool(cint(doc.get("sheet_show_deals"))),
		"sheet_terms": [line.strip() for line in (doc.get("sheet_terms") or "").splitlines() if line.strip()],
		"sheet_footer": doc.get("sheet_footer") or "",
		"rows": rows,
		"available_flavours": flavours,
		"available_sizes": groups,
		"price_lists": list(price_lists),
		"can_edit": bool(can_edit),
	}
