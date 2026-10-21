"""Tests for stock-transit-matching.

Two halves:

* :mod:`jarz_pos.services.transit_match` -- the pure recommendation engine (no DB).
* The transit side of :mod:`jarz_pos.services.stock_handover` -- the board, approving /
  rejecting a pool receipt, write-off / return of a send, cancelling a send, and the
  transit-warehouse resolution. Fully mocked like ``test_api_stock_handover``: no row,
  no DocType, no Stock Entry is posted (CI runs before ``bench migrate``).
"""

import types
import unittest
from datetime import datetime
from unittest.mock import patch

import frappe

from jarz_pos.api import transfer as transfer_api
from jarz_pos.services import stock_handover as svc
from jarz_pos.services import transit_match as tm
from jarz_pos.tests.test_api_stock_handover import (
	DOKKI,
	FACTORY,
	ZAMALEK,
	NegativeStockError,
	_branch_actor,
	_dispatch,
	_item_meta,
	_manager_actor,
	_receipt,
)

HELIOPOLIS = "Heliopolis - J"
TRANSIT = "Goods In Transit - J"

_NOW_PATCH = patch.object(svc, "_now", return_value=datetime(2026, 10, 20, 12, 0, 0))


def setUpModule():
	_NOW_PATCH.start()


def tearDownModule():
	_NOW_PATCH.stop()


def _codes(reasons):
	return [r["code"] for r in reasons]


def _send(name, source, lines, at="2026-10-20 08:00:00"):
	return {"name": name, "source": source, "declared_at": at, "lines": lines}


def _rcpt(name, target, lines, at="2026-10-20 09:00:00", named=None):
	return {"name": name, "target": target, "declared_at": at, "named_source": named, "lines": lines}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TestRecommendEngine(unittest.TestCase):
	def test_exact_unique_pair_is_high_confidence(self):
		out = tm.recommend([_send("S1", FACTORY, {"A": 10})], [_rcpt("R1", DOKKI, {"A": 10})])
		line = out["R1"][0]
		self.assertEqual(line["qty"], 10.0)
		self.assertEqual(line["unresolved_qty"], 0.0)
		self.assertEqual(len(line["suggestions"]), 1)
		sug = line["suggestions"][0]
		self.assertEqual(sug["dispatch"], "S1")
		self.assertEqual(sug["source_warehouse"], FACTORY)
		self.assertEqual(sug["qty"], 10.0)
		self.assertEqual(sug["confidence"], tm.CONF_HIGH)
		self.assertIn(tm.R_EXACT, _codes(sug["reasons"]))
		self.assertIn(tm.R_ONLY_SENDER, _codes(sug["reasons"]))
		self.assertIn(tm.R_SENT_BEFORE, _codes(sug["reasons"]))

	def test_blueberry_two_senders_two_receivers(self):
		# Factory sends 12, Zamalek sends 5; Dokki receives 12, Heliopolis receives 5.
		sends = [
			_send("SF", FACTORY, {"BB": 12}, at="2026-10-20 08:00:00"),
			_send("SZ", ZAMALEK, {"BB": 5}, at="2026-10-20 08:30:00"),
		]
		receipts = [
			_rcpt("RD", DOKKI, {"BB": 12}, at="2026-10-20 09:00:00"),
			_rcpt("RH", HELIOPOLIS, {"BB": 5}, at="2026-10-20 09:10:00"),
		]
		out = tm.recommend(sends, receipts)
		rd = out["RD"][0]["suggestions"]
		rh = out["RH"][0]["suggestions"]
		self.assertEqual([(s["dispatch"], s["qty"]) for s in rd], [("SF", 12.0)])
		self.assertEqual([(s["dispatch"], s["qty"]) for s in rh], [("SZ", 5.0)])
		self.assertEqual(rd[0]["confidence"], tm.CONF_HIGH)
		self.assertEqual(rh[0]["confidence"], tm.CONF_HIGH)

	def test_whole_shipment_from_the_one_send_that_covers_every_line(self):
		sends = [
			_send("S1", FACTORY, {"A": 10, "B": 10}),
			_send("S2", ZAMALEK, {"A": 10}),
		]
		out = tm.recommend(sends, [_rcpt("R1", DOKKI, {"A": 6, "B": 4})])
		for line in out["R1"]:
			self.assertEqual(len(line["suggestions"]), 1)
			sug = line["suggestions"][0]
			self.assertEqual(sug["dispatch"], "S1")
			self.assertEqual(sug["qty"], line["qty"])
			self.assertEqual(sug["confidence"], tm.CONF_HIGH)
			self.assertEqual(sug["reasons"][0]["code"], tm.R_SAME_SHIPMENT)

	def test_a_named_sender_restricts_the_receipt(self):
		sends = [_send("S1", FACTORY, {"A": 5}), _send("S2", ZAMALEK, {"A": 5})]
		out = tm.recommend(sends, [_rcpt("R1", DOKKI, {"A": 5}, named=ZAMALEK)])
		sug = out["R1"][0]["suggestions"]
		self.assertEqual([s["dispatch"] for s in sug], ["S2"])
		self.assertIn(tm.R_NAMED, _codes(sug[0]["reasons"]))

	def test_a_send_never_supplies_its_own_sender(self):
		out = tm.recommend(
			[_send("S1", DOKKI, {"A": 5})], [_rcpt("R1", DOKKI, {"A": 5})], default_source=FACTORY
		)
		line = out["R1"][0]
		self.assertEqual(len(line["suggestions"]), 1)
		sug = line["suggestions"][0]
		self.assertIsNone(sug["dispatch"])
		self.assertEqual(sug["source_warehouse"], FACTORY)

	def test_no_send_gives_one_low_direct_suggestion(self):
		out = tm.recommend([], [_rcpt("R1", DOKKI, {"A": 3})], default_source=FACTORY)
		line = out["R1"][0]
		sug = line["suggestions"][0]
		self.assertIsNone(sug["dispatch"])
		self.assertEqual(sug["source_warehouse"], FACTORY)
		self.assertEqual(sug["qty"], 3.0)
		self.assertEqual(sug["confidence"], tm.CONF_LOW)
		self.assertEqual(sug["reasons"][0]["code"], tm.R_NO_SEND)
		self.assertEqual(line["unresolved_qty"], 0.0)

	def test_direct_suggestion_prefers_the_usual_supplier(self):
		out = tm.recommend(
			[], [_rcpt("R1", DOKKI, {"A": 3})], affinity={(ZAMALEK, DOKKI): 0.8}, default_source=FACTORY
		)
		sug = out["R1"][0]["suggestions"][0]
		self.assertEqual(sug["source_warehouse"], ZAMALEK)
		self.assertIn(tm.R_USUAL_SUPPLIER, _codes(sug["reasons"]))

	def test_a_short_send_is_topped_up_from_the_next_one(self):
		sends = [
			_send("S1", FACTORY, {"A": 4}, at="2026-10-20 08:00:00"),
			_send("S2", ZAMALEK, {"A": 3}, at="2026-10-20 08:10:00"),
		]
		out = tm.recommend(sends, [_rcpt("R1", DOKKI, {"A": 6})])
		line = out["R1"][0]
		self.assertEqual({s["dispatch"] for s in line["suggestions"]}, {"S1", "S2"})
		self.assertAlmostEqual(sum(s["qty"] for s in line["suggestions"]), 6.0)
		for sug in line["suggestions"]:
			self.assertEqual(sug["confidence"], tm.CONF_LOW)
			self.assertIn(tm.R_SPLIT, _codes(sug["reasons"]))
		self.assertEqual(line["unresolved_qty"], 0.0)

	def test_an_exhausted_send_leaves_the_rest_unresolved(self):
		sends = [_send("S1", FACTORY, {"A": 5})]
		receipts = [
			_rcpt("R1", DOKKI, {"A": 5}, at="2026-10-20 09:00:00"),
			_rcpt("R2", ZAMALEK, {"A": 3}, at="2026-10-20 09:30:00"),
		]
		out = tm.recommend(sends, receipts, default_source=FACTORY)
		self.assertEqual(out["R1"][0]["suggestions"][0]["dispatch"], "S1")
		r2 = out["R2"][0]
		self.assertEqual(r2["suggestions"], [])
		self.assertEqual(r2["unresolved_qty"], 3.0)

	def test_a_send_is_never_over_allocated(self):
		sends = [_send("S1", FACTORY, {"A": 5})]
		receipts = [
			_rcpt("R1", DOKKI, {"A": 4}, at="2026-10-20 09:00:00"),
			_rcpt("R2", ZAMALEK, {"A": 4}, at="2026-10-20 09:30:00"),
		]
		out = tm.recommend(sends, receipts)
		used = sum(
			s["qty"] for r in out.values() for line in r for s in line["suggestions"] if s["dispatch"] == "S1"
		)
		self.assertAlmostEqual(used, 5.0)
		self.assertEqual(out["R1"][0]["suggestions"][0]["qty"], 4.0)
		self.assertEqual(out["R2"][0]["suggestions"][0]["qty"], 1.0)
		self.assertEqual(out["R2"][0]["unresolved_qty"], 3.0)

	def test_affinity_from_totals(self):
		shares = tm.affinity_from_totals(
			[
				(FACTORY, DOKKI, 30),
				(ZAMALEK, DOKKI, 10),
				(FACTORY, ZAMALEK, 5),
				(DOKKI, DOKKI, 9),  # self: ignored
				(ZAMALEK, DOKKI, 0),  # zero: ignored
				(None, DOKKI, 4),  # unknown source: ignored
			]
		)
		self.assertEqual(shares, {(FACTORY, DOKKI): 0.75, (ZAMALEK, DOKKI): 0.25, (FACTORY, ZAMALEK): 1.0})


# ---------------------------------------------------------------------------
# Service fixtures
# ---------------------------------------------------------------------------


def _tsend(name="HND-S1", declared_by="dokki@jarz.test", from_wh=DOKKI, qty=10.0, **extra):
	"""A transit-backed send (its stock already sits in TRANSIT)."""
	extra.setdefault("transit_entry", "MAT-STE-T1")
	extra.setdefault("transit_warehouse", TRANSIT)
	extra.setdefault("company", "JARZ")
	return _dispatch(declared_by, from_wh=from_wh, qty=qty, name=name, **extra)


def _pool_receipt(
	name="HND-R1", declared_by="zamalek@jarz.test", to_wh=ZAMALEK, qty=6.0, from_wh=None, **extra
):
	return _receipt(declared_by, to_wh, from_wh=from_wh, qty=qty, name=name, **extra)


def _director_actor(user="director@jarz.test"):
	"""A manager with one-step stock power (ROLES.STOCK_TRANSFER_DIRECT)."""
	return svc._Actor(
		user=user,
		roles=["Stock Manager", "JARZ Manager"],
		sides=_manager_actor().sides,
		assigned_profiles=[],
		open_profiles=[],
	)


def _alloc(qty, dispatch="HND-S1", source=DOKKI, **extra):
	data = {"item_code": "JAR-L", "dispatch": dispatch, "source_warehouse": source, "qty": qty}
	data.update(extra)
	return data


class _SE:
	def __init__(self, name="MAT-STE-2026-00088"):
		self.name = name
		self.items = []
		self.flags = types.SimpleNamespace()

	def append(self, key, row):
		self.items.append(row)


class _Record:
	def __init__(self, payload, sink):
		self.payload = payload
		self.flags = types.SimpleNamespace()
		self._sink = sink

	def insert(self, ignore_permissions=False):
		self._sink.append(self.payload)


# ---------------------------------------------------------------------------
# approve_receipt
# ---------------------------------------------------------------------------


class TestApproveReceipt(unittest.TestCase):
	def _approve(
		self,
		docs,
		allocations,
		actor=None,
		received=None,
		version=None,
		board_version="v1",
		transit_sources=(DOKKI, FACTORY),
		post_side_effect=None,
		receipt="HND-R1",
		dead=(),
	):
		actor = actor or _manager_actor()
		by_name = {d.name: d for d in docs}
		se = _SE()
		records = []
		events = []

		def fake_lock(name):
			events.append(name)
			if name not in by_name:
				raise frappe.DoesNotExistError(name)
			return by_name[name]

		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_lock", side_effect=fake_lock),
			patch.object(svc, "_transit_sources", return_value=set(transit_sources)),
			patch.object(svc, "_current_board_version", return_value=board_version),
			patch.object(svc, "_live_transit_entries", side_effect=lambda names: set(names) - set(dead)),
			patch.object(svc, "_item_meta", side_effect=_item_meta),
			patch.object(svc, "_safe_log"),
			patch.object(svc, "_enqueue_push") as push,
			patch.object(svc, "_after_dispatch_closed") as closed_cb,
			patch.object(transfer_api, "_build_material_transfer", return_value=se) as build,
			patch.object(transfer_api, "_insert_and_submit_stock_entry", side_effect=post_side_effect) as post,
			patch.object(svc.frappe.db, "savepoint"),
			patch.object(svc.frappe.db, "rollback") as rollback,
			patch.object(svc.frappe.db, "get_value", return_value=3),
			patch.object(svc.frappe, "get_doc", side_effect=lambda payload: _Record(payload, records)),
		):
			try:
				result = svc.approve_receipt(receipt, allocations, received=received, version=version)
			finally:
				self.se, self.build, self.post, self.push = se, build, post, push
				self.closed_cb, self.rollback, self.records, self.events = closed_cb, rollback, records, events
		return result

	def test_partial_match_posts_from_transit_and_leaves_the_send_open(self):
		send, rcpt = _tsend(), _pool_receipt()
		result = self._approve([send, rcpt], [_alloc(6, was_recommended=1, confidence="high")])

		# Lock order: the referenced sends first, then the receipt.
		self.assertEqual(self.events, ["HND-S1", "HND-R1"])
		self.build.assert_called_once()
		self.assertEqual(
			self.build.call_args.args[:3], (TRANSIT, ZAMALEK, [{"item_code": "JAR-L", "qty": 6.0}])
		)
		self.assertEqual(self.se.items, [])
		self.post.assert_called_once()

		self.assertEqual(
			result, {"ok": True, "stock_entry": self.se.name, "receipt": "HND-R1", "closed_sends": []}
		)
		self.assertEqual(rcpt.status, svc.STATUS_CONFIRMED)
		self.assertEqual(rcpt.stock_entry, self.se.name)
		self.assertEqual(rcpt.items[0].matched_qty, 6.0)
		self.assertTrue(rcpt.match_note.startswith(svc._TRANSIT_NOTE_PREFIX))

		self.assertEqual(send.status, svc.STATUS_PENDING)
		self.assertEqual(send.items[0].matched_qty, 6.0)
		self.assertEqual(svc._line_remaining(send.items[0]), 4.0)
		self.closed_cb.assert_not_called()

		self.assertEqual(len(self.records), 1)
		rec = self.records[0]
		self.assertEqual(rec["doctype"], svc.TRANSIT_MATCH_DOCTYPE)
		self.assertEqual(
			(rec["receipt"], rec["dispatch"], rec["source_warehouse"], rec["target_warehouse"], rec["qty"]),
			("HND-R1", "HND-S1", DOKKI, ZAMALEK, 6.0),
		)
		self.assertEqual((rec["was_recommended"], rec["confidence"]), (1, "high"))
		self.push.assert_called_once()
		self.assertEqual(self.push.call_args.args[:2], ("HND-R1", svc.EVENT_CONFIRMED))

	def test_full_match_closes_the_send(self):
		send, rcpt = _tsend(qty=6.0), _pool_receipt(qty=6.0)
		result = self._approve([send, rcpt], [_alloc(6)])
		self.assertEqual(result["closed_sends"], ["HND-S1"])
		self.assertEqual(send.status, svc.STATUS_CONFIRMED)
		self.closed_cb.assert_called_once()
		self.assertEqual(self.closed_cb.call_args.args[0], DOKKI)

	def test_send_plus_direct_allocation_is_one_stock_entry(self):
		send, rcpt = _tsend(qty=4.0), _pool_receipt(qty=6.0)
		self._approve(
			[send, rcpt], [_alloc(4), _alloc(2, dispatch=None, source=FACTORY)], actor=_director_actor()
		)
		self.build.assert_called_once()
		self.assertEqual(self.build.call_args.args[:2], (TRANSIT, ZAMALEK))
		self.assertEqual(
			self.se.items,
			[{"item_code": "JAR-L", "uom": "Nos", "qty": 2.0, "s_warehouse": FACTORY, "t_warehouse": ZAMALEK}],
		)
		self.post.assert_called_once()
		self.assertEqual([r["dispatch"] for r in self.records], ["HND-S1", None])

	def test_manager_corrects_what_arrived(self):
		send, rcpt = _tsend(), _pool_receipt(qty=6.0)
		self._approve([send, rcpt], [_alloc(5)], received={"JAR-L": 5})
		self.assertEqual(rcpt.items[0].qty, 5.0)
		self.assertEqual(rcpt.items[0].declared_qty, 6.0)
		self.assertEqual(rcpt.was_changed, 1)

	def _refused(self, exc, docs, allocations, **kw):
		with self.assertRaises(exc):
			self._approve(docs, allocations, **kw)
		self.post.assert_not_called()
		self.assertEqual(self.records, [])
		for doc in docs:
			self.assertEqual(doc.status, svc.STATUS_PENDING)

	def test_allocations_must_add_up_to_what_arrived(self):
		self._refused(frappe.ValidationError, [_tsend(), _pool_receipt(qty=6.0)], [_alloc(5)])

	def test_a_send_cannot_give_more_than_it_has_left(self):
		self._refused(frappe.ValidationError, [_tsend(qty=4.0), _pool_receipt(qty=6.0)], [_alloc(6)])

	def test_a_send_already_partly_used_counts_only_its_remaining(self):
		send = _tsend(qty=10.0)
		send.items[0].matched_qty = 7.0
		self._refused(frappe.ValidationError, [send, _pool_receipt(qty=6.0)], [_alloc(6)])

	def test_the_receipts_declarer_cannot_approve_it(self):
		self._refused(
			frappe.PermissionError,
			[_tsend(), _pool_receipt()],
			[_alloc(6)],
			actor=_manager_actor("zamalek@jarz.test"),
		)

	def test_the_sends_declarer_cannot_approve_with_it(self):
		self._refused(
			frappe.PermissionError,
			[_tsend(), _pool_receipt()],
			[_alloc(6)],
			actor=_manager_actor("dokki@jarz.test"),
		)

	def test_a_branch_user_cannot_approve(self):
		self._refused(frappe.PermissionError, [_tsend(), _pool_receipt()], [_alloc(6)], actor=_branch_actor())

	def test_the_allocation_must_name_the_sends_real_sender(self):
		self._refused(frappe.ValidationError, [_tsend(), _pool_receipt()], [_alloc(6, source=FACTORY)])

	def test_a_direct_allocation_cannot_come_from_the_receiver(self):
		self._refused(
			frappe.ValidationError,
			[_pool_receipt()],
			[_alloc(6, dispatch=None, source=ZAMALEK)],
			actor=_director_actor(),
		)

	def test_a_stale_version_is_drift(self):
		self._refused(frappe.ValidationError, [_tsend(), _pool_receipt()], [_alloc(6)], version="old")

	def test_a_vanished_send_is_drift(self):
		self._refused(frappe.ValidationError, [_pool_receipt()], [_alloc(6)])

	def test_a_closed_send_is_drift(self):
		send = _tsend(status=svc.STATUS_CANCELLED)
		with self.assertRaises(frappe.ValidationError):
			self._approve([send, _pool_receipt()], [_alloc(6)])
		self.post.assert_not_called()

	def test_nothing_received_means_reject_instead(self):
		self._refused(frappe.ValidationError, [_tsend(), _pool_receipt()], [], received={"JAR-L": 0})

	def test_a_receipt_naming_a_sender_without_transit_stays_one_to_one(self):
		self._refused(
			frappe.ValidationError,
			[_pool_receipt(from_wh=FACTORY)],
			[_alloc(6, dispatch=None, source=FACTORY)],
			transit_sources=(DOKKI,),
		)

	def test_a_shortage_records_nothing(self):
		send, rcpt = _tsend(), _pool_receipt()
		with self.assertRaises(frappe.ValidationError):
			self._approve([send, rcpt], [_alloc(6)], post_side_effect=NegativeStockError("short"))
		self.rollback.assert_called_once()
		self.assertEqual(rcpt.status, svc.STATUS_PENDING)
		self.assertEqual(self.records, [])
		self.assertFalse(send.items[0].get("matched_qty"))

	# --- a transit entry cancelled outside the app ---------------------------

	def test_a_send_whose_transit_entry_was_cancelled_is_refused(self):
		send, rcpt = _tsend(), _pool_receipt()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._approve([send, rcpt], [_alloc(6)], dead=("MAT-STE-T1",))
		self.assertIn("no longer submitted", str(ctx.exception))
		self.post.assert_not_called()
		self.assertEqual(rcpt.status, svc.STATUS_PENDING)
		self.assertFalse(send.items[0].get("matched_qty"))

	# --- one-step power ------------------------------------------------------

	def test_a_line_manager_cannot_allocate_directly(self):
		self._refused(
			frappe.PermissionError, [_pool_receipt()], [_alloc(6, dispatch=None, source=FACTORY)]
		)

	def test_a_director_may_allocate_directly(self):
		rcpt = _pool_receipt()
		self._approve([rcpt], [_alloc(6, dispatch=None, source=FACTORY)], actor=_director_actor())
		self.assertEqual(self.build.call_args.args[:2], (FACTORY, ZAMALEK))
		self.assertEqual(rcpt.status, svc.STATUS_CONFIRMED)

	def test_a_line_manager_cannot_correct_received_up(self):
		self._refused(
			frappe.PermissionError,
			[_tsend(), _pool_receipt(qty=6.0)],
			[_alloc(7)],
			received={"JAR-L": 7},
		)

	def test_a_director_may_correct_received_up(self):
		send, rcpt = _tsend(), _pool_receipt(qty=6.0)
		self._approve([send, rcpt], [_alloc(7)], received={"JAR-L": 7}, actor=_director_actor())
		self.assertEqual(rcpt.items[0].qty, 7.0)
		self.assertEqual(rcpt.status, svc.STATUS_CONFIRMED)

	# --- overriding the named sender ----------------------------------------

	def test_overriding_the_named_sender_is_recorded(self):
		send, rcpt = _tsend(), _pool_receipt(from_wh=FACTORY)
		self._approve([send, rcpt], [_alloc(6)])
		self.assertEqual(rcpt.status, svc.STATUS_CONFIRMED)
		self.assertIn(
			"Sender overridden by manager@jarz.test: receipt named Factory, used Dokki", rcpt.match_note
		)

	def test_using_the_named_sender_adds_no_override_line(self):
		send, rcpt = _tsend(), _pool_receipt(from_wh=DOKKI)
		self._approve([send, rcpt], [_alloc(6)])
		self.assertNotIn("overridden", rcpt.match_note)

	# --- stale drift ---------------------------------------------------------

	def test_over_allocation_with_a_version_is_drift(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._approve(
				[_tsend(qty=4.0), _pool_receipt(qty=6.0)], [_alloc(6)], version="v1", board_version="v1"
			)
		self.assertIn(svc._DRIFT_MESSAGE, str(ctx.exception))
		self.post.assert_not_called()

	def test_over_allocation_without_a_version_names_what_is_left(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._approve([_tsend(qty=4.0), _pool_receipt(qty=6.0)], [_alloc(6)])
		self.assertIn("left in transit", str(ctx.exception))
		self.assertNotIn(svc._DRIFT_MESSAGE, str(ctx.exception))


# ---------------------------------------------------------------------------
# reject_receipt
# ---------------------------------------------------------------------------


class TestRejectReceipt(unittest.TestCase):
	def _reject(self, doc, actor=None, transit_sources=(DOKKI,)):
		with (
			patch.object(svc, "_build_actor", return_value=actor or _manager_actor()),
			patch.object(svc, "_lock_with_group", return_value=(doc, False)),
			patch.object(svc, "_transit_sources", return_value=set(transit_sources)),
			patch.object(svc, "_pending_dispatch_sources", return_value=set()),
			patch.object(svc, "_user_names", return_value={}),
			patch.object(svc, "_enqueue_push") as push,
		):
			result = svc.reject_receipt(doc.name, "nothing came")
		self.push = push
		return result

	def test_a_manager_rejects_and_nothing_posts(self):
		doc = _pool_receipt()
		result = self._reject(doc)
		self.assertTrue(result["ok"])
		self.assertEqual(doc.status, svc.STATUS_REJECTED)
		self.assertEqual(doc.reject_reason, "nothing came")
		self.assertIsNone(doc.stock_entry)
		self.assertEqual(self.push.call_args.args[:2], (doc.name, svc.EVENT_REJECTED))

	def test_its_declarer_cannot_reject_it(self):
		with self.assertRaises(frappe.PermissionError):
			self._reject(_pool_receipt(), actor=_manager_actor("zamalek@jarz.test"))


# ---------------------------------------------------------------------------
# write_off_transit / return_to_sender
# ---------------------------------------------------------------------------


class TestSettleSend(unittest.TestCase):
	def _settle(
		self, fn, send, lines, reason="broken", actor=None, account="Stock Adjustment - J", dead=()
	):
		se = _SE("MAT-STE-W1")
		company_fields = {"stock_adjustment_account": account, "cost_center": "Main - J"}
		with (
			patch.object(svc, "_build_actor", return_value=actor or _manager_actor()),
			patch.object(svc, "_lock", return_value=send),
			patch.object(svc, "_live_transit_entries", side_effect=lambda names: set(names) - set(dead)),
			patch.object(svc, "_item_meta", side_effect=_item_meta),
			patch.object(svc, "_safe_log"),
			patch.object(svc, "_after_dispatch_closed") as closed_cb,
			patch.object(svc.frappe, "new_doc", return_value=se),
			patch.object(transfer_api, "_build_material_transfer", return_value=se) as build,
			patch.object(transfer_api, "_insert_and_submit_stock_entry") as post,
			patch.object(svc.frappe.db, "savepoint"),
			patch.object(svc.frappe.db, "rollback"),
			patch.object(
				svc.frappe.db, "get_value", side_effect=lambda dt, name, field, *a, **k: company_fields.get(field)
			),
		):
			try:
				result = fn(send.name, lines, reason=reason)
			finally:
				self.se, self.build, self.post, self.closed_cb = se, build, post, closed_cb
		return result

	def test_write_off_is_a_material_issue_from_transit(self):
		send = _tsend(qty=10.0)
		result = self._settle(svc.write_off_transit, send, [{"item_code": "JAR-L", "qty": 3}])
		self.assertEqual(
			result, {"ok": True, "stock_entry": "MAT-STE-W1", "dispatch": "HND-S1", "closed": False}
		)
		self.assertEqual(self.se.stock_entry_type, "Material Issue")
		self.assertEqual(self.se.company, "JARZ")
		self.assertEqual(
			self.se.items,
			[
				{
					"item_code": "JAR-L",
					"uom": "Nos",
					"qty": 3.0,
					"s_warehouse": TRANSIT,
					"expense_account": "Stock Adjustment - J",
					"cost_center": "Main - J",
				}
			],
		)
		self.post.assert_called_once()
		self.assertEqual(send.items[0].written_off_qty, 3.0)
		self.assertEqual(send.status, svc.STATUS_PENDING)
		self.assertIn("Written off", send.match_note)
		self.closed_cb.assert_not_called()

	def test_writing_off_the_rest_closes_the_send(self):
		send = _tsend(qty=10.0)
		send.items[0].matched_qty = 6.0
		result = self._settle(svc.write_off_transit, send, [{"item_code": "JAR-L", "qty": 4}])
		self.assertTrue(result["closed"])
		self.assertEqual(send.status, svc.STATUS_CONFIRMED)
		self.closed_cb.assert_called_once()
		self.assertEqual(self.closed_cb.call_args.args[0], DOKKI)

	def test_write_off_needs_a_reason(self):
		with self.assertRaises(frappe.ValidationError):
			self._settle(svc.write_off_transit, _tsend(), [{"item_code": "JAR-L", "qty": 1}], reason="  ")
		self.post.assert_not_called()

	def test_write_off_cannot_exceed_the_remaining(self):
		send = _tsend(qty=10.0)
		send.items[0].matched_qty = 8.0
		with self.assertRaises(frappe.ValidationError):
			self._settle(svc.write_off_transit, send, [{"item_code": "JAR-L", "qty": 3}])
		self.post.assert_not_called()

	def test_write_off_needs_the_stock_adjustment_account(self):
		with self.assertRaises(frappe.ValidationError):
			self._settle(svc.write_off_transit, _tsend(), [{"item_code": "JAR-L", "qty": 1}], account=None)
		self.post.assert_not_called()

	def test_the_sends_declarer_cannot_settle_it(self):
		with self.assertRaises(frappe.PermissionError):
			self._settle(
				svc.write_off_transit,
				_tsend(),
				[{"item_code": "JAR-L", "qty": 1}],
				actor=_manager_actor("dokki@jarz.test"),
			)

	def test_return_moves_transit_back_to_the_sender(self):
		send = _tsend(qty=10.0)
		result = self._settle(svc.return_to_sender, send, [{"item_code": "JAR-L", "qty": 4}], reason=None)
		self.build.assert_called_once()
		self.assertEqual(
			self.build.call_args.args[:3], (TRANSIT, DOKKI, [{"item_code": "JAR-L", "qty": 4.0}])
		)
		self.post.assert_called_once()
		self.assertEqual(send.items[0].returned_qty, 4.0)
		self.assertFalse(result["closed"])

	def test_a_cancelled_transit_entry_cannot_be_written_off(self):
		send = _tsend()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._settle(svc.write_off_transit, send, [{"item_code": "JAR-L", "qty": 1}], dead=("MAT-STE-T1",))
		self.assertIn("no longer submitted", str(ctx.exception))
		self.post.assert_not_called()
		self.assertFalse(send.items[0].get("written_off_qty"))

	def test_a_cancelled_transit_entry_cannot_be_returned(self):
		with self.assertRaises(frappe.ValidationError):
			self._settle(
				svc.return_to_sender, _tsend(), [{"item_code": "JAR-L", "qty": 1}], dead=("MAT-STE-T1",)
			)
		self.post.assert_not_called()


# ---------------------------------------------------------------------------
# cancel a send, confirm_match refusal, transit warehouse resolution
# ---------------------------------------------------------------------------


class TestCancelTransitSend(unittest.TestCase):
	def _cancel(self, send, actor):
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_lock_with_group", return_value=(send, True)),
			patch.object(svc, "_cancel_stock_entry") as cancel_se,
			patch.object(svc, "_after_dispatch_closed") as closed_cb,
			patch.object(svc, "_pending_dispatch_sources", return_value=set()),
			patch.object(svc, "_user_names", return_value={}),
			patch.object(svc, "_safe_log"),
			patch.object(svc.frappe.db, "savepoint"),
			patch.object(svc.frappe.db, "rollback"),
		):
			try:
				return svc.cancel(send.name)
			finally:
				self.cancel_se, self.closed_cb = cancel_se, closed_cb

	def test_its_declarer_cancels_and_the_transit_entry_is_cancelled(self):
		send = _tsend()
		self._cancel(send, _branch_actor())
		self.cancel_se.assert_called_once_with("MAT-STE-T1")
		self.assertEqual(send.status, svc.STATUS_CANCELLED)
		self.closed_cb.assert_called_once()

	def test_a_manager_may_cancel_it(self):
		send = _tsend()
		self._cancel(send, _manager_actor())
		self.assertEqual(send.status, svc.STATUS_CANCELLED)

	def test_another_branch_user_cannot(self):
		with self.assertRaises(frappe.PermissionError):
			self._cancel(_tsend(), _branch_actor(user="other@jarz.test"))
		self.cancel_se.assert_not_called()

	def test_after_any_movement_it_cannot_be_cancelled(self):
		send = _tsend()
		send.items[0].matched_qty = 1.0
		with self.assertRaises(frappe.ValidationError):
			self._cancel(send, _manager_actor())
		self.cancel_se.assert_not_called()
		self.assertEqual(send.status, svc.STATUS_PENDING)


class TestConfirmMatchRefusesTransit(unittest.TestCase):
	def test_a_transit_send_in_the_group_is_refused(self):
		send = _tsend(name="HND-D1")
		rcpt = _receipt("factory@jarz.test", FACTORY, from_wh=DOKKI, name="HND-R1", qty=10.0)
		docs = {d.name: d for d in (send, rcpt)}
		with (
			patch.object(svc, "_build_actor", return_value=_manager_actor()),
			patch.object(svc, "_current_group_names", return_value=(["HND-D1"], ["HND-R1"])),
			patch.object(svc, "_lock", side_effect=lambda n: docs[n]),
			patch.object(transfer_api, "_insert_and_submit_stock_entry") as post,
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				svc.confirm_match(
					DOKKI,
					["HND-D1", "HND-R1"],
					[{"item_code": "JAR-L", "sent": 10, "splits": [{"warehouse": FACTORY, "qty": 10}]}],
				)
		self.assertIn("Update the app", str(ctx.exception))
		post.assert_not_called()


class TestResolveTransitWarehouse(unittest.TestCase):
	def _resolve(self, default, found):
		with (
			patch.object(svc.frappe.db, "get_value", return_value=default),
			patch.object(svc.frappe, "get_all", return_value=found),
		):
			return svc._resolve_transit_warehouse("JARZ")

	def test_the_company_default_wins(self):
		self.assertEqual(self._resolve(TRANSIT, ["Other Transit - J", "X - J"]), TRANSIT)

	def test_a_single_transit_warehouse_is_used(self):
		self.assertEqual(self._resolve(None, [TRANSIT]), TRANSIT)

	def test_none_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._resolve(None, [])
		self.assertIn("No transit warehouse", str(ctx.exception))

	def test_several_need_the_company_default(self):
		with self.assertRaises(frappe.ValidationError):
			self._resolve(None, [TRANSIT, "Other Transit - J"])


# ---------------------------------------------------------------------------
# get_transit_board
# ---------------------------------------------------------------------------


class TestTransitBoard(unittest.TestCase):
	def _board(self, actor):
		send = {
			"name": "HND-S1",
			"status": svc.STATUS_PENDING,
			"kind": svc.DOC_KIND_DISPATCH,
			"from_warehouse": DOKKI,
			"declared_by": "dokki@jarz.test",
			"declared_at": "2026-10-19 08:00:00",
			"transit_entry": "MAT-STE-T1",
			"transit_warehouse": TRANSIT,
			"modified": "2026-10-19 08:00:00",
		}
		rcpt = {
			"name": "HND-R1",
			"status": svc.STATUS_PENDING,
			"kind": svc.DOC_KIND_HANDOVER,
			"from_warehouse": None,
			"to_warehouse": ZAMALEK,
			"declared_side": svc.SIDE_TO,
			"declared_by": "zamalek@jarz.test",
			"declared_at": "2026-10-20 09:00:00",
			"modified": "2026-10-20 09:00:00",
		}
		jar = {"item_code": "JAR-L", "item_name": "Large Jar", "stock_uom": "Nos"}
		items = {
			"HND-S1": [{**jar, "qty": 10, "matched_qty": 4}],
			"HND-R1": [{**jar, "qty": 6}],
		}
		with (
			patch.object(svc, "_build_actor", return_value=actor),
			patch.object(svc, "_load_transit_pool", return_value=([send], [rcpt])),
			patch.object(svc, "_load_transit_items", return_value=items),
			patch.object(svc, "_user_names", return_value={}),
			patch.object(svc, "_transit_affinity", return_value={}),
		):
			return svc.get_transit_board()

	def test_the_board_shape(self):
		board = self._board(_manager_actor())
		self.assertEqual(len(board["version"]), 40)
		self.assertEqual(board["summary"], {"to_approve": 1, "units_in_transit": 6.0, "overdue_sends": 1})
		send = board["sends"][0]
		self.assertEqual(send["source_label"], "Dokki")
		self.assertTrue(send["overdue"])
		self.assertEqual(send["age_hours"], 28.0)
		self.assertTrue(send["can_act"])
		self.assertEqual(send["lines"][0]["remaining"], 6.0)
		receipt = board["receipts"][0]
		self.assertIsNone(receipt["named_source"])
		self.assertTrue(receipt["can_approve"])
		sug = receipt["lines"][0]["suggestions"][0]
		self.assertEqual((sug["dispatch"], sug["source_warehouse"], sug["qty"]), ("HND-S1", DOKKI, 6.0))
		self.assertEqual(sug["source_label"], "Dokki")
		self.assertEqual(receipt["lines"][0]["unresolved_qty"], 0.0)
		self.assertEqual([s["warehouse"] for s in board["senders"]], [FACTORY, DOKKI, ZAMALEK])
		self.assertEqual(board["senders"][0]["label"], svc.FACTORY_LABEL)

	def test_the_declarer_cannot_act_on_its_own_send(self):
		board = self._board(_manager_actor("dokki@jarz.test"))
		self.assertFalse(board["sends"][0]["can_act"])
		self.assertTrue(board["receipts"][0]["can_approve"])

	def test_managers_only(self):
		with self.assertRaises(frappe.PermissionError):
			self._board(_branch_actor())

	def test_can_direct_follows_the_one_step_roles(self):
		self.assertFalse(self._board(_manager_actor())["can_direct"])
		self.assertTrue(self._board(_director_actor())["can_direct"])

	def test_the_version_changes_when_a_member_changes(self):
		a = svc._version_of([{"name": "X", "modified": "2026-10-20 09:00:00"}])
		b = svc._version_of([{"name": "X", "modified": "2026-10-20 09:00:01"}])
		self.assertNotEqual(a, b)
		self.assertEqual(
			svc._version_of([{"name": "A", "modified": None}, {"name": "B", "modified": None}]),
			svc._version_of([{"name": "B", "modified": None}, {"name": "A", "modified": None}]),
		)


# ---------------------------------------------------------------------------
# A transit Stock Entry cancelled outside the app
# ---------------------------------------------------------------------------


class TestCancelledTransitEntry(unittest.TestCase):
	def test_the_pool_leaves_out_a_send_whose_entry_is_cancelled(self):
		live = {
			"name": "HND-S1",
			"status": svc.STATUS_PENDING,
			"kind": svc.DOC_KIND_DISPATCH,
			"from_warehouse": FACTORY,
			"transit_entry": "MAT-STE-T1",
		}
		dead = {**live, "name": "HND-S2", "from_warehouse": DOKKI, "transit_entry": "MAT-STE-T2"}
		rcpt = {
			"name": "HND-R1",
			"status": svc.STATUS_PENDING,
			"kind": svc.DOC_KIND_HANDOVER,
			"from_warehouse": DOKKI,
			"to_warehouse": ZAMALEK,
			"declared_side": svc.SIDE_TO,
		}

		def fake_get_all(doctype, filters=None, **kw):
			return [live, dead] if "transit_entry" in (filters or {}) else [rcpt]

		with (
			patch.object(svc.frappe, "get_all", side_effect=fake_get_all),
			patch.object(svc, "_live_transit_entries", return_value={"MAT-STE-T1"}),
		):
			sends, receipts = svc._load_transit_pool()
		self.assertEqual([s["name"] for s in sends], ["HND-S1"])
		# The receipt naming Dokki stays a pool receipt (same rule as _transit_sources).
		self.assertEqual([r["name"] for r in receipts], ["HND-R1"])

	def test_cancelling_a_pending_sends_transit_entry_is_refused(self):
		se = types.SimpleNamespace(name="MAT-STE-T1", flags=types.SimpleNamespace())
		with patch.object(svc.frappe.db, "get_value", return_value="HND-S1") as get_value:
			with self.assertRaises(frappe.ValidationError) as ctx:
				svc.guard_transit_entry_cancel(se, "before_cancel")
		self.assertIn("HND-S1", str(ctx.exception))
		self.assertEqual(
			get_value.call_args.args[1],
			{"transit_entry": "MAT-STE-T1", "status": svc.STATUS_PENDING, "kind": svc.DOC_KIND_DISPATCH},
		)

	def test_an_entry_no_pending_send_holds_may_be_cancelled(self):
		se = types.SimpleNamespace(name="MAT-STE-9", flags=types.SimpleNamespace())
		with patch.object(svc.frappe.db, "get_value", return_value=None):
			svc.guard_transit_entry_cancel(se, "before_cancel")

	def test_the_sends_own_cancel_path_passes_the_guard(self):
		seen = {}

		class _CancellableSE:
			name = "MAT-STE-T1"
			docstatus = 1

			def __init__(self):
				self.flags = types.SimpleNamespace()

			def cancel(self):
				seen["flag"] = getattr(self.flags, "from_handover_service", False)
				with patch.object(svc.frappe.db, "get_value", return_value="HND-S1") as get_value:
					svc.guard_transit_entry_cancel(self, "before_cancel")
				seen["looked_up"] = get_value.called

		with patch.object(svc.frappe, "get_doc", return_value=_CancellableSE()):
			svc._cancel_stock_entry("MAT-STE-T1")
		self.assertEqual(seen, {"flag": True, "looked_up": False})


# ---------------------------------------------------------------------------
# Jarz Stock Handover controller: the send counters
# ---------------------------------------------------------------------------


class TestSendCounterInvariant(unittest.TestCase):
	def _validate(self, matched=0.0, written_off=0.0, returned=0.0, qty=10.0, kind=svc.DOC_KIND_DISPATCH):
		from jarz_pos.doctype.jarz_stock_handover.jarz_stock_handover import JarzStockHandover

		doc = _tsend(qty=qty)
		row = doc.items[0]
		row.matched_qty, row.written_off_qty, row.returned_qty = matched, written_off, returned
		obj = JarzStockHandover.__new__(JarzStockHandover)
		obj.__dict__.update(
			kind=kind,
			from_warehouse=DOKKI,
			to_warehouse=None if kind == svc.DOC_KIND_DISPATCH else ZAMALEK,
			declared_side=svc.SIDE_FROM,
			items=doc.items,
		)
		with patch.object(JarzStockHandover, "_guard_closed_rows"):
			obj.validate()

	def test_counters_within_qty_pass(self):
		self._validate(matched=6.0, written_off=2.0, returned=2.0)

	def test_float_noise_is_tolerated(self):
		self._validate(matched=3.3334, written_off=3.3333, returned=3.3334)

	def test_counters_above_qty_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(matched=6.0, written_off=3.0, returned=2.0)

	def test_a_handover_is_not_checked(self):
		self._validate(matched=12.0, kind=svc.DOC_KIND_HANDOVER)
