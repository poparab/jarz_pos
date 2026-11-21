"""Carton plan on a pending Jarz Stock Handover (factory -> branch send).

Fully mocked: no DB, no DocType. Pins that the plan rides submit_transfer onto the
handover path only, is dropped (never fails the transfer) when malformed or
oversized, and that ``_serialize`` returns the parsed dict or None.
"""

import json
import types
import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import transfer as transfer_api
from jarz_pos.services import stock_handover as svc

FACTORY = "Finished Goods - J"
DOKKI = "Dokki - J"
PLAN = {
	"version": 1,
	"total_cartons": 1,
	"total_jars": 20,
	"large": 1,
	"b2b": 0,
	"cartons": [
		{"number": 1, "type": "large", "jars": 20, "lines": [{"item_code": "Lotus", "item_name": "Lotus", "qty": 20}]}
	],
	"unpacked": [],
}
COMPACT = json.dumps(PLAN, separators=(",", ":"), ensure_ascii=False)
LINES = [{"item_code": "JAR-L", "qty": 5}]


def _send(plan, branch=True, direct=0):
	with (
		patch.object(transfer_api, "_ensure_transfer_access"),
		patch.object(transfer_api, "_claim_transfer", return_value=None),
		patch.object(transfer_api, "_record_transfer_claim"),
		patch.object(transfer_api, "can_post_direct", return_value=True),
		patch.object(svc, "touches_branch", return_value=branch),
		patch.object(svc, "create_manager_handover", return_value="HND-1") as create,
		patch.object(transfer_api, "_build_material_transfer"),
		patch.object(transfer_api, "_insert_and_submit_stock_entry"),
		patch.object(transfer_api.frappe.db, "commit"),
	):
		result = transfer_api.submit_transfer(FACTORY, DOKKI, LINES, direct=direct, carton_plan=plan)
	return result, create


class TestSubmitTransferCartonPlan(unittest.TestCase):
	def test_dict_plan_is_stored_as_compact_json(self):
		result, create = _send(PLAN)
		self.assertTrue(result["pending"])
		create.assert_called_once_with(FACTORY, DOKKI, [{"item_code": "JAR-L", "qty": 5.0}], carton_plan=COMPACT)

	def test_string_plan_is_reencoded_compact(self):
		_result, create = _send(json.dumps(PLAN, indent=2))
		self.assertEqual(create.call_args.kwargs["carton_plan"], COMPACT)

	def test_bad_plans_are_dropped_without_failing(self):
		huge = {"version": 1, "pad": "x" * 20001}
		for bad in ("not json", "[1,2]", "", [1], 5, huge, json.dumps(huge)):
			result, create = _send(bad)
			self.assertTrue(result["pending"], bad)
			create.assert_called_once_with(FACTORY, DOKKI, [{"item_code": "JAR-L", "qty": 5.0}])

	def test_direct_and_non_branch_paths_ignore_the_plan(self):
		for branch, direct in ((True, 1), (False, 0)):
			result, create = _send(PLAN, branch=branch, direct=direct)
			self.assertNotIn("pending", result)
			create.assert_not_called()


class TestHandoverCartonPlanService(unittest.TestCase):
	def _insert(self, has_column, plan):
		doc = MagicMock()
		doc.name = "HND-1"
		with (
			patch.object(svc.frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(svc, "_has_carton_plan_column", return_value=has_column),
			patch.object(svc, "_warehouse_company", return_value="J"),
			patch.object(svc, "_item_child_rows", return_value=[]),
			patch.object(svc, "_now", return_value=None),
		):
			svc._insert_handover(
				from_warehouse=FACTORY,
				to_warehouse=DOKKI,
				declared_side=svc.SIDE_FROM,
				source=svc.SOURCE_MANAGER,
				lines={},
				user="a@b.c",
				carton_plan=plan,
			)
		return get_doc.call_args.args[0]

	def test_insert_sets_the_field_when_the_column_exists(self):
		self.assertEqual(self._insert(True, COMPACT)["carton_plan"], COMPACT)

	def test_insert_skips_the_field_before_migrate_or_without_plan(self):
		self.assertNotIn("carton_plan", self._insert(False, COMPACT))
		self.assertNotIn("carton_plan", self._insert(True, None))

	def test_create_manager_handover_passes_the_plan_down(self):
		doc = types.SimpleNamespace(name="HND-1")
		with (
			patch.object(svc, "_insert_handover", return_value=doc) as insert,
			patch.object(svc, "_enqueue_push"),
		):
			svc.create_manager_handover(FACTORY, DOKKI, LINES, carton_plan=COMPACT)
		self.assertEqual(insert.call_args.kwargs["carton_plan"], COMPACT)

	def test_list_fields_include_the_column_only_when_it_exists(self):
		with patch.object(svc, "_has_carton_plan_column", return_value=True):
			self.assertIn("carton_plan", svc._list_fields())
		with patch.object(svc, "_has_carton_plan_column", return_value=False):
			self.assertNotIn("carton_plan", svc._list_fields())


class TestSerializeCartonPlan(unittest.TestCase):
	def _out(self, stored):
		row = {
			"name": "HND-1",
			"status": svc.STATUS_PENDING,
			"from_warehouse": FACTORY,
			"to_warehouse": DOKKI,
			"carton_plan": stored,
		}
		actor = MagicMock()
		actor.sides.label.return_value = "x"
		actor.can_act_for.return_value = False
		with patch.object(svc, "_is_changed", return_value=False):
			return svc._serialize(row, [], actor, {})

	def test_returns_the_parsed_dict(self):
		self.assertEqual(self._out(COMPACT)["carton_plan"], PLAN)

	def test_missing_or_bad_json_is_none(self):
		for stored in (None, "", "{oops", "[1]"):
			self.assertIsNone(self._out(stored)["carton_plan"], stored)


if __name__ == "__main__":
	unittest.main()
