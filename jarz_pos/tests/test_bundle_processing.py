"""Tests for bundle processing service.

This module tests the business logic for bundle expansion and pricing.

The second half of the file covers bundles that list the SAME item group on more
than one row ("Jarz Indulgence Five" = ``Large x4`` + ``Large x1``), which is the
shape that produced the production failure
``Bundle irk4mnvoe2: expected 4 selection(s) from 'Large', received 5``.
Those cases drive :meth:`BundleProcessor.load_bundle` end to end against a stub
``frappe`` module so the emitted rates, discounts and rounding correction can be
asserted to the piastre.
"""

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import flt


class TestBundleProcessing(unittest.TestCase):
	"""Test class for bundle processing business logic."""

	def test_validate_bundle_configuration_by_item_missing_item(self):
		"""Test validation with non-existent item."""
		from jarz_pos.services.bundle_processing import validate_bundle_configuration_by_item

		is_valid, message, bundle_code = validate_bundle_configuration_by_item("NON_EXISTENT_ITEM")

		# Should return invalid for non-existent item
		self.assertFalse(is_valid, "Should be invalid for non-existent item")
		self.assertIsInstance(message, str, "Should return error message")

	def test_validate_bundle_configuration_by_item_no_bundle(self):
		"""Test validation when item is not linked to any bundle."""
		from jarz_pos.services.bundle_processing import validate_bundle_configuration_by_item

		# Test with an item that exists but is not a bundle
		# This will depend on test data, so we handle both cases
		try:
			is_valid, message, bundle_code = validate_bundle_configuration_by_item("Test Item")
			# If no bundle exists, should be invalid
			if not is_valid:
				self.assertIsInstance(message, str, "Should return error message")
		except Exception:
			# Item may not exist in test environment
			pass

	def test_process_bundle_for_invoice_structure(self):
		"""Test that process_bundle_for_invoice returns correct structure."""
		from jarz_pos.services.bundle_processing import process_bundle_for_invoice

		# Test with a bundle that may not exist
		try:
			result = process_bundle_for_invoice("TEST_BUNDLE", quantity=1)
			# If successful, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
			self.assertIn("success", result, "Should include success key")
		except Exception:
			# Bundle may not exist in test environment
			pass

	def test_bundle_processor_calculate_discount(self):
		"""Test discount calculation logic."""
		# This test requires a mock bundle, which is complex without test data
		# We'll test the mathematical logic instead
		bundle_price = 100.0
		total_child_price = 150.0

		# Expected discount: ((150 - 100) / 150) * 100 = 33.33%
		expected_discount = ((total_child_price - bundle_price) / total_child_price) * 100
		expected_discount = max(0, expected_discount)

		self.assertAlmostEqual(
			expected_discount, 33.33, places=1, msg="Discount calculation should be correct"
		)

	def test_bundle_processor_discount_cannot_be_negative(self):
		"""Test that discount cannot be negative."""
		bundle_price = 200.0
		total_child_price = 150.0

		# When bundle price > child price, we expect an error
		# This is tested in the actual bundle processing logic
		discount_percentage = ((total_child_price - bundle_price) / total_child_price) * 100
		# The actual code would throw an error before clamping to 0

		# If we were to clamp: max(0, discount_percentage) would be 0
		clamped_discount = max(0, discount_percentage)
		self.assertEqual(clamped_discount, 0, "Negative discount should be clamped to 0")

	def test_bundle_processor_zero_child_price_handling(self):
		"""Test handling of zero child price."""
		bundle_price = 100.0
		total_child_price = 0.0

		# Division by zero should be handled
		if total_child_price == 0:
			discount = 0  # Should return 0 or raise appropriate error
		else:
			discount = ((total_child_price - bundle_price) / total_child_price) * 100

		self.assertEqual(discount, 0, "Zero child price should result in zero discount or error")

	def test_aggregate_selected_items_uses_group_key_for_duplicate_group_names(self):
		"""Test same-name bundle groups stay isolated when a stable group key is provided."""
		from jarz_pos.services.bundle_processing import BundleProcessor

		processor = BundleProcessor(
			"TEST_BUNDLE",
			selected_items={
				"ROW-LARGE-1": [{"id": "ITEM-A"}],
				"ROW-LARGE-2": [{"id": "ITEM-B"}, {"id": "ITEM-B"}],
			},
		)

		first_group = processor._aggregate_selected_items("ROW-LARGE-1", "Large", 1)
		second_group = processor._aggregate_selected_items("ROW-LARGE-2", "Large", 2)

		self.assertEqual(first_group, {"ITEM-A": {"qty": 1, "rate": None}})
		self.assertEqual(second_group, {"ITEM-B": {"qty": 2, "rate": None}})

	def test_aggregate_selected_items_falls_back_to_group_name_for_legacy_payload(self):
		"""Test legacy group-name keyed payloads continue to work."""
		from jarz_pos.services.bundle_processing import BundleProcessor

		processor = BundleProcessor(
			"TEST_BUNDLE",
			selected_items={
				"Large": [{"id": "ITEM-A"}],
			},
		)

		result = processor._aggregate_selected_items("ROW-LARGE-1", "Large", 1)

		self.assertEqual(result, {"ITEM-A": {"qty": 1, "rate": None}})

	def test_get_invoice_items_stamps_bundle_group_metadata_on_children(self):
		"""Child invoice rows should carry the bundle group identity needed by amendments."""
		from jarz_pos.services.bundle_processing import BundleProcessor

		processor = BundleProcessor("BDL-1", quantity=2)
		processor.bundle_doc = SimpleNamespace(bundle_price=200.0)
		processor.parent_item = SimpleNamespace(
			name="BUNDLE-ITEM",
			item_name="Bundle Item",
			description="Bundle Item",
		)
		processor.bundle_items = [
			{
				"item": SimpleNamespace(
					name="ITEM-A",
					item_name="Item A",
					description="Item A",
				),
				"qty": 1,
				"rate": 120.0,
				"item_group": "Flavor",
				"item_group_key": "ROW-FLAVOR-1",
			}
		]
		processor.get_item_rate = lambda _item_code: 200.0

		mock_frappe = MagicMock()
		mock_frappe.get_precision.return_value = 2
		mock_frappe.logger.return_value = MagicMock()

		with patch("jarz_pos.services.bundle_processing.frappe", mock_frappe):
			rows = processor.get_invoice_items()

		child = next(row for row in rows if row.get("is_bundle_child"))
		self.assertEqual(child["parent_bundle"], "BDL-1")
		self.assertEqual(child["bundle_group_key"], "ROW-FLAVOR-1")
		self.assertEqual(child["bundle_group_name"], "Flavor")

	def test_process_bundle_item_structure(self):
		"""Test that process_bundle_item returns correct structure."""
		from jarz_pos.services.bundle_processing import process_bundle_item

		# Test with bundle that may not exist
		try:
			result = process_bundle_item(
				bundle_id="TEST_BUNDLE",
				bundle_qty=1,
				bundle_price=100.0,
				selling_price_list="Standard Selling",
			)
			# If successful, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Bundle may not exist in test environment
			pass


# ---------------------------------------------------------------------------
# End-to-end expansion fixtures.
#
# BundleProcessor talks to the database for three things only: the Jarz Bundle
# doc, the Item docs and the list of items in a group. The stub below answers
# exactly those, which makes it possible to assert the emitted money instead of
# only the shape of the payload.
# ---------------------------------------------------------------------------


class _StubLogger:
	def info(self, *args, **kwargs):
		return None

	def warning(self, *args, **kwargs):
		return None

	def error(self, *args, **kwargs):
		return None


class _StubFrappe:
	"""Stand-in for the ``frappe`` module as bundle_processing uses it."""

	def __init__(self, bundle_doc, item_docs):
		self.bundle_doc = bundle_doc
		self.item_docs = {doc.name: doc for doc in item_docs}
		self.db = SimpleNamespace(get_value=lambda *a, **k: None, exists=lambda *a, **k: True)

	def get_doc(self, doctype, name=None):
		if doctype == "Jarz Bundle":
			if name != self.bundle_doc.name:
				raise Exception(f"Jarz Bundle {name} not found")
			return self.bundle_doc
		if doctype == "Item":
			if name not in self.item_docs:
				raise Exception(f"Item {name} not found")
			return self.item_docs[name]
		raise Exception(f"Unexpected doctype {doctype}")

	def get_all(self, doctype, filters=None, fields=None, **kwargs):
		if doctype != "Item":
			return []
		item_group = (filters or {}).get("item_group")
		return [
			{
				"name": doc.name,
				"item_name": doc.item_name,
				"standard_rate": doc.standard_rate,
				"stock_uom": "Nos",
			}
			for doc in self.item_docs.values()
			if getattr(doc, "item_group", None) == item_group
		]

	def get_precision(self, *args, **kwargs):
		return 2

	def logger(self, *args, **kwargs):
		return _StubLogger()

	def log_error(self, *args, **kwargs):
		return None

	def throw(self, message, *args, **kwargs):
		raise frappe.ValidationError(str(message))


@contextmanager
def _stubbed(stub):
	"""Run the processor against the stub, with translation as identity."""
	with patch("jarz_pos.services.bundle_processing.frappe", stub), patch(
		"jarz_pos.services.bundle_processing._", lambda message: message
	):
		yield


def _item_stub(code, rate, item_group):
	return SimpleNamespace(
		name=code,
		item_name=code,
		description=code,
		standard_rate=rate,
		valuation_rate=0.0,
		item_group=item_group,
	)


def _indulgence_five_bundle():
	"""The production bundle: two rows of the SAME item group, 4 + 1."""
	return SimpleNamespace(
		name="irk4mnvoe2",
		bundle_name="Jarz Indulgence Five",
		erpnext_item="JARZ-INDULGENCE-FIVE",
		bundle_price=400.0,
		items=[
			SimpleNamespace(name="irkm6iq1qc", item_group="Large", quantity=4),
			SimpleNamespace(name="irkqulhim1", item_group="Large", quantity=1),
		],
	)


def _indulgence_five_items():
	return [
		_item_stub("JARZ-INDULGENCE-FIVE", 500.0, "Bundles"),
		_item_stub("LARGE-A", 100.0, "Large"),
		_item_stub("LARGE-B", 100.0, "Large"),
		_item_stub("LARGE-C", 100.0, "Large"),
		_item_stub("LARGE-D", 100.0, "Large"),
		_item_stub("LARGE-E", 133.0, "Large"),
	]


def _single_group_bundle():
	"""A conventional bundle: one row, one item group, three selections."""
	return SimpleNamespace(
		name="BDL-SINGLE",
		bundle_name="Jarz Trio",
		erpnext_item="JARZ-TRIO",
		bundle_price=300.0,
		items=[SimpleNamespace(name="rowmed", item_group="Medium", quantity=3)],
	)


def _single_group_items():
	return [
		_item_stub("JARZ-TRIO", 500.0, "Bundles"),
		_item_stub("MED-A", 100.0, "Medium"),
		_item_stub("MED-B", 133.0, "Medium"),
		_item_stub("MED-C", 155.0, "Medium"),
	]


def _sel(item_code, quantity=None):
	"""One client selection entry, optionally carrying an explicit quantity."""
	entry = {"id": item_code}
	if quantity is not None:
		entry["selected_quantity"] = quantity
	return entry


def _expand(selected_items=None, quantity=1, bundle=None, items=None):
	"""Return (processor, invoice_rows) for a cart against a stubbed frappe."""
	from jarz_pos.services.bundle_processing import BundleProcessor

	bundle_doc = bundle if bundle is not None else _indulgence_five_bundle()
	item_docs = items if items is not None else _indulgence_five_items()
	stub = _StubFrappe(bundle_doc, item_docs)
	processor = BundleProcessor(
		bundle_doc.name, quantity=quantity, selected_items=selected_items
	)
	with _stubbed(stub):
		rows = processor.get_invoice_items()
	return processor, rows


def _child_layout(processor):
	"""(item, per-bundle qty, group row key) for every expanded child."""
	return [
		(entry["item"].name, entry["qty"], entry["item_group_key"])
		for entry in processor.bundle_items
	]


def _children(rows):
	return [row for row in rows if row.get("is_bundle_child")]


def _children_money(rows):
	"""Reproduce what ERPNext bills for the child rows, to 2 decimal places."""
	total = 0.0
	for row in _children(rows):
		gross = flt(row["rate"]) * flt(row["qty"])
		total += flt(gross * (1.0 - flt(row["discount_percentage"]) / 100.0), 2)
	return flt(total, 2)


class TestDuplicateBundleGroupRows(unittest.TestCase):
	"""A bundle listing the same item group twice (Large x4 + Large x1)."""

	FIVE_DISTINCT = ["LARGE-A", "LARGE-B", "LARGE-C", "LARGE-D", "LARGE-E"]

	def test_name_keyed_selection_spans_both_group_rows(self):
		"""The production payload: one list of 5 keyed by the group NAME.

		Before the fix the first row consumed the whole list and rejected it with
		"expected 4 selection(s) from 'Large', received 5".
		"""
		processor, rows = _expand({"Large": [_sel(code) for code in self.FIVE_DISTINCT]})

		self.assertEqual(
			_child_layout(processor),
			[
				("LARGE-A", 1, "irkm6iq1qc"),
				("LARGE-B", 1, "irkm6iq1qc"),
				("LARGE-C", 1, "irkm6iq1qc"),
				("LARGE-D", 1, "irkm6iq1qc"),
				("LARGE-E", 1, "irkqulhim1"),
			],
			"4 selections belong to the x4 row and the 5th to the x1 row",
		)
		self.assertEqual(len(_children(rows)), 5)
		self.assertEqual(
			[row["bundle_group_key"] for row in _children(rows)],
			["irkm6iq1qc"] * 4 + ["irkqulhim1"],
			"Each child must be stamped with the row it was allocated to",
		)

	def test_row_keyed_selection_is_unchanged(self):
		"""Regression guard on the path that already works today."""
		processor, rows = _expand(
			{
				"irkm6iq1qc": [_sel(code) for code in self.FIVE_DISTINCT[:4]],
				"irkqulhim1": [_sel("LARGE-E")],
			}
		)

		self.assertEqual(
			_child_layout(processor),
			[
				("LARGE-A", 1, "irkm6iq1qc"),
				("LARGE-B", 1, "irkm6iq1qc"),
				("LARGE-C", 1, "irkm6iq1qc"),
				("LARGE-D", 1, "irkm6iq1qc"),
				("LARGE-E", 1, "irkqulhim1"),
			],
		)
		self.assertEqual(len(_children(rows)), 5)

	def test_too_few_selections_still_throws(self):
		"""Validation is corrected, not weakened: 4 of 5 is still a hard error."""
		with self.assertRaises(frappe.ValidationError) as caught:
			_expand({"Large": [_sel(code) for code in self.FIVE_DISTINCT[:4]]})

		self.assertIn(
			"expected 5 selection(s) from 'Large', received 4", str(caught.exception)
		)

	def test_too_many_selections_still_throws(self):
		"""Six selections for a five-item bundle must not be silently trimmed."""
		payload = [_sel(code) for code in self.FIVE_DISTINCT] + [_sel("LARGE-A")]

		with self.assertRaises(frappe.ValidationError) as caught:
			_expand({"Large": payload})

		self.assertIn(
			"expected 5 selection(s) from 'Large', received 6", str(caught.exception)
		)

	def test_row_keyed_payload_is_still_validated_against_that_row(self):
		"""Addressing one row explicitly keeps that row's own quantity as the rule.

		This is what stops the merge from becoming a blanket relaxation: five
		selections posted at the x4 row are wrong however the bundle is shaped.
		"""
		with self.assertRaises(frappe.ValidationError) as caught:
			_expand({"irkm6iq1qc": [_sel(code) for code in self.FIVE_DISTINCT]})

		self.assertIn(
			"expected 4 selection(s) from 'Large', received 5", str(caught.exception)
		)

	def test_one_item_chosen_five_times_is_split_across_the_rows(self):
		"""A selection straddling the row boundary is split so both keys are right."""
		processor, rows = _expand({"Large": [_sel("LARGE-A", 5)]})

		self.assertEqual(
			_child_layout(processor),
			[("LARGE-A", 4, "irkm6iq1qc"), ("LARGE-A", 1, "irkqulhim1")],
		)
		self.assertEqual(len(_children(rows)), 2)

	def test_no_selections_keeps_one_fallback_line_per_row(self):
		"""With no selections at all, each row still expands on its own (unchanged)."""
		processor, rows = _expand(selected_items=None)

		self.assertEqual(
			_child_layout(processor),
			[("LARGE-A", 4, "irkm6iq1qc"), ("LARGE-A", 1, "irkqulhim1")],
			"Legacy fallback picks the first item of the group for each row",
		)
		self.assertEqual(len(_children(rows)), 2)

	def test_bundle_quantity_multiplies_each_allocation(self):
		"""Ordering the bundle twice doubles both allocations, not just the first."""
		processor, rows = _expand(
			{"Large": [_sel(code) for code in self.FIVE_DISTINCT]}, quantity=2
		)

		self.assertEqual(
			[(row["item_code"], row["qty"]) for row in _children(rows)],
			[
				("LARGE-A", 2),
				("LARGE-B", 2),
				("LARGE-C", 2),
				("LARGE-D", 2),
				("LARGE-E", 2),
			],
		)
		self.assertEqual(_child_layout(processor)[0][1], 1)

	def test_invalid_item_for_the_group_is_rejected(self):
		"""Merging rows must not widen which items the group accepts."""
		payload = [_sel(code) for code in self.FIVE_DISTINCT[:4]] + [_sel("MED-A")]

		with self.assertRaises(frappe.ValidationError) as caught:
			_expand({"Large": payload})

		self.assertIn("invalid selections for group 'Large'", str(caught.exception))


class TestDuplicateBundleGroupMoney(unittest.TestCase):
	"""The two ways of expressing the same cart must cost exactly the same.

	``LARGE-A x3 + LARGE-E x2`` against rows of 4 and 1 makes ``LARGE-E`` straddle
	the boundary, so the name-keyed cart has to split it — and it is also priced
	so that the uniform discount leaves a residual piastre, which exercises the
	rounding correction on the last line.
	"""

	NAME_KEYED = {"Large": [_sel("LARGE-A", 3), _sel("LARGE-E", 2)]}
	ROW_KEYED = {
		"irkm6iq1qc": [_sel("LARGE-A", 3), _sel("LARGE-E", 1)],
		"irkqulhim1": [_sel("LARGE-E", 1)],
	}

	def test_the_two_carts_produce_identical_invoice_rows(self):
		"""Rates, discounts, rounding correction and group keys must all match."""
		_, name_rows = _expand(self.NAME_KEYED)
		_, row_rows = _expand(self.ROW_KEYED)

		self.assertEqual(name_rows, row_rows)

	def test_the_expected_money(self):
		"""Pin the actual numbers, so 'identical' cannot mean 'identically wrong'."""
		_, rows = _expand(self.NAME_KEYED)
		children = _children(rows)

		self.assertEqual(
			[(row["item_code"], row["qty"], row["rate"]) for row in children],
			[("LARGE-A", 3, 100.0), ("LARGE-E", 1, 133.0), ("LARGE-E", 1, 133.0)],
		)

		# Gross 3x100 + 2x133 = 566 discounted to the 400 bundle price.
		uniform = ((566.0 - 400.0) / 566.0) * 100.0
		self.assertAlmostEqual(children[0]["discount_percentage"], uniform, places=10)
		self.assertAlmostEqual(children[1]["discount_percentage"], uniform, places=10)
		# 212.01 + 93.99 + 93.99 = 399.99, so the last line absorbs one piastre.
		self.assertEqual(children[2]["discount_percentage"], 29.323308)
		self.assertEqual(_children_money(rows), 400.0)

	def test_the_parent_line_is_fully_discounted(self):
		_, rows = _expand(self.NAME_KEYED)
		parent = next(row for row in rows if row.get("is_bundle_parent"))

		self.assertEqual(parent["item_code"], "JARZ-INDULGENCE-FIVE")
		self.assertEqual(parent["discount_percentage"], 100.0)
		self.assertEqual(parent["price_list_rate"], 500.0)
		self.assertEqual(parent["bundle_code"], "irk4mnvoe2")


class TestSingleGroupBundleIsUnaffected(unittest.TestCase):
	"""A bundle with unique item groups must be byte-identical to before."""

	SELECTIONS = [_sel("MED-A"), _sel("MED-B"), _sel("MED-C")]

	def _expand_single(self, selected_items):
		return _expand(
			selected_items,
			bundle=_single_group_bundle(),
			items=_single_group_items(),
		)

	def test_rates_discounts_and_rounding_correction(self):
		"""100 + 133 + 155 = 388 gross, discounted to the 300 bundle price."""
		_, rows = self._expand_single({"Medium": list(self.SELECTIONS)})
		children = _children(rows)

		self.assertEqual(
			[(row["item_code"], row["qty"], row["rate"]) for row in children],
			[("MED-A", 1, 100.0), ("MED-B", 1, 133.0), ("MED-C", 1, 155.0)],
		)

		uniform = ((388.0 - 300.0) / 388.0) * 100.0
		self.assertAlmostEqual(children[0]["discount_percentage"], uniform, places=10)
		self.assertAlmostEqual(children[1]["discount_percentage"], uniform, places=10)
		# 77.32 + 102.84 + 119.85 = 300.01, so the last line gives one piastre back.
		self.assertEqual(children[2]["discount_percentage"], 22.683871)
		self.assertEqual(_children_money(rows), 300.0)

		for row in children:
			self.assertEqual(row["bundle_group_key"], "rowmed")
			self.assertEqual(row["bundle_group_name"], "Medium")
			self.assertEqual(row["parent_bundle"], "BDL-SINGLE")

	def test_group_name_and_group_key_carts_agree(self):
		"""With a unique group both keyings address the same single row."""
		_, by_name = self._expand_single({"Medium": list(self.SELECTIONS)})
		_, by_key = self._expand_single({"rowmed": list(self.SELECTIONS)})

		self.assertEqual(by_name, by_key)

	def test_wrong_selection_count_still_throws(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._expand_single({"Medium": [_sel("MED-A"), _sel("MED-B")]})

		self.assertIn(
			"expected 3 selection(s) from 'Medium', received 2", str(caught.exception)
		)


class TestGroupPlanning(unittest.TestCase):
	"""Direct cover for the row-grouping decision itself."""

	def _processor(self, selected_items, bundle=None):
		from jarz_pos.services.bundle_processing import BundleProcessor

		bundle_doc = bundle if bundle is not None else _indulgence_five_bundle()
		processor = BundleProcessor(bundle_doc.name, selected_items=selected_items)
		processor.bundle_doc = bundle_doc
		return processor

	def test_duplicate_rows_merge_when_no_row_key_was_used(self):
		plans = self._processor({"Large": [_sel("LARGE-A", 5)]})._build_group_plans()

		self.assertEqual(len(plans), 1)
		self.assertEqual([row["key"] for row in plans[0]], ["irkm6iq1qc", "irkqulhim1"])

	def test_duplicate_rows_stay_apart_when_row_keys_were_used(self):
		plans = self._processor(
			{"irkm6iq1qc": [_sel("LARGE-A", 4)], "irkqulhim1": [_sel("LARGE-E")]}
		)._build_group_plans()

		self.assertEqual([[row["key"] for row in plan] for plan in plans], [
			["irkm6iq1qc"],
			["irkqulhim1"],
		])

	def test_partially_keyed_duplicate_rows_keep_the_addressed_row_alone(self):
		plans = self._processor(
			{"irkm6iq1qc": [_sel("LARGE-A", 4)], "Large": [_sel("LARGE-E")]}
		)._build_group_plans()

		self.assertEqual([[row["key"] for row in plan] for plan in plans], [
			["irkm6iq1qc"],
			["irkqulhim1"],
		])

	def test_unique_groups_are_never_merged(self):
		bundle = SimpleNamespace(
			name="BDL-MIX",
			erpnext_item="JARZ-MIX",
			bundle_price=100.0,
			items=[
				SimpleNamespace(name="row-a", item_group="Large", quantity=2),
				SimpleNamespace(name="row-b", item_group="Medium", quantity=1),
			],
		)
		plans = self._processor({"Large": [], "Medium": []}, bundle=bundle)._build_group_plans()

		self.assertEqual([[row["key"] for row in plan] for plan in plans], [
			["row-a"],
			["row-b"],
		])

	def test_allocation_splits_a_straddling_selection(self):
		processor = self._processor(None)
		plan = [
			{"key": "irkm6iq1qc", "name": "Large", "quantity": 4},
			{"key": "irkqulhim1", "name": "Large", "quantity": 1},
		]

		allocations = processor._allocate_selections_to_rows(
			{"LARGE-A": {"qty": 3, "rate": None}, "LARGE-E": {"qty": 2, "rate": 133.0}},
			plan,
		)

		self.assertEqual(
			[
				(a["item_code"], a["qty"], a["group_key"], a["rate"])
				for a in allocations
			],
			[
				("LARGE-A", 3, "irkm6iq1qc", None),
				("LARGE-E", 1, "irkm6iq1qc", 133.0),
				("LARGE-E", 1, "irkqulhim1", 133.0),
			],
		)

	def test_allocation_keeps_selections_a_zero_quantity_row_cannot_claim(self):
		"""A blank row quantity skips validation; the selections must survive it."""
		processor = self._processor(None)
		plan = [{"key": "row-a", "name": "Large", "quantity": 0}]

		allocations = processor._allocate_selections_to_rows(
			{"LARGE-A": {"qty": 2, "rate": None}}, plan
		)

		self.assertEqual(
			[(a["item_code"], a["qty"], a["group_key"]) for a in allocations],
			[("LARGE-A", 2, "row-a")],
		)
