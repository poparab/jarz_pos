"""Tests for the generated B2B price sheet (backend half).

What these pin is what is *silent* when it breaks -- the rep sends a link, the
customer opens it, and only the customer can tell the sheet is wrong:

* **A cell is a real price or nothing.** The resolver echoes back the fallback
  we hand it (``0.0``) with provenance ``"client"`` when it found no price; if
  that ever printed it would read "0 EGP" on a price list.
* **A deal is shown as a deal, and its end date is the earliest one.** One line
  under the table must be true of every deal price above it.
* **The digest is a pure function of what the page prints.** Unstable, and two
  sends of the same sheet pile up directories; too coarse, and a changed rate
  is served from the old pixels.
* **The link keeps the prices it was minted with.** ``get_public_share`` serves
  the snapshot stored on the share item, never today's prices.
* **A build failure never yields a link.** ``create_material_share`` throws
  before the share row exists.
* **A generated material never reaches the file rasteriser** (no attachment to
  rasterise, so a queued job would fail on every save).

Mock/unittest, no fixtures: the DB, the resolver and the PDF engine are
replaced, so these also run pre-migrate.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from jarz_pos.api import materials as api_materials
from jarz_pos.api import price_sheet as api_sheet
from jarz_pos.services import price_sheet as sheet


def _doc(**overrides):
	"""A stand-in material: ``.get`` plus attribute access, like a real Document."""
	data = {
		"name": "MAT-00001",
		"title": "Jarz B2B Price List",
		"title_ar": None,
		"source_type": sheet.SOURCE_GENERATED,
		"sheet_heading": "Price List",
		"sheet_price_list": None,
		"sheet_sizes": "Medium,Large",
		"sheet_show_deals": 1,
		"sheet_footer": "More Than Just a Jar",
		"sheet_logo": None,
		"sheet_terms": "الحد الأدنى للطلب: ١٢ جار\nملاحظة بدون عنوان",
		"sheet_rows": [
			frappe._dict(flavour="Strawberry", label="Strawberry cheesecake jar", enabled=1),
			frappe._dict(flavour="Molten", label="", enabled=1),
			frappe._dict(flavour="Lotus", label="Lotus jar", enabled=0),
		],
		"enabled": 1,
		"is_default": 0,
	}
	data.update(overrides)
	return frappe._dict(data)


class TestParsers(unittest.TestCase):
	def test_sizes_keep_order_trim_and_drop_repeats(self):
		self.assertEqual(sheet.parse_sizes(" Medium, Large ,Medium,,"), ["Medium", "Large"])
		self.assertEqual(sheet.parse_sizes(["Large", " Small "]), ["Large", "Small"])
		self.assertEqual(sheet.parse_sizes(None), [])
		self.assertEqual(sheet.parse_sizes(""), [])

	def test_terms_split_on_the_first_colon_only(self):
		terms = sheet.parse_terms("الأسعار: غير شاملة الضريبة: ١٤٪\n\n  \nسطر بلا عنوان")
		self.assertEqual(
			terms,
			[
				{"label": "الأسعار", "text": "غير شاملة الضريبة: ١٤٪"},
				{"label": None, "text": "سطر بلا عنوان"},
			],
		)

	def test_a_label_with_no_body_or_body_with_no_label_is_plain_text(self):
		self.assertEqual(sheet.parse_terms(["عنوان:"]), [{"label": None, "text": "عنوان:"}])
		self.assertEqual(sheet.parse_terms([": نص"]), [{"label": None, "text": ": نص"}])

	def test_the_seeded_terms_all_carry_a_label(self):
		from jarz_pos.setup.price_sheet_setup import SEED_TERMS

		parsed = sheet.parse_terms(list(SEED_TERMS))
		self.assertEqual(len(parsed), 7)
		self.assertTrue(all(t["label"] for t in parsed))

	def test_amount_has_no_decimals_when_whole(self):
		self.assertEqual(sheet.format_amount(77.0), "77")
		self.assertEqual(sheet.format_amount(77), "77")
		self.assertEqual(sheet.format_amount(77.5), "77.50")
		self.assertEqual(sheet.format_amount(1250), "1,250")

	def test_date_text_is_locale_independent(self):
		self.assertEqual(sheet.format_date("2026-11-30"), "30 Nov 2026")
		self.assertEqual(sheet.format_date("2026-03-05"), "5 Mar 2026")


class TestCustomerResolution(unittest.TestCase):
	def _db(self, **kw):
		return SimpleNamespace(**kw)

	def test_customer_is_itself(self):
		db = self._db(exists=lambda dt, n=None: True)
		with mock.patch.object(frappe, "db", db, create=True):
			self.assertEqual(sheet.resolve_customer("Customer", "CUST-1"), "CUST-1")

	def test_unknown_customer_is_none(self):
		db = self._db(exists=lambda dt, n=None: False)
		with mock.patch.object(frappe, "db", db, create=True):
			self.assertIsNone(sheet.resolve_customer("Customer", "NOPE"))

	def test_converted_lead_prices_as_its_customer(self):
		calls = []

		def get_value(dt, filters, field=None, **kw):
			calls.append((dt, filters))
			return "CUST-9"

		with mock.patch.object(frappe, "db", self._db(get_value=get_value), create=True):
			self.assertEqual(sheet.resolve_customer("Lead", "CRM-LEAD-1"), "CUST-9")
		self.assertEqual(calls, [("Customer", {"lead_name": "CRM-LEAD-1"})])

	def test_unconverted_lead_has_no_customer(self):
		with mock.patch.object(frappe, "db", self._db(get_value=lambda *a, **k: None), create=True):
			self.assertIsNone(sheet.resolve_customer("Lead", "CRM-LEAD-1"))

	def test_opportunity_from_customer(self):
		opp = frappe._dict(opportunity_from="Customer", party_name="CUST-3")
		with mock.patch.object(frappe, "db", self._db(get_value=lambda *a, **k: opp), create=True):
			self.assertEqual(sheet.resolve_customer("Opportunity", "OPP-1"), "CUST-3")

	def test_opportunity_from_converted_lead(self):
		def get_value(dt, filters, field=None, as_dict=False):
			if dt == "Opportunity":
				return frappe._dict(opportunity_from="Lead", party_name="CRM-LEAD-7")
			return "CUST-7"

		with mock.patch.object(frappe, "db", self._db(get_value=get_value), create=True):
			self.assertEqual(sheet.resolve_customer("Opportunity", "OPP-1"), "CUST-7")

	def test_blank_reference_is_none(self):
		self.assertIsNone(sheet.resolve_customer(None, None))
		self.assertIsNone(sheet.resolve_customer("Lead", ""))


class TestPriceCell(unittest.TestCase):
	RESOLVER = "jarz_pos.services.invoice_creation._resolve_item_rate_with_provenance"

	def test_a_list_rate_is_a_plain_cell(self):
		with mock.patch(self.RESOLVER, return_value=(77.0, "item_price")):
			cell = sheet._price_cell("Strawberry Medium", "B2B Selling", "C1", True, "2026-11-21")
		self.assertEqual(cell["rate"], 77.0)
		self.assertFalse(cell["is_deal"])
		self.assertIsNone(cell["normal_rate"])

	def test_client_provenance_is_null_not_zero(self):
		with mock.patch(self.RESOLVER, return_value=(0.0, "client")):
			cell = sheet._price_cell("Strawberry Medium", "B2B Selling", None, False, "2026-11-21")
		self.assertIsNone(cell["rate"])
		self.assertFalse(cell["is_deal"])

	def test_the_resolver_is_asked_with_a_zero_fallback(self):
		with mock.patch(self.RESOLVER, return_value=(77.0, "item_price")) as resolver:
			sheet._price_cell("X Large", "B2B Selling", "C1", True, "2026-11-21")
		kwargs = resolver.call_args.kwargs
		self.assertEqual(kwargs["fallback_rate"], 0.0)
		self.assertTrue(kwargs["include_deals"])
		self.assertEqual(kwargs["customer"], "C1")

	def test_a_deal_carries_the_normal_price_and_the_end_date(self):
		def resolver(item, price_list, fallback_rate=0.0, customer=None, on_date=None, include_deals=False):
			return (70.0, "deal") if include_deals else (77.0, "item_price")

		db = SimpleNamespace(get_value=lambda dt, name, field=None, **k: "2026-11-30")
		with (
			mock.patch(self.RESOLVER, side_effect=resolver),
			mock.patch("jarz_pos.services.customer_deals.find_deal_rate", return_value=(70.0, "DEAL-1")),
			mock.patch.object(frappe, "db", db, create=True),
		):
			cell = sheet._price_cell("Strawberry Medium", "B2B Selling", "C1", True, "2026-11-21")
		self.assertEqual(cell["rate"], 70.0)
		self.assertTrue(cell["is_deal"])
		self.assertEqual(cell["normal_rate"], 77.0)
		self.assertEqual(cell["_deal_until"], "2026-11-30")


def _cell(rate, deal=False, until=None, normal=None):
	return {"rate": rate, "normal_rate": normal, "is_deal": deal, "_deal_until": until}


class TestBuildSheetData(unittest.TestCase):
	"""``build_sheet_data`` with the resolver, the DB and the customer lookup replaced."""

	def _build(self, cells, items_live=None, customer="C1", doc=None, **patches):
		"""``cells``: item_code -> _price_cell result. ``items_live``: codes that exist."""
		doc = doc or _doc()
		live = items_live
		if live is None:
			live = list(cells)

		def get_all(doctype, **kw):
			return [frappe._dict(name=n, disabled=0) for n in live]

		def get_value(dt, name, field=None, **k):
			if dt == "Customer":
				return "Cafe Example"
			if dt == "Price List":
				return "EGP"
			return None

		db = SimpleNamespace(get_value=get_value)
		with (
			mock.patch.object(sheet, "resolve_customer", return_value=customer),
			mock.patch.object(sheet, "resolve_price_list", return_value="B2B Selling"),
			mock.patch.object(
				sheet, "_price_cell", side_effect=lambda code, *a, **k: dict(cells[code])
			) as price_cell,
			mock.patch.object(frappe, "get_all", side_effect=get_all),
			mock.patch.object(frappe, "db", db, create=True),
		):
			data = sheet.build_sheet_data(doc, "Customer", "C1", on_date="2026-11-21")
		return data, price_cell

	def test_shape_and_order(self):
		cells = {
			"Strawberry Medium": _cell(77.0),
			"Strawberry Large": _cell(92.0),
			"Molten Medium": _cell(77.0),
			"Molten Large": _cell(92.0),
		}
		data, _ = self._build(cells)
		self.assertEqual(
			set(data),
			{
				"heading",
				"customer_name",
				"price_list",
				"currency",
				"as_of",
				"sizes",
				"rows",
				"deal_until",
				"terms",
				"footer",
			},
		)
		self.assertEqual(data["sizes"], ["Medium", "Large"])
		self.assertEqual([r["flavour"] for r in data["rows"]], ["Strawberry", "Molten"])
		# Blank label prints the flavour; the disabled Lotus row is not even priced.
		self.assertEqual(data["rows"][1]["label"], "Molten")
		self.assertEqual(
			data["rows"][0]["cells"][0],
			{"size": "Medium", "rate": 77.0, "normal_rate": None, "is_deal": False},
		)
		self.assertEqual(data["customer_name"], "Cafe Example")
		self.assertEqual(data["currency"], "EGP")
		self.assertEqual(data["as_of"], "2026-11-21")
		self.assertIsNone(data["deal_until"])
		self.assertEqual(data["terms"][0], {"label": "الحد الأدنى للطلب", "text": "١٢ جار"})

	def test_missing_item_is_a_null_cell_and_never_priced(self):
		cells = {
			"Strawberry Medium": _cell(77.0),
			"Strawberry Large": _cell(92.0),
			"Molten Medium": _cell(77.0),
			"Molten Large": _cell(92.0),
		}
		data, price_cell = self._build(
			cells, items_live=["Strawberry Medium", "Strawberry Large", "Molten Medium"]
		)
		molten = data["rows"][1]["cells"]
		self.assertEqual(molten[0]["rate"], 77.0)
		self.assertIsNone(molten[1]["rate"])
		asked = [call.args[0] for call in price_cell.call_args_list]
		self.assertNotIn("Molten Large", asked)

	def test_a_row_with_every_cell_null_is_dropped(self):
		cells = {
			"Strawberry Medium": _cell(None),
			"Strawberry Large": _cell(None),
			"Molten Medium": _cell(77.0),
			"Molten Large": _cell(92.0),
		}
		data, _ = self._build(cells)
		self.assertEqual([r["flavour"] for r in data["rows"]], ["Molten"])

	def test_a_row_with_one_priced_cell_is_kept(self):
		cells = {
			"Strawberry Medium": _cell(None),
			"Strawberry Large": _cell(92.0),
			"Molten Medium": _cell(None),
			"Molten Large": _cell(None),
		}
		data, _ = self._build(cells)
		self.assertEqual([r["flavour"] for r in data["rows"]], ["Strawberry"])

	def test_deal_until_is_the_earliest_deal_end(self):
		cells = {
			"Strawberry Medium": _cell(70.0, True, "2026-11-30", 77.0),
			"Strawberry Large": _cell(85.0, True, "2026-11-15", 92.0),
			"Molten Medium": _cell(77.0),
			"Molten Large": _cell(92.0),
		}
		data, _ = self._build(cells)
		self.assertEqual(data["deal_until"], "2026-11-15")
		self.assertTrue(data["rows"][0]["cells"][0]["is_deal"])
		self.assertNotIn("_deal_until", data["rows"][0]["cells"][0])

	def test_deals_are_only_requested_with_a_customer_and_the_switch_on(self):
		cells = {
			k: _cell(1.0) for k in ("Strawberry Medium", "Strawberry Large", "Molten Medium", "Molten Large")
		}
		_, price_cell = self._build(cells, customer="C1")
		self.assertTrue(price_cell.call_args.args[3])
		_, price_cell = self._build(cells, customer=None)
		self.assertFalse(price_cell.call_args.args[3])
		_, price_cell = self._build(cells, customer="C1", doc=_doc(sheet_show_deals=0))
		self.assertFalse(price_cell.call_args.args[3])

	def test_no_customer_means_no_prepared_for_line(self):
		cells = {
			k: _cell(1.0) for k in ("Strawberry Medium", "Strawberry Large", "Molten Medium", "Molten Large")
		}
		data, _ = self._build(cells, customer=None)
		self.assertIsNone(data["customer_name"])

	def test_an_uploaded_material_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			sheet.build_sheet_data(_doc(source_type=sheet.SOURCE_UPLOADED))


class TestDigest(unittest.TestCase):
	DATA = {
		"heading": "Price List",
		"rows": [{"flavour": "A", "cells": [{"size": "Medium", "rate": 77.0}]}],
		"as_of": "2026-11-21",
	}

	def test_is_sixteen_hex_characters(self):
		digest = sheet.sheet_digest(self.DATA)
		self.assertRegex(digest, r"^[0-9a-f]{16}$")

	def test_is_stable_across_calls_and_key_order(self):
		reordered = {"as_of": "2026-11-21", "rows": self.DATA["rows"], "heading": "Price List"}
		self.assertEqual(sheet.sheet_digest(self.DATA), sheet.sheet_digest(self.DATA))
		self.assertEqual(sheet.sheet_digest(self.DATA), sheet.sheet_digest(reordered))

	def test_changes_when_a_price_changes(self):
		changed = json.loads(json.dumps(self.DATA))
		changed["rows"][0]["cells"][0]["rate"] = 78.0
		self.assertNotEqual(sheet.sheet_digest(self.DATA), sheet.sheet_digest(changed))

	def test_changes_with_the_logo(self):
		self.assertNotEqual(sheet.sheet_digest(self.DATA, "bundled"), sheet.sheet_digest(self.DATA, "abc123"))

	def test_load_snapshot_refuses_a_path_like_digest(self):
		self.assertIsNone(sheet.load_snapshot("../../etc/passwd"))
		self.assertIsNone(sheet.load_snapshot(""))
		self.assertIsNone(sheet.load_snapshot(None))


class TestSheetConfigValidation(unittest.TestCase):
	def _validate(self, doc, groups=("Medium", "Large"), price_lists=("B2B Selling",)):
		def exists(dt, name=None):
			if dt == "Item Group":
				return name in groups
			if dt == "Price List":
				return name in price_lists
			return False

		with mock.patch.object(frappe, "db", SimpleNamespace(exists=exists), create=True):
			sheet.validate_sheet_config(doc)

	def test_a_good_config_passes_and_is_normalised(self):
		doc = _doc(sheet_sizes=" Medium , Large ", sheet_price_list="")
		self._validate(doc)
		self.assertEqual(doc.sheet_sizes, "Medium,Large")
		self.assertIsNone(doc.sheet_price_list)

	def test_at_least_one_size(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(_doc(sheet_sizes=" , "))

	def test_every_size_must_be_an_item_group(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(_doc(sheet_sizes="Medium,Huge"))

	def test_at_least_one_enabled_row(self):
		rows = [frappe._dict(flavour="Lotus", label="", enabled=0)]
		with self.assertRaises(frappe.ValidationError):
			self._validate(_doc(sheet_rows=rows))

	def test_price_list_must_exist_when_set(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(_doc(sheet_price_list="Nope"))
		self._validate(_doc(sheet_price_list="B2B Selling"))


class _FakeMaterial:
	"""A saveable stand-in recording what the template endpoint wrote."""

	def __init__(self):
		self.name = "MAT-00001"
		self.title = "Jarz B2B Price List"
		self.source_type = sheet.SOURCE_GENERATED
		self.sheet_rows = [frappe._dict(flavour="Old", label="Old", enabled=1)]
		self.saved = False
		self.ignore_permissions = None

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def append(self, key, value):
		getattr(self, key).append(frappe._dict(value))

	def save(self, ignore_permissions=False):
		self.saved = True
		self.ignore_permissions = ignore_permissions


class TestTemplateEndpoints(unittest.TestCase):
	def _db(self, exists=True):
		return SimpleNamespace(exists=lambda *a, **k: exists)

	def test_get_template_is_gated_first(self):
		with (
			mock.patch.object(api_sheet, "_ensure_pricing_read_access", side_effect=frappe.PermissionError),
			mock.patch.object(api_sheet, "_generated_material") as load,
		):
			with self.assertRaises(frappe.PermissionError):
				api_sheet.get_price_sheet_template("MAT-00001")
		load.assert_not_called()

	def test_get_template_returns_the_contract_keys(self):
		doc = _doc()
		doc.doctype = "Jarz Sales Material"
		with (
			mock.patch.object(api_sheet, "_ensure_pricing_read_access"),
			mock.patch.object(api_sheet, "_generated_material", return_value=doc),
			mock.patch.object(api_sheet, "_can_edit_sheets", return_value=False),
			mock.patch.object(
				sheet,
				"available_catalog",
				return_value=(["Carrot cake", "Strawberry", "Mango"], ["Large", "Medium"]),
			),
			mock.patch.object(frappe, "get_all", return_value=["B2B Selling", "Standard Selling"]),
		):
			out = api_sheet.get_price_sheet_template("MAT-00001")
		self.assertEqual(
			set(out),
			{
				"name",
				"title",
				"title_ar",
				"enabled",
				"is_default",
				"sheet_heading",
				"sheet_price_list",
				"sheet_sizes",
				"sheet_show_deals",
				"sheet_terms",
				"sheet_footer",
				"rows",
				"available_flavours",
				"available_sizes",
				"price_lists",
				"can_edit",
			},
		)
		self.assertFalse(out["can_edit"])
		self.assertEqual(out["sheet_sizes"], ["Medium", "Large"])
		self.assertIsNone(out["sheet_price_list"])
		self.assertEqual(out["sheet_terms"], ["الحد الأدنى للطلب: ١٢ جار", "ملاحظة بدون عنوان"])
		# Rows' flavours first in row order, then the rest alphabetically.
		self.assertEqual(out["available_flavours"], ["Strawberry", "Molten", "Lotus", "Carrot cake", "Mango"])
		self.assertEqual(out["rows"][1], {"flavour": "Molten", "label": "Molten", "enabled": True})

	def test_save_template_is_gated_first(self):
		with (
			mock.patch.object(
				api_sheet, "_ensure_full_manager_pricing_access", side_effect=frappe.PermissionError
			),
			mock.patch.object(api_sheet, "_generated_material") as load,
		):
			with self.assertRaises(frappe.PermissionError):
				api_sheet.save_price_sheet_template("MAT-00001", "{}")
		load.assert_not_called()

	def test_save_template_replaces_rows_and_normalises_lists(self):
		fake = _FakeMaterial()
		payload = {
			"title": " New title ",
			"sheet_heading": "Wholesale",
			"sheet_price_list": "",
			"sheet_sizes": ["Medium", "Large", "Medium"],
			"sheet_terms": ["أ: ب", "  ", "ج"],
			"sheet_show_deals": False,
			"rows": [
				{"flavour": "Strawberry", "label": "Straw", "enabled": True},
				{"flavour": " ", "label": "dropped"},
				{"flavour": "Lotus", "label": "", "enabled": False},
			],
			"attachment": "/files/evil.pdf",
			"source_type": "Uploaded File",
		}
		with (
			mock.patch.object(api_sheet, "_ensure_full_manager_pricing_access"),
			mock.patch.object(api_sheet, "_generated_material", return_value=fake),
			mock.patch.object(sheet, "template_payload", return_value={"ok": True}) as payload_fn,
		):
			out = api_sheet.save_price_sheet_template("MAT-00001", json.dumps(payload))
		self.assertEqual(out, {"ok": True})
		self.assertTrue(fake.saved)
		self.assertTrue(fake.ignore_permissions)
		self.assertEqual(fake.title, "New title")
		self.assertEqual(fake.sheet_heading, "Wholesale")
		self.assertIsNone(fake.sheet_price_list)
		self.assertEqual(fake.sheet_sizes, "Medium,Large")
		self.assertEqual(fake.sheet_terms, "أ: ب\nج")
		self.assertEqual(fake.sheet_show_deals, 0)
		self.assertEqual([r.flavour for r in fake.sheet_rows], ["Strawberry", "Lotus"])
		self.assertEqual([r.enabled for r in fake.sheet_rows], [1, 0])
		self.assertIsNone(fake.sheet_rows[1].label)
		# The endpoint cannot be used to repoint the material at a file.
		self.assertFalse(hasattr(fake, "attachment"))
		self.assertEqual(fake.source_type, sheet.SOURCE_GENERATED)
		self.assertTrue(payload_fn.call_args.kwargs["can_edit"])

	def test_save_template_rejects_bad_json(self):
		with (
			mock.patch.object(api_sheet, "_ensure_full_manager_pricing_access"),
			mock.patch.object(api_sheet, "_generated_material", return_value=_FakeMaterial()),
		):
			with self.assertRaises(frappe.ValidationError):
				api_sheet.save_price_sheet_template("MAT-00001", "{not json")

	def test_an_uploaded_material_has_no_template(self):
		db = SimpleNamespace(exists=lambda *a, **k: True)
		with (
			mock.patch.object(frappe, "db", db, create=True),
			mock.patch.object(frappe, "get_doc", return_value=_doc(source_type=sheet.SOURCE_UPLOADED)),
		):
			with self.assertRaises(frappe.ValidationError):
				api_sheet._generated_material("MAT-00001")

	def test_preview_is_b2b_gated_and_returns_the_contract_shape(self):
		snapshot = {
			"digest": "0123456789abcdef",
			"pdf_url": "/files/jarz_price_sheets/0123456789abcdef/price-list.pdf",
			"pages": [{"thumb": "t", "screen": "s", "full": "f", "w": 1, "h": 2}],
			"sheet": {"heading": "Price List"},
		}
		with (
			mock.patch.object(api_sheet, "_ensure_b2b_access", side_effect=frappe.PermissionError),
		):
			with self.assertRaises(frappe.PermissionError):
				api_sheet.preview_price_sheet("MAT-00001")
		with (
			mock.patch.object(api_sheet, "_ensure_b2b_access"),
			mock.patch.object(api_sheet, "_generated_material", return_value=_doc()),
			mock.patch.object(api_sheet, "_ensure_reference") as ensure_ref,
			mock.patch.object(sheet, "ensure_snapshot", return_value=snapshot) as build,
		):
			out = api_sheet.preview_price_sheet("MAT-00001", "Customer", "C1")
		ensure_ref.assert_called_once_with("Customer", "C1")
		self.assertEqual(set(out), {"pdf_url", "pages", "sheet"})
		self.assertEqual(out["pdf_url"], snapshot["pdf_url"])
		self.assertEqual(build.call_args.args[1:], ("Customer", "C1"))


class TestSalesMaterialListing(unittest.TestCase):
	def test_generated_material_is_ready_with_no_download(self):
		rows = [
			frappe._dict(
				name="MAT-1",
				title="Generated",
				title_ar=None,
				material_type="Price List",
				attachment=None,
				is_default=1,
				sort_order=0,
				page_count=0,
				render_status="Pending",
				source_type=sheet.SOURCE_GENERATED,
			),
			frappe._dict(
				name="MAT-2",
				title="Uploaded",
				title_ar=None,
				material_type="Catalog",
				attachment="/files/x.pdf",
				is_default=0,
				sort_order=1,
				page_count=3,
				render_status="Ready",
				source_type=sheet.SOURCE_UPLOADED,
			),
		]
		with (
			mock.patch.object(api_materials, "_ensure_b2b_access"),
			mock.patch.object(api_materials, "_can_edit_sheets", return_value=True),
			mock.patch.object(frappe, "get_all", return_value=rows),
		):
			out = api_materials.get_sales_materials()
		generated, uploaded = out["materials"]
		self.assertTrue(generated["generated"])
		self.assertEqual(generated["source_type"], "Generated Price List")
		self.assertTrue(generated["ready"])
		self.assertIsNone(generated["download_url"])
		self.assertEqual(generated["page_count"], 1)
		self.assertFalse(uploaded["generated"])
		self.assertEqual(uploaded["source_type"], "Uploaded File")
		self.assertEqual(uploaded["download_url"], "/files/x.pdf")
		self.assertEqual(uploaded["page_count"], 3)
		self.assertTrue(out["can_edit_sheets"])


class _FakeShare:
	"""What ``frappe.get_doc({...share...})`` returns in the create tests."""

	def __init__(self, spec):
		self.items = spec["items"]
		self.contact_name = spec.get("contact_name")
		self.contact_phone = spec.get("contact_phone")
		self.reference_doctype = spec["reference_doctype"]
		self.reference_name = spec["reference_name"]
		self.name = "MSH-0001"
		self.token = "tok123"
		self.message = None
		self.inserted = False

	def insert(self, ignore_permissions=False):
		self.inserted = True


class TestCreateShareWithGeneratedMaterial(unittest.TestCase):
	def _rows(self):
		return [
			frappe._dict(
				name="MAT-1",
				title="Generated",
				title_ar=None,
				material_type="Price List",
				render_status="Pending",
				source_type=sheet.SOURCE_GENERATED,
			),
			frappe._dict(
				name="MAT-2",
				title="Uploaded",
				title_ar=None,
				material_type="Catalog",
				render_status="Ready",
				source_type=sheet.SOURCE_UPLOADED,
			),
		]

	def _run(self, ensure_snapshot):
		made = {}

		def get_doc(spec):
			made["doc"] = _FakeShare(spec)
			return made["doc"]

		db = SimpleNamespace(set_value=lambda *a, **k: None, exists=lambda *a, **k: True)
		with (
			mock.patch.object(api_materials, "_ensure_b2b_access"),
			mock.patch.object(api_materials, "_ensure_reference"),
			mock.patch.object(frappe, "get_all", return_value=self._rows()),
			mock.patch.object(frappe, "get_doc", side_effect=get_doc),
			mock.patch.object(frappe, "db", db, create=True),
			mock.patch.object(api_materials.price_sheet_service, "ensure_snapshot", ensure_snapshot),
			mock.patch.object(
				api_materials.materials_service, "share_url", return_value="https://x/m/tok123"
			),
			mock.patch.object(api_materials.materials_service, "enqueue_build") as enqueue,
			mock.patch.object(api_materials, "_log_send"),
		):
			try:
				out = api_materials.create_material_share(
					"C1", ["MAT-1", "MAT-2"], reference_doctype="Customer", contact_phone="01111034268"
				)
			finally:
				self.enqueue = enqueue
		return out, made.get("doc")

	def test_the_snapshot_digest_is_stored_on_the_generated_item_only(self):
		build = mock.Mock(return_value={"digest": "0123456789abcdef"})
		out, doc = self._run(build)
		build.assert_called_once_with("MAT-1", "Customer", "C1")
		self.assertEqual([i["snapshot"] for i in doc.items], ["0123456789abcdef", None])
		self.assertTrue(doc.inserted)
		self.assertEqual(out["url"], "https://x/m/tok123")
		# The generated material is never handed to the file rasteriser, even
		# though its render_status reads "Pending".
		self.enqueue.assert_not_called()
		self.assertEqual(out["pending_render"], [])

	def test_a_build_failure_throws_before_any_share_exists(self):
		build = mock.Mock(side_effect=RuntimeError("wkhtmltopdf exploded"))
		made = []
		with mock.patch.object(frappe, "get_doc", side_effect=lambda spec: made.append(spec)):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._run(build)
		self.assertIn("Could not build the price sheet", str(ctx.exception))
		self.assertEqual(made, [])


class TestPublicShareServesTheSnapshot(unittest.TestCase):
	def _share(self, items):
		share = mock.MagicMock()
		share.is_expired.return_value = False
		share.items = items
		share.contact_name = "Mona"
		return share

	def _resolve(self, items, stored):
		def get_value(dt, filters, fields=None, as_dict=False):
			if dt == api_materials.SHARE_DOCTYPE:
				return "MSH-0001"
			return frappe._dict(
				name=filters,
				title="Jarz B2B Price List",
				title_ar=None,
				material_type="Price List",
				attachment=None,
				enabled=1,
			)

		with (
			mock.patch.object(frappe, "db", SimpleNamespace(get_value=get_value), create=True),
			mock.patch.object(frappe, "get_doc", return_value=self._share(items)),
			mock.patch.object(
				api_materials.price_sheet_service, "load_snapshot", return_value=stored
			) as load,
			mock.patch.object(api_materials.materials_service, "manifest_for") as manifest_for,
			mock.patch.object(api_materials, "_record_view", return_value="VIEW-1"),
		):
			out = api_materials._resolve_public_share("tok123")
		return out, load, manifest_for

	def test_snapshot_pages_and_pdf_url_are_served(self):
		stored = {
			"digest": "0123456789abcdef",
			"pdf_url": "/files/jarz_price_sheets/0123456789abcdef/price-list.pdf",
			"pages": [{"thumb": "t", "screen": "s", "full": "f", "w": 3200, "h": 4525}],
		}
		out, load, manifest_for = self._resolve(
			[frappe._dict(material="MAT-1", snapshot="0123456789abcdef")], stored
		)
		load.assert_called_once_with("0123456789abcdef")
		manifest_for.assert_not_called()
		self.assertTrue(out["ok"])
		self.assertEqual(out["items"][0]["pages"], stored["pages"])
		self.assertEqual(out["items"][0]["download_url"], stored["pdf_url"])
		self.assertEqual(set(out["items"][0]), {"title", "type", "download_url", "pages"})
		self.assertEqual(out["greeting_name"], "Mona")

	def test_a_snapshot_gone_from_disk_is_never_silently_re_priced(self):
		out, _, manifest_for = self._resolve(
			[frappe._dict(material="MAT-1", snapshot="0123456789abcdef")], None
		)
		manifest_for.assert_not_called()
		self.assertEqual(out, {"ok": False, "error": "not found"})


class TestControllerSkipsTheRasteriser(unittest.TestCase):
	def _make(self, **values):
		from jarz_pos.doctype.jarz_sales_material.jarz_sales_material import JarzSalesMaterial

		doc = JarzSalesMaterial.__new__(JarzSalesMaterial)
		doc.__dict__.update(
			{
				"title": "Generated",
				"title_ar": "",
				"attachment": None,
				"source_type": sheet.SOURCE_GENERATED,
				"render_status": "Pending",
				"page_count": 0,
				**values,
			}
		)
		return doc

	def test_generated_validate_needs_no_attachment_and_is_ready(self):
		doc = self._make()
		with mock.patch.object(sheet, "validate_sheet_config") as validate:
			doc.validate()
		validate.assert_called_once_with(doc)
		self.assertEqual(doc.render_status, "Ready")
		self.assertEqual(doc.page_count, 1)

	def test_generated_on_update_never_enqueues_a_build(self):
		doc = self._make()
		with mock.patch("jarz_pos.services.materials.enqueue_build") as enqueue:
			doc.on_update()
		enqueue.assert_not_called()

	def test_an_uploaded_material_still_requires_its_file(self):
		doc = self._make(source_type=sheet.SOURCE_UPLOADED)
		with self.assertRaises(frappe.ValidationError):
			doc.validate()


class TestSeed(unittest.TestCase):
	def test_seed_matches_the_owners_pdf(self):
		from jarz_pos.setup import price_sheet_setup as seed

		self.assertEqual(
			[f for f, _ in seed.SEED_ROWS],
			[
				"Strawberry",
				"Blueberry",
				"Chocolate Hazelnut",
				"Lotus",
				"Redvelvet",
				"Pistachio",
				"Tiramisu",
				"Molten",
			],
		)
		self.assertEqual(len(seed.SEED_TERMS), 7)

	def test_seed_does_nothing_when_a_generated_material_exists(self):
		from jarz_pos.setup import price_sheet_setup as seed

		db = SimpleNamespace(exists=lambda *a, **k: True, has_column=lambda *a: True)
		with (
			mock.patch.object(frappe, "db", db, create=True),
			mock.patch.object(frappe, "get_doc") as get_doc,
		):
			self.assertEqual(seed.ensure_price_sheet_material()["status"], "exists")
		get_doc.assert_not_called()

	def test_seed_creates_one_generated_material(self):
		from jarz_pos.setup import price_sheet_setup as seed

		def exists(dt, filters=None):
			return dt == "DocType"

		db = SimpleNamespace(exists=exists, has_column=lambda *a: True)
		doc = mock.Mock()
		doc.name = "MAT-00099"
		with (
			mock.patch.object(frappe, "db", db, create=True),
			mock.patch.object(frappe, "get_doc", return_value=doc) as get_doc,
			mock.patch.object(seed, "_logger"),
		):
			self.assertEqual(seed.ensure_price_sheet_material()["status"], "seeded")
		spec = get_doc.call_args.args[0]
		self.assertEqual(spec["source_type"], "Generated Price List")
		self.assertEqual(spec["sheet_sizes"], "Medium,Large")
		self.assertEqual(len(spec["sheet_rows"]), 8)
		self.assertEqual(spec["is_default"], 0)
		doc.insert.assert_called_once()

	def test_seed_swallows_its_own_failures(self):
		from jarz_pos.setup import price_sheet_setup as seed

		db = SimpleNamespace(
			exists=mock.Mock(side_effect=RuntimeError("db down")), has_column=lambda *a: True
		)
		with mock.patch.object(frappe, "db", db, create=True), mock.patch.object(seed, "_logger"):
			self.assertEqual(seed.ensure_price_sheet_material()["status"], "failed")


class TestHtmlRendering(unittest.TestCase):
	"""The Jinja template, with the font and logo data URIs stubbed out."""

	def _render(self, **overrides):
		data = {
			"heading": "Price <List>",
			"customer_name": "Cafe & Co",
			"price_list": "B2B Selling",
			"currency": "EGP",
			"as_of": "2026-11-21",
			"sizes": ["Medium", "Large"],
			"rows": [
				{
					"flavour": "Strawberry",
					"label": "Strawberry <b>jar</b>",
					"cells": [
						{"size": "Medium", "rate": 70.0, "normal_rate": 77.0, "is_deal": True},
						{"size": "Large", "rate": 92.0, "normal_rate": None, "is_deal": False},
					],
				},
				{
					"flavour": "Molten",
					"label": "Molten jar",
					"cells": [
						{"size": "Medium", "rate": None, "normal_rate": None, "is_deal": False},
						{"size": "Large", "rate": 92.5, "normal_rate": None, "is_deal": False},
					],
				},
			],
			"deal_until": "2026-11-30",
			"terms": [{"label": "التخزين", "text": "في الفريزر"}, {"label": None, "text": "سطر"}],
			"footer": "More Than Just a Jar",
		}
		data.update(overrides)
		fonts = {k: "data:font/truetype;base64,AAAA" for k in sheet._FONT_FILES}
		try:
			with mock.patch.object(sheet, "_font_uris", return_value=fonts):
				return sheet.render_html(data, "data:image/png;base64,AAAA")
		except Exception as exc:  # no Frappe site in this process
			self.skipTest(f"frappe.render_template unavailable here: {exc}")

	def test_prices_deal_and_note_are_printed(self):
		html = self._render()
		self.assertIn("70 EGP", html)
		self.assertIn("77 EGP", html)  # the struck-through normal price
		self.assertIn("92", html)
		self.assertIn("92.50", html)
		self.assertIn("Special prices valid until 30 Nov 2026", html)
		self.assertIn("Prepared for Cafe &amp; Co", html)
		self.assertIn("Prices as of 21 Nov 2026", html)
		self.assertIn("التخزين", html)

	def test_untrusted_text_is_escaped(self):
		html = self._render()
		self.assertIn("Price &lt;List&gt;", html)
		self.assertIn("Strawberry &lt;b&gt;jar&lt;/b&gt;", html)
		self.assertNotIn("<b>jar</b>", html)

	def test_no_deal_means_no_note_and_no_customer_means_no_prepared_for(self):
		html = self._render(deal_until=None, customer_name=None)
		self.assertNotIn("Special prices valid until", html)
		self.assertNotIn("Prepared for", html)
		self.assertIn("Prices as of", html)

	def test_a_null_cell_is_a_dash(self):
		self.assertIn("&mdash;", self._render())


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
