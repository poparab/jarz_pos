"""One-step "bought & received" label batch (``api.labels.record_bought_batch``).

Pure mock / unittest, same style as ``test_label_stock``: the label DocTypes may
not exist yet when the backend CI gate runs (it runs before ``bench migrate``),
so nothing here inserts a real document. ``frappe.new_doc`` returns a fake
print order that records every call into one shared ``events`` list, which is
how the order of operations (insert -> bill -> receive) is asserted.

Assertions are key-scoped on purpose (``kwargs["posting_date"]``), never a
whole-call or whole-dict equality: a new keyword added to the bill call later
must not break tests that are about something else.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import labels

LABEL = "LBL-1"
SUPPLIER = "Print House"
PRINTING_ITEM = "Customer Label Printing"
PI = "ACC-PINV-0001"
PER_SHEET = 21


class _FakePrintOrder(SimpleNamespace):
	"""Stands in for a Jarz Label Print Order; logs insert / reload / save."""

	def __init__(self, events):
		super().__init__(
			name=None,
			label=None,
			qty_sheets=0,
			status=None,
			requested_on=None,
			supplier=None,
			total_cost=0,
			notes=None,
			purchase_invoice=None,
			received_qty=0,
			received_on=None,
		)
		self._events = events

	def insert(self, ignore_permissions=False):
		self.name = "JLPO-00001"
		self._events.append(("insert", self.status))

	def reload(self):
		self._events.append(("reload", None))

	def save(self, ignore_permissions=False):
		self._events.append(("save", self.status))


def _get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
	if doctype == "Purchase Invoice":
		return 1  # docstatus: submitted
	if doctype == labels.LABEL_DOCTYPE:
		return {"size": "Medium", "labels_per_sheet": 0}
	if doctype == labels.PRINT_ORDER_DOCTYPE:
		return {"label": LABEL, "purchase_invoice": PI}
	return None


def _exists(doctype, name=None, *args, **kwargs):
	return doctype in (labels.LABEL_DOCTYPE, "Supplier", "Item", labels.PRINT_ORDER_DOCTYPE)


def _args(**overrides):
	values = {
		"label": LABEL,
		"qty_sheets": 5,
		"supplier": SUPPLIER,
		"total_cost": 420,
	}
	values.update(overrides)
	return values


class TestRecordBoughtBatch(unittest.TestCase):
	@contextmanager
	def _env(self, existing_order=None, exists=_exists):
		"""Patch every collaborator; yield (events, doc, mocks)."""
		events: list[tuple[str, object]] = []
		doc = _FakePrintOrder(events)

		def _create_pi(**kwargs):
			events.append(("create_purchase_invoice", kwargs))
			return {"purchase_invoice": PI}

		def _link_bill(print_order, purchase_invoice):
			events.append(("link_bill", (print_order, purchase_invoice)))

		mocks = SimpleNamespace()
		with ExitStack() as stack:
			mocks.gate = stack.enter_context(patch.object(labels, "_ensure_manager"))
			mocks.lookup = stack.enter_context(
				patch.object(labels, "_print_order_for_request", return_value=existing_order)
			)
			stack.enter_context(patch.object(labels, "_request_id_supported", return_value=True))
			stack.enter_context(patch.object(labels.frappe.db, "exists", side_effect=exists))
			stack.enter_context(patch.object(labels.frappe.db, "get_value", side_effect=_get_value))
			mocks.new_doc = stack.enter_context(patch.object(labels.frappe, "new_doc", return_value=doc))
			stack.enter_context(
				patch.object(
					labels.label_stock, "get_label_settings", return_value={"printing_item": PRINTING_ITEM}
				)
			)
			stack.enter_context(
				patch.object(labels.label_stock, "labels_per_sheet_for", return_value=PER_SHEET)
			)
			mocks.link_bill = stack.enter_context(
				patch.object(labels.label_stock, "link_bill", side_effect=_link_bill)
			)
			mocks.create_pi = stack.enter_context(
				patch("jarz_pos.api.purchase.create_purchase_invoice", side_effect=_create_pi)
			)
			mocks.detail = stack.enter_context(
				patch.object(labels, "get_label_detail", return_value={"name": LABEL})
			)
			yield events, doc, mocks

	def _bill_kwargs(self, events):
		calls = [payload for kind, payload in events if kind == "create_purchase_invoice"]
		self.assertEqual(len(calls), 1, "exactly one Purchase Invoice call")
		return calls[0]

	# -- gate ---------------------------------------------------------------
	def test_manager_gate_is_called(self):
		with self._env() as (_events, _doc, mocks):
			labels.record_bought_batch(**_args())
		mocks.gate.assert_called_once()

	def test_gate_refusal_writes_nothing(self):
		with self._env() as (_events, _doc, mocks):
			mocks.gate.side_effect = frappe.PermissionError("no")
			with self.assertRaises(frappe.PermissionError):
				labels.record_bought_batch(**_args())
		mocks.new_doc.assert_not_called()
		mocks.create_pi.assert_not_called()

	# -- idempotent replay -------------------------------------------------
	def test_replay_returns_the_existing_order_without_inserting(self):
		with self._env(existing_order="JLPO-00009") as (_events, _doc, mocks):
			result = labels.record_bought_batch(**_args(client_request_id="req-1"))
		mocks.lookup.assert_called_once_with("req-1")
		mocks.new_doc.assert_not_called()
		mocks.create_pi.assert_not_called()
		mocks.link_bill.assert_not_called()
		self.assertEqual(result["print_order"], "JLPO-00009")
		self.assertEqual(result["purchase_invoice"], PI)
		self.assertEqual(result["label"], {"name": LABEL})

	def test_new_request_id_is_stamped_on_the_order(self):
		with self._env() as (_events, doc, _mocks):
			labels.record_bought_batch(**_args(client_request_id="req-2"))
		self.assertEqual(doc.client_request_id, "req-2")

	# -- validation before any insert ----------------------------------------
	def _assert_refused_before_insert(self, exists=_exists, **overrides):
		with self._env(exists=exists) as (_events, _doc, mocks):
			with self.assertRaises(frappe.ValidationError):
				labels.record_bought_batch(**_args(**overrides))
		mocks.new_doc.assert_not_called()
		mocks.create_pi.assert_not_called()

	def test_missing_supplier_is_refused_before_insert(self):
		self._assert_refused_before_insert(supplier="")

	def test_unknown_supplier_is_refused_before_insert(self):
		self._assert_refused_before_insert(exists=lambda dt, name=None, *a, **k: dt != "Supplier")

	def test_zero_cost_is_refused_before_insert(self):
		self._assert_refused_before_insert(total_cost=0)

	def test_zero_sheets_is_refused_before_insert(self):
		self._assert_refused_before_insert(qty_sheets=0)

	def test_zero_received_qty_is_refused_before_insert(self):
		self._assert_refused_before_insert(received_qty=0)

	def test_unknown_label_is_refused_before_insert(self):
		self._assert_refused_before_insert(exists=lambda dt, name=None, *a, **k: dt != labels.LABEL_DOCTYPE)

	def test_unconfigured_printing_item_is_refused_before_insert(self):
		self._assert_refused_before_insert(exists=lambda dt, name=None, *a, **k: dt != "Item")

	def test_future_received_on_is_refused_before_insert(self):
		self._assert_refused_before_insert(received_on="2999-01-01")

	# -- order of operations -------------------------------------------------
	def test_insert_then_bill_then_receive(self):
		with self._env() as (events, _doc, _mocks):
			labels.record_bought_batch(**_args())
		kinds = [kind for kind, _payload in events]
		self.assertEqual(kinds, ["insert", "create_purchase_invoice", "link_bill", "reload", "save"])
		statuses = dict((kind, payload) for kind, payload in events if kind in ("insert", "save"))
		self.assertEqual(statuses["insert"], "Requested")
		self.assertEqual(statuses["save"], "Received")

	def test_receipt_carries_qty_and_date(self):
		with self._env() as (_events, doc, _mocks):
			labels.record_bought_batch(**_args(received_qty=90, received_on="2026-10-20"))
		self.assertEqual(doc.received_qty, 90)
		self.assertEqual(doc.received_on, "2026-10-20")

	def test_received_qty_defaults_to_sheets_times_labels_per_sheet(self):
		with self._env() as (_events, doc, _mocks):
			labels.record_bought_batch(**_args(qty_sheets=5))
		self.assertEqual(doc.received_qty, 5 * PER_SHEET)

	def test_returns_the_bill_print_order_shape(self):
		with self._env() as (_events, _doc, _mocks):
			result = labels.record_bought_batch(**_args())
		self.assertEqual(result["print_order"], "JLPO-00001")
		self.assertEqual(result["purchase_invoice"], PI)
		self.assertEqual(result["label"], {"name": LABEL})

	# -- what reaches the bill -----------------------------------------------
	def test_posting_date_is_the_bill_date_when_given(self):
		with self._env() as (events, doc, _mocks):
			labels.record_bought_batch(**_args(bill_date="2026-10-18", received_on="2026-10-20"))
		kwargs = self._bill_kwargs(events)
		self.assertEqual(kwargs["posting_date"], "2026-10-18")
		self.assertEqual(kwargs["bill_date"], "2026-10-18")
		self.assertEqual(doc.requested_on, "2026-10-18")

	def test_posting_date_falls_back_to_received_on(self):
		with self._env() as (events, doc, _mocks):
			labels.record_bought_batch(**_args(received_on="2026-10-20"))
		kwargs = self._bill_kwargs(events)
		self.assertEqual(kwargs["posting_date"], "2026-10-20")
		self.assertEqual(doc.requested_on, "2026-10-20")

	def test_bill_carries_cost_supplier_and_key(self):
		with self._env() as (events, _doc, _mocks):
			labels.record_bought_batch(**_args(qty_sheets=4, total_cost=400, bill_no="INV-77"))
		kwargs = self._bill_kwargs(events)
		self.assertEqual(kwargs["supplier"], SUPPLIER)
		self.assertEqual(kwargs["bill_no"], "INV-77")
		self.assertEqual(kwargs["idempotency_key"], "label-po-JLPO-00001")
		self.assertEqual(kwargs["items"][0]["item_code"], PRINTING_ITEM)
		self.assertEqual(kwargs["items"][0]["qty"], 4)
		self.assertEqual(kwargs["items"][0]["rate"], 100.0)

	def test_paid_forwards_is_paid_and_payment_option(self):
		with self._env() as (events, _doc, _mocks):
			labels.record_bought_batch(**_args(is_paid=1, payment_option="custody:Ahmed"))
		kwargs = self._bill_kwargs(events)
		self.assertEqual(kwargs["is_paid"], 1)
		self.assertEqual(kwargs["payment_option"], "custody:Ahmed")

	def test_unpaid_ignores_payment_option(self):
		with self._env() as (events, _doc, _mocks):
			labels.record_bought_batch(**_args(is_paid=0, payment_option="instapay"))
		kwargs = self._bill_kwargs(events)
		self.assertEqual(kwargs["is_paid"], 0)
		self.assertIsNone(kwargs["payment_option"])

	def test_never_commits(self):
		with self._env() as (_events, _doc, _mocks):
			with patch.object(labels.frappe.db, "commit") as commit:
				labels.record_bought_batch(**_args())
		commit.assert_not_called()


class TestBillPrintOrderUnchanged(unittest.TestCase):
	"""The refactor onto ``_bill_print_order_doc`` keeps bill_print_order as it was."""

	def _run(self, **kwargs):
		doc = SimpleNamespace(
			name="JLPO-00002",
			label=LABEL,
			qty_sheets=2,
			status="Received",
			supplier=SUPPLIER,
			total_cost=200,
			purchase_invoice=None,
		)
		create_pi = MagicMock(return_value={"purchase_invoice": PI})
		with (
			patch.object(labels, "_ensure_manager") as gate,
			patch.object(labels.frappe.db, "exists", side_effect=_exists),
			patch.object(labels.frappe.db, "get_value", side_effect=_get_value),
			patch.object(labels.frappe, "get_doc", return_value=doc),
			patch.object(
				labels.label_stock, "get_label_settings", return_value={"printing_item": PRINTING_ITEM}
			),
			patch.object(labels.label_stock, "link_bill") as link,
			patch("jarz_pos.api.purchase.create_purchase_invoice", create_pi),
			patch.object(labels, "get_label_detail", return_value={"name": LABEL}),
		):
			result = labels.bill_print_order("JLPO-00002", **kwargs)
		return result, create_pi, gate, link

	def test_no_posting_date_means_today(self):
		_result, create_pi, gate, link = self._run()
		gate.assert_called_once()
		self.assertIsNone(create_pi.call_args.kwargs["posting_date"])
		link.assert_called_once_with("JLPO-00002", PI)

	def test_still_forwards_is_paid_and_payment_option(self):
		_result, create_pi, _gate, _link = self._run(is_paid=1, payment_option="instapay")
		self.assertEqual(create_pi.call_args.kwargs["is_paid"], 1)
		self.assertEqual(create_pi.call_args.kwargs["payment_option"], "instapay")

	def test_returns_the_same_shape(self):
		result, _create_pi, _gate, _link = self._run()
		self.assertEqual(result["print_order"], "JLPO-00002")
		self.assertEqual(result["purchase_invoice"], PI)
		self.assertEqual(result["label"], {"name": LABEL})

	def test_draft_bill_is_refused(self):
		def _draft(doctype, *args, **kwargs):
			return 0 if doctype == "Purchase Invoice" else _get_value(doctype, *args, **kwargs)

		doc = SimpleNamespace(
			name="JLPO-00003",
			label=LABEL,
			qty_sheets=1,
			status="Requested",
			supplier=SUPPLIER,
			total_cost=50,
			purchase_invoice=None,
		)
		with (
			patch.object(labels.frappe.db, "exists", side_effect=_exists),
			patch.object(labels.frappe.db, "get_value", side_effect=_draft),
			patch.object(
				labels.label_stock, "get_label_settings", return_value={"printing_item": PRINTING_ITEM}
			),
			patch.object(labels.label_stock, "link_bill") as link,
			patch("jarz_pos.api.purchase.create_purchase_invoice", return_value={"purchase_invoice": PI}),
		):
			with self.assertRaises(frappe.ValidationError):
				labels._bill_print_order_doc(doc)
		link.assert_not_called()


if __name__ == "__main__":
	unittest.main()
