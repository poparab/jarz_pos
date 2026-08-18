"""Tests for the branch<>territory parity catch.

These pin the rules behind ``Jarz Territory Exception``:

* what counts as an exception at all (and, just as importantly, what does not),
* the lane split that separates the 25 live POS-lane divergences on production
  from the 1,260 Woo-lane historical re-pointings,
* the idempotency and never-raise guarantees the invoice-submit path depends on.

They run without a bench: ``frappe`` is stubbed when it cannot be imported, and
every behavioural test swaps the service module's own ``frappe`` reference for a
fake, so nothing here needs a site or a database.

Return/credit-note behaviour is covered here on purpose: production carries zero
credit notes, so tests are the only place that path can be exercised at all.
"""

from __future__ import annotations

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal frappe stub so the service module can be imported outside a bench
# ---------------------------------------------------------------------------

def _make_frappe_stub():
    frappe = types.ModuleType("frappe")

    class ValidationError(Exception):
        pass

    class PermissionError_(Exception):
        pass

    class DuplicateEntryError(Exception):
        pass

    frappe.ValidationError = ValidationError
    frappe.PermissionError = PermissionError_
    frappe.DuplicateEntryError = DuplicateEntryError

    def throw(msg, exc=None, title=None):
        raise (exc or ValidationError)(msg)

    frappe.throw = throw
    # The service decorates two entry points at import time.
    frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe.db = MagicMock()
    frappe.get_meta = MagicMock()
    frappe.get_all = MagicMock(return_value=[])
    frappe.get_doc = MagicMock()
    frappe.get_roles = MagicMock(return_value=["System Manager"])
    frappe.get_traceback = MagicMock(return_value="")
    frappe.log_error = MagicMock()
    frappe.logger = MagicMock()
    frappe.session = SimpleNamespace(user="Administrator")
    frappe.utils = _make_utils_stub()
    return frappe


def _make_utils_stub():
    utils = types.ModuleType("frappe.utils")
    utils.now_datetime = lambda: datetime.datetime(2026, 8, 19, 12, 0, 0)
    utils.nowdate = lambda: "2026-08-19"

    def add_days(date_str, days):
        base = datetime.date.fromisoformat(str(date_str))
        return (base + datetime.timedelta(days=days)).isoformat()

    utils.add_days = add_days
    return utils


try:  # pragma: no cover - depends on whether the runner has a bench
    import frappe as _real_frappe  # noqa: F401
except Exception:  # pragma: no cover
    _stub = _make_frappe_stub()
    sys.modules["frappe"] = _stub
    sys.modules["frappe.utils"] = _stub.utils
else:  # pragma: no cover
    if not hasattr(_real_frappe, "whitelist"):
        _real_frappe.whitelist = lambda *a, **k: (lambda fn: fn)  # type: ignore


from jarz_pos.services import territory_exceptions as mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fake frappe used for the behavioural tests
# ---------------------------------------------------------------------------

class _FakeDoc:
    """Just enough of a Document for ``_insert_exception``/``resolve_*``."""

    def __init__(self, data, registry):
        self.data = dict(data)
        self.flags = SimpleNamespace()
        self.name = None
        self._registry = registry
        for key, value in self.data.items():
            setattr(self, key, value)

    def insert(self, ignore_permissions=False):
        self.name = f"TEXC-{len(self._registry) + 1:04d}"
        self._registry.append(self)
        return self

    def save(self, ignore_permissions=False):
        self._registry.append(self)
        return self

    def __getattr__(self, item):
        # Only reached when normal lookup fails. A real Document exposes every
        # field of its DocType, so an unset one reads as None rather than
        # exploding.
        if item.startswith("_"):
            raise AttributeError(item)
        return None


def _fake_frappe(
    *,
    exists=None,
    get_value=None,
    has_column=True,
    table_exists=True,
    territory_field=True,
    get_all=None,
    count=0,
    docs=None,
):
    """A frappe double with only what this module touches."""
    inserted: list = []
    saved: list = []
    stored = dict(docs or {})
    fake = SimpleNamespace()

    fake.db = SimpleNamespace()
    fake.db.table_exists = MagicMock(return_value=table_exists)
    fake.db.has_column = MagicMock(return_value=has_column)
    fake.db.exists = MagicMock(side_effect=exists or (lambda *a, **k: None))
    fake.db.get_value = MagicMock(side_effect=get_value or (lambda *a, **k: None))
    fake.db.savepoint = MagicMock()
    fake.db.rollback = MagicMock()
    fake.db.commit = MagicMock()
    fake.db.count = MagicMock(return_value=count)

    meta = MagicMock()
    meta.get_field.return_value = MagicMock() if territory_field else None
    fake.get_meta = MagicMock(return_value=meta)

    fake.get_all = MagicMock(side_effect=get_all or (lambda *a, **k: []))

    def _get_doc(*args, **kwargs):
        # New document: get_doc({...})
        if len(args) == 1 and isinstance(args[0], dict):
            return _FakeDoc(args[0], inserted)
        # Existing document: get_doc(doctype, name)
        name = args[1]
        doc = _FakeDoc(stored.get(name, {}), saved)
        doc.name = name
        return doc

    fake.get_doc = MagicMock(side_effect=_get_doc)
    fake.get_roles = MagicMock(return_value=["System Manager"])
    fake.get_traceback = MagicMock(return_value="traceback")
    fake.log_error = MagicMock()
    fake.logger = MagicMock()
    fake.session = SimpleNamespace(user="manager@jarz.test")
    fake.utils = _make_utils_stub()
    fake.PermissionError = RuntimeError

    def _throw(msg, exc=None, title=None):
        raise (exc or RuntimeError)(msg)

    fake.throw = _throw
    fake.inserted = inserted
    fake.saved = saved
    return fake


def _invoice(**overrides):
    row = {
        "name": "ACC-SINV-2026-00123",
        "docstatus": 1,
        "posting_date": "2026-08-01",
        "customer": "CUST-0001",
        "territory": "EGDOKKI",
        "pos_profile": "Nasr city",
        "shipping_address_name": None,
        "customer_address": None,
        "grand_total": 520.0,
        "currency": "EGP",
        "custom_kanban_profile": None,
        "woo_source_type": None,
        "woo_transaction_id": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestDetectExceptionType(unittest.TestCase):
    def test_blank_territory_is_unresolved(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertEqual(
                    mod.detect_exception_type(
                        territory=value, pos_profile="Nasr city", territory_pos_profile="Dokki"
                    ),
                    mod.TYPE_TERRITORY_UNRESOLVED,
                )

    def test_root_all_territories_is_unresolved(self):
        """The ERPNext root node carries no branch, no income and no routing."""
        for value in ("All Territories", "all territories", "  ALL TERRITORIES "):
            with self.subTest(value=value):
                self.assertEqual(
                    mod.detect_exception_type(
                        territory=value, pos_profile="Nasr city", territory_pos_profile=None
                    ),
                    mod.TYPE_TERRITORY_UNRESOLVED,
                )

    def test_agreeing_branches_are_clean(self):
        self.assertIsNone(
            mod.detect_exception_type(
                territory="EGDOKKI", pos_profile="Dokki", territory_pos_profile="Dokki"
            )
        )

    def test_disagreeing_branches_are_a_mismatch(self):
        self.assertEqual(
            mod.detect_exception_type(
                territory="EGDOKKI", pos_profile="Nasr city", territory_pos_profile="Dokki"
            ),
            mod.TYPE_BRANCH_MISMATCH,
        )

    def test_case_only_difference_is_not_a_mismatch(self):
        """Two POS Profiles differing only in case cannot both exist."""
        self.assertIsNone(
            mod.detect_exception_type(
                territory="EGDOKKI", pos_profile="nasr city", territory_pos_profile="Nasr City"
            )
        )

    def test_missing_invoice_profile_is_not_a_mismatch(self):
        """533 invoices have no pos_profile; there is no second opinion to disagree with."""
        self.assertIsNone(
            mod.detect_exception_type(
                territory="EGDOKKI", pos_profile=None, territory_pos_profile="Dokki"
            )
        )

    def test_territory_without_a_profile_is_not_a_mismatch(self):
        self.assertIsNone(
            mod.detect_exception_type(
                territory="EGDOKKI", pos_profile="Nasr city", territory_pos_profile=None
            )
        )


class TestDetectOrderLane(unittest.TestCase):
    def test_woo_source_type_marks_the_woo_lane(self):
        self.assertEqual(
            mod.detect_order_lane(woo_source_type="woo", woo_transaction_id=None), mod.LANE_WOO
        )

    def test_woo_transaction_id_marks_the_woo_lane(self):
        self.assertEqual(
            mod.detect_order_lane(woo_source_type=None, woo_transaction_id="wc_txn_991"),
            mod.LANE_WOO,
        )

    def test_no_woo_markers_is_the_pos_lane(self):
        self.assertEqual(
            mod.detect_order_lane(woo_source_type=None, woo_transaction_id=None), mod.LANE_POS
        )
        self.assertEqual(
            mod.detect_order_lane(woo_source_type="  ", woo_transaction_id=""), mod.LANE_POS
        )

    def test_source_type_pos_stays_on_the_pos_lane(self):
        """The regression spec §6 would otherwise create.

        WP-3b stamps ``woo_source_type = "pos"`` on POS orders. A naive
        non-empty test would then reclassify every future POS order as
        Woo-origin and silently flag its mismatches as historical -- burying
        exactly the rows the owner needs to see.
        """
        for value in ("pos", "POS", " Pos "):
            with self.subTest(value=value):
                self.assertEqual(
                    mod.detect_order_lane(woo_source_type=value, woo_transaction_id=None),
                    mod.LANE_POS,
                )

    def test_source_type_pos_with_a_txn_id_is_still_woo(self):
        """A POS order pushed INTO Woo acquires a transaction id and counts as Woo."""
        self.assertEqual(
            mod.detect_order_lane(woo_source_type="pos", woo_transaction_id="wc_txn_5"),
            mod.LANE_WOO,
        )


class TestIsHistoricalRepointing(unittest.TestCase):
    def test_woo_lane_mismatch_is_historical(self):
        self.assertTrue(mod.is_historical_repointing(mod.TYPE_BRANCH_MISMATCH, mod.LANE_WOO))

    def test_pos_lane_mismatch_is_live(self):
        self.assertFalse(mod.is_historical_repointing(mod.TYPE_BRANCH_MISMATCH, mod.LANE_POS))

    def test_unresolved_territory_is_never_historical(self):
        """All 262 NULL-territory invoices are actionable whichever lane made them."""
        for lane in (mod.LANE_POS, mod.LANE_WOO, ""):
            with self.subTest(lane=lane):
                self.assertFalse(
                    mod.is_historical_repointing(mod.TYPE_TERRITORY_UNRESOLVED, lane)
                )


# ---------------------------------------------------------------------------
# Detail text
# ---------------------------------------------------------------------------

class TestBuildDetail(unittest.TestCase):
    def _snapshot(self, **overrides):
        snapshot = {
            "sales_invoice": "ACC-SINV-2026-00123",
            "posting_date": "2026-08-01",
            "grand_total": 520.0,
            "currency": "EGP",
            "pos_profile_used": "Nasr city",
            "territory_pos_profile": "Dokki",
            "invoice_territory": "EGDOKKI",
            "raw_territory": "EGDOKKI",
            "customer": "CUST-0001",
            "shipping_address": "ADDR-0009",
            "address_city": "Dokki",
            "address_state": "Giza",
            "expected_territory": "EGDOKKI",
            "order_lane": mod.LANE_POS,
        }
        snapshot.update(overrides)
        return snapshot

    def test_mismatch_detail_names_both_branches(self):
        detail = mod.build_detail(self._snapshot(), mod.TYPE_BRANCH_MISMATCH)

        self.assertIn("Nasr city", detail)
        self.assertIn("Dokki", detail)
        self.assertIn("ACC-SINV-2026-00123", detail)
        self.assertIn("EGP 520.00", detail)

    def test_pos_lane_mismatch_is_flagged_live(self):
        detail = mod.build_detail(self._snapshot(), mod.TYPE_BRANCH_MISMATCH)

        self.assertIn("[POS lane]", detail)
        self.assertIn("LIVE", detail)
        self.assertNotIn("HISTORICAL", detail)

    def test_woo_lane_mismatch_is_flagged_historical(self):
        detail = mod.build_detail(
            self._snapshot(order_lane=mod.LANE_WOO), mod.TYPE_BRANCH_MISMATCH
        )

        self.assertIn("[Woo lane]", detail)
        self.assertIn("HISTORICAL", detail)
        self.assertIn("nothing to correct", detail)

    def test_unresolved_detail_reports_the_stored_value(self):
        detail = mod.build_detail(
            self._snapshot(raw_territory="", invoice_territory=None),
            mod.TYPE_TERRITORY_UNRESOLVED,
        )

        self.assertIn("no usable delivery", detail)
        self.assertNotIn("HISTORICAL", detail)

    def test_detail_never_overflows_the_small_text_budget(self):
        detail = mod.build_detail(
            self._snapshot(customer="C" * 4000, shipping_address="A" * 4000),
            mod.TYPE_BRANCH_MISMATCH,
        )

        self.assertLessEqual(len(detail), 1000)


# ---------------------------------------------------------------------------
# record_invoice_territory_exception
# ---------------------------------------------------------------------------

class TestRecordInvoiceTerritoryException(unittest.TestCase):
    def setUp(self):
        mod._OPTIONAL_FIELD_CACHE.clear()
        self.addCleanup(mod._OPTIONAL_FIELD_CACHE.clear)

    def _territory_lookup(self, mapping):
        def _get_value(doctype, name=None, fields=None, as_dict=False, **kwargs):
            if doctype == "Territory":
                return mapping.get(name)
            if doctype == "Address":
                return {} if as_dict else None
            return None

        return _get_value

    def test_clean_invoice_records_nothing(self):
        fake = _fake_frappe(get_value=self._territory_lookup({"EGDOKKI": "Dokki"}))
        with patch.object(mod, "frappe", fake):
            result = mod.record_invoice_territory_exception(_invoice(pos_profile="Dokki"))

        self.assertIsNone(result)
        self.assertEqual(fake.inserted, [])

    def test_mismatch_is_recorded_with_lane_and_flag(self):
        fake = _fake_frappe(get_value=self._territory_lookup({"EGDOKKI": "Dokki"}))
        with patch.object(mod, "frappe", fake):
            name = mod.record_invoice_territory_exception(_invoice())

        self.assertEqual(name, "TEXC-0001")
        self.assertEqual(len(fake.inserted), 1)
        payload = fake.inserted[0].data
        self.assertEqual(payload["exception_type"], mod.TYPE_BRANCH_MISMATCH)
        self.assertEqual(payload["status"], mod.STATUS_OPEN)
        self.assertEqual(payload["order_lane"], mod.LANE_POS)
        self.assertEqual(payload["historical_repointing"], 0)
        self.assertEqual(payload["pos_profile_used"], "Nasr city")
        self.assertEqual(payload["territory_pos_profile"], "Dokki")
        self.assertEqual(payload["grand_total"], 520.0)
        self.assertTrue(payload["detail"])

    def test_woo_mismatch_is_marked_historical(self):
        fake = _fake_frappe(get_value=self._territory_lookup({"EGDOKKI": "Dokki"}))
        with patch.object(mod, "frappe", fake):
            mod.record_invoice_territory_exception(_invoice(woo_source_type="woo"))

        payload = fake.inserted[0].data
        self.assertEqual(payload["order_lane"], mod.LANE_WOO)
        self.assertEqual(payload["historical_repointing"], 1)

    def test_unresolved_territory_is_recorded(self):
        fake = _fake_frappe(get_value=self._territory_lookup({}))
        with patch.object(mod, "frappe", fake):
            mod.record_invoice_territory_exception(_invoice(territory=None))

        payload = fake.inserted[0].data
        self.assertEqual(payload["exception_type"], mod.TYPE_TERRITORY_UNRESOLVED)
        self.assertIsNone(payload["invoice_territory"])
        self.assertEqual(payload["historical_repointing"], 0)

    def test_is_idempotent_per_invoice_and_type(self):
        fake = _fake_frappe(
            get_value=self._territory_lookup({"EGDOKKI": "Dokki"}),
            exists=lambda *a, **k: "TEXC-EXISTING",
        )
        with patch.object(mod, "frappe", fake):
            name = mod.record_invoice_territory_exception(_invoice())

        self.assertEqual(name, "TEXC-EXISTING")
        self.assertEqual(fake.inserted, [], "must not insert a second row for the same pair")

    def test_cancelled_invoice_is_skipped(self):
        fake = _fake_frappe(get_value=self._territory_lookup({"EGDOKKI": "Dokki"}))
        with patch.object(mod, "frappe", fake):
            result = mod.record_invoice_territory_exception(_invoice(docstatus=2))

        self.assertIsNone(result)
        self.assertEqual(fake.inserted, [])

    def test_return_invoice_gets_its_own_row(self):
        """Production has zero credit notes, so this path exists only in tests.

        A credit note copies the original's territory and pos_profile, so it
        carries the same crossing. It is a different document and must get its
        own row -- the idempotency key is (invoice, type), not (order, type).
        """
        seen = set()

        def _exists(doctype, filters=None, *a, **k):
            key = (filters or {}).get("sales_invoice")
            return "TEXC-PRIOR" if key in seen else None

        fake = _fake_frappe(
            get_value=self._territory_lookup({"EGDOKKI": "Dokki"}), exists=_exists
        )
        with patch.object(mod, "frappe", fake):
            original = mod.record_invoice_territory_exception(_invoice())
            seen.add("ACC-SINV-2026-00123")
            credit_note = mod.record_invoice_territory_exception(
                _invoice(name="ACC-SINV-2026-00123-1", grand_total=-520.0)
            )

        self.assertEqual(original, "TEXC-0001")
        self.assertEqual(credit_note, "TEXC-0002")
        self.assertEqual(len(fake.inserted), 2)

    def test_falls_back_to_the_kanban_profile_when_pos_profile_is_null(self):
        fake = _fake_frappe(get_value=self._territory_lookup({"EGDOKKI": "Dokki"}))
        with patch.object(mod, "frappe", fake):
            mod.record_invoice_territory_exception(
                _invoice(pos_profile=None, custom_kanban_profile="Nasr city")
            )

        self.assertEqual(fake.inserted[0].data["pos_profile_used"], "Nasr city")

    def test_never_raises_when_the_database_explodes(self):
        fake = _fake_frappe(get_value=self._territory_lookup({"EGDOKKI": "Dokki"}))
        fake.get_doc = MagicMock(side_effect=RuntimeError("table is on fire"))

        with patch.object(mod, "frappe", fake):
            result = mod.record_invoice_territory_exception(_invoice())

        self.assertIsNone(result)
        self.assertTrue(fake.log_error.called, "a swallowed failure must still be logged")

    def test_never_raises_when_the_doctype_is_not_migrated_yet(self):
        fake = _fake_frappe(table_exists=False)
        with patch.object(mod, "frappe", fake):
            self.assertIsNone(mod.record_invoice_territory_exception(_invoice()))

    def test_never_raises_on_garbage_input(self):
        fake = _fake_frappe()
        with patch.object(mod, "frappe", fake):
            self.assertIsNone(mod.record_invoice_territory_exception(None))
            self.assertIsNone(mod.record_invoice_territory_exception({}))

    def test_doc_event_wrapper_swallows_everything(self):
        fake = _fake_frappe(get_value=self._territory_lookup({"EGDOKKI": "Dokki"}))
        fake.get_doc = MagicMock(side_effect=RuntimeError("nope"))

        with patch.object(mod, "frappe", fake):
            # Returns None and must not propagate: this runs inside submit().
            self.assertIsNone(
                mod.record_territory_exception_on_submit(_invoice(), "on_submit")
            )


# ---------------------------------------------------------------------------
# Backfill window
# ---------------------------------------------------------------------------

class TestBackfillWindow(unittest.TestCase):
    def test_defaults_to_the_last_90_days(self):
        fake = _fake_frappe()
        with patch.object(mod, "frappe", fake):
            from_date, to_date = mod._resolve_backfill_window(None, None, False)

        self.assertEqual(from_date, "2026-05-21")  # 2026-08-19 minus 90 days
        self.assertIsNone(to_date)

    def test_all_history_drops_the_date_filter(self):
        fake = _fake_frappe()
        with patch.object(mod, "frappe", fake):
            from_date, _ = mod._resolve_backfill_window(None, None, True)

        self.assertIsNone(from_date)

    def test_explicit_from_date_wins_over_the_default(self):
        fake = _fake_frappe()
        with patch.object(mod, "frappe", fake):
            from_date, _ = mod._resolve_backfill_window("2024-01-01", None, False)

        self.assertEqual(from_date, "2024-01-01")

    def test_summary_reports_what_the_window_skipped(self):
        """A partial run must never be able to look complete."""
        fake = _fake_frappe(count=1260)
        with patch.object(mod, "frappe", fake):
            summary = mod._run_backfill(dry_run=True)

        self.assertEqual(summary["skipped_by_window"], 1260)
        self.assertEqual(summary["window_from"], "2026-05-21")
        self.assertFalse(summary["all_history"])

    def test_all_history_run_skips_nothing(self):
        fake = _fake_frappe(count=1260)
        with patch.object(mod, "frappe", fake):
            summary = mod._run_backfill(all_history=True, dry_run=True)

        self.assertEqual(summary["skipped_by_window"], 0)
        self.assertEqual(summary["window_from"], "(all history)")
        self.assertTrue(summary["all_history"])


class TestBackfillRun(unittest.TestCase):
    def setUp(self):
        mod._OPTIONAL_FIELD_CACHE.clear()
        self.addCleanup(mod._OPTIONAL_FIELD_CACHE.clear)

    def _run(self, invoices, *, dry_run=False, exists=None):
        batches = [list(invoices), []]

        def _get_all(doctype, **kwargs):
            if doctype == mod.INVOICE_DOCTYPE:
                return batches.pop(0) if batches else []
            return []

        def _get_value(doctype, name=None, fields=None, as_dict=False, **kwargs):
            if doctype == "Territory":
                return {"EGDOKKI": "Dokki", "EGNASRCITY": "Nasr city"}.get(name)
            if doctype == "Address":
                return {} if as_dict else None
            return None

        fake = _fake_frappe(get_value=_get_value, get_all=_get_all, exists=exists)
        with patch.object(mod, "frappe", fake):
            summary = mod._run_backfill(all_history=True, dry_run=dry_run)
        return summary, fake

    def test_counts_by_type_lane_and_liveness(self):
        summary, fake = self._run(
            [
                # live POS-lane mismatch
                _invoice(name="SI-1"),
                # historical Woo-lane mismatch
                _invoice(name="SI-2", woo_source_type="woo"),
                # clean
                _invoice(name="SI-3", pos_profile="Dokki"),
                # unresolved territory
                _invoice(name="SI-4", territory=None),
            ]
        )

        self.assertEqual(summary["scanned"], 4)
        self.assertEqual(summary["clean"], 1)
        self.assertEqual(summary["created"], 3)
        self.assertEqual(summary["by_type"][mod.TYPE_BRANCH_MISMATCH], 2)
        self.assertEqual(summary["by_type"][mod.TYPE_TERRITORY_UNRESOLVED], 1)
        self.assertEqual(summary["by_lane"][mod.LANE_WOO], 1)
        self.assertEqual(summary["by_lane"][mod.LANE_POS], 2)
        self.assertEqual(summary["historical_repointings"], 1)
        self.assertEqual(summary["live"], 2)
        self.assertEqual(len(fake.inserted), 3)

    def test_dry_run_writes_nothing(self):
        summary, fake = self._run([_invoice(name="SI-1")], dry_run=True)

        self.assertEqual(summary["created"], 1)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(fake.inserted, [])

    def test_rerun_creates_nothing_new(self):
        summary, fake = self._run(
            [_invoice(name="SI-1"), _invoice(name="SI-2", territory=None)],
            exists=lambda *a, **k: "TEXC-PRIOR",
        )

        self.assertEqual(summary["already_recorded"], 2)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(fake.inserted, [])

    def test_backfill_never_writes_to_a_sales_invoice(self):
        """Those documents are submitted and carry GL entries."""
        _summary, fake = self._run([_invoice(name="SI-1")])

        written_doctypes = {doc.data.get("doctype") for doc in fake.inserted}
        self.assertEqual(written_doctypes, {mod.EXCEPTION_DOCTYPE})
        self.assertFalse(
            any(call.args and call.args[0] == mod.INVOICE_DOCTYPE for call in fake.get_doc.call_args_list),
            "must never load a Sales Invoice document for writing",
        )

    def test_one_bad_row_does_not_abort_the_sweep(self):
        bad = _invoice(name="SI-BAD")
        bad["docstatus"] = object()  # int() will explode on this

        summary, _fake = self._run([bad, _invoice(name="SI-2")])

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["created"], 1)


# ---------------------------------------------------------------------------
# Auto-close
# ---------------------------------------------------------------------------

class TestCloseResolvedTerritoryExceptions(unittest.TestCase):
    def _run(self, rows, territory_map):
        territory_reads: list = []

        def _get_all(doctype, **kwargs):
            return list(rows) if doctype == mod.EXCEPTION_DOCTYPE else []

        def _get_value(doctype, name=None, fields=None, as_dict=False, **kwargs):
            if doctype == "Territory":
                territory_reads.append(name)
                return territory_map.get(name)
            return None

        fake = _fake_frappe(
            get_all=_get_all,
            get_value=_get_value,
            docs={row["name"]: {"detail": "original detail"} for row in rows},
        )
        with patch.object(mod, "frappe", fake):
            summary = mod.close_resolved_territory_exceptions()
        return summary, fake, territory_reads

    def test_closes_a_row_whose_territory_now_agrees(self):
        """One Territory.pos_profile fix must clear the queue by itself."""
        rows = [
            {
                "name": "TEXC-1",
                "sales_invoice": "SI-1",
                "invoice_territory": "EGDOKKI",
                "pos_profile_used": "Dokki",
            }
        ]
        summary, fake, _reads = self._run(rows, {"EGDOKKI": "Dokki"})

        self.assertEqual(summary["closed"], 1)
        self.assertEqual(len(fake.saved), 1)
        closed = fake.saved[0]
        self.assertEqual(closed.status, mod.STATUS_TERRITORY_CORRECTED)
        self.assertEqual(closed.resolved_by, "manager@jarz.test")
        self.assertIsNotNone(closed.resolved_on)
        self.assertIn("[auto-closed]", closed.detail)
        self.assertIn("original detail", closed.detail)

    def test_leaves_a_still_divergent_row_open(self):
        rows = [
            {
                "name": "TEXC-1",
                "sales_invoice": "SI-1",
                "invoice_territory": "EGDOKKI",
                "pos_profile_used": "Nasr city",
            }
        ]
        summary, fake, _reads = self._run(rows, {"EGDOKKI": "Dokki"})

        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["closed"], 0)
        self.assertEqual(fake.saved, [])

    def test_reads_each_territory_only_once(self):
        """1,260 historical rows must not cost 1,260 Territory reads a night."""
        rows = [
            {
                "name": f"TEXC-{i}",
                "sales_invoice": f"SI-{i}",
                "invoice_territory": "EGDOKKI",
                "pos_profile_used": "Nasr city",
            }
            for i in range(1, 6)
        ]
        summary, _fake, reads = self._run(rows, {"EGDOKKI": "Dokki"})

        self.assertEqual(summary["checked"], 5)
        self.assertEqual(reads, ["EGDOKKI"])

    def test_never_reads_the_invoice_back(self):
        """The invoice's territory and profile are immutable once submitted."""
        rows = [
            {
                "name": "TEXC-1",
                "sales_invoice": "SI-1",
                "invoice_territory": "EGDOKKI",
                "pos_profile_used": "Dokki",
            }
        ]
        _summary, fake, _reads = self._run(rows, {"EGDOKKI": "Dokki"})

        read_doctypes = [call.args[0] for call in fake.db.get_value.call_args_list if call.args]
        self.assertNotIn(mod.INVOICE_DOCTYPE, read_doctypes)


if __name__ == "__main__":
    unittest.main()
