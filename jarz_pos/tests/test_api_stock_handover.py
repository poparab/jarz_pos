"""Tests for the Stock Handover flow (``jarz_pos.services.stock_handover``).

Fully mocked: no DB row, no Jarz Stock Handover DocType (CI runs on the live
staging site before ``bench migrate`` has created it), no Stock Entry is posted.
The acting user is built directly as a ``_Actor`` so every rule -- shift-bound
branch sides, the "a proposer never confirms" rule, turn flipping, cancel
rules -- is exercised against plain objects.
"""

import unittest
from unittest.mock import patch

import frappe

from jarz_pos.api import transfer as transfer_api
from jarz_pos.services import stock_handover as svc
from jarz_pos.utils.access_control import ShiftRequiredError

FACTORY = "Finished Goods - J"
DOKKI = "Dokki - J"
ZAMALEK = "Zamalek - J"


def _sides():
	return svc._Sides(
		FACTORY,
		[
			{"name": "Dokki", "warehouse": DOKKI},
			{"name": "Zamalek", "warehouse": ZAMALEK},
		],
	)


def _branch_actor(user="dokki@jarz.test", shift=True, profile="Dokki"):
	return svc._Actor(
		user=user,
		roles=["Employee"],
		sides=_sides(),
		assigned_profiles=[profile],
		open_profiles=[profile] if shift else [],
		pos_profile=profile,
	)


def _factory_actor(user="factory@jarz.test"):
	return svc._Actor(
		user=user,
		roles=["Production Operator"],
		sides=_sides(),
		assigned_profiles=[],
		open_profiles=[],
	)


def _manager_actor(user="manager@jarz.test"):
	return svc._Actor(
		user=user,
		roles=["Stock Manager"],
		sides=_sides(),
		assigned_profiles=[],
		open_profiles=[],
	)


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class _FakeDoc:
	"""Just enough of a Document for the service: attributes, get/set/append/save."""

	def __init__(self, items=(), **kw):
		self.saved = 0
		self.items = [_Row(**i) for i in items]
		self.__dict__.update(kw)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def append(self, key, row):
		getattr(self, key).append(_Row(**row))

	def save(self, ignore_permissions=False):
		self.saved += 1


def _pending(declared_by, declared_side, from_wh=FACTORY, to_wh=DOKKI, qty=12.0, **extra):
	data = {
		"name": "HND-2026-00001",
		"status": svc.STATUS_PENDING,
		"from_warehouse": from_wh,
		"to_warehouse": to_wh,
		"declared_side": declared_side,
		"awaiting_side": svc._other_side(declared_side),
		"source": svc.SOURCE_BRANCH,
		"declared_by": declared_by,
		"declared_at": "2026-10-20 09:00:00",
		"last_proposed_by": declared_by,
		"last_proposed_at": "2026-10-20 09:00:00",
		"stock_entry": None,
		"reject_reason": None,
	}
	data.update(extra)
	items = [
		{"item_code": "JAR-L", "item_name": "Large Jar", "stock_uom": "Nos", "qty": qty, "declared_qty": qty}
	]
	return _FakeDoc(items=items, **data)


def _item_meta(codes):
	return {c: {"name": c, "item_name": c, "stock_uom": "Nos"} for c in codes}


class TestStockHandoverLines(unittest.TestCase):
	def test_zero_lines_are_dropped_and_repeats_summed(self):
		merged = svc._normalize_lines(
			[
				{"item_code": "JAR-L", "qty": 0},
				{"item_code": "JAR-M", "qty": 3},
				{"item_code": "JAR-M", "qty": "2"},
				{"item_code": "", "qty": 0},
			]
		)
		self.assertEqual(merged, {"JAR-M": 5.0})

	def test_json_string_lines_are_accepted(self):
		self.assertEqual(svc._normalize_lines('[{"item_code": "JAR-S", "qty": 4}]'), {"JAR-S": 4.0})

	def test_all_zero_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_lines([{"item_code": "JAR-L", "qty": 0}])

	def test_non_finite_qty_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_lines([{"item_code": "JAR-L", "qty": float("nan")}])
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_lines([{"item_code": "JAR-L", "qty": float("inf")}])

	def test_positive_qty_without_item_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_lines([{"item_code": "", "qty": 2}])


class TestStockHandoverDeclare(unittest.TestCase):
	def _declare(self, actor, direction, my=DOKKI, other=FACTORY, lines=None):
		captured = {}

		def fake_insert(**kwargs):
			captured.update(kwargs)
			return _pending(
				kwargs["user"],
				kwargs["declared_side"],
				from_wh=kwargs["from_warehouse"],
				to_wh=kwargs["to_warehouse"],
				qty=next(iter(kwargs["lines"].values())),
			)

		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_allowed_item_codes", return_value={"JAR-L", "JAR-M"}),
			patch.object(svc, "_insert_handover", side_effect=fake_insert),
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_user_names", return_value={}),
		):
			result = svc.declare(
				my,
				other,
				direction,
				lines if lines is not None else [{"item_code": "JAR-L", "qty": 12}],
				pos_profile=actor.pos_profile,
			)
		return result, captured, push

	def test_in_means_from_counterpart_to_me(self):
		result, captured, push = self._declare(_branch_actor(), "in")
		self.assertTrue(result["ok"])
		self.assertEqual(captured["from_warehouse"], FACTORY)
		self.assertEqual(captured["to_warehouse"], DOKKI)
		self.assertEqual(captured["declared_side"], svc.SIDE_TO)
		self.assertEqual(captured["source"], svc.SOURCE_BRANCH)
		self.assertEqual(captured["pos_profile"], "Dokki")
		self.assertEqual(result["handover"]["awaiting_side"], svc.SIDE_FROM)
		self.assertEqual(result["handover"]["awaiting_label"], svc.FACTORY_LABEL)
		self.assertEqual(result["handover"]["to_label"], "Dokki")
		push.assert_called_once()
		self.assertEqual(push.call_args.args[1], svc.EVENT_DECLARED)

	def test_out_means_from_me_to_counterpart(self):
		_result, captured, _push = self._declare(_branch_actor(), "out")
		self.assertEqual(captured["from_warehouse"], DOKKI)
		self.assertEqual(captured["to_warehouse"], FACTORY)
		self.assertEqual(captured["declared_side"], svc.SIDE_FROM)

	def test_factory_declaration_by_production(self):
		_result, captured, _push = self._declare(_factory_actor(), "out", my=FACTORY, other=DOKKI)
		self.assertEqual(captured["from_warehouse"], FACTORY)
		self.assertEqual(captured["to_warehouse"], DOKKI)
		self.assertEqual(captured["source"], svc.SOURCE_FACTORY)

	def test_zero_lines_dropped_on_declare(self):
		_result, captured, _push = self._declare(
			_branch_actor(),
			"in",
			lines=[{"item_code": "JAR-L", "qty": 0}, {"item_code": "JAR-M", "qty": 6}],
		)
		self.assertEqual(captured["lines"], {"JAR-M": 6.0})

	def test_item_outside_the_allowed_list_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._declare(_branch_actor(), "in", lines=[{"item_code": "FLOUR", "qty": 1}])

	def test_bad_direction_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._declare(_branch_actor(), "sideways")

	def test_branch_user_without_an_open_shift_is_refused(self):
		with self.assertRaises(ShiftRequiredError):
			self._declare(_branch_actor(shift=False), "in")

	def test_branch_user_cannot_declare_for_another_branch(self):
		with self.assertRaises(frappe.PermissionError):
			self._declare(_branch_actor(), "in", my=ZAMALEK, other=FACTORY)

	def test_branch_user_cannot_declare_for_the_factory(self):
		with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			self._declare(_branch_actor(), "out", my=FACTORY, other=DOKKI)

	def test_insert_sets_awaiting_to_the_other_side(self):
		built = {}

		class _Inserted:
			name = "HND-2026-00009"

			def insert(self, ignore_permissions=False):
				built["ignore_permissions"] = ignore_permissions

		def fake_get_doc(payload):
			built["payload"] = payload
			return _Inserted()

		with (
			patch.object(svc.frappe, "get_doc", side_effect=fake_get_doc),
			patch.object(svc, "_item_meta", side_effect=_item_meta),
			patch.object(svc, "_warehouse_company", return_value="JARZ"),
		):
			svc._insert_handover(
				from_warehouse=FACTORY,
				to_warehouse=DOKKI,
				declared_side=svc.SIDE_TO,
				source=svc.SOURCE_BRANCH,
				lines={"JAR-L": 12.0},
				user="dokki@jarz.test",
			)
		payload = built["payload"]
		self.assertEqual(payload["awaiting_side"], svc.SIDE_FROM)
		self.assertEqual(payload["status"], svc.STATUS_PENDING)
		self.assertEqual(payload["last_proposed_by"], "dokki@jarz.test")
		self.assertEqual(payload["items"][0]["qty"], 12.0)
		self.assertEqual(payload["items"][0]["declared_qty"], 12.0)
		self.assertTrue(built["ignore_permissions"])


class TestStockHandoverRespond(unittest.TestCase):
	def _respond(self, actor, doc, action, **kwargs):
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_lock", return_value=doc),
			patch.object(svc, "_allowed_item_codes", return_value={"JAR-L", "JAR-M"}),
			patch.object(svc, "_item_meta", side_effect=_item_meta),
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_user_names", return_value={}),
		):
			result = svc.respond(doc.name, action, **kwargs)
		return result, push

	def test_declarer_cannot_confirm_their_own_declaration(self):
		manager = _manager_actor()
		doc = _pending(manager.user, svc.SIDE_FROM)
		with patch.object(svc, "_confirm") as confirm:
			with self.assertRaises(frappe.PermissionError):
				self._respond(manager, doc, "confirm")
		confirm.assert_not_called()
		self.assertEqual(doc.status, svc.STATUS_PENDING)

	def test_change_flips_the_awaiting_side(self):
		# Dokki declared "I received 12 from Factory"; the factory says it was 10.
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		factory = _factory_actor()
		result, push = self._respond(factory, doc, "change", lines=[{"item_code": "JAR-L", "qty": 10}])
		self.assertEqual(doc.awaiting_side, svc.SIDE_TO)
		self.assertEqual(doc.last_proposed_by, factory.user)
		self.assertEqual(doc.items[0].qty, 10.0)
		self.assertEqual(doc.items[0].declared_qty, 12.0)
		self.assertEqual(doc.status, svc.STATUS_PENDING)
		self.assertEqual(doc.saved, 1)
		self.assertTrue(result["handover"]["changed"])
		# The factory proposed last, so it cannot answer its own change...
		self.assertFalse(result["handover"]["can_respond"])
		push.assert_called_once()
		self.assertEqual(push.call_args.args[1], svc.EVENT_CHANGED)

	def test_after_a_change_the_declarer_can_answer_but_not_cancel(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		self._respond(_factory_actor(), doc, "change", lines=[{"item_code": "JAR-L", "qty": 10}])
		dokki = _branch_actor()
		self.assertTrue(svc._can_respond(doc, dokki))
		self.assertFalse(svc._can_cancel(doc, list(doc.items), dokki))

	def test_change_with_the_same_quantities_is_refused(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		with self.assertRaises(frappe.ValidationError):
			self._respond(_factory_actor(), doc, "change", lines=[{"item_code": "JAR-L", "qty": 12}])

	def test_confirm_posts_the_stock_entry(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		factory = _factory_actor()

		class _SE:
			name = "MAT-STE-2026-00001"

		with (
			patch.object(transfer_api, "_build_material_transfer", return_value=_SE()) as build,
			patch.object(transfer_api, "_insert_and_submit_stock_entry") as post,
			patch.object(svc.frappe.db, "savepoint"),
		):
			result, push = self._respond(factory, doc, "confirm")
		build.assert_called_once()
		self.assertEqual(build.call_args.args[0], FACTORY)
		self.assertEqual(build.call_args.args[1], DOKKI)
		self.assertEqual(build.call_args.args[2], [{"item_code": "JAR-L", "qty": 12.0}])
		self.assertIsNone(build.call_args.kwargs.get("posting_date"))
		post.assert_called_once()
		self.assertEqual(doc.status, svc.STATUS_CONFIRMED)
		self.assertEqual(doc.stock_entry, "MAT-STE-2026-00001")
		self.assertEqual(doc.confirmed_by, factory.user)
		self.assertEqual(result["handover"]["stock_entry"], "MAT-STE-2026-00001")
		self.assertEqual(push.call_args.args[1], svc.EVENT_CONFIRMED)

	def test_failed_stock_entry_keeps_it_pending_and_names_the_shortage(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)

		class _SE:
			name = None

		with (
			patch.object(transfer_api, "_build_material_transfer", return_value=_SE()),
			patch.object(
				transfer_api, "_insert_and_submit_stock_entry", side_effect=frappe.ValidationError("negative stock")
			),
			patch.object(svc.frappe.db, "savepoint"),
			patch.object(svc.frappe.db, "rollback") as rollback,
			patch.object(svc.frappe.db, "get_value", return_value=3),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._respond(_factory_actor(), doc, "confirm")
		rollback.assert_called_once()
		self.assertEqual(doc.status, svc.STATUS_PENDING)
		self.assertIsNone(doc.stock_entry)
		self.assertEqual(doc.saved, 0)
		message = str(ctx.exception)
		self.assertIn("Large Jar", message)
		self.assertIn("3", message)

	def test_reject_sets_rejected_with_reason(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		_result, push = self._respond(_factory_actor(), doc, "reject", reason="  Never sent  ")
		self.assertEqual(doc.status, svc.STATUS_REJECTED)
		self.assertEqual(doc.reject_reason, "Never sent")
		self.assertEqual(push.call_args.args[1], svc.EVENT_REJECTED)

	def test_cannot_answer_a_closed_handover(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO, status=svc.STATUS_CONFIRMED)
		with self.assertRaises(frappe.ValidationError):
			self._respond(_factory_actor(), doc, "reject")

	def test_not_my_turn_is_refused(self):
		# Awaiting the factory; another branch cannot answer it.
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			self._respond(_branch_actor(user="zamalek@jarz.test", profile="Zamalek"), doc, "confirm")
		self.assertEqual(doc.status, svc.STATUS_PENDING)

	def test_awaiting_branch_without_a_shift_gets_shift_required(self):
		# The factory declared "Dokki took 12"; Dokki's user has no open shift.
		doc = _pending("factory@jarz.test", svc.SIDE_FROM)
		with self.assertRaises(ShiftRequiredError):
			self._respond(_branch_actor(shift=False), doc, "confirm")


class TestStockHandoverCancel(unittest.TestCase):
	def _cancel(self, actor, doc):
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_lock", return_value=doc),
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_user_names", return_value={}),
		):
			result = svc.cancel(doc.name)
		return result, push

	def test_declarer_side_cancels_while_unchanged(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		result, push = self._cancel(_branch_actor(), doc)
		self.assertEqual(doc.status, svc.STATUS_CANCELLED)
		self.assertEqual(doc.cancelled_by, "dokki@jarz.test")
		self.assertEqual(result["handover"]["status"], svc.STATUS_CANCELLED)
		self.assertEqual(push.call_args.args[1], svc.EVENT_CANCELLED)

	def test_the_other_side_cannot_cancel(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		with self.assertRaises(frappe.PermissionError):
			self._cancel(_factory_actor(), doc)
		self.assertEqual(doc.status, svc.STATUS_PENDING)

	def test_cannot_cancel_once_changed(self):
		doc = _pending(
			"dokki@jarz.test",
			svc.SIDE_TO,
			awaiting_side=svc.SIDE_TO,
			last_proposed_by="factory@jarz.test",
		)
		doc.items[0].qty = 10.0
		with self.assertRaises(frappe.ValidationError):
			self._cancel(_branch_actor(), doc)
		self.assertEqual(doc.status, svc.STATUS_PENDING)

	def test_cannot_cancel_a_closed_handover(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO, status=svc.STATUS_REJECTED)
		with self.assertRaises(frappe.ValidationError):
			self._cancel(_branch_actor(), doc)


class TestStockHandoverContext(unittest.TestCase):
	def test_branch_side_only_with_an_open_shift(self):
		with patch.object(svc, "_build_actor", return_value=_branch_actor()):
			ctx = svc.get_context("Dokki")
		self.assertEqual(
			ctx["my_sides"], [{"warehouse": DOKKI, "label": "Dokki", "kind": "branch", "pos_profile": "Dokki"}]
		)
		self.assertFalse(ctx["can_manage"])
		self.assertEqual([c["warehouse"] for c in ctx["counterparts"]], [FACTORY, DOKKI, ZAMALEK])

		with patch.object(svc, "_build_actor", return_value=_branch_actor(shift=False)):
			ctx = svc.get_context("Dokki")
		self.assertEqual(ctx["my_sides"], [])

	def test_production_gets_the_factory_side(self):
		with patch.object(svc, "_build_actor", return_value=_factory_actor()):
			ctx = svc.get_context()
		self.assertEqual(
			ctx["my_sides"],
			[{"warehouse": FACTORY, "label": "Factory", "kind": "factory", "pos_profile": None}],
		)


class TestStockHandoverPush(unittest.TestCase):
	def test_enqueue_uses_kwargs_that_frappe_enqueue_does_not_swallow(self):
		with (
			patch.object(svc, "_suppressed", return_value=False),
			patch.object(svc.frappe, "enqueue") as enqueue,
		):
			self.assertTrue(svc._enqueue_push("HND-2026-00001", svc.EVENT_DECLARED, "dokki@jarz.test"))
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["handover_name"], "HND-2026-00001")
		self.assertEqual(kwargs["kind"], svc.EVENT_DECLARED)
		self.assertEqual(kwargs["actor_user"], "dokki@jarz.test")
		self.assertTrue(kwargs["enqueue_after_commit"])
		for reserved in ("event", "job_name", "timeout", "now"):
			self.assertNotIn(reserved, kwargs)

	def test_push_goes_to_the_awaiting_side_after_declare_and_the_proposer_after_confirm(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		self.assertEqual(svc._notify_side(doc, svc.EVENT_DECLARED), FACTORY)
		self.assertEqual(svc._notify_side(doc, svc.EVENT_CONFIRMED), DOKKI)

	def test_push_body_is_arabic_first(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		title, body = svc._push_text(doc, svc.EVENT_DECLARED, _sides(), None)
		self.assertTrue(title)
		arabic, english = body.split("\n")
		self.assertIn("Dokki says it received 12 jars from Factory", english)
		self.assertTrue(any("؀" <= ch <= "ۿ" for ch in arabic))

	def test_notify_never_raises(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		with (
			patch.object(svc, "_suppressed", return_value=False),
			patch.object(svc, "_load_sides", side_effect=Exception("db down")),
			patch.object(svc, "_safe_log"),
		):
			result = svc.notify_stock_handover(doc, svc.EVENT_DECLARED, "dokki@jarz.test")
		self.assertFalse(result["ok"])


class TestSubmitTransferRouting(unittest.TestCase):
	def test_transfer_touching_a_branch_becomes_a_pending_handover(self):
		with (
			patch.object(transfer_api, "_ensure_transfer_access"),
			patch.object(svc, "touches_branch", return_value=True),
			patch.object(svc, "create_manager_handover", return_value="HND-2026-00002") as create,
			patch.object(transfer_api, "_insert_and_submit_stock_entry") as post,
		):
			result = transfer_api.submit_transfer(
				source_warehouse=FACTORY,
				target_warehouse=DOKKI,
				lines=[{"item_code": "JAR-L", "qty": 5}],
			)
		self.assertEqual(
			result, {"ok": True, "stock_entry": None, "handover": "HND-2026-00002", "pending": True}
		)
		create.assert_called_once_with(FACTORY, DOKKI, [{"item_code": "JAR-L", "qty": 5.0}])
		post.assert_not_called()

	def test_transfer_touching_no_branch_still_posts_immediately(self):
		class _SE:
			name = "MAT-STE-2026-00002"

		with (
			patch.object(transfer_api, "_ensure_transfer_access"),
			patch.object(svc, "touches_branch", return_value=False),
			patch.object(svc, "create_manager_handover") as create,
			patch.object(transfer_api, "_build_material_transfer", return_value=_SE()),
			patch.object(transfer_api, "_insert_and_submit_stock_entry") as post,
			patch.object(transfer_api.frappe.db, "commit"),
		):
			result = transfer_api.submit_transfer(
				source_warehouse=FACTORY,
				target_warehouse="Stores - J",
				lines=[{"item_code": "JAR-L", "qty": 5}],
			)
		self.assertEqual(result, {"ok": True, "stock_entry": "MAT-STE-2026-00002"})
		create.assert_not_called()
		post.assert_called_once()

	def test_touches_branch_is_false_before_migrate(self):
		with (
			patch.object(svc, "_doctype_ready", return_value=False),
			patch.object(svc, "_load_sides") as load,
		):
			self.assertFalse(svc.touches_branch(FACTORY, DOKKI))
		load.assert_not_called()

	def test_touches_branch_reads_pos_profile_warehouses(self):
		with (
			patch.object(svc, "_doctype_ready", return_value=True),
			patch.object(svc, "_load_sides", return_value=_sides()),
		):
			self.assertTrue(svc.touches_branch(FACTORY, DOKKI))
			self.assertTrue(svc.touches_branch(ZAMALEK, "Stores - J"))
			self.assertFalse(svc.touches_branch(FACTORY, "Stores - J"))


if __name__ == "__main__":
	unittest.main()
