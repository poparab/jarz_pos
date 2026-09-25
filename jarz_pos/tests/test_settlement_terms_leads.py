"""Settlement terms on a Lead + carry-over to the Customer
(feature settlement-terms-leads: api/settlement_terms.py lead paths,
services/settlement_lead_terms.py, setup/settlement_terms_leads.py) -- mocked.

What is pinned, and why:

* **exactly one party.** ``customer`` and ``lead`` together, or neither, is
  refused before anything is read or written.
* **Lead round trip.** An unconverted Lead's terms are stored as JSON on the
  Lead through the SAME ``normalize_terms_input`` the Customer path uses (notes
  capped), and read back in the Customer response shape with ``party_type:
  "Lead"``, ``customer: null`` and a no-invoice status. A re-save keeps the
  stored anchor.
* **converted-lead redirect** under the Lead lock with LOCKING reads
  (REPEATABLE READ: a lock does not refresh the snapshot).
* **lazy carry-over on the customer path** (the app sends ``customer=`` for a
  converted lead): only when the Customer has no terms, commit-flagged on a GET
  only when it wrote, failures logged with a deferred insert.
* **carry-over.** Customer ``after_insert`` copies the Lead JSON once, re-reads
  it FOR UPDATE under the Lead lock (the stamp is built from that read), is a
  no-op without ``lead_name``, never overwrites existing Customer terms, never
  raises -- except a deadlock / lock-wait timeout, which is re-raised.
* **gates (owner decision).** A B2B Sales Rep reads and saves terms for Leads
  and Company customers only, never deletes; a Lead needs Lead write permission
  to save; a cashier is refused by the original credit gates.

Pure ``unittest`` with mocks, same conventions as test_settlement_terms_api.
"""

import datetime
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import frappe  # noqa: F401  (real frappe: this module runs under bench)
from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

from jarz_pos.api import credit
from jarz_pos.api import manager
from jarz_pos.api import settlement_terms as st
from jarz_pos.services import settlement_lead_terms as slt
from jarz_pos.services import settlement_schedule as ss

D = datetime.date
FRI = D(2026, 9, 25)
REP = ("B2B Sales Rep",)


class _Thrown(Exception):
    pass


def _throwing(message="throw", *args, **kwargs):
    raise _Thrown(str(message))


def _frappe(roles=REP, customer_type="Company"):
    fr = MagicMock()
    fr.throw.side_effect = _throwing
    fr.get_roles.return_value = list(roles)
    fr.db.exists.return_value = True

    def _get_value(doctype, name=None, field=None, *a, **k):
        if doctype == "Customer" and field == "customer_type":
            return customer_type
        return None

    fr.db.get_value.side_effect = _get_value
    return fr


class _LeadStore:
    """In-memory Lead JSON column, round-tripped through the real encoder."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.writes = []
        self.reads = []

    def read(self, lead, for_update=False):
        self.reads.append((lead, for_update))
        return self.data.get(lead)

    def write(self, lead, values):
        self.writes.append((lead, values))
        if values is None:
            self.data[lead] = None
        else:
            text = slt.encode_lead_terms(values, user="rep@x", modified="2026-09-25 10:00:00")
            self.data[lead] = slt.decode_lead_terms(text)


class _LeadPathMixin:
    """Patches shared by the Lead-path endpoint tests (unconverted by default)."""

    def _patches(self, fr, store, converted=None, today=FRI):
        return [
            patch.object(st, "frappe", fr),
            patch.object(st, "_lock_relationship_row"),
            patch.object(st, "_resolve_lead_customer", return_value=converted),
            patch.object(st, "_resolve_lead_customer_locked", return_value=converted),
            patch.object(st, "_lead_title", return_value="Cafe Lead"),
            patch.object(st, "_credit_currency", return_value="EGP"),
            patch.object(st, "_today", return_value=today),
            patch.object(slt, "lead_field_ready", return_value=True),
            patch.object(slt, "read_lead_terms", side_effect=store.read),
            patch.object(slt, "write_lead_terms", side_effect=store.write),
        ]

    def _run(self, fn, fr, store, *, converted=None, today=FRI, **kwargs):
        patches = self._patches(fr, store, converted=converted, today=today)
        for p in patches:
            p.start()
        try:
            return fn(**kwargs)
        finally:
            for p in reversed(patches):
                p.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Party arguments
# ─────────────────────────────────────────────────────────────────────────────


class TestPartyArgs(unittest.TestCase):
    def test_exactly_one_party(self):
        fr = _frappe()
        for kwargs in ({"customer": "C1", "lead": "L1"}, {}, {"customer": " ", "lead": " "}):
            for fn in (st.get_settlement_terms, st.delete_settlement_terms):
                with self.subTest(fn=fn.__name__, kwargs=kwargs):
                    with patch.object(st, "frappe", fr), \
                            patch.object(st, "_ensure_credit_payment_access"), \
                            patch.object(st, "_resolve_lead_customer") as resolve, \
                            patch.object(st, "_resolve_lead_customer_locked") as resolve_locked:
                        with self.assertRaises(_Thrown):
                            fn(**kwargs)
                    resolve.assert_not_called()
                    resolve_locked.assert_not_called()
            with self.subTest(fn="save", kwargs=kwargs):
                with patch.object(st, "frappe", fr), patch.object(ss, "normalize_terms_input") as normalize:
                    with self.assertRaises(_Thrown):
                        st.save_settlement_terms(cycle="Weekly", weekdays="Thu", **kwargs)
                normalize.assert_not_called()

    def test_customer_payload_carries_party(self):
        with patch.object(st, "_load_terms_row", return_value=None), \
                patch.object(st, "_open_credit_invoices", return_value=[]), \
                patch.object(st, "_credit_currency", return_value="EGP"), \
                patch.object(st, "_today", return_value=FRI), \
                patch.object(st, "_can_edit", return_value=True) as can_edit, \
                patch.object(st, "frappe") as fr:
            fr.db.get_value.return_value = "Cafe One"
            out = st._terms_payload("C1")
        self.assertEqual(out["party_type"], "Customer")
        self.assertEqual(out["party"], "C1")
        self.assertEqual(out["customer"], "C1")
        self.assertNotIn("lead", out, "the plain customer response gains only party_type/party")
        can_edit.assert_called_once_with(customer="C1")


# ─────────────────────────────────────────────────────────────────────────────
# Unconverted Lead: save / get / delete
# ─────────────────────────────────────────────────────────────────────────────


class TestLeadRoundTrip(_LeadPathMixin, unittest.TestCase):
    def test_save_then_get(self):
        fr = _frappe()
        store = _LeadStore()
        saved = self._run(
            st.save_settlement_terms, fr, store,
            lead="L1", cycle="weekly", weekdays='["thu"]', week_interval="2", notes=" returns ok ",
        )
        self.assertEqual(len(store.writes), 1)
        self.assertEqual(store.writes[0][0], "L1")
        fr.new_doc.assert_not_called()
        fr.get_doc.assert_not_called()

        got = self._run(st.get_settlement_terms, fr, store, lead="L1")
        for out in (saved, got):
            with self.subTest(out="saved" if out is saved else "got"):
                self.assertTrue(out["success"])
                self.assertEqual(out["party_type"], "Lead")
                self.assertEqual(out["party"], "L1")
                self.assertEqual(out["lead"], "L1")
                self.assertIsNone(out["customer"])
                self.assertEqual(out["customer_name"], "Cafe Lead")
                self.assertTrue(out["can_edit"])
                self.assertEqual(out["currency"], "EGP")
                terms = out["terms"]
                self.assertTrue(terms["exists"])
                self.assertIsNone(terms["customer"])
                self.assertEqual(terms["cycle"], "Weekly")
                self.assertEqual(terms["weekdays"], ["Thu"])
                self.assertEqual(terms["week_interval"], 2)
                self.assertEqual(terms["anchor_date"], "2026-09-25", "a fortnight defaults its anchor to today")
                self.assertEqual(terms["notes"], "returns ok")
                self.assertEqual(out["description"], "Every 2 weeks on Thursday")
                status = out["status"]
                self.assertEqual(status["state"], "none")
                self.assertEqual(status["open_balance"], 0.0)
                self.assertEqual(status["due_now_amount"], 0.0)
                self.assertEqual(status["overdue_amount"], 0.0)
                self.assertEqual(status["upcoming_dates"], ["2026-10-08", "2026-10-22", "2026-11-05"])
        self.assertIsNone(got["terms"]["last_reminder_on"])
        # The save's anchor lookup ran under the Lead lock, so it was a locking read.
        self.assertIn(("L1", True), store.reads)

    def test_stored_json_is_the_normalized_dict(self):
        fr = _frappe()
        store = _LeadStore()
        self._run(st.save_settlement_terms, fr, store, lead="L1", cycle="Days of Month", month_days="last, 15")
        stored = store.data["L1"]
        for key in slt.STORED_KEYS:
            self.assertIn(key, stored)
        self.assertEqual(stored["month_days"], "15,last")
        self.assertIsNone(stored["weekdays"], "fields of other cycles are cleared")
        self.assertEqual(stored["enabled"], 1)

    def test_resave_keeps_the_stored_anchor(self):
        fr = _frappe()
        store = _LeadStore()
        self._run(st.save_settlement_terms, fr, store, lead="L1", cycle="Every N Days", interval_days=10)
        out = self._run(
            st.save_settlement_terms, fr, store, today=D(2026, 10, 1),
            lead="L1", cycle="Every N Days", interval_days=14,
        )
        self.assertEqual(out["terms"]["anchor_date"], "2026-09-25")
        self.assertEqual(out["terms"]["interval_days"], 14)

    def test_get_without_terms(self):
        fr = _frappe()
        out = self._run(st.get_settlement_terms, fr, _LeadStore(), lead="L1")
        self.assertIsNone(out["terms"])
        self.assertIsNone(out["description"])
        self.assertEqual(out["status"]["state"], "none")
        self.assertEqual(out["status"]["upcoming_dates"], [])
        self.assertEqual(out["party_type"], "Lead")

    def test_manager_delete_clears_the_json(self):
        fr = _frappe(roles=("JARZ Manager",))
        store = _LeadStore()
        with patch.object(st, "_ensure_credit_payment_access"):
            self._run(st.save_settlement_terms, fr, store, lead="L1", cycle="On Delivery")
            out = self._run(st.delete_settlement_terms, fr, store, lead="L1")
        self.assertEqual(store.writes[-1], ("L1", None))
        self.assertIn(("L1", True), store.reads, "the delete decision is a locking read")
        self.assertIsNone(out["terms"])
        fr.delete_doc.assert_not_called()

    def test_missing_column_is_refused(self):
        fr = _frappe()
        store = _LeadStore()
        patches = self._patches(fr, store)
        for p in patches:
            p.start()
        try:
            with patch.object(slt, "lead_field_ready", return_value=False):
                with self.assertRaises(_Thrown):
                    st.save_settlement_terms(lead="L1", cycle="On Delivery")
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(store.writes, [])


class TestLeadValidationReuse(_LeadPathMixin, unittest.TestCase):
    def test_same_validator_refuses_before_any_write(self):
        cases = [
            {"cycle": "Weekly", "weekdays": "Thx"},
            {"cycle": "Weekly", "weekdays": ""},
            {"cycle": "Days of Month", "month_days": "32"},
            {"cycle": "Every N Days", "interval_days": 0},
            {"cycle": "Monthly"},
            {"cycle": None},
            {"cycle": "On Delivery", "remind_days_before": -1},
            {"cycle": "Every N Days", "interval_days": 3, "anchor_date": "2026-02-30"},
            {"cycle": "On Delivery", "notes": "x" * (ss.NOTES_MAX_LENGTH + 1)},
        ]
        for case in cases:
            with self.subTest(case={k: (v[:10] if isinstance(v, str) else v) for k, v in case.items()}):
                fr = _frappe()
                store = _LeadStore()
                with patch.object(ss, "normalize_terms_input", wraps=ss.normalize_terms_input) as normalize:
                    with self.assertRaises(_Thrown):
                        self._run(st.save_settlement_terms, fr, store, lead="L1", **case)
                normalize.assert_called_once()
                self.assertEqual(store.writes, [])

    def test_notes_cap(self):
        ok = ss.normalize_terms_input({"cycle": "On Delivery", "notes": "y" * ss.NOTES_MAX_LENGTH})
        self.assertEqual(len(ok["notes"]), ss.NOTES_MAX_LENGTH)
        with self.assertRaises(ss.SettlementTermsError):
            ss.normalize_terms_input({"cycle": "On Delivery", "notes": "y" * (ss.NOTES_MAX_LENGTH + 1)})

    def test_disabled_responsible_user_is_refused(self):
        fr = _frappe()
        fr.db.get_value.side_effect = None
        fr.db.get_value.return_value = 0  # User.enabled
        store = _LeadStore()
        with self.assertRaises(_Thrown):
            self._run(
                st.save_settlement_terms, fr, store,
                lead="L1", cycle="On Delivery", responsible_user="gone@x",
            )
        self.assertEqual(store.writes, [])


# ─────────────────────────────────────────────────────────────────────────────
# Converted Lead: redirect to the Customer, under the lock with locking reads
# ─────────────────────────────────────────────────────────────────────────────


class TestConvertedLeadRedirect(_LeadPathMixin, unittest.TestCase):
    def test_save_writes_the_customer_record(self):
        fr = _frappe()
        store = _LeadStore()
        with patch.object(st, "_require_customer", side_effect=lambda c: c) as require, \
                patch.object(st, "_save_customer_terms",
                             return_value={"success": True, "party_type": "Customer", "party": "CUST-1"}) as save:
            out = self._run(
                st.save_settlement_terms, fr, store, converted="CUST-1",
                lead="L1", cycle="Weekly", weekdays="Thu",
            )
        require.assert_called_once_with("CUST-1")
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], "CUST-1")
        self.assertEqual(save.call_args.args[1]["weekdays"], "Thu")
        self.assertEqual(store.writes, [], "the Lead JSON is not touched once converted")
        self.assertEqual(out["party_type"], "Customer")
        self.assertEqual(out["lead"], "L1")

    def test_save_locks_then_resolves_with_locking_reads(self):
        fr = _frappe()
        order = MagicMock()
        patches = self._patches(fr, _LeadStore(), converted=None)
        for p in patches:
            p.start()
        try:
            st._lock_relationship_row.side_effect = lambda *a, **k: order.lock(*a)
            st._resolve_lead_customer_locked.side_effect = lambda *a, **k: order.resolve(*a) and None
            st.save_settlement_terms(lead="L1", cycle="On Delivery")
            st._resolve_lead_customer.assert_not_called()
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(order.mock_calls[:2], [call.lock("Lead", "L1"), call.resolve("L1")])

    def test_locked_resolver_reads_for_update(self):
        fr = _frappe()
        fr.db.get_value.side_effect = None
        fr.db.get_value.return_value = None
        fr.db.sql.return_value = [{"name": "CUST-1"}]
        with patch.object(st, "frappe", fr):
            self.assertEqual(st._resolve_lead_customer_locked("L1"), "CUST-1")
        fr.db.get_value.assert_called_once_with("Lead", "L1", "customer", for_update=True)
        sql = fr.db.sql.call_args.args[0]
        self.assertIn("lead_name", sql)
        self.assertIn("FOR UPDATE", sql)

    def test_locked_resolver_refuses_conflicts(self):
        for direct, rows in (("CUST-2", [{"name": "CUST-1"}]), (None, [{"name": "A"}, {"name": "B"}])):
            with self.subTest(direct=direct, rows=rows):
                fr = _frappe()
                fr.db.get_value.side_effect = None
                fr.db.get_value.return_value = direct
                fr.db.sql.return_value = rows
                with patch.object(st, "frappe", fr):
                    with self.assertRaises(_Thrown):
                        st._resolve_lead_customer_locked("L1")

    def test_get_carries_over_preferring_the_lead(self):
        fr = _frappe()
        with patch.object(st, "_require_customer", side_effect=lambda c: c), \
                patch.object(st, "_carry_lead_terms_to") as carry, \
                patch.object(st, "_terms_payload",
                             return_value={"success": True, "party_type": "Customer", "party": "CUST-1"}) as payload:
            out = self._run(st.get_settlement_terms, fr, _LeadStore(), converted="CUST-1", lead="L1")
        carry.assert_called_once_with("CUST-1", get_request=True, prefer_lead="L1")
        payload.assert_called_once_with("CUST-1")
        self.assertEqual(out["lead"], "L1")
        self.assertEqual(out["party"], "CUST-1")

    def test_manager_delete_goes_to_the_customer(self):
        fr = _frappe(roles=("JARZ Manager",))
        store = _LeadStore()
        with patch.object(st, "_ensure_credit_payment_access"), \
                patch.object(st, "_require_customer", side_effect=lambda c: c), \
                patch.object(st, "_delete_customer_terms", return_value={"success": True}) as delete:
            out = self._run(st.delete_settlement_terms, fr, store, converted="CUST-1", lead="L1")
        delete.assert_called_once_with("CUST-1")
        self.assertEqual(store.writes, [])
        self.assertEqual(out["lead"], "L1")


# ─────────────────────────────────────────────────────────────────────────────
# Lazy carry-over on the customer path (W1)
# ─────────────────────────────────────────────────────────────────────────────


class TestCustomerPathCarry(unittest.TestCase):
    def _fr(self, has_terms):
        fr = _frappe(roles=("JARZ Manager",))
        fr.local.flags.commit = False
        fr.db.exists.side_effect = lambda doctype, filters=None, *a, **k: (
            has_terms if doctype == st.TERMS_DOCTYPE else True
        )
        return fr

    def test_skipped_when_the_customer_has_terms(self):
        fr = self._fr(has_terms=True)
        with patch.object(st, "frappe", fr), patch.object(slt, "leads_of_customer") as leads, \
                patch.object(slt, "safe_carry_over") as carry:
            self.assertIsNone(st._carry_lead_terms_to("C1", get_request=True))
        leads.assert_not_called()
        carry.assert_not_called()
        self.assertIs(fr.local.flags.commit, False)

    def test_carries_and_flags_a_get_for_commit(self):
        fr = self._fr(has_terms=False)
        with patch.object(st, "frappe", fr), \
                patch.object(slt, "leads_of_customer", return_value=["L0", "L1"]), \
                patch.object(slt, "safe_carry_over", side_effect=[None, slt.CARRY_CREATED]) as carry:
            outcome = st._carry_lead_terms_to("C1", get_request=True, prefer_lead="L1")
        self.assertEqual(outcome, slt.CARRY_CREATED)
        # The preferred lead goes first and the loop stops once a record exists.
        self.assertEqual(carry.call_args_list[0], call("L1", "C1", defer_log=True))
        self.assertIs(fr.local.flags.commit, True)

    def test_nothing_written_no_commit(self):
        fr = self._fr(has_terms=False)
        with patch.object(st, "frappe", fr), \
                patch.object(slt, "leads_of_customer", return_value=["L1"]), \
                patch.object(slt, "safe_carry_over", return_value=None):
            self.assertIsNone(st._carry_lead_terms_to("C1", get_request=True))
        self.assertIs(fr.local.flags.commit, False)

    def test_get_customer_runs_it(self):
        fr = _frappe(roles=("JARZ Manager",))
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_ledger_access"), \
                patch.object(st, "_require_customer", side_effect=lambda c: c), \
                patch.object(st, "_carry_lead_terms_to") as carry, \
                patch.object(st, "_terms_payload", return_value={"success": True}):
            st.get_settlement_terms(customer="C1")
        carry.assert_called_once_with("C1", get_request=True)

    def test_save_customer_carries_before_reading_the_record(self):
        fr = _frappe(roles=("JARZ Manager",))
        fr.db.get_value.side_effect = None
        fr.db.get_value.return_value = None
        order = MagicMock()
        fr.db.get_value.side_effect = lambda *a, **k: order.get_value(*a)
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_payment_access"), \
                patch.object(st, "_require_customer", side_effect=lambda c: c), \
                patch.object(st, "_carry_lead_terms_to", side_effect=lambda *a, **k: order.carry(*a, **k)), \
                patch.object(st, "_terms_payload", return_value={"success": True}):
            order.get_value.return_value = None
            st.save_settlement_terms(customer="C1", cycle="On Delivery")
        names = [c[0] for c in order.mock_calls if not c[0].startswith("get_value().")]
        self.assertEqual(names[0], "carry")
        self.assertEqual(order.mock_calls[0], call.carry("C1", get_request=False))


# ─────────────────────────────────────────────────────────────────────────────
# Gates (W4)
# ─────────────────────────────────────────────────────────────────────────────


class TestGates(_LeadPathMixin, unittest.TestCase):
    def test_can_edit_per_party(self):
        cases = [
            (("JARZ Manager",), "Individual", {"customer": "C1"}, True, True),
            (REP, "Company", {"customer": "C1"}, True, True),
            (REP, "Individual", {"customer": "C1"}, True, False),
            (("POS User",), "Company", {"customer": "C1"}, True, False),
            (REP, "Company", {"lead": "L1"}, True, True),
            (REP, "Company", {"lead": "L1"}, False, False),
            (("JARZ Manager",), "Company", {"lead": "L1"}, False, False),
            ((), "Company", {"lead": "L1"}, True, False),
        ]
        for roles, ctype, party, lead_write, expected in cases:
            with self.subTest(roles=roles, ctype=ctype, party=party, lead_write=lead_write):
                fr = _frappe(roles, customer_type=ctype)
                fr.has_permission.return_value = lead_write
                with patch.object(st, "frappe", fr):
                    self.assertEqual(st._can_edit(**party), expected)

    def test_rep_on_a_lead_without_the_credit_gates(self):
        fr = _frappe(REP)
        store = _LeadStore()
        with patch.object(st, "_ensure_credit_payment_access", side_effect=_Thrown("manager gate")), \
                patch.object(st, "_ensure_credit_ledger_access", side_effect=_Thrown("manager gate")):
            saved = self._run(st.save_settlement_terms, fr, store, lead="L1", cycle="On Delivery")
            got = self._run(st.get_settlement_terms, fr, store, lead="L1")
        self.assertTrue(saved["can_edit"])
        self.assertTrue(got["can_edit"])
        self.assertEqual(got["terms"]["cycle"], "On Delivery")

    def test_lead_save_needs_lead_write_permission(self):
        fr = _frappe(("JARZ Manager",))

        def _perm(doctype, ptype="read", doc=None, throw=False, *a, **k):
            if doctype == "Lead" and ptype == "write" and throw:
                raise _Thrown("no Lead write")
            return True

        fr.has_permission.side_effect = _perm
        store = _LeadStore()
        with patch.object(st, "_ensure_credit_payment_access"), patch.object(st, "_ensure_credit_ledger_access"):
            with self.assertRaises(_Thrown):
                self._run(st.save_settlement_terms, fr, store, lead="L1", cycle="On Delivery")
            with self.assertRaises(_Thrown):
                self._run(st.delete_settlement_terms, fr, store, lead="L1")
            out = self._run(st.get_settlement_terms, fr, store, lead="L1")  # read is enough to look
        self.assertEqual(store.writes, [])
        self.assertEqual(out["party_type"], "Lead")

    def test_rep_on_a_company_customer(self):
        fr = _frappe(REP, customer_type="Company")
        with patch.object(st, "frappe", fr), \
                patch.object(st, "_ensure_credit_payment_access", side_effect=_Thrown("manager gate")), \
                patch.object(st, "_ensure_credit_ledger_access", side_effect=_Thrown("manager gate")), \
                patch.object(st, "_require_customer", side_effect=lambda c: c), \
                patch.object(st, "_carry_lead_terms_to"), \
                patch.object(st, "_terms_payload", return_value={"success": True}) as payload, \
                patch.object(st, "_save_customer_terms", return_value={"success": True}) as save:
            st.get_settlement_terms(customer="C1")
            st.save_settlement_terms(customer="C1", cycle="On Delivery")
        payload.assert_called_once_with("C1")
        save.assert_called_once()

    def test_rep_refused_on_a_non_company_customer(self):
        fr = _frappe(REP, customer_type="Individual")
        with patch.object(st, "frappe", fr), \
                patch.object(st, "_require_customer", side_effect=lambda c: c), \
                patch.object(st, "_carry_lead_terms_to") as carry, \
                patch.object(st, "_terms_payload") as payload, \
                patch.object(st, "_save_customer_terms") as save:
            with self.assertRaises(_Thrown):
                st.get_settlement_terms(customer="C1")
            with self.assertRaises(_Thrown):
                st.save_settlement_terms(customer="C1", cycle="On Delivery")
        payload.assert_not_called()
        save.assert_not_called()
        carry.assert_not_called()

    def test_rep_refused_on_a_lead_converted_to_a_non_company_customer(self):
        fr = _frappe(REP, customer_type="Individual")
        store = _LeadStore()
        with patch.object(st, "_require_customer", side_effect=lambda c: c), \
                patch.object(st, "_save_customer_terms") as save:
            with self.assertRaises(_Thrown):
                self._run(st.save_settlement_terms, fr, store, converted="CUST-9", lead="L1", cycle="On Delivery")
        save.assert_not_called()

    def test_rep_may_not_delete(self):
        fr = _frappe(REP, customer_type="Company")
        store = _LeadStore({"L1": {"cycle": "On Delivery"}})
        # The real credit write gate runs, reading roles off the same mock.
        with patch.object(credit, "frappe", fr), \
                patch.object(st, "_delete_customer_terms") as delete:
            for kwargs in ({"lead": "L1"}, {"customer": "C1"}):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(_Thrown):
                        self._run(st.delete_settlement_terms, fr, store, **kwargs)
        delete.assert_not_called()
        self.assertEqual(store.writes, [])

    def test_cashier_is_refused(self):
        fr = _frappe(("POS User",))
        store = _LeadStore()
        with patch.object(credit, "frappe", fr), patch.object(manager, "frappe", fr), \
                patch.object(ss, "normalize_terms_input") as normalize:
            for fn, kwargs in (
                (st.save_settlement_terms, {"lead": "L1", "cycle": "On Delivery"}),
                (st.save_settlement_terms, {"customer": "C1", "cycle": "On Delivery"}),
                (st.get_settlement_terms, {"lead": "L1"}),
                (st.get_settlement_terms, {"customer": "C1"}),
                (st.delete_settlement_terms, {"lead": "L1"}),
            ):
                with self.subTest(fn=fn.__name__, kwargs=kwargs):
                    with self.assertRaises(_Thrown):
                        self._run(fn, fr, store, **kwargs)
        normalize.assert_not_called()
        self.assertEqual(store.writes, [])

    def test_manager_still_allowed(self):
        fr = _frappe(("JARZ Manager",))
        store = _LeadStore()
        with patch.object(credit, "frappe", fr), patch.object(manager, "frappe", fr):
            out = self._run(st.save_settlement_terms, fr, store, lead="L1", cycle="On Delivery")
        self.assertTrue(out["can_edit"])


# ─────────────────────────────────────────────────────────────────────────────
# Carry-over (services/settlement_lead_terms)
# ─────────────────────────────────────────────────────────────────────────────


def _lead_json(**overrides):
    data = {
        "cycle": "Weekly", "enabled": 1, "weekdays": "Thu", "week_interval": 1,
        "month_days": None, "interval_days": None, "anchor_date": None,
        "remind_days_before": 1, "overdue_repeat_days": 2, "responsible_user": None,
        "notes": "returns ok", "modified": "2026-09-20 10:00:00", "modified_by": "rep@x",
    }
    data.update(overrides)
    return json.dumps(data)


LEAD_JSON = _lead_json()


class _FakeDb:
    """Lead column + Settlement Terms existence, as a stateful MagicMock frappe.

    ``locked_json`` (optional) is what a FOR UPDATE read returns -- a newer value
    a concurrent save committed while the plain snapshot still shows the old one.
    """

    def __init__(self, lead_json=LEAD_JSON, customer_has_terms=False, locked_json=None):
        self.lead_column = {"L1": lead_json}
        self.locked_json = locked_json
        self.terms_customers = {"CUST-1"} if customer_has_terms else set()
        self.fr = MagicMock()
        self.fr.throw.side_effect = _throwing
        self.fr.local.message_log = ["earlier message"]
        self.fr.db.has_column.return_value = True
        self.fr.db.get_value.side_effect = self._get_value
        self.fr.db.set_value.side_effect = self._set_value
        self.fr.db.exists.side_effect = self._exists
        self.doc = MagicMock()
        self.doc.insert.side_effect = self._insert
        self.fr.new_doc.return_value = self.doc

    def _get_value(self, doctype, name, field=None, *a, **k):
        if doctype == "Lead" and field == slt.LEAD_FIELD:
            if k.get("for_update") and self.locked_json is not None:
                self.lead_column[name] = self.locked_json
                self.locked_json = None
            return self.lead_column.get(name)
        return None

    def _set_value(self, doctype, name, field, value=None, *a, **k):
        if doctype == "Lead" and field == slt.LEAD_FIELD:
            self.lead_column[name] = value

    def _exists(self, doctype, filters=None, *a, **k):
        if doctype == "DocType":
            return True
        if doctype == slt.TERMS_DOCTYPE:
            return (filters or {}).get("customer") in self.terms_customers
        return True

    def _insert(self, *a, **k):
        self.terms_customers.add(self.doc.customer)


class TestCarryOver(unittest.TestCase):
    def _hook(self, db, lead_name="L1", name="CUST-1"):
        with patch.object(slt, "frappe", db.fr), patch.object(st, "invalidate_collections_count_cache"):
            slt.on_customer_after_insert(SimpleNamespace(name=name, lead_name=lead_name))

    def test_creates_once_and_is_idempotent(self):
        db = _FakeDb()
        self._hook(db)
        db.fr.new_doc.assert_called_once_with(slt.TERMS_DOCTYPE)
        self.assertEqual(db.doc.customer, "CUST-1")
        written = {c.args[0]: c.args[1] for c in db.doc.set.call_args_list}
        self.assertEqual(written["cycle"], "Weekly")
        self.assertEqual(written["weekdays"], "Thu")
        self.assertEqual(written["notes"], "returns ok")
        db.doc.insert.assert_called_once_with(ignore_permissions=True)
        stamped = json.loads(db.lead_column["L1"])
        self.assertEqual(stamped[slt.CARRIED_TO_KEY], "CUST-1")
        self.assertEqual(stamped["cycle"], "Weekly", "the Lead JSON stays as history")

        with patch.object(slt, "frappe", db.fr), patch.object(st, "invalidate_collections_count_cache"):
            self.assertIsNone(slt.safe_carry_over("L1", "CUST-1"))
        db.fr.new_doc.assert_called_once()

    def test_locks_the_lead_and_uses_the_locked_read(self):
        # The snapshot says "returns ok"; a save committed "newer terms" while
        # this transaction waited for the lock. Both the record and the stamp
        # must come from the locking read.
        db = _FakeDb(locked_json=_lead_json(notes="newer terms", weekdays="Mon"))
        self._hook(db)
        sql = db.fr.db.sql.call_args.args
        self.assertIn("FOR UPDATE", sql[0])
        self.assertEqual(sql[1], ("L1",))
        self.assertIn(
            call("Lead", "L1", slt.LEAD_FIELD, for_update=True), db.fr.db.get_value.call_args_list
        )
        written = {c.args[0]: c.args[1] for c in db.doc.set.call_args_list}
        self.assertEqual(written["notes"], "newer terms")
        self.assertEqual(written["weekdays"], "Mon")
        stamped = json.loads(db.lead_column["L1"])
        self.assertEqual(stamped["notes"], "newer terms", "the stamp never writes a stale dict back")

    def test_writes_bump_modified(self):
        db = _FakeDb()
        self._hook(db)
        lead_writes = [c for c in db.fr.db.set_value.call_args_list if c.args[0] == "Lead"]
        self.assertTrue(lead_writes)
        for c in lead_writes:
            self.assertEqual(c.kwargs, {"update_modified": True})

    def test_customer_terms_win(self):
        db = _FakeDb(customer_has_terms=True)
        with patch.object(slt, "frappe", db.fr), patch.object(st, "invalidate_collections_count_cache"):
            outcome = slt.safe_carry_over("L1", "CUST-1")
        self.assertEqual(outcome, slt.CARRY_SUPERSEDED)
        db.fr.new_doc.assert_not_called()
        stamped = json.loads(db.lead_column["L1"])
        self.assertEqual(stamped[slt.CARRIED_TO_KEY], "CUST-1")
        self.assertEqual(stamped[slt.CARRY_OUTCOME_KEY], slt.CARRY_SUPERSEDED)

    def test_customer_without_lead_does_no_db_work(self):
        db = _FakeDb()
        self._hook(db, lead_name=None)
        db.fr.db.get_value.assert_not_called()
        db.fr.db.exists.assert_not_called()
        db.fr.db.has_column.assert_not_called()
        db.fr.db.sql.assert_not_called()
        db.fr.new_doc.assert_not_called()

    def test_nothing_stored_nothing_made_and_nothing_locked(self):
        for raw in (None, "", "not json", json.dumps({"notes": "no cycle"})):
            with self.subTest(raw=raw):
                db = _FakeDb(lead_json=raw)
                self._hook(db)
                db.fr.new_doc.assert_not_called()
                db.fr.db.sql.assert_not_called()

    def test_failure_never_raises_and_rolls_back(self):
        db = _FakeDb()

        def _boom(*a, **k):
            db.fr.local.message_log.append("Invalid settlement terms")
            raise RuntimeError("validation failed")

        db.doc.insert.side_effect = _boom
        self._hook(db)  # must not raise
        db.fr.db.savepoint.assert_called_once_with(slt._HOOK_SAVEPOINT)
        db.fr.db.rollback.assert_called_once_with(save_point=slt._HOOK_SAVEPOINT)
        db.fr.log_error.assert_called_once()
        self.assertFalse(db.fr.log_error.call_args.kwargs["defer_insert"])
        self.assertEqual(db.fr.local.message_log, ["earlier message"], "queued message withdrawn")

    def test_get_path_defers_the_error_log(self):
        db = _FakeDb()
        db.doc.insert.side_effect = RuntimeError("validation failed")
        with patch.object(slt, "frappe", db.fr):
            self.assertIsNone(slt.safe_carry_over("L1", "CUST-1", defer_log=True))
        self.assertTrue(db.fr.log_error.call_args.kwargs["defer_insert"])

    def test_deadlock_and_lock_timeout_are_re_raised(self):
        for exc in (QueryDeadlockError("deadlock"), QueryTimeoutError("lock wait timeout")):
            with self.subTest(exc=type(exc).__name__):
                db = _FakeDb()
                db.doc.insert.side_effect = exc
                with patch.object(slt, "frappe", db.fr):
                    with self.assertRaises(type(exc)):
                        slt.on_customer_after_insert(SimpleNamespace(name="CUST-1", lead_name="L1"))
                db.fr.log_error.assert_not_called()

    def test_hook_never_raises_even_if_everything_is_broken(self):
        fr = MagicMock()
        fr.db.savepoint.side_effect = RuntimeError("no db")
        fr.db.has_column.side_effect = RuntimeError("no db")
        fr.db.exists.side_effect = RuntimeError("no db")
        fr.log_error.side_effect = RuntimeError("log down")
        with patch.object(slt, "frappe", fr):
            slt.on_customer_after_insert(SimpleNamespace(name="CUST-1", lead_name="L1"))

    def test_mark_leads_superseded_after_a_delete(self):
        db = _FakeDb()
        base_get = db.fr.db.get_value.side_effect

        def _get_value(doctype, name, field=None, *a, **k):
            if doctype == "Customer" and field == "lead_name":
                return "L1"
            return base_get(doctype, name, field, *a, **k)

        db.fr.db.get_value.side_effect = _get_value
        db.fr.get_all.return_value = []
        with patch.object(slt, "frappe", db.fr):
            self.assertEqual(slt.mark_leads_superseded("CUST-1"), 1)
            self.assertIsNone(slt.carry_over_lead_terms("L1", "CUST-1"))
        db.fr.new_doc.assert_not_called()
        self.assertIn("FOR UPDATE", db.fr.db.sql.call_args.args[0])


class TestLeadJson(unittest.TestCase):
    def test_write_encodes_dates_and_bumps_modified(self):
        fr = MagicMock()
        fr.session.user = "rep@x"
        values = {"cycle": "Every N Days", "enabled": 1, "interval_days": 10, "anchor_date": D(2026, 9, 25)}
        with patch.object(slt, "frappe", fr):
            slt.write_lead_terms("L1", values)
        args, kwargs = fr.db.set_value.call_args
        self.assertEqual(args[:3], ("Lead", "L1", slt.LEAD_FIELD))
        self.assertEqual(kwargs, {"update_modified": True}, "a stale Desk form must hit 'modified since'")
        stored = json.loads(args[3])
        self.assertEqual(stored["anchor_date"], "2026-09-25")
        self.assertEqual(stored["modified_by"], "rep@x")
        fr.get_doc.assert_not_called()

    def test_clear(self):
        fr = MagicMock()
        with patch.object(slt, "frappe", fr):
            slt.write_lead_terms("L1", None)
        self.assertIsNone(fr.db.set_value.call_args.args[3])

    def test_locking_read(self):
        fr = MagicMock()
        fr.db.has_column.return_value = True
        fr.db.get_value.return_value = LEAD_JSON
        with patch.object(slt, "frappe", fr):
            self.assertEqual(slt.read_lead_terms("L1", for_update=True)["cycle"], "Weekly")
        fr.db.get_value.assert_called_once_with("Lead", "L1", slt.LEAD_FIELD, for_update=True)

    def test_decode_accepts_dict_and_text(self):
        self.assertEqual(slt.decode_lead_terms({"cycle": "Weekly"})["cycle"], "Weekly")
        self.assertEqual(slt.decode_lead_terms(LEAD_JSON)["weekdays"], "Thu")
        self.assertIsNone(slt.decode_lead_terms("[1, 2]"))


# ─────────────────────────────────────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────────────────────────────────────


class TestWiring(unittest.TestCase):
    def test_hooks_registered(self):
        from jarz_pos import hooks

        self.assertEqual(
            hooks.doc_events["Customer"]["after_insert"],
            "jarz_pos.services.settlement_lead_terms.on_customer_after_insert",
        )
        self.assertIn(
            "jarz_pos.setup.settlement_terms_leads.ensure_lead_settlement_terms_field",
            hooks.after_migrate,
        )

    def test_field_spec(self):
        from jarz_pos.setup import settlement_terms_leads as setup

        spec = setup.lead_field_spec()
        self.assertEqual(spec["fieldname"], slt.LEAD_FIELD)
        self.assertEqual(spec["fieldtype"], "JSON")
        self.assertEqual(spec["read_only"], 1)
        self.assertEqual(spec["no_copy"], 1)

    def test_seeder_never_raises(self):
        from jarz_pos.setup import settlement_terms_leads as setup

        with patch.object(setup, "frappe") as fr:
            fr.db.exists.side_effect = RuntimeError("db down")
            log = setup.ensure_lead_settlement_terms_field()
        self.assertIsInstance(log, dict)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
