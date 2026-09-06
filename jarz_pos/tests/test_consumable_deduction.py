"""Tests for automatic consumable deduction on the Out for Delivery transition.

Pins the fixes for the 2026-09-06 production incident:

* 167/167 consumable Stock Entries failed since 2026-08-26 because
  ``_get_warehouse`` resolved the invoice's POS Profile (branch) warehouse,
  which has never carried Consumable-group stock — see
  ``jarz_pos.services.consumable_deduction`` module docstring for the full
  production evidence. ``test_branch_fulfilment.py:212`` only ever asserted
  that the OFD *state* was written (so the hook fires); it never asserted a
  Stock Ledger Entry actually posts, which is exactly why a 100%-failure
  feature passed CI for 11 days. The tests below close that hole.
* ``custom_consumable_stock_entry`` was assigned only after the call that
  raised, so a cancelled invoice could never find its Stock Entry to reverse.
* A failed submit left an orphaned, GL-less Stock Entry behind with no
  rollback.

Pure ``unittest`` with ``frappe`` mocked out — same style as
``test_branch_fulfilment.py`` / ``test_territory_exceptions.py`` — so the
suite runs without a site (the CI logic gate runs before ``bench migrate``).
A live site would let a test assert on a real Stock Ledger Entry row; without
one, "produces SLEs in the resolved warehouse" is pinned the way this app's
other site-less tests pin ERPNext side effects: asserting the exact Stock
Entry line built (item, qty, ``s_warehouse``) and that ``insert()``/``submit()``
are actually invoked on it — submitting a Stock Entry is what ERPNext's own
(separately tested) stock ledger machinery turns into SLEs.
"""

from __future__ import annotations

import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.services import consumable_deduction as cd


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Doc(types.SimpleNamespace):
    """SimpleNamespace that also supports dict-style ``.get()`` like a Frappe doc."""

    def get(self, key, default=None):
        return getattr(self, key, default)


def _jar_line(item_group, qty):
    return SimpleNamespace(item_group=item_group, qty=qty)


def _invoice(
    *,
    name="_TEST-SINV-1",
    state="Out for Delivery",
    was_ofd=0,
    company="_Test Company",
    items=None,
    kanban_profile="_TEST BRANCH",
):
    return _Doc(
        name=name,
        custom_sales_invoice_state=state,
        custom_was_out_for_delivery=was_ofd,
        company=company,
        custom_kanban_profile=kanban_profile,
        pos_profile=kanban_profile,
        items=items if items is not None else [_jar_line("Medium", 3)],
    )


class _FakeStockEntry:
    """Just enough of a Stock Entry Document for ``_create_material_issue``."""

    def __init__(self, name="STE-0001", submit_side_effect=None):
        self.name = name
        self.items = []
        self.flags = SimpleNamespace()
        # Stock Entry.company is reqd=1. It starts as None here on purpose so a
        # test can assert the handler STATES it -- if it is left unset in
        # production, frappe.new_doc falls back to the session user's default
        # company, and a company that differs from the invoice's invalidates the
        # warehouse routing this whole module is about.
        self.company = None
        self.stock_entry_type = None
        self.posting_date = None
        self.set_posting_time = None
        self.remarks = None
        self.insert = MagicMock()
        self.submit = MagicMock(side_effect=submit_side_effect)

    def append(self, table, row):
        assert table == "items"
        self.items.append(row)


def _make_fake_frappe(
    *,
    bin_qty_by_item=None,
    uom_by_item=None,
    stock_entry=None,
    existing_link=None,
):
    """A frappe double with only what consumable_deduction touches."""
    bin_qty_by_item = bin_qty_by_item or {}
    uom_by_item = uom_by_item or {}

    def get_value(doctype, filters=None, fieldname=None, **kwargs):
        if doctype == "Bin":
            item_code = (filters or {}).get("item_code")
            return bin_qty_by_item.get(item_code, 0)
        if doctype == "Item":
            # called positionally: get_value("Item", item_code, "stock_uom")
            return uom_by_item.get(filters, "Nos")
        if doctype == "Sales Invoice":
            return existing_link
        if doctype == "Stock Entry":
            return None
        return None

    fake = SimpleNamespace()
    fake.db = SimpleNamespace(
        get_value=MagicMock(side_effect=get_value),
        set_value=MagicMock(),
        savepoint=MagicMock(),
        rollback=MagicMock(),
    )
    fake.utils = SimpleNamespace(today=MagicMock(return_value="2026-09-06"))

    def _enforcing_log_error(*args, **kwargs):
        """Mirror the REAL ``Error Log`` constraint instead of accepting anything.

        ``Error Log.method`` is ``Data``/varchar(140) and
        ``BaseDocument._validate_length`` THROWS on overflow -- it does not
        truncate. And ``frappe.log_error(a, b)`` only swaps its arguments when
        ``"\\n" in a``, so a single-line first positional stays the TITLE and
        lands in ``method``.

        A bare ``MagicMock()`` here accepted everything, which is exactly how a
        155-character message on the line-drop path passed CI while raising
        ``CharacterLengthExceededError`` on every single production order -- on
        the very line whose job is to make an unstocked item survivable. Keep
        this enforcing so that class of defect cannot pass again.
        """
        title = kwargs.get("title")
        if title is None and args:
            first = str(args[0])
            # Reproduce frappe's swap heuristic.
            title = args[1] if (len(args) > 1 and "\n" in first) else args[0]
        if title is not None and len(str(title)) > 140:
            raise ValueError(
                "Error Log.method exceeds 140 chars "
                f"({len(str(title))}): {str(title)[:60]}..."
            )

    fake.log_error = MagicMock(side_effect=_enforcing_log_error)
    fake.get_traceback = MagicMock(return_value="traceback")
    fake.new_doc = MagicMock(return_value=stock_entry or _FakeStockEntry())
    fake.get_doc = MagicMock()
    return fake


# ---------------------------------------------------------------------------
# _get_warehouse — A1: must resolve via the shared purchase-warehouse router,
# never the POS Profile / kanban-profile warehouse.
# ---------------------------------------------------------------------------

class TestGetWarehouse(unittest.TestCase):
    def test_resolves_via_the_shared_purchase_warehouse_router(self):
        with patch(
            "jarz_pos.utils.warehouse_utils.resolve_purchase_warehouse",
            return_value="Consumables - J",
        ) as mock_resolve:
            warehouse = cd._get_warehouse("covier", "_Test Company")

        self.assertEqual(warehouse, "Consumables - J")
        mock_resolve.assert_called_once_with("covier", "_Test Company")

    def test_never_falls_back_to_a_pos_profile_lookup(self):
        """Regression guard for the 167/167 production failure.

        The old implementation read ``POS Profile.warehouse``. The rewrite
        must not touch ``POS Profile`` at all — it has to go through the
        Item-Group-routed resolver.
        """
        with patch(
            "jarz_pos.utils.warehouse_utils.resolve_purchase_warehouse",
            return_value="Consumables - J",
        ), patch.object(cd, "frappe") as mock_frappe:
            cd._get_warehouse("covier", "_Test Company")

        mock_frappe.db.get_value.assert_not_called()

    def test_resolver_exception_returns_none_instead_of_raising(self):
        """``resolve_purchase_warehouse`` frappe.throw()s for purchasing; here a
        missing route must degrade to "drop this line", never block OFD."""
        with patch(
            "jarz_pos.utils.warehouse_utils.resolve_purchase_warehouse",
            side_effect=Exception("no route configured"),
        ):
            warehouse = cd._get_warehouse("Nylon Inside bag", "_Test Company")

        self.assertIsNone(warehouse)

    def test_blank_item_or_company_short_circuits(self):
        self.assertIsNone(cd._get_warehouse("", "_Test Company"))
        self.assertIsNone(cd._get_warehouse("covier", ""))


# ---------------------------------------------------------------------------
# _build_coverable_lines — A4: pre-check Bin.actual_qty, clamp/drop per line.
# ---------------------------------------------------------------------------

@patch.object(cd, "_get_warehouse")
class TestBuildCoverableLines(unittest.TestCase):
    def test_full_stock_covers_every_line(self, mock_get_warehouse):
        mock_get_warehouse.return_value = "Consumables - J"
        fake = _make_fake_frappe(
            bin_qty_by_item={"covier": 7000, "colored bag": 2250, "Nylon Inside bag": 500},
        )
        with patch.object(cd, "frappe", fake):
            lines = cd._build_coverable_lines(
                "_Test Company",
                [("covier", 3), ("colored bag", 1), ("Nylon Inside bag", 1)],
                invoice_name="_TEST-SINV-1",
            )

        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(line["s_warehouse"], "Consumables - J")
        self.assertEqual(lines[0]["qty"], 3)

    def test_item_with_zero_stock_is_dropped_not_fatal(self, mock_get_warehouse):
        """Nylon Inside bag has never carried a Bin row anywhere on production —
        it must be dropped, not sink the two good lines behind it."""
        mock_get_warehouse.return_value = "Consumables - J"
        fake = _make_fake_frappe(
            bin_qty_by_item={"covier": 7000, "colored bag": 2250, "Nylon Inside bag": 0},
        )
        with patch.object(cd, "frappe", fake):
            lines = cd._build_coverable_lines(
                "_Test Company",
                [("covier", 3), ("colored bag", 1), ("Nylon Inside bag", 1)],
                invoice_name="_TEST-SINV-1",
            )

        item_codes = [line["item_code"] for line in lines]
        self.assertEqual(item_codes, ["covier", "colored bag"])
        # Keyword form is the point, not a detail: _log() calls
        # frappe.log_error(title=..., message=...) explicitly, because a
        # single-line positional first argument becomes the TITLE and lands in
        # Error Log.method -- varchar(140), which THROWS on overflow. The detail
        # string here is 155 characters, so as a positional it would have raised
        # on the very line whose job is to make an unstocked item survivable.
        fake.log_error.assert_any_call(
            title="consumable_deduction: no stock", message=unittest.mock.ANY
        )

    def test_line_is_clamped_to_available_stock(self, mock_get_warehouse):
        mock_get_warehouse.return_value = "Consumables - J"
        fake = _make_fake_frappe(bin_qty_by_item={"covier": 2})
        with patch.object(cd, "frappe", fake):
            lines = cd._build_coverable_lines(
                "_Test Company", [("covier", 10)], invoice_name="_TEST-SINV-1"
            )

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["qty"], 2)
        fake.log_error.assert_any_call(
            title="consumable_deduction: clamped to available stock",
            message=unittest.mock.ANY,
        )

    def test_unresolvable_warehouse_drops_the_line(self, mock_get_warehouse):
        mock_get_warehouse.return_value = None
        fake = _make_fake_frappe(bin_qty_by_item={"covier": 100})
        with patch.object(cd, "frappe", fake):
            lines = cd._build_coverable_lines(
                "_Test Company", [("covier", 3)], invoice_name="_TEST-SINV-1"
            )

        self.assertEqual(lines, [])
        fake.log_error.assert_any_call(
            title="consumable_deduction: warehouse unresolved",
            message=unittest.mock.ANY,
        )

    def test_zero_or_negative_requested_qty_is_skipped_silently(self, mock_get_warehouse):
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake):
            lines = cd._build_coverable_lines(
                "_Test Company", [("covier", 0)], invoice_name="_TEST-SINV-1"
            )

        self.assertEqual(lines, [])
        mock_get_warehouse.assert_not_called()


# ---------------------------------------------------------------------------
# _create_material_issue — A2: savepoint + rollback on failure; A4: link
# persisted at success time.
# ---------------------------------------------------------------------------

class TestCreateMaterialIssue(unittest.TestCase):
    def test_returns_none_and_never_creates_a_doc_when_nothing_is_coverable(self):
        fake = _make_fake_frappe(bin_qty_by_item={})  # nothing in stock anywhere
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_get_warehouse", return_value="Consumables - J"
        ):
            result = cd._create_material_issue(
                invoice_name="_TEST-SINV-1",
                company="_Test Company",
                couvert_qty=3,
                bag_qty=1,
                nylon_qty=1,
            )

        self.assertIsNone(result)
        fake.new_doc.assert_not_called()

    def test_stock_entry_lines_use_the_resolved_consumables_warehouse(self):
        """Regression guard for the 167/167 failure: rows must carry the
        resolved consumables warehouse, never a POS Profile warehouse.

        NOTE what is patched here, and what is deliberately NOT. An earlier
        version of this test patched ``cd._get_warehouse`` itself -- the very
        function that held the defect -- and then asserted the value it had just
        injected via ``return_value``. It would have passed unchanged with the
        POS Profile lookup restored, i.e. it guarded nothing. The seam has to be
        BELOW the code under test, so the real ``_get_warehouse`` runs and only
        the shared router underneath it is stubbed.
        """
        se = _FakeStockEntry()
        fake = _make_fake_frappe(
            bin_qty_by_item={"covier": 7000, "colored bag": 2250, "Nylon Inside bag": 500},
            stock_entry=se,
        )
        with patch.object(cd, "frappe", fake), patch(
            "jarz_pos.utils.warehouse_utils.resolve_purchase_warehouse",
            return_value="Consumables - J",
        ) as mock_router:
            result = cd._create_material_issue(
                invoice_name="_TEST-SINV-1",
                company="_Test Company",
                couvert_qty=3,
                bag_qty=1,
                nylon_qty=1,
            )

        self.assertEqual(result, se.name)
        self.assertEqual(len(se.items), 3)
        for row in se.items:
            self.assertEqual(row["s_warehouse"], "Consumables - J")
            self.assertNotIn(row["s_warehouse"], ("Nasr city - J", "Dokki - J", "6th of october - J"))
        # The real _get_warehouse must have gone through the shared purchase
        # router for every line -- that routing IS the fix.
        self.assertEqual(mock_router.call_count, 3)
        for call_args in mock_router.call_args_list:
            self.assertEqual(call_args.args[1], "_Test Company")
        # Stock Entry.company must be stated, not inherited from the session
        # user's default company (see _create_material_issue).
        self.assertEqual(se.company, "_Test Company")
        se.insert.assert_called_once()
        se.submit.assert_called_once()

    def test_link_is_persisted_immediately_after_a_successful_submit(self):
        se = _FakeStockEntry(name="STE-0099")
        fake = _make_fake_frappe(bin_qty_by_item={"covier": 100}, stock_entry=se)
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_get_warehouse", return_value="Consumables - J"
        ):
            cd._create_material_issue(
                invoice_name="_TEST-SINV-1",
                company="_Test Company",
                couvert_qty=3,
                bag_qty=0,
                nylon_qty=0,
            )

        fake.db.set_value.assert_called_once_with(
            "Sales Invoice",
            "_TEST-SINV-1",
            "custom_consumable_stock_entry",
            "STE-0099",
            update_modified=False,
        )

    def test_failed_submit_rolls_back_to_the_savepoint_and_reraises(self):
        """The core of A2: a mid-batch validation failure must not leave a
        submitted, GL-less orphan Stock Entry behind — the exact state 168
        Stock Entries were found in on production."""
        se = _FakeStockEntry(submit_side_effect=RuntimeError("negative stock"))
        fake = _make_fake_frappe(bin_qty_by_item={"covier": 100}, stock_entry=se)
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_get_warehouse", return_value="Consumables - J"
        ):
            with self.assertRaises(RuntimeError):
                cd._create_material_issue(
                    invoice_name="_TEST-SINV-1",
                    company="_Test Company",
                    couvert_qty=3,
                    bag_qty=0,
                    nylon_qty=0,
                )

        fake.db.savepoint.assert_called_once()
        fake.db.rollback.assert_called_once()
        savepoint_name = fake.db.savepoint.call_args.args[0]
        self.assertEqual(fake.db.rollback.call_args.kwargs.get("save_point"), savepoint_name)
        # No orphan link: the invoice must never point at a Stock Entry that
        # was rolled back.
        fake.db.set_value.assert_not_called()

    def test_a_failed_rollback_does_not_mask_the_original_error(self):
        se = _FakeStockEntry(submit_side_effect=RuntimeError("negative stock"))
        fake = _make_fake_frappe(bin_qty_by_item={"covier": 100}, stock_entry=se)
        fake.db.rollback = MagicMock(side_effect=Exception("rollback also failed"))
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_get_warehouse", return_value="Consumables - J"
        ):
            with self.assertRaises(RuntimeError) as ctx:
                cd._create_material_issue(
                    invoice_name="_TEST-SINV-1",
                    company="_Test Company",
                    couvert_qty=3,
                    bag_qty=0,
                    nylon_qty=0,
                )

        self.assertEqual(str(ctx.exception), "negative stock")


# ---------------------------------------------------------------------------
# deduct_consumables_on_ofd — the hook entry point: never raises, always
# alerts on failure (A3).
# ---------------------------------------------------------------------------

class TestDeductConsumablesOnOfd(unittest.TestCase):
    def test_success_sets_the_link_on_the_in_memory_doc_and_does_not_alert(self):
        doc = _invoice()
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_create_material_issue", return_value="STE-0001"
        ), patch.object(cd, "_notify_deduction_failure") as mock_notify:
            cd.deduct_consumables_on_ofd(doc)

        self.assertEqual(doc.custom_consumable_stock_entry, "STE-0001")
        mock_notify.assert_not_called()

    def test_already_processed_invoice_is_a_no_op(self):
        doc = _invoice(was_ofd=1)
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_create_material_issue"
        ) as mock_create:
            cd.deduct_consumables_on_ofd(doc)

        mock_create.assert_not_called()

    def test_no_medium_or_large_jars_is_a_no_op(self):
        doc = _invoice(items=[_jar_line("Merch", 5)])
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_create_material_issue"
        ) as mock_create:
            cd.deduct_consumables_on_ofd(doc)

        mock_create.assert_not_called()

    def test_a_raising_create_material_issue_never_escapes_the_hook(self):
        """The consumable ledger must never be able to block the OFD write
        that the hook hangs off of."""
        doc = _invoice()
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_create_material_issue", side_effect=RuntimeError("negative stock")
        ), patch.object(cd, "_notify_deduction_failure") as mock_notify:
            try:
                cd.deduct_consumables_on_ofd(doc)
            except Exception as exc:  # pragma: no cover - test failure path
                self.fail(f"deduct_consumables_on_ofd must never raise, got {exc!r}")

        mock_notify.assert_called_once()
        reason = mock_notify.call_args.kwargs.get("reason") or mock_notify.call_args.args[-1]
        self.assertIn("negative stock", reason)
        fake.log_error.assert_called_once()

    def test_nothing_coverable_notifies_a_human_without_raising(self):
        doc = _invoice()
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_create_material_issue", return_value=None
        ), patch.object(cd, "_notify_deduction_failure") as mock_notify:
            cd.deduct_consumables_on_ofd(doc)

        mock_notify.assert_called_once()
        self.assertIsNone(getattr(doc, "custom_consumable_stock_entry", None))

    def test_missing_company_notifies_and_never_calls_create(self):
        doc = _invoice(company="")
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake), patch.object(
            cd, "_create_material_issue"
        ) as mock_create, patch.object(cd, "_notify_deduction_failure") as mock_notify:
            cd.deduct_consumables_on_ofd(doc)

        mock_create.assert_not_called()
        mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# _notify_deduction_failure — A3: a human must hear about it, never
# frappe.throw (must not block the OFD transition).
# ---------------------------------------------------------------------------

class TestNotifyDeductionFailure(unittest.TestCase):
    def test_publishes_realtime_and_writes_a_notification_log_per_recipient(self):
        doc = _invoice()
        fake = _make_fake_frappe()
        notification_logs = []

        def new_doc(doctype):
            self.assertEqual(doctype, "Notification Log")
            note = SimpleNamespace(flags=SimpleNamespace())
            note.insert = MagicMock(side_effect=lambda **k: notification_logs.append(note))
            return note

        fake.new_doc = MagicMock(side_effect=new_doc)

        with patch.object(cd, "frappe", fake), patch(
            "jarz_pos.utils.access_control.get_invoice_branch", return_value="_TEST BRANCH"
        ), patch(
            "jarz_pos.utils.access_control.get_users_for_pos_profiles",
            return_value=["manager@jarz.test"],
        ), patch(
            "jarz_pos.utils.realtime.publish_invoice_event"
        ) as mock_publish:
            cd._notify_deduction_failure(doc, reason="No stock in Consumables - J.")

        self.assertEqual(len(notification_logs), 1)
        self.assertEqual(notification_logs[0].for_user, "manager@jarz.test")
        self.assertEqual(notification_logs[0].type, "Alert")
        mock_publish.assert_called_once()
        event_arg = mock_publish.call_args.args[0]
        payload_arg = mock_publish.call_args.args[1]
        self.assertIn("consumable_deduction_failed", event_arg)
        self.assertEqual(payload_arg["invoice"], doc.name)

    def test_never_raises_when_the_alert_plumbing_itself_fails(self):
        doc = _invoice()
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake), patch(
            "jarz_pos.utils.access_control.get_invoice_branch",
            side_effect=RuntimeError("boom"),
        ):
            try:
                cd._notify_deduction_failure(doc, reason="whatever")
            except Exception as exc:  # pragma: no cover
                self.fail(f"_notify_deduction_failure must never raise, got {exc!r}")

        fake.log_error.assert_called_once()

    def test_is_a_no_op_without_a_document_name(self):
        fake = _make_fake_frappe()
        with patch.object(cd, "frappe", fake):
            cd._notify_deduction_failure(_Doc(name=None), reason="whatever")

        fake.new_doc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
