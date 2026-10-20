"""Tests for the Stock Handover flow (``jarz_pos.services.stock_handover``).

Fully mocked: no DB row, no Jarz Stock Handover DocType (CI runs on the live
staging site before ``bench migrate`` has created it), no Stock Entry is posted.
The acting user is built directly as a ``_Actor`` so every rule -- shift-bound
branch sides, the "a proposer never confirms" rule, turn flipping, cancel
rules, the manager match of a sender's whole batch -- is exercised against
plain objects.
"""

import types
import unittest
from datetime import datetime
from unittest.mock import patch

import frappe

from jarz_pos.api import stock_handover as handover_api
from jarz_pos.api import transfer as transfer_api
from jarz_pos.services import stock_handover as svc
from jarz_pos.utils.access_control import ShiftRequiredError

FACTORY = "Finished Goods - J"
DOKKI = "Dokki - J"
ZAMALEK = "Zamalek - J"

# now_datetime() reads System Settings through frappe.client_cache / frappe.db, which several
# tests mock wholesale; on a cold timezone cache (bench run-tests) that mock answers instead.
_NOW_PATCH = patch.object(svc, "_now", return_value=datetime(2026, 10, 20, 12, 0, 0))


def setUpModule():
	_NOW_PATCH.start()


def tearDownModule():
	_NOW_PATCH.stop()


class NegativeStockError(frappe.ValidationError):
	"""Same class name as ERPNext's, which is what the service looks for."""


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
		self.flags = types.SimpleNamespace()
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
		"kind": svc.DOC_KIND_HANDOVER,
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
		"was_changed": 0,
		"match_note": None,
	}
	items = extra.pop("items", None)
	data.update(extra)
	if items is None:
		items = [
			{
				"item_code": "JAR-L",
				"item_name": "Large Jar",
				"stock_uom": "Nos",
				"qty": qty,
				"declared_qty": qty,
			}
		]
	return _FakeDoc(items=items, **data)


def _dispatch(declared_by, from_wh=DOKKI, qty=10.0, **extra):
	"""A "sent out" Dispatch: no destination, answered by a manager."""
	extra.setdefault("awaiting_side", svc.SIDE_MANAGER)
	return _pending(
		declared_by,
		svc.SIDE_FROM,
		from_wh=from_wh,
		to_wh=None,
		qty=qty,
		kind=svc.DOC_KIND_DISPATCH,
		**extra,
	)


def _receipt(declared_by, to_wh, from_wh=DOKKI, qty=6.0, **extra):
	"""A receiving side's "I got stock from <from_wh>" declaration."""
	return _pending(declared_by, svc.SIDE_TO, from_wh=from_wh, to_wh=to_wh, qty=qty, **extra)


def _item_meta(codes):
	return {c: {"name": c, "item_name": c, "stock_uom": "Nos"} for c in codes}


def _fake_lock_dispatches(sources):
	"""Stand-in for ``_lock_dispatches``: a Dispatch is pending for each warehouse in *sources*."""
	sources = set(sources)
	return lambda source: ["HND-D"] if source in sources else []


def _line(sent, **splits):
	"""A confirm_match line for JAR-L; ``splits`` maps a warehouse alias to its qty."""
	aliases = {"factory": FACTORY, "zamalek": ZAMALEK, "dokki": DOKKI}
	return {
		"item_code": "JAR-L",
		"sent": sent,
		"splits": [{"warehouse": aliases[k], "qty": v} for k, v in splits.items()],
	}


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

	def test_quantity_is_rounded_to_three_places_before_the_zero_check(self):
		# 0.0004 rounds to 0.000 and is dropped; 1.00049 rounds to 1.0.
		merged = svc._normalize_lines(
			[{"item_code": "JAR-L", "qty": 0.0004}, {"item_code": "JAR-M", "qty": 1.00049}]
		)
		self.assertEqual(merged, {"JAR-M": 1.0})
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_lines([{"item_code": "JAR-L", "qty": 0.0004}])


class TestStockHandoverDeclare(unittest.TestCase):
	def _declare(
		self,
		actor,
		direction,
		my=DOKKI,
		other=FACTORY,
		lines=None,
		dispatch_sources=(),
		client_request_id=None,
	):
		captured = {}

		def fake_insert(**kwargs):
			captured.update(kwargs)
			dispatch = kwargs.get("kind") == svc.DOC_KIND_DISPATCH
			return _pending(
				kwargs["user"],
				kwargs["declared_side"],
				from_wh=kwargs["from_warehouse"],
				to_wh=kwargs["to_warehouse"],
				qty=next(iter(kwargs["lines"].values())),
				kind=kwargs.get("kind", svc.DOC_KIND_HANDOVER),
				awaiting_side=svc.SIDE_MANAGER if dispatch else svc._other_side(kwargs["declared_side"]),
			)

		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_allowed_item_codes", return_value={"JAR-L", "JAR-M"}),
			patch.object(svc, "_dispatch_item_codes", return_value={"JAR-L", "JAR-M"}),
			patch.object(svc, "_insert_handover", side_effect=fake_insert),
			patch.object(svc, "_pending_dispatch_sources", return_value=set(dispatch_sources)),
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_maybe_push_balanced") as balanced,
			patch.object(svc, "_user_names", return_value={}),
			patch.object(svc.frappe, "get_all", return_value=[]),
		):
			result = svc.declare(
				my,
				other,
				direction,
				lines if lines is not None else [{"item_code": "JAR-L", "qty": 12}],
				pos_profile=actor.pos_profile,
				client_request_id=client_request_id,
			)
		self.balanced = balanced
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
		self.balanced.assert_not_called()

	def test_out_is_a_dispatch_with_no_destination(self):
		result, captured, push = self._declare(_branch_actor(), "out")
		self.assertEqual(captured["kind"], svc.DOC_KIND_DISPATCH)
		self.assertEqual(captured["from_warehouse"], DOKKI)
		self.assertIsNone(captured["to_warehouse"])
		self.assertEqual(captured["declared_side"], svc.SIDE_FROM)
		handover = result["handover"]
		self.assertEqual(handover["kind"], svc.DOC_KIND_DISPATCH)
		self.assertIsNone(handover["to_warehouse"])
		self.assertIsNone(handover["to_label"])
		self.assertEqual(handover["awaiting_side"], svc.SIDE_MANAGER)
		self.assertEqual(handover["awaiting_label"], svc.MANAGER_LABEL)
		# Nobody answers it one-to-one: only the balanced check runs.
		push.assert_not_called()
		self.balanced.assert_called_once()
		self.assertEqual(self.balanced.call_args.args[0], DOKKI)

	def test_out_needs_no_counterpart(self):
		_result, captured, _push = self._declare(_branch_actor(), "out", other=None)
		self.assertEqual(captured["kind"], svc.DOC_KIND_DISPATCH)

	def test_factory_dispatch_by_production(self):
		_result, captured, _push = self._declare(_factory_actor(), "out", my=FACTORY, other=None)
		self.assertEqual(captured["from_warehouse"], FACTORY)
		self.assertIsNone(captured["to_warehouse"])
		self.assertEqual(captured["source"], svc.SOURCE_FACTORY)

	def test_a_receipt_from_a_sender_with_a_dispatch_joins_the_group(self):
		# Factory has a sent-out batch open; Dokki says it received from Factory.
		result, _captured, push = self._declare(_branch_actor(), "in", dispatch_sources={FACTORY})
		push.assert_not_called()
		self.balanced.assert_called_once()
		self.assertEqual(self.balanced.call_args.args[0], FACTORY)
		self.assertEqual(result["handover"]["group_source"], FACTORY)
		self.assertEqual(result["handover"]["awaiting_label"], svc.MANAGER_LABEL)
		self.assertFalse(result["handover"]["can_respond"])

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
		with self.assertRaises(frappe.ValidationError):
			self._declare(_branch_actor(), "out", lines=[{"item_code": "FLOUR", "qty": 1}])

	def test_in_without_a_counterpart_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._declare(_branch_actor(), "in", other=None)

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
			self._declare(_branch_actor(), "out", my=FACTORY, other=None)

	def test_a_repeated_client_request_id_returns_the_first_row(self):
		actor = _branch_actor()
		first = _pending(actor.user, svc.SIDE_TO)
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc.frappe, "get_all", return_value=["HND-2026-00001"]) as get_all,
			patch.object(svc.frappe, "get_doc", return_value=first),
			patch.object(svc, "_insert_handover") as insert,
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_pending_dispatch_sources", return_value=set()),
			patch.object(svc, "_user_names", return_value={}),
		):
			result = svc.declare(
				DOKKI,
				FACTORY,
				"in",
				[{"item_code": "JAR-L", "qty": 12}],
				pos_profile="Dokki",
				client_request_id="req-1",
			)
		insert.assert_not_called()
		push.assert_not_called()
		self.assertEqual(result["handover"]["name"], "HND-2026-00001")
		filters = (
			get_all.call_args.args[1]
			if len(get_all.call_args.args) > 1
			else get_all.call_args.kwargs["filters"]
		)
		self.assertEqual(filters, {"client_request_id": "req-1", "declared_by": actor.user})

	def test_client_request_id_reaches_the_insert(self):
		_result, captured, _push = self._declare(_branch_actor(), "out", client_request_id="req-2")
		self.assertEqual(captured["client_request_id"], "req-2")

	def test_insert_sets_awaiting_to_the_other_side(self):
		built = {}

		class _Inserted:
			name = "HND-2026-00009"

			def __init__(self):
				self.flags = types.SimpleNamespace()

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
		self.assertEqual(payload["kind"], svc.DOC_KIND_HANDOVER)
		self.assertEqual(payload["awaiting_side"], svc.SIDE_FROM)
		self.assertEqual(payload["status"], svc.STATUS_PENDING)
		self.assertEqual(payload["last_proposed_by"], "dokki@jarz.test")
		self.assertEqual(payload["items"][0]["qty"], 12.0)
		self.assertEqual(payload["items"][0]["declared_qty"], 12.0)
		self.assertTrue(built["ignore_permissions"])

	def test_insert_of_a_dispatch_has_no_destination_and_awaits_the_manager(self):
		built = {}

		class _Inserted:
			name = "HND-2026-00010"

			def __init__(self):
				self.flags = types.SimpleNamespace()

			def insert(self, ignore_permissions=False):
				built["flag"] = self.flags.from_handover_service

		def fake_get_doc(payload):
			built["payload"] = payload
			return _Inserted()

		with (
			patch.object(svc.frappe, "get_doc", side_effect=fake_get_doc),
			patch.object(svc, "_item_meta", side_effect=_item_meta),
			patch.object(svc, "_warehouse_company", return_value="JARZ"),
		):
			svc._insert_handover(
				from_warehouse=DOKKI,
				to_warehouse=None,
				declared_side=svc.SIDE_FROM,
				source=svc.SOURCE_BRANCH,
				lines={"JAR-L": 10.0},
				user="dokki@jarz.test",
				kind=svc.DOC_KIND_DISPATCH,
				client_request_id="req-9",
			)
		payload = built["payload"]
		self.assertEqual(payload["kind"], svc.DOC_KIND_DISPATCH)
		self.assertIsNone(payload["to_warehouse"])
		self.assertEqual(payload["awaiting_side"], svc.SIDE_MANAGER)
		self.assertEqual(payload["client_request_id"], "req-9")
		self.assertTrue(built["flag"])


class TestStockHandoverItems(unittest.TestCase):
	def _items(self, actor, counterpart):
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_allowed_item_codes", return_value={"JAR-L"}) as allowed,
			patch.object(svc, "_dispatch_item_codes", return_value={"JAR-L", "JAR-M"}) as dispatch,
			patch.object(svc, "_item_rows", side_effect=lambda codes, search=None: sorted(codes)),
		):
			result = svc.list_items(DOKKI, counterpart)
		return result, allowed, dispatch

	def test_with_a_counterpart_the_pair_scope_is_used(self):
		result, allowed, dispatch = self._items(_branch_actor(), FACTORY)
		self.assertEqual(result["items"], ["JAR-L"])
		allowed.assert_called_once()
		dispatch.assert_not_called()

	def test_without_a_counterpart_the_sent_out_scope_is_used(self):
		result, allowed, dispatch = self._items(_branch_actor(), None)
		self.assertEqual(result["items"], ["JAR-L", "JAR-M"])
		dispatch.assert_called_once()
		allowed.assert_not_called()

	def test_a_side_the_user_cannot_act_for_is_refused(self):
		with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
			with (
				patch.object(svc, "_build_actor", return_value=_branch_actor(shift=False)),
				patch.object(svc, "_dispatch_item_codes", return_value=set()),
			):
				svc.list_items(DOKKI, None)


class TestStockHandoverRespond(unittest.TestCase):
	def _respond(self, actor, doc, action, dispatch_sources=(), **kwargs):
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_lock", return_value=doc),
			patch.object(svc, "_lock_dispatches", side_effect=_fake_lock_dispatches(dispatch_sources)),
			patch.object(
				svc.frappe.db,
				"get_value",
				side_effect=lambda doctype, *a, **k: (
					doc.from_warehouse if doctype == svc.DOCTYPE else getattr(self, "available", 0)
				),
			),
			patch.object(svc, "_allowed_item_codes", return_value={"JAR-L", "JAR-M"}),
			patch.object(svc, "_item_meta", side_effect=_item_meta),
			patch.object(svc, "_pending_dispatch_sources", return_value=set(dispatch_sources)),
			patch.object(svc, "_after_dispatch_closed") as closed,
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_user_names", return_value={}),
		):
			result = svc.respond(doc.name, action, **kwargs)
		self.closed = closed
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
		self.assertEqual(doc.was_changed, 1)
		self.assertTrue(doc.flags.from_handover_service)
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

	def test_changed_stays_true_when_the_quantities_are_changed_back(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO, was_changed=1)
		self.assertTrue(svc._is_changed(doc, list(doc.items)))
		self.assertFalse(svc._can_cancel(doc, list(doc.items), _branch_actor()))

	def test_changed_without_the_flag_still_reads_the_quantities(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		self.assertFalse(svc._is_changed(doc, list(doc.items)))
		doc.items[0].qty = 10.0
		self.assertTrue(svc._is_changed(doc, list(doc.items)))

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
				transfer_api,
				"_insert_and_submit_stock_entry",
				side_effect=NegativeStockError("negative stock"),
			),
			patch.object(svc.frappe.db, "savepoint"),
			patch.object(svc.frappe.db, "rollback") as rollback,
		):
			self.available = 3
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._respond(_factory_actor(), doc, "confirm")
		rollback.assert_called_once()
		self.assertEqual(doc.status, svc.STATUS_PENDING)
		self.assertIsNone(doc.stock_entry)
		self.assertEqual(doc.saved, 0)
		message = str(ctx.exception)
		self.assertIn("Large Jar", message)
		self.assertIn("3", message)

	def test_a_failure_that_is_not_a_shortage_shows_its_real_message(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)

		class _SE:
			name = None

		with (
			patch.object(transfer_api, "_build_material_transfer", return_value=_SE()),
			patch.object(
				transfer_api,
				"_insert_and_submit_stock_entry",
				side_effect=frappe.ValidationError("Warehouse is frozen"),
			),
			patch.object(svc.frappe.db, "savepoint"),
			patch.object(svc.frappe.db, "rollback"),
		):
			self.available = 0
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._respond(_factory_actor(), doc, "confirm")
		message = str(ctx.exception)
		self.assertIn("Warehouse is frozen", message)
		self.assertNotIn("Not enough stock", message)
		self.assertEqual(doc.status, svc.STATUS_PENDING)

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

	def test_a_receipt_in_a_match_group_cannot_be_answered_one_to_one(self):
		# Factory has a sent-out batch open, so Dokki's receipt from it is the manager's to match.
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		with patch.object(svc, "_confirm") as confirm:
			with self.assertRaises(frappe.PermissionError) as ctx:
				self._respond(_factory_actor(), doc, "confirm", dispatch_sources={FACTORY})
		confirm.assert_not_called()
		self.assertIn("manager matches it", str(ctx.exception))
		self.assertEqual(doc.status, svc.STATUS_PENDING)
		self.assertEqual(doc.saved, 0)

	def test_a_receipt_answers_one_to_one_again_once_the_group_is_gone(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		_result, push = self._respond(_factory_actor(), doc, "reject", dispatch_sources=set())
		self.assertEqual(doc.status, svc.STATUS_REJECTED)
		self.assertEqual(push.call_args.args[1], svc.EVENT_REJECTED)

	def test_a_dispatch_cannot_be_confirmed_or_changed_through_respond(self):
		doc = _dispatch("dokki@jarz.test")
		for action in ("confirm", "change"):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._respond(_manager_actor(), doc, action, lines=[{"item_code": "JAR-L", "qty": 5}])
			self.assertIn("manager match", str(ctx.exception))
		self.assertEqual(doc.status, svc.STATUS_PENDING)

	def test_a_manager_can_reject_a_dispatch(self):
		doc = _dispatch("dokki@jarz.test")
		result, push = self._respond(_manager_actor(), doc, "reject", reason="Not sent")
		self.assertEqual(doc.status, svc.STATUS_REJECTED)
		self.assertEqual(doc.reject_reason, "Not sent")
		self.assertEqual(doc.saved, 1)
		self.assertEqual(push.call_args.args[1], svc.EVENT_REJECTED)
		self.closed.assert_called_once()
		self.assertEqual(self.closed.call_args.args[0], DOKKI)
		self.assertEqual(result["handover"]["status"], svc.STATUS_REJECTED)

	def test_only_a_manager_who_did_not_declare_it_can_reject_a_dispatch(self):
		doc = _dispatch("dokki@jarz.test")
		with self.assertRaises(frappe.PermissionError):
			self._respond(_branch_actor(), doc, "reject")
		own = _dispatch("manager@jarz.test")
		with self.assertRaises(frappe.PermissionError):
			self._respond(_manager_actor(), own, "reject")
		self.assertEqual(doc.status, svc.STATUS_PENDING)
		self.assertEqual(own.status, svc.STATUS_PENDING)


class TestStockHandoverCancel(unittest.TestCase):
	def _cancel(self, actor, doc, dispatch_sources=()):
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_lock", return_value=doc),
			patch.object(svc, "_lock_dispatches", side_effect=_fake_lock_dispatches(dispatch_sources)),
			patch.object(svc.frappe.db, "get_value", return_value=doc.from_warehouse),
			patch.object(svc, "_pending_dispatch_sources", return_value=set(dispatch_sources)),
			patch.object(svc, "_after_dispatch_closed") as closed,
			patch.object(svc, "_maybe_push_balanced") as balanced,
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_user_names", return_value={}),
		):
			result = svc.cancel(doc.name)
		self.closed = closed
		self.balanced = balanced
		return result, push

	def test_declarer_side_cancels_while_unchanged(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		result, push = self._cancel(_branch_actor(), doc)
		self.assertEqual(doc.status, svc.STATUS_CANCELLED)
		self.assertEqual(doc.cancelled_by, "dokki@jarz.test")
		self.assertEqual(result["handover"]["status"], svc.STATUS_CANCELLED)
		self.assertEqual(push.call_args.args[1], svc.EVENT_CANCELLED)
		self.closed.assert_not_called()
		self.balanced.assert_not_called()

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

	def test_cannot_cancel_when_the_changed_flag_is_set(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO, was_changed=1)
		with self.assertRaises(frappe.ValidationError):
			self._cancel(_branch_actor(), doc)
		self.assertEqual(doc.status, svc.STATUS_PENDING)

	def test_cannot_cancel_a_closed_handover(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO, status=svc.STATUS_REJECTED)
		with self.assertRaises(frappe.ValidationError):
			self._cancel(_branch_actor(), doc)

	def test_cancelling_a_dispatch_re_checks_the_group(self):
		doc = _dispatch("dokki@jarz.test")
		result, push = self._cancel(_branch_actor(), doc, dispatch_sources={DOKKI})
		self.assertEqual(doc.status, svc.STATUS_CANCELLED)
		self.assertEqual(result["handover"]["status"], svc.STATUS_CANCELLED)
		self.closed.assert_called_once()
		self.assertEqual(self.closed.call_args.args[0], DOKKI)
		push.assert_not_called()

	def test_cancelling_a_grouped_receipt_re_checks_the_balance(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		_result, push = self._cancel(_branch_actor(), doc, dispatch_sources={FACTORY})
		self.assertEqual(doc.status, svc.STATUS_CANCELLED)
		self.balanced.assert_called_once()
		self.assertEqual(self.balanced.call_args.args[0], FACTORY)
		push.assert_not_called()


class TestMatchGroups(unittest.TestCase):
	def _group(self):
		dispatch = _dispatch("dokki@jarz.test", name="HND-D1", qty=10.0)
		to_factory = _receipt(
			"factory@jarz.test", FACTORY, name="HND-R1", qty=6.0, declared_at="2026-10-20 10:00:00"
		)
		to_zamalek = _receipt(
			"zamalek@jarz.test", ZAMALEK, name="HND-R2", qty=4.0, declared_at="2026-10-20 10:05:00"
		)
		return dispatch, to_factory, to_zamalek

	def _summary(self, *docs):
		dispatches = [d for d in docs if svc._is_dispatch(d)]
		receipts = [d for d in docs if not svc._is_dispatch(d)]
		items = {d.name: list(d.items) for d in docs}
		return svc._group_summary(DOKKI, dispatches, receipts, items)

	def test_a_group_is_a_pending_dispatch_plus_the_receipts_naming_its_sender(self):
		dispatch, to_factory, to_zamalek = self._group()
		unrelated = _pending("dokki@jarz.test", svc.SIDE_TO, from_wh=FACTORY, to_wh=DOKKI, name="HND-X")
		sender_declared = _pending(
			"dokki@jarz.test", svc.SIDE_FROM, from_wh=DOKKI, to_wh=ZAMALEK, name="HND-Y"
		)
		closed = _receipt("factory@jarz.test", FACTORY, name="HND-Z", status=svc.STATUS_CONFIRMED)
		groups = svc._match_groups([unrelated, sender_declared, closed, to_zamalek, dispatch, to_factory])
		self.assertEqual(list(groups), [DOKKI])
		self.assertEqual([d.name for d in groups[DOKKI]["dispatches"]], ["HND-D1"])
		self.assertEqual([r.name for r in groups[DOKKI]["receipts"]], ["HND-R1", "HND-R2"])

	def test_receipts_without_a_dispatch_form_no_group(self):
		_dispatch_doc, to_factory, to_zamalek = self._group()
		self.assertEqual(svc._match_groups([to_factory, to_zamalek]), {})

	def test_balanced_when_the_claims_add_up_to_what_was_sent(self):
		summary = self._summary(*self._group())
		self.assertTrue(summary["balanced"])
		self.assertEqual(summary["unmatched_units"], 0)
		item = summary["items"][0]
		self.assertEqual(item["sent"], 10.0)
		self.assertEqual(item["remaining"], 0.0)
		self.assertEqual(
			{c["warehouse"]: c["qty"] for c in item["claims"]},
			{FACTORY: 6.0, ZAMALEK: 4.0},
		)
		self.assertEqual(summary["declarers"], ["dokki@jarz.test", "factory@jarz.test", "zamalek@jarz.test"])
		self.assertEqual(summary["newest"], "HND-R2")
		self.assertEqual(summary["newest_dispatch"], "HND-D1")

	def test_not_balanced_when_a_receiver_has_not_answered(self):
		dispatch, to_factory, _to_zamalek = self._group()
		summary = self._summary(dispatch, to_factory)
		self.assertFalse(summary["balanced"])
		self.assertEqual(summary["items"][0]["remaining"], 4.0)
		self.assertEqual(summary["unmatched_units"], 4.0)

	def test_not_balanced_when_the_receivers_claim_more_than_was_sent(self):
		dispatch, to_factory, to_zamalek = self._group()
		to_zamalek.items[0].qty = 5.0
		summary = self._summary(dispatch, to_factory, to_zamalek)
		self.assertFalse(summary["balanced"])
		self.assertEqual(summary["items"][0]["remaining"], -1.0)

	def test_two_dispatches_from_one_sender_add_up(self):
		first = _dispatch("dokki@jarz.test", name="HND-D1", qty=6.0)
		second = _dispatch("dokki@jarz.test", name="HND-D2", qty=4.0, declared_at="2026-10-20 09:30:00")
		receipt = _receipt("factory@jarz.test", FACTORY, name="HND-R1", qty=10.0)
		summary = self._summary(first, second, receipt)
		self.assertEqual(summary["items"][0]["sent"], 10.0)
		self.assertTrue(summary["balanced"])
		self.assertEqual(summary["newest_dispatch"], "HND-D2")

	def test_a_rounding_sliver_does_not_unbalance_a_group(self):
		dispatch = _dispatch("dokki@jarz.test", name="HND-D1", qty=0.3)
		receipt = _receipt("factory@jarz.test", FACTORY, name="HND-R1", qty=0.1 + 0.2)
		self.assertTrue(self._summary(dispatch, receipt)["balanced"])

	def test_group_source_follows_the_pending_dispatches(self):
		dispatch, to_factory, _to_zamalek = self._group()
		self.assertEqual(svc._group_source(dispatch, set()), DOKKI)
		self.assertEqual(svc._group_source(to_factory, {DOKKI}), DOKKI)
		# The last Dispatch was cancelled: the receipt is a one-to-one handover again.
		self.assertIsNone(svc._group_source(to_factory, set()))
		closed = _receipt("factory@jarz.test", FACTORY, status=svc.STATUS_CONFIRMED)
		self.assertIsNone(svc._group_source(closed, {DOKKI}))
		# A declaration by the sending side is never grouped.
		sender = _pending("dokki@jarz.test", svc.SIDE_FROM, from_wh=DOKKI, to_wh=ZAMALEK)
		self.assertIsNone(svc._group_source(sender, {DOKKI}))

	def test_a_grouped_receipt_awaits_the_manager_and_loses_its_buttons(self):
		_dispatch_doc, to_factory, _to_zamalek = self._group()
		receiver = _factory_actor()
		grouped = svc._serialize(to_factory, list(to_factory.items), receiver, {}, {DOKKI})
		self.assertEqual(grouped["awaiting_side"], svc.SIDE_MANAGER)
		self.assertEqual(grouped["awaiting_label"], svc.MANAGER_LABEL)
		self.assertEqual(grouped["group_source"], DOKKI)
		self.assertFalse(grouped["can_respond"])
		# Back to one-to-one: the sender (Dokki) is asked, and the factory is not.
		alone = svc._serialize(to_factory, list(to_factory.items), _branch_actor(), {}, set())
		self.assertIsNone(alone["group_source"])
		self.assertEqual(alone["awaiting_side"], svc.SIDE_FROM)
		self.assertTrue(alone["can_respond"])

	def test_a_dispatch_is_serialized_for_a_manager_with_reject_available(self):
		dispatch, _a, _b = self._group()
		out = svc._serialize(dispatch, list(dispatch.items), _manager_actor(), {}, {DOKKI})
		self.assertEqual(out["kind"], svc.DOC_KIND_DISPATCH)
		self.assertIsNone(out["to_warehouse"])
		self.assertIsNone(out["to_label"])
		self.assertEqual(out["awaiting_label"], svc.MANAGER_LABEL)
		self.assertTrue(out["can_respond"])
		# Another branch can neither answer nor cancel it.
		other = svc._serialize(
			dispatch,
			list(dispatch.items),
			_branch_actor(user="zamalek@jarz.test", profile="Zamalek"),
			{},
			{DOKKI},
		)
		self.assertFalse(other["can_cancel"])
		self.assertFalse(other["can_respond"])
		# Its declarer's own branch can cancel it, but not answer it.
		mine = svc._serialize(dispatch, list(dispatch.items), _branch_actor(), {}, {DOKKI})
		self.assertFalse(mine["can_respond"])

	def test_assign_splits_fills_the_oldest_first_and_the_newest_takes_the_rest(self):
		receipts = [{"JAR-L": 5.0}, {"JAR-L": 3.0}]
		self.assertEqual(svc._assign_splits(receipts, {"JAR-L": 8.0}), [{"JAR-L": 5.0}, {"JAR-L": 3.0}])
		self.assertEqual(svc._assign_splits(receipts, {"JAR-L": 10.0}), [{"JAR-L": 5.0}, {"JAR-L": 5.0}])
		self.assertEqual(svc._assign_splits(receipts, {"JAR-L": 6.0}), [{"JAR-L": 5.0}, {"JAR-L": 1.0}])
		self.assertEqual(svc._assign_splits(receipts, {"JAR-L": 4.0}), [{"JAR-L": 4.0}, {"JAR-L": 0.0}])
		self.assertEqual(svc._assign_splits(receipts, {"JAR-L": 0.0}), [{"JAR-L": 0.0}, {"JAR-L": 0.0}])
		self.assertEqual(svc._assign_splits([{"JAR-L": 2.0}], {"JAR-L": 9.0}), [{"JAR-L": 9.0}])
		self.assertEqual(svc._assign_splits([], {"JAR-L": 9.0}), [])

	def test_assign_splits_keeps_each_item_separate(self):
		receipts = [{"JAR-L": 2.0, "JAR-M": 1.0}, {"JAR-L": 2.0}]
		assigned = svc._assign_splits(receipts, {"JAR-L": 3.0, "JAR-M": 4.0})
		self.assertEqual(assigned[0], {"JAR-L": 2.0, "JAR-M": 1.0})
		self.assertEqual(assigned[1], {"JAR-L": 1.0, "JAR-M": 3.0})


class TestBalancedAndFallbackPush(unittest.TestCase):
	def test_balanced_group_pushes_once_on_the_newest_member(self):
		summary = {"balanced": True, "newest": "HND-R2"}
		with (
			patch.object(svc, "_load_group", return_value=summary),
			patch.object(svc, "_enqueue_push", return_value=True) as push,
		):
			self.assertTrue(svc._maybe_push_balanced(DOKKI, "dokki@jarz.test"))
		push.assert_called_once_with("HND-R2", svc.EVENT_BALANCED, "dokki@jarz.test")

	def test_unbalanced_or_missing_group_pushes_nothing(self):
		for summary in ({"balanced": False, "newest": "HND-R2"}, None):
			with (
				patch.object(svc, "_load_group", return_value=summary),
				patch.object(svc, "_enqueue_push") as push,
			):
				self.assertFalse(svc._maybe_push_balanced(DOKKI, "dokki@jarz.test"))
			push.assert_not_called()

	def test_balanced_check_never_raises(self):
		with (
			patch.object(svc, "_load_group", side_effect=Exception("db down")),
			patch.object(svc, "_safe_log"),
		):
			self.assertFalse(svc._maybe_push_balanced(DOKKI, None))

	def test_last_dispatch_closed_asks_the_sender_to_confirm_each_receipt(self):
		with (
			patch.object(svc, "_pending_dispatch_sources", return_value=set()),
			patch.object(svc.frappe, "get_all", return_value=["HND-R1", "HND-R2"]),
			patch.object(svc, "_enqueue_push") as push,
		):
			svc._after_dispatch_closed(DOKKI, "manager@jarz.test")
		self.assertEqual(
			[(c.args[0], c.args[1]) for c in push.call_args_list],
			[("HND-R1", svc.EVENT_DECLARED), ("HND-R2", svc.EVENT_DECLARED)],
		)

	def test_another_dispatch_still_open_only_re_checks_the_balance(self):
		with (
			patch.object(svc, "_pending_dispatch_sources", return_value={DOKKI}),
			patch.object(svc, "_maybe_push_balanced") as balanced,
			patch.object(svc, "_enqueue_push") as push,
		):
			svc._after_dispatch_closed(DOKKI, "manager@jarz.test")
		balanced.assert_called_once_with(DOKKI, "manager@jarz.test")
		push.assert_not_called()


class TestListHandovers(unittest.TestCase):
	def _list(self, actor, docs, dispatch_sources=(DOKKI,)):
		def fake_get_all(doctype, filters=None, **kw):
			if filters and filters.get("status") == svc.STATUS_PENDING:
				return docs
			return []

		items = {d.name: [dict(r.__dict__) for r in d.items] for d in docs}
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc.frappe, "get_all", side_effect=fake_get_all),
			patch.object(svc, "_load_items", return_value=items),
			patch.object(svc, "_pending_dispatch_sources", return_value=set(dispatch_sources)),
			patch.object(svc, "_user_names", return_value={}),
		):
			return svc.list_handovers()

	def _docs(self):
		return [
			_dispatch("dokki@jarz.test", name="HND-D1", qty=10.0),
			_receipt("factory@jarz.test", FACTORY, name="HND-R1", qty=6.0),
			_receipt("zamalek@jarz.test", ZAMALEK, name="HND-R2", qty=4.0),
		]

	def test_a_manager_gets_the_group_to_match(self):
		out = self._list(_manager_actor(), self._docs())
		self.assertEqual(len(out["to_match"]), 1)
		group = out["to_match"][0]
		self.assertEqual(group["source_warehouse"], DOKKI)
		self.assertEqual(group["source_label"], "Dokki")
		self.assertEqual(group["dispatches"], ["HND-D1"])
		self.assertEqual(group["receipts"], ["HND-R1", "HND-R2"])
		self.assertTrue(group["balanced"])
		self.assertTrue(group["can_confirm"])
		self.assertEqual({d["warehouse"] for d in group["destinations"]}, {FACTORY, ZAMALEK})
		# Grouped members never wait for a one-to-one answer.
		self.assertEqual(out["waiting_for_me"], [])
		self.assertEqual(sorted(r["name"] for r in out["waiting_for_them"]), ["HND-D1", "HND-R1", "HND-R2"])

	def test_a_manager_who_declared_part_of_the_group_cannot_confirm_it(self):
		out = self._list(_manager_actor(user="dokki@jarz.test"), self._docs())
		self.assertFalse(out["to_match"][0]["can_confirm"])

	def test_a_branch_user_gets_no_to_match(self):
		out = self._list(_branch_actor(), self._docs())
		self.assertEqual(out["to_match"], [])

	def test_without_a_dispatch_the_receipts_stay_one_to_one(self):
		docs = [d for d in self._docs() if not svc._is_dispatch(d)]
		out = self._list(_manager_actor(), docs, dispatch_sources=())
		self.assertEqual(out["to_match"], [])


class TestConfirmMatch(unittest.TestCase):
	class _SE:
		name = "MAT-STE-2026-00077"

		def __init__(self):
			self.items = []

		def append(self, key, row):
			self.items.append(row)

	def _docs(self):
		return [
			_dispatch("dokki@jarz.test", name="HND-D1", qty=10.0),
			_receipt("factory@jarz.test", FACTORY, name="HND-R1", qty=6.0, declared_at="2026-10-20 10:00:00"),
			_receipt("zamalek@jarz.test", ZAMALEK, name="HND-R2", qty=4.0, declared_at="2026-10-20 10:05:00"),
		]

	def _run(
		self,
		docs,
		lines,
		actor=None,
		group=None,
		members=None,
		note=None,
		post_side_effect=None,
		source=DOKKI,
		events=None,
	):
		actor = actor or _manager_actor()
		by_name = {d.name: d for d in docs}
		dispatch_names = [d.name for d in docs if svc._is_dispatch(d)]
		receipt_names = [d.name for d in docs if not svc._is_dispatch(d)]
		se = self._SE()

		def fake_lock(name):
			if events is not None:
				events.append(("lock", name))
			if name not in by_name:
				raise frappe.DoesNotExistError(name)
			return by_name[name]

		def fake_group(src, for_update=False):
			if events is not None:
				events.append(("group", src, for_update))
			return group or (dispatch_names, receipt_names)

		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_lock", side_effect=fake_lock),
			patch.object(svc, "_current_group_names", side_effect=fake_group),
			patch.object(svc, "_item_meta", side_effect=_item_meta),
			patch.object(svc, "_warehouse_company", return_value="JARZ"),
			patch.object(svc, "_enqueue_push") as push,
			patch.object(transfer_api, "_build_material_transfer", return_value=se) as build,
			patch.object(
				transfer_api, "_insert_and_submit_stock_entry", side_effect=post_side_effect
			) as post,
			patch.object(svc.frappe.db, "savepoint"),
			patch.object(svc.frappe.db, "rollback") as rollback,
			patch.object(svc.frappe.db, "get_value", return_value=3),
		):
			try:
				result = svc.confirm_match(
					source,
					members if members is not None else [d.name for d in docs],
					lines,
					note=note,
				)
			finally:
				self.se, self.build, self.post, self.push, self.rollback = se, build, post, push, rollback
		return result

	def test_happy_path_posts_one_transfer_and_closes_every_member(self):
		docs = self._docs()
		manager = _manager_actor()
		result = self._run(docs, [_line(10, factory=6, zamalek=4)], actor=manager)
		dispatch, to_factory, to_zamalek = docs

		self.build.assert_called_once()
		self.assertEqual(self.build.call_args.args[:2], (DOKKI, FACTORY))
		self.assertEqual(self.build.call_args.args[2], [{"item_code": "JAR-L", "qty": 6.0}])
		self.assertIsNone(self.build.call_args.kwargs.get("posting_date"))
		# The second destination is a row on the SAME entry.
		self.assertEqual(
			self.se.items,
			[{"item_code": "JAR-L", "uom": "Nos", "qty": 4.0, "s_warehouse": DOKKI, "t_warehouse": ZAMALEK}],
		)
		self.post.assert_called_once()

		self.assertEqual(result["stock_entry"], "MAT-STE-2026-00077")
		self.assertEqual(result["handovers"], ["HND-D1", "HND-R1", "HND-R2"])
		for doc in docs:
			self.assertEqual(doc.status, svc.STATUS_CONFIRMED)
			self.assertEqual(doc.stock_entry, "MAT-STE-2026-00077")
			self.assertEqual(doc.confirmed_by, manager.user)
			self.assertEqual(doc.match_note, "Matched as declared.")
			self.assertEqual(doc.saved, 1)
		self.assertEqual(dispatch.items[0].qty, 10.0)
		self.assertEqual(to_factory.items[0].qty, 6.0)
		self.assertEqual(to_zamalek.items[0].qty, 4.0)

		# One confirmation push per declarer.
		self.assertEqual(
			[(c.args[0], c.args[1]) for c in self.push.call_args_list],
			[
				("HND-D1", svc.EVENT_BATCH_CONFIRMED),
				("HND-R1", svc.EVENT_BATCH_CONFIRMED),
				("HND-R2", svc.EVENT_BATCH_CONFIRMED),
			],
		)

	def test_the_manager_can_correct_the_sent_quantity_and_a_receivers_claim(self):
		docs = self._docs()
		self._run(docs, [_line(9, factory=5, zamalek=4)], note="One jar broke")
		dispatch, to_factory, to_zamalek = docs
		self.assertEqual(self.build.call_args.args[2], [{"item_code": "JAR-L", "qty": 5.0}])
		# A single Dispatch carries the final sent quantity; its declaration is kept.
		self.assertEqual(dispatch.items[0].qty, 9.0)
		self.assertEqual(dispatch.items[0].declared_qty, 10.0)
		self.assertEqual(to_factory.items[0].qty, 5.0)
		self.assertEqual(to_factory.items[0].declared_qty, 6.0)
		self.assertEqual(to_zamalek.items[0].qty, 4.0)
		note = dispatch.match_note
		self.assertIn("JAR-L: sent 10 -> 9", note)
		self.assertIn("Factory: JAR-L 6 -> 5", note)
		self.assertIn("One jar broke", note)
		self.assertEqual(note, to_factory.match_note)
		self.assertEqual(note, to_zamalek.match_note)

	def test_a_destination_the_manager_adds_gets_a_confirmed_record(self):
		dispatch = _dispatch("factory@jarz.test", from_wh=FACTORY, name="HND-D1", qty=10.0)
		receipt = _receipt("dokki@jarz.test", DOKKI, from_wh=FACTORY, name="HND-R1", qty=10.0)
		inserted = {}

		class _New:
			name = "HND-NEW"

			def __init__(self):
				self.flags = types.SimpleNamespace()

			def insert(self, ignore_permissions=False):
				inserted["ignore_permissions"] = ignore_permissions
				inserted["flag"] = self.flags.from_handover_service

		def fake_get_doc(payload):
			inserted["payload"] = payload
			return _New()

		manager = _manager_actor()
		with patch.object(svc.frappe, "get_doc", side_effect=fake_get_doc):
			result = self._run(
				[dispatch, receipt],
				[_line(10, dokki=6, zamalek=4)],
				actor=manager,
				source=FACTORY,
			)
		self.assertEqual(self.build.call_args.args[:2], (FACTORY, DOKKI))
		self.assertEqual(self.build.call_args.args[2], [{"item_code": "JAR-L", "qty": 6.0}])
		self.assertEqual(self.se.items[0]["t_warehouse"], ZAMALEK)
		self.assertEqual(self.se.items[0]["qty"], 4.0)
		# The one claimant takes the remainder after the manager's split.
		self.assertEqual(receipt.items[0].qty, 6.0)
		payload = inserted["payload"]
		self.assertEqual(payload["kind"], svc.DOC_KIND_HANDOVER)
		self.assertEqual(payload["from_warehouse"], FACTORY)
		self.assertEqual(payload["to_warehouse"], ZAMALEK)
		self.assertEqual(payload["status"], svc.STATUS_CONFIRMED)
		self.assertEqual(payload["source"], svc.SOURCE_MANAGER)
		self.assertEqual(payload["declared_by"], manager.user)
		self.assertEqual(payload["stock_entry"], "MAT-STE-2026-00077")
		self.assertEqual(payload["items"][0]["qty"], 4.0)
		self.assertTrue(inserted["ignore_permissions"])
		self.assertTrue(inserted["flag"])
		self.assertIn("HND-NEW", result["handovers"])
		self.assertIn("Added Zamalek", dispatch.match_note)

	def test_a_receiver_with_nothing_left_is_rejected_as_not_in_the_shipment(self):
		docs = self._docs()
		self._run(docs, [_line(10, factory=10, zamalek=0)])
		dispatch, to_factory, to_zamalek = docs
		self.build.assert_called_once()
		self.assertEqual(self.build.call_args.args[2], [{"item_code": "JAR-L", "qty": 10.0}])
		self.assertEqual(self.se.items, [])
		self.assertEqual(to_factory.status, svc.STATUS_CONFIRMED)
		self.assertEqual(to_factory.items[0].qty, 10.0)
		self.assertEqual(to_zamalek.status, svc.STATUS_REJECTED)
		self.assertIn("Dokki", to_zamalek.reject_reason)
		self.assertIsNone(to_zamalek.stock_entry)
		self.assertEqual(dispatch.status, svc.STATUS_CONFIRMED)
		self.assertIn("Zamalek was not in the shipment", dispatch.match_note)

	def test_several_dispatches_keep_their_quantities_and_the_final_goes_in_the_note(self):
		first = _dispatch("dokki@jarz.test", name="HND-D1", qty=6.0)
		second = _dispatch("dokki@jarz.test", name="HND-D2", qty=4.0, declared_at="2026-10-20 09:30:00")
		receipt = _receipt("factory@jarz.test", FACTORY, name="HND-R1", qty=10.0)
		self._run([first, second, receipt], [_line(10, factory=10)])
		self.assertEqual(first.items[0].qty, 6.0)
		self.assertEqual(second.items[0].qty, 4.0)
		self.assertEqual(first.status, svc.STATUS_CONFIRMED)
		self.assertEqual(second.status, svc.STATUS_CONFIRMED)
		self.assertIn("Final: JAR-L 10 (Factory 10)", first.match_note)
		# One push per declarer: dokki once even though two of its rows were closed.
		self.assertEqual(len(self.push.call_args_list), 2)

	def test_every_unit_sent_must_go_somewhere(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(docs, [_line(10, factory=6, zamalek=3)])
		self.assertIn("sent 10, split 9", str(ctx.exception))
		self.assertIn("Every unit must go somewhere", str(ctx.exception))
		self.post.assert_not_called()
		for doc in docs:
			self.assertEqual(doc.status, svc.STATUS_PENDING)
			self.assertEqual(doc.saved, 0)

	def test_a_total_of_zero_is_refused(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError):
			self._run(docs, [_line(0, factory=0, zamalek=0)])
		self.post.assert_not_called()

	def test_every_item_of_the_batch_needs_a_line(self):
		docs = self._docs()
		other = {"item_code": "JAR-M", "sent": 2, "splits": [{"warehouse": FACTORY, "qty": 2}]}
		with self.assertRaises(frappe.ValidationError):
			self._run(docs, [other])
		self.post.assert_not_called()

	def test_a_member_added_to_the_group_meanwhile_is_a_drift(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(
				docs,
				[_line(10, factory=6, zamalek=4)],
				group=(["HND-D1"], ["HND-R1", "HND-R2", "HND-R3"]),
			)
		self.assertIn("Something changed", str(ctx.exception))
		self.post.assert_not_called()
		self.assertTrue(all(d.status == svc.STATUS_PENDING for d in docs))

	def test_a_member_removed_from_the_group_meanwhile_is_a_drift(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError):
			self._run(docs, [_line(10, factory=6, zamalek=4)], members=["HND-D1", "HND-R1"])
		self.post.assert_not_called()

	def test_a_member_that_no_longer_exists_is_a_drift(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(docs, [_line(10, factory=6, zamalek=4)], members=["HND-D1", "HND-R1", "HND-GONE"])
		self.assertIn("Something changed", str(ctx.exception))

	def test_a_member_closed_meanwhile_is_a_drift(self):
		docs = self._docs()
		docs[1].status = svc.STATUS_CANCELLED
		with self.assertRaises(frappe.ValidationError):
			self._run(docs, [_line(10, factory=6, zamalek=4)])
		self.post.assert_not_called()

	def test_a_group_with_no_dispatch_is_a_drift(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError):
			self._run(docs, [_line(10, factory=6, zamalek=4)], group=([], ["HND-R1", "HND-R2"]))

	def test_nobody_approves_their_own_declaration(self):
		docs = self._docs()
		with self.assertRaises(frappe.PermissionError):
			self._run(docs, [_line(10, factory=6, zamalek=4)], actor=_manager_actor(user="dokki@jarz.test"))
		self.post.assert_not_called()
		self.assertTrue(all(d.status == svc.STATUS_PENDING for d in docs))

	def test_only_a_manager_can_match(self):
		docs = self._docs()
		for actor in (_branch_actor(), _factory_actor()):
			with self.assertRaises(frappe.PermissionError):
				self._run(docs, [_line(10, factory=6, zamalek=4)], actor=actor)
		self.post.assert_not_called()

	def test_a_batch_cannot_be_sent_back_to_its_sender(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError):
			self._run(docs, [_line(10, factory=6, dokki=4)])
		self.post.assert_not_called()

	def test_an_unknown_destination_is_refused(self):
		docs = self._docs()
		line = {"item_code": "JAR-L", "sent": 10, "splits": [{"warehouse": "Stores - J", "qty": 10}]}
		with self.assertRaises(frappe.ValidationError):
			self._run(docs, [line])

	def test_a_stock_shortage_leaves_every_member_pending_and_names_the_item(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(
				docs,
				[_line(10, factory=6, zamalek=4)],
				post_side_effect=NegativeStockError("negative stock"),
			)
		self.rollback.assert_called_once()
		self.assertIn("JAR-L", str(ctx.exception))
		for doc in docs:
			self.assertEqual(doc.status, svc.STATUS_PENDING)
			self.assertEqual(doc.saved, 0)
			self.assertIsNone(doc.stock_entry)
		self.push.assert_not_called()

	def test_a_posting_failure_shows_its_real_message(self):
		docs = self._docs()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(
				docs,
				[_line(10, factory=6, zamalek=4)],
				post_side_effect=frappe.ValidationError("Warehouse is frozen"),
			)
		self.assertIn("Warehouse is frozen", str(ctx.exception))
		self.assertTrue(all(d.status == svc.STATUS_PENDING for d in docs))

	def test_the_group_is_locked_before_its_members_and_members_go_in_name_order(self):
		docs = self._docs()
		events = []
		self._run(
			docs,
			[_line(10, factory=6, zamalek=4)],
			events=events,
			members=["HND-R2", "HND-D1", "HND-R1"],
		)
		self.assertEqual(
			events,
			[
				("group", DOKKI, True),
				("lock", "HND-D1"),
				("lock", "HND-R1"),
				("lock", "HND-R2"),
			],
		)

	def test_the_endpoint_is_post_only_and_forwards_to_the_service(self):
		methods = frappe.allowed_http_methods_for_whitelisted_func[handover_api.confirm_match]
		self.assertEqual(list(methods), ["POST"])
		with patch.object(svc, "confirm_match", return_value={"ok": True}) as confirm:
			out = handover_api.confirm_match(DOKKI, ["HND-D1"], [], note="n", pos_profile="Dokki")
		self.assertEqual(out, {"ok": True})
		confirm.assert_called_once_with(DOKKI, ["HND-D1"], [], note="n", pos_profile="Dokki")


class _StrictDb:
	"""``frappe.db`` with the v16 signatures of the calls the locking code makes."""

	def __init__(self, dispatches=(), receipts=(), source=DOKKI):
		self.dispatches = list(dispatches)
		self.receipts = list(receipts)
		self.source = source
		self.calls = []

	def get_values(
		self,
		doctype,
		filters=None,
		fieldname="name",
		ignore=None,
		as_dict=False,
		debug=False,
		order_by="KEEP_DEFAULT_ORDERING",
		update=None,
		distinct=False,
		*,
		for_update=False,
	):
		self.calls.append(("get_values", filters, order_by, for_update))
		is_dispatch = filters.get("kind") == svc.DOC_KIND_DISPATCH
		return list(self.dispatches if is_dispatch else self.receipts)

	def get_value(self, doctype, filters=None, fieldname="name", ignore=False, *, for_update=False):
		self.calls.append(("get_value", doctype, filters, for_update))
		return self.source


def _strict_get_all(
	doctype, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None, or_filters=None
):
	"""v16's ``get_all`` has no ``for_update``: passing one is a TypeError, as in production."""
	return []


class TestLockedReads(unittest.TestCase):
	"""The locking helpers against fakes with the exact v16 signatures (the v16 TypeError class of bug)."""

	def test_unlocked_reads_use_get_all_without_for_update(self):
		def get_all(doctype, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None):
			if pluck == "from_warehouse":
				return [DOKKI]
			return [
				{"name": "HND-B", "declared_at": "2026-10-20 09:00:00"},
				{"name": "HND-A", "declared_at": "2026-10-20 10:00:00"},
			]

		with patch.object(svc.frappe, "get_all", side_effect=get_all):
			self.assertEqual(svc._pending_dispatch_sources(), {DOKKI})
			self.assertEqual(svc._current_group_names(DOKKI), (["HND-B", "HND-A"], ["HND-B", "HND-A"]))

	def test_a_for_update_get_all_fails_like_production(self):
		with patch.object(svc.frappe, "get_all", side_effect=_strict_get_all):
			self.assertEqual(svc._pending_dispatch_sources(), set())
			with self.assertRaises(TypeError):
				svc.frappe.get_all(svc.DOCTYPE, filters={}, for_update=True)

	def test_lock_dispatches_takes_a_locking_get_values_on_one_source(self):
		db = _StrictDb(dispatches=[{"name": "HND-D1"}, {"name": "HND-D2"}])
		with (
			patch.object(svc.frappe, "db", db),
			patch.object(svc.frappe, "get_all", side_effect=_strict_get_all),
		):
			self.assertEqual(svc._lock_dispatches(DOKKI), ["HND-D1", "HND-D2"])
			self.assertEqual(svc._lock_dispatches(""), [])
		self.assertEqual(len(db.calls), 1)
		_name, filters, order_by, for_update = db.calls[0]
		self.assertTrue(for_update)
		self.assertEqual(order_by, "name asc")
		self.assertEqual(filters["from_warehouse"], DOKKI)
		self.assertEqual(filters["kind"], svc.DOC_KIND_DISPATCH)
		self.assertEqual(filters["status"], svc.STATUS_PENDING)

	def test_the_locked_group_read_locks_dispatches_then_receipts_and_never_uses_get_all(self):
		db = _StrictDb(
			dispatches=[{"name": "HND-D1", "declared_at": "2026-10-20 09:00:00"}],
			receipts=[
				{"name": "HND-R1", "declared_at": "2026-10-20 11:00:00"},
				{"name": "HND-R2", "declared_at": "2026-10-20 10:00:00"},
			],
		)

		def no_get_all(*args, **kwargs):
			raise AssertionError("a locked read must not go through get_all")

		with patch.object(svc.frappe, "db", db), patch.object(svc.frappe, "get_all", side_effect=no_get_all):
			dispatches, receipts = svc._current_group_names(DOKKI, for_update=True)
		self.assertEqual(dispatches, ["HND-D1"])
		# Locked in name order, reported oldest first.
		self.assertEqual(receipts, ["HND-R2", "HND-R1"])
		self.assertEqual([c[3] for c in db.calls], [True, True])
		self.assertEqual(db.calls[0][1]["kind"], svc.DOC_KIND_DISPATCH)
		self.assertEqual(db.calls[1][1]["kind"], svc.DOC_KIND_HANDOVER)
		self.assertEqual(db.calls[1][1]["declared_side"], svc.SIDE_TO)
		self.assertEqual({c[2] for c in db.calls}, {"name asc"})

	def test_a_group_with_no_dispatch_locks_no_receipts(self):
		db = _StrictDb(dispatches=[], receipts=[{"name": "HND-R1"}])
		with patch.object(svc.frappe, "db", db):
			self.assertEqual(svc._current_group_names(DOKKI, for_update=True), ([], []))
		self.assertEqual(len(db.calls), 1)

	def test_respond_and_cancel_lock_in_one_order_unlocked_source_dispatches_then_the_row(self):
		db = _StrictDb(dispatches=[{"name": "HND-D1"}], source=FACTORY)
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		get_doc_calls = []

		def get_doc(doctype, name, **kwargs):
			get_doc_calls.append((name, kwargs.get("for_update")))
			return doc

		with patch.object(svc.frappe, "db", db), patch.object(svc.frappe, "get_doc", side_effect=get_doc):
			locked, grouped = svc._lock_with_group("HND-2026-00001")
		self.assertIs(locked, doc)
		self.assertTrue(grouped)
		self.assertEqual([c[0] for c in db.calls], ["get_value", "get_values"])
		self.assertFalse(db.calls[0][3])
		self.assertEqual(db.calls[1][1]["from_warehouse"], FACTORY)
		self.assertTrue(db.calls[1][3])
		self.assertEqual(get_doc_calls, [("HND-2026-00001", True)])

	def test_a_source_with_no_pending_dispatch_is_not_grouped(self):
		db = _StrictDb(dispatches=[], source=FACTORY)
		with (
			patch.object(svc.frappe, "db", db),
			patch.object(svc.frappe, "get_doc", return_value=_pending("dokki@jarz.test", svc.SIDE_TO)),
		):
			_doc, grouped = svc._lock_with_group("HND-2026-00001")
		self.assertFalse(grouped)

	def test_declare_serializes_the_users_requests_before_the_dedupe_lookup(self):
		actor = _branch_actor()
		order = []

		def get_value(doctype, filters=None, fieldname="name", ignore=False, *, for_update=False):
			order.append(("get_value", doctype, filters, for_update))
			return filters

		def get_all(doctype, filters=None, **kwargs):
			order.append(("get_all", doctype))
			return []

		def fake_insert(**kwargs):
			return _pending(kwargs["user"], kwargs["declared_side"])

		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_allowed_item_codes", return_value={"JAR-L"}),
			patch.object(svc, "_dispatch_item_codes", return_value={"JAR-L"}),
			patch.object(svc, "_insert_handover", side_effect=fake_insert),
			patch.object(svc, "_pending_dispatch_sources", return_value=set()),
			patch.object(svc, "_enqueue_push"),
			patch.object(svc, "_maybe_push_balanced"),
			patch.object(svc, "_user_names", return_value={}),
			patch.object(svc.frappe.db, "get_value", side_effect=get_value),
			patch.object(svc.frappe, "get_all", side_effect=get_all),
		):
			svc.declare(
				DOKKI,
				FACTORY,
				"in",
				[{"item_code": "JAR-L", "qty": 1}],
				pos_profile="Dokki",
				client_request_id="req-7",
			)
		self.assertEqual(order[0], ("get_value", "User", actor.user, True))
		self.assertEqual(order[1], ("get_all", svc.DOCTYPE))


class TestMatchInputs(unittest.TestCase):
	def test_members_accept_json_and_drop_repeats(self):
		self.assertEqual(svc._parse_members('["A", "B", "A", ""]'), ["A", "B"])
		with self.assertRaises(frappe.ValidationError):
			svc._parse_members([])
		with self.assertRaises(frappe.ValidationError):
			svc._parse_members("not json")
		with self.assertRaises(frappe.ValidationError):
			svc._parse_members({"a": 1})

	def test_match_lines_are_finite_non_negative_and_three_decimals(self):
		out = svc._normalize_match_lines(
			[
				{"item_code": "JAR-L", "sent": "4.00049", "splits": [{"warehouse": FACTORY, "qty": 1.5}]},
				{"item_code": "JAR-L", "sent": 1, "splits": [{"warehouse": FACTORY, "qty": 0.25}]},
			]
		)
		self.assertEqual(out["JAR-L"]["sent"], 5.0)
		self.assertEqual(out["JAR-L"]["splits"], {FACTORY: 1.75})
		for bad in (-1, float("nan"), float("inf"), "abc"):
			with self.assertRaises(frappe.ValidationError):
				svc._normalize_match_lines([{"item_code": "JAR-L", "sent": bad, "splits": []}])
			with self.assertRaises(frappe.ValidationError):
				svc._normalize_match_lines(
					[{"item_code": "JAR-L", "sent": 1, "splits": [{"warehouse": FACTORY, "qty": bad}]}]
				)

	def test_match_lines_need_an_item_and_a_warehouse_per_split(self):
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_match_lines([{"item_code": "", "sent": 1, "splits": []}])
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_match_lines(
				[{"item_code": "JAR-L", "sent": 1, "splits": [{"warehouse": "", "qty": 1}]}]
			)
		with self.assertRaises(frappe.ValidationError):
			svc._normalize_match_lines([])


class TestStockHandoverContext(unittest.TestCase):
	def test_branch_side_only_with_an_open_shift(self):
		with patch.object(svc, "_build_actor", return_value=_branch_actor()):
			ctx = svc.get_context("Dokki")
		self.assertEqual(
			ctx["my_sides"],
			[{"warehouse": DOKKI, "label": "Dokki", "kind": "branch", "pos_profile": "Dokki"}],
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

	def test_a_rejected_dispatch_is_pushed_to_its_sender(self):
		doc = _dispatch("dokki@jarz.test", status=svc.STATUS_REJECTED, reject_reason="Not sent")
		self.assertEqual(svc._notify_side(doc, svc.EVENT_REJECTED), DOKKI)
		_title, body = svc._push_text(doc, svc.EVENT_REJECTED, _sides(), None)
		arabic, english = body.split("\n")
		self.assertIn("Manager rejected the handover (Dokki): Not sent", english)
		self.assertNotIn("→", english)
		self.assertNotIn("←", arabic)

	def test_push_body_is_arabic_first(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		title, body = svc._push_text(doc, svc.EVENT_DECLARED, _sides(), None)
		self.assertTrue(title)
		arabic, english = body.split("\n")
		self.assertIn("Dokki says it received 12 jars from Factory", english)
		self.assertTrue(any("؀" <= ch <= "ۿ" for ch in arabic))

	def test_batch_push_texts(self):
		dispatch = _dispatch("dokki@jarz.test")
		_t, body = svc._push_text(dispatch, svc.EVENT_BALANCED, _sides(), None, {"sent": 10})
		self.assertIn("Dokki's sent-out batch adds up (10 jars)", body.split("\n")[1])
		_t, body = svc._push_text(
			dispatch, svc.EVENT_BATCH_REMINDER, _sides(), None, {"hours": 14, "unmatched": 4}
		)
		self.assertIn("waited 14h", body.split("\n")[1])
		self.assertIn("4 jars unmatched", body.split("\n")[1])
		confirmed = _receipt(
			"factory@jarz.test", FACTORY, status=svc.STATUS_CONFIRMED, match_note="Factory: JAR-L 6 -> 5"
		)
		_t, body = svc._push_text(confirmed, svc.EVENT_BATCH_CONFIRMED, _sides(), "Mona")
		self.assertIn("Mona matched Dokki's shipment and confirmed 6 jars", body.split("\n")[1])
		self.assertIn("Factory: JAR-L 6 -> 5", body.split("\n")[1])
		rejected = _receipt("zamalek@jarz.test", ZAMALEK, status=svc.STATUS_REJECTED)
		_t, body = svc._push_text(rejected, svc.EVENT_BATCH_CONFIRMED, _sides(), "Mona")
		self.assertIn("not part of it", body.split("\n")[1])

	def test_batch_recipients(self):
		dispatch = _dispatch("dokki@jarz.test")
		with patch.object(svc, "_manager_recipients", return_value=["m1@jarz.test", "m2@jarz.test"]):
			for event in (svc.EVENT_BALANCED, svc.EVENT_BATCH_REMINDER):
				self.assertEqual(
					svc._recipients_for(dispatch, event, _sides()), ["m1@jarz.test", "m2@jarz.test"]
				)
		receipt = _receipt("factory@jarz.test", FACTORY)
		self.assertEqual(
			svc._recipients_for(receipt, svc.EVENT_BATCH_CONFIRMED, _sides()), ["factory@jarz.test"]
		)

	def test_notify_never_raises(self):
		doc = _pending("dokki@jarz.test", svc.SIDE_TO)
		with (
			patch.object(svc, "_suppressed", return_value=False),
			patch.object(svc, "_load_sides", side_effect=Exception("db down")),
			patch.object(svc, "_safe_log"),
		):
			result = svc.notify_stock_handover(doc, svc.EVENT_DECLARED, "dokki@jarz.test")
		self.assertFalse(result["ok"])


class TestStockHandoverReminders(unittest.TestCase):
	NOW = datetime(2026, 10, 21, 12, 0, 0)

	def _one_to_one(self, rows, docs=None):
		summary = {"checked": 0, "sent": 0, "errors": 0}
		cutoff = datetime(2026, 10, 21, 0, 0, 0)
		with (
			patch.object(svc, "_pending_dispatch_sources", return_value={FACTORY}),
			patch.object(svc.frappe, "get_all", return_value=rows) as get_all,
			patch.object(svc.frappe, "get_doc", side_effect=lambda dt, name: {"name": name}),
			patch.object(svc, "notify_stock_handover") as notify,
			patch.object(svc.frappe.db, "set_value") as set_value,
			patch.object(svc.frappe.db, "commit") as commit,
			patch.object(svc, "_now", return_value=self.NOW),
		):
			svc._remind_one_to_one(summary, cutoff, _sides())
		self.get_all, self.notify, self.set_value, self.commit = get_all, notify, set_value, commit
		return summary

	def test_the_reminder_filter_is_in_the_query_and_commits_after_each_row(self):
		rows = [
			{
				"name": "HND-A",
				"status": svc.STATUS_PENDING,
				"kind": svc.DOC_KIND_HANDOVER,
				"from_warehouse": ZAMALEK,
				"to_warehouse": DOKKI,
				"declared_side": svc.SIDE_TO,
				"awaiting_side": svc.SIDE_FROM,
			},
			{
				"name": "HND-B",
				"status": svc.STATUS_PENDING,
				"kind": svc.DOC_KIND_HANDOVER,
				"from_warehouse": DOKKI,
				"to_warehouse": ZAMALEK,
				"declared_side": svc.SIDE_FROM,
				"awaiting_side": svc.SIDE_TO,
			},
		]
		summary = self._one_to_one(rows)
		self.assertEqual(summary, {"checked": 2, "sent": 2, "errors": 0})
		or_filters = self.get_all.call_args.kwargs["or_filters"]
		self.assertIn([svc.DOCTYPE, "reminder_sent_at", "is", "not set"], or_filters)
		# Dispatches and grouped receipts are left out of the query, so they cannot
		# use up the 200-row window; and get_all gets no for_update.
		filters = self.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["kind"], svc.DOC_KIND_HANDOVER)
		self.assertEqual(filters["from_warehouse"], ["not in", [FACTORY]])
		self.assertNotIn("for_update", self.get_all.call_args.kwargs)
		self.assertEqual(self.commit.call_count, 2)
		self.assertEqual(self.notify.call_count, 2)

	def test_dispatches_and_grouped_receipts_are_not_reminded_one_to_one(self):
		rows = [
			{
				"name": "HND-D",
				"status": svc.STATUS_PENDING,
				"kind": svc.DOC_KIND_DISPATCH,
				"from_warehouse": DOKKI,
				"to_warehouse": None,
				"declared_side": svc.SIDE_FROM,
				"awaiting_side": svc.SIDE_MANAGER,
			},
			{
				# Factory has a Dispatch open (the stubbed sources), so this receipt is grouped.
				"name": "HND-R",
				"status": svc.STATUS_PENDING,
				"kind": svc.DOC_KIND_HANDOVER,
				"from_warehouse": FACTORY,
				"to_warehouse": DOKKI,
				"declared_side": svc.SIDE_TO,
				"awaiting_side": svc.SIDE_FROM,
			},
		]
		summary = self._one_to_one(rows)
		self.assertEqual(summary["sent"], 0)
		self.notify.assert_not_called()
		self.commit.assert_not_called()

	def test_a_side_that_is_not_known_any_more_is_skipped(self):
		rows = [
			{
				"name": "HND-GONE",
				"status": svc.STATUS_PENDING,
				"kind": svc.DOC_KIND_HANDOVER,
				"from_warehouse": "Closed Branch - J",
				"to_warehouse": DOKKI,
				"declared_side": svc.SIDE_TO,
				"awaiting_side": svc.SIDE_FROM,
			}
		]
		summary = self._one_to_one(rows)
		self.assertEqual(summary["sent"], 0)
		self.notify.assert_not_called()

	def _group_rows(self, reminder_sent_at=None, declared_at="2026-10-20 09:00:00"):
		return [
			_dispatch("dokki@jarz.test", name="HND-D1", qty=10.0, declared_at=declared_at),
			_receipt(
				"factory@jarz.test",
				FACTORY,
				name="HND-R1",
				qty=6.0,
				declared_at="2026-10-20 10:00:00",
				reminder_sent_at=reminder_sent_at,
			),
		]

	def _groups(self, rows):
		summary = {"checked": 0, "sent": 0, "errors": 0}
		cutoff = datetime(2026, 10, 21, 0, 0, 0)
		items = {d.name: [dict(r.__dict__) for r in d.items] for d in rows}
		with (
			patch.object(svc.frappe, "get_all", return_value=rows),
			patch.object(svc, "_load_items", return_value=items),
			patch.object(svc.frappe, "get_doc", side_effect=lambda dt, name: _FakeDoc(name=name)),
			patch.object(svc, "notify_stock_handover") as notify,
			patch.object(svc.frappe.db, "set_value") as set_value,
			patch.object(svc.frappe.db, "commit") as commit,
			patch.object(svc, "_now", return_value=self.NOW),
		):
			svc._remind_groups(summary, cutoff)
		self.notify, self.set_value, self.commit = notify, set_value, commit
		return summary

	def test_a_stale_group_is_pushed_to_the_managers_and_stamped_on_the_newest_dispatch(self):
		summary = self._groups(self._group_rows())
		self.assertEqual(summary["sent"], 1)
		self.notify.assert_called_once()
		self.assertEqual(self.notify.call_args.args[1], svc.EVENT_BATCH_REMINDER)
		extra = self.notify.call_args.kwargs["extra"]
		self.assertEqual(extra["hours"], 27)
		self.assertEqual(extra["unmatched"], 4.0)
		self.assertEqual(self.set_value.call_args.args[:3], (svc.DOCTYPE, "HND-D1", "reminder_sent_at"))
		self.commit.assert_called_once()

	def test_a_group_reminded_recently_or_still_young_is_left_alone(self):
		# Any member reminded after the cutoff holds the whole group back.
		self.assertEqual(self._groups(self._group_rows(reminder_sent_at="2026-10-21 03:00:00"))["sent"], 0)
		self.notify.assert_not_called()
		# The oldest member is under 12h old.
		young = self._group_rows(declared_at="2026-10-21 08:00:00")
		young[1].declared_at = "2026-10-21 09:00:00"
		self.assertEqual(self._groups(young)["sent"], 0)
		self.notify.assert_not_called()

	def test_the_sweep_never_raises(self):
		with (
			patch.object(svc, "_doctype_ready", side_effect=Exception("db down")),
			patch.object(svc, "_safe_log"),
		):
			summary = svc.remind_stale_handovers()
		self.assertEqual(summary["errors"], 1)


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

	def test_touches_branch_needs_both_sides_known_and_one_a_branch(self):
		with (
			patch.object(svc, "_doctype_ready", return_value=True),
			patch.object(svc, "_load_sides", return_value=_sides()),
		):
			self.assertTrue(svc.touches_branch(FACTORY, DOKKI))
			self.assertTrue(svc.touches_branch(DOKKI, ZAMALEK))
			self.assertFalse(svc.touches_branch(FACTORY, "Stores - J"))
			# A branch against Stores / Waste / WIP posts immediately.
			self.assertFalse(svc.touches_branch(ZAMALEK, "Stores - J"))
			self.assertFalse(svc.touches_branch("Waste - J", DOKKI))
			self.assertFalse(svc.touches_branch("Stores - J", "Waste - J"))


if __name__ == "__main__":
	unittest.main()
