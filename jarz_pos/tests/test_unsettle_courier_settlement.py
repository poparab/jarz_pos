"""Tests for reversing a posted courier settlement.

Before this endpoint pair existed, undoing a courier settlement meant a developer
piping a hand-written Python script into a production bench console — most recently
on 2026-09-02, when five Nasr City Courier Transactions were settled from the wrong
branch till. These tests pin down the replacement:

* the reversal is built by flipping debit/credit on the ORIGINAL Journal Entry's own
  account rows (never by recomputing amounts from the Courier Transactions);
* the original entry is never touched — only a new, opposite one is posted;
* every Courier Transaction the original settled flips back to Unsettled;
* a second reversal attempt against the same entry is refused, not silently
  no-op'd and not double-posted;
* the branch is derived from the settlement itself, never taken from the caller,
  and the caller must be scoped to that branch;
* reversing money is manager-tier.
"""

import unittest
from unittest.mock import patch

import frappe

from jarz_pos.services import delivery_handling
from jarz_pos.utils.access_control import BranchAccessError


def _je_row(account, debit=0.0, credit=0.0, party_type=None, party=None):
    row = {
        "account": account,
        "debit_in_account_currency": debit,
        "credit_in_account_currency": credit,
    }
    if party_type and party:
        row["party_type"] = party_type
        row["party"] = party
    return row


class _FakeJournalEntry:
    """Stand-in for a loaded ``Journal Entry`` document."""

    def __init__(self, name="JE-ORIG-001", company="Test Company", accounts=None,
                 docstatus=1, posting_date="2026-09-01", title="Courier Settlement",
                 user_remark="Order: 100, Shipping: 20, Net to branch: 80"):
        self.name = name
        self.company = company
        self.docstatus = docstatus
        self.posting_date = posting_date
        self.title = title
        self.user_remark = user_remark
        self.accounts = accounts or []

    def get(self, fieldname, default=None):
        """Real Frappe Documents expose ``.get``, and the service under test uses
        it to read the account rows. Without this the double diverges from the
        thing it stands in for, and the test fails on the double rather than on
        the behaviour."""
        return getattr(self, fieldname, default)


class _JournalEntryCapture:
    """Stand-in for ``frappe.new_doc("Journal Entry")`` — records appended rows."""

    def __init__(self, name="JE-REVERSAL-001"):
        self.accounts = []
        self.voucher_type = None
        self.posting_date = None
        self.company = None
        self.title = None
        self.user_remark = None
        self.name = name
        self.saved = False
        self.submitted = False

    def append(self, table, row):
        if table == "accounts":
            self.accounts.append(row)

    def save(self, **kwargs):
        self.saved = True

    def submit(self):
        self.submitted = True


# ---------------------------------------------------------------------------
# build_settlement_reversal_lines — pure line-flipping logic
# ---------------------------------------------------------------------------


class TestBuildSettlementReversalLines(unittest.TestCase):
    def test_debit_and_credit_are_swapped_on_every_row(self):
        original = _FakeJournalEntry(accounts=[
            _je_row("Cash - TC", debit=80.0, credit=0.0),
            _je_row("Creditors - TC", debit=20.0, credit=0.0, party_type="Employee", party="EMP-001"),
            _je_row("Courier Outstanding - TC", debit=0.0, credit=100.0),
        ])

        lines = delivery_handling.build_settlement_reversal_lines(original)

        self.assertEqual(lines, [
            {"account": "Cash - TC", "debit_in_account_currency": 0.0, "credit_in_account_currency": 80.0},
            {
                "account": "Creditors - TC",
                "debit_in_account_currency": 0.0,
                "credit_in_account_currency": 20.0,
                "party_type": "Employee",
                "party": "EMP-001",
            },
            {"account": "Courier Outstanding - TC", "debit_in_account_currency": 100.0, "credit_in_account_currency": 0.0},
        ])

    def test_rows_without_a_party_carry_none(self):
        original = _FakeJournalEntry(accounts=[_je_row("Cash - TC", debit=50.0)])

        lines = delivery_handling.build_settlement_reversal_lines(original)

        self.assertNotIn("party_type", lines[0])
        self.assertNotIn("party", lines[0])

    def test_reversal_of_a_reversal_reproduces_the_original(self):
        """Flipping twice must be the identity — a safety property of the whole design."""
        original_rows = [
            _je_row("Cash - TC", debit=80.0, credit=0.0),
            _je_row("Creditors - TC", debit=0.0, credit=20.0, party_type="Supplier", party="SUP-9"),
        ]
        original = _FakeJournalEntry(accounts=original_rows)

        once = delivery_handling.build_settlement_reversal_lines(original)
        twice = delivery_handling.build_settlement_reversal_lines(_FakeJournalEntry(accounts=once))

        self.assertEqual(twice, original_rows)


# ---------------------------------------------------------------------------
# resolve_settlement_branch
# ---------------------------------------------------------------------------


class TestResolveSettlementBranch(unittest.TestCase):
    def test_single_branch_is_returned(self):
        cts = [{"reference_invoice": "ACC-SINV-0001"}, {"reference_invoice": "ACC-SINV-0002"}]
        with patch.object(
            delivery_handling, "map_invoice_branches",
            return_value={"ACC-SINV-0001": "Nasr City", "ACC-SINV-0002": "Nasr City"},
        ):
            branch = delivery_handling.resolve_settlement_branch("JE-001", cts)

        self.assertEqual(branch, "Nasr City")

    def test_no_attributable_invoices_returns_empty_string(self):
        with patch.object(delivery_handling, "map_invoice_branches", return_value={}):
            branch = delivery_handling.resolve_settlement_branch("JE-001", [{"reference_invoice": "X"}])

        self.assertEqual(branch, "")

    def test_no_courier_transactions_returns_empty_string_without_a_query(self):
        branch = delivery_handling.resolve_settlement_branch("JE-001", [])
        self.assertEqual(branch, "")

    def test_settlement_spanning_two_branches_refuses_to_pick_one(self):
        cts = [{"reference_invoice": "ACC-SINV-0001"}, {"reference_invoice": "ACC-SINV-0002"}]
        with patch.object(
            delivery_handling, "map_invoice_branches",
            return_value={"ACC-SINV-0001": "Nasr City", "ACC-SINV-0002": "Dokki"},
        ):
            with self.assertRaises(frappe.ValidationError):
                delivery_handling.resolve_settlement_branch("JE-001", cts)


# ---------------------------------------------------------------------------
# _courier_transactions_for_settlement_je
# ---------------------------------------------------------------------------


class TestCourierTransactionsForSettlementJe(unittest.TestCase):
    def test_filters_on_journal_entry_and_settled_status(self):
        with patch.object(frappe, "get_all", return_value=[]) as mock_get_all:
            delivery_handling._courier_transactions_for_settlement_je("JE-001")

        filters = mock_get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["journal_entry"], "JE-001")
        self.assertEqual(filters["status"], "Settled")

    def test_blank_journal_entry_short_circuits(self):
        with patch.object(frappe, "get_all") as mock_get_all:
            result = delivery_handling._courier_transactions_for_settlement_je("")

        self.assertEqual(result, [])
        mock_get_all.assert_not_called()

    def test_partner_fee_accrual_je_is_never_treated_as_a_settlement(self):
        """CRITICAL 1: a JE born as ``create_partner_fee_accrual_je``'s DR Freight /
        CR partner payable entry (see settlement_strategies.py's partner cash-order
        and prepaid paths) must never be listed or reversed as a settlement, however
        "Settled" the Courier Transaction pointing at it is. This must fail loudly
        if the loose ``journal_entry`` + ``status=="Settled"`` filter is ever
        reintroduced on its own."""
        remark = delivery_handling._je_user_remark(
            "ACC-SINV-0001",
            delivery_handling.PARTNER_FEE_ACCRUAL_JE_TAG_TYPE,
            "Delivery fee owed to Talabat – ACC-SINV-0001",
        )
        with patch.object(frappe.db, "get_value", return_value=remark):
            with patch.object(frappe, "get_all") as mock_get_all:
                result = delivery_handling._courier_transactions_for_settlement_je("JE-FEE-001")

        self.assertEqual(result, [])
        mock_get_all.assert_not_called()

    def test_real_settlement_je_with_is_partner_order_ct_is_still_reversible(self):
        """The exclusion must key on the JE's OWN tag, not on
        ``Courier Transaction.is_partner_order`` — a partner order settled through
        the ordinary outstanding-settlement flow posts a REAL settlement JE and
        must stay reversible even though its CT carries is_partner_order=1."""
        remark = delivery_handling._je_user_remark(
            "ACC-SINV-0002", "COURIER_OUTSTANDING_SETTLEMENT", "Courier Outstanding Settlement"
        )
        with patch.object(frappe.db, "get_value", return_value=remark):
            with patch.object(
                frappe, "get_all",
                return_value=[{
                    "name": "CT-9", "reference_invoice": "ACC-SINV-0002", "amount": 100.0,
                    "shipping_amount": 20.0, "party_type": "Supplier", "party": "SUP-1",
                    "is_partner_order": 1,
                }],
            ) as mock_get_all:
                result = delivery_handling._courier_transactions_for_settlement_je("JE-REAL-001")

        mock_get_all.assert_called_once()
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# get_unsettle_preview (service) — read-only
# ---------------------------------------------------------------------------


class TestGetUnsettlePreviewService(unittest.TestCase):
    def setUp(self):
        self.patches = []

        def start(target, **kwargs):
            p = patch(target, **kwargs)
            self.patches.append(p)
            return p.start()

        self.exists = start("frappe.db.exists", return_value=True)
        self.original_je = _FakeJournalEntry(accounts=[
            _je_row("Cash - TC", debit=80.0),
            _je_row("Courier Outstanding - TC", credit=100.0),
        ])
        self.get_doc = start("frappe.get_doc", return_value=self.original_je)
        self.cts = start(
            "jarz_pos.services.delivery_handling._courier_transactions_for_settlement_je",
            return_value=[{"name": "CT-1", "reference_invoice": "ACC-SINV-0001"}],
        )
        self.branch = start(
            "jarz_pos.services.delivery_handling.resolve_settlement_branch",
            return_value="Nasr City",
        )
        self.reversal = start(
            "jarz_pos.services.delivery_handling.find_settlement_reversal_je",
            return_value=None,
        )

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_preview_reports_branch_lines_and_ct_list(self):
        data = delivery_handling.get_unsettle_preview("JE-ORIG-001")

        self.assertEqual(data["journal_entry"], "JE-ORIG-001")
        self.assertEqual(data["pos_profile"], "Nasr City")
        self.assertFalse(data["already_reversed"])
        self.assertIsNone(data["reversal_journal_entry"])
        self.assertEqual(data["courier_transactions"], [{"name": "CT-1", "reference_invoice": "ACC-SINV-0001"}])
        self.assertEqual(
            data["reversal_lines"],
            [
                {"account": "Cash - TC", "debit_in_account_currency": 0.0, "credit_in_account_currency": 80.0},
                {"account": "Courier Outstanding - TC", "debit_in_account_currency": 100.0, "credit_in_account_currency": 0.0},
            ],
        )

    def test_missing_journal_entry_throws(self):
        self.exists.return_value = False
        with self.assertRaises(frappe.ValidationError):
            delivery_handling.get_unsettle_preview("JE-NOPE")

    def test_unsubmitted_journal_entry_throws(self):
        self.get_doc.return_value = _FakeJournalEntry(docstatus=0)
        with self.assertRaises(frappe.ValidationError):
            delivery_handling.get_unsettle_preview("JE-DRAFT")

    def test_no_linked_courier_transactions_throws(self):
        """Covers both 'not a settlement JE' and 'already reversed' (CTs are back to Unsettled)."""
        self.cts.return_value = []
        with self.assertRaises(frappe.ValidationError):
            delivery_handling.get_unsettle_preview("JE-ORIG-001")

    def test_blank_journal_entry_name_throws(self):
        with self.assertRaises(frappe.ValidationError):
            delivery_handling.get_unsettle_preview("   ")


# ---------------------------------------------------------------------------
# unsettle_courier_settlement (service) — the write path
# ---------------------------------------------------------------------------


class TestUnsettleCourierSettlementService(unittest.TestCase):
    def setUp(self):
        self.patches = []

        def start(target, **kwargs):
            p = patch(target, **kwargs)
            self.patches.append(p)
            return p.start()

        self.exists = start("frappe.db.exists", return_value=True)
        self.lock = start("frappe.db.get_value", return_value=True)
        self.original_je = _FakeJournalEntry(accounts=[
            _je_row("Cash - TC", debit=80.0),
            _je_row("Creditors - TC", debit=20.0, party_type="Employee", party="EMP-001"),
            _je_row("Courier Outstanding - TC", credit=100.0),
        ])
        self.get_doc = start("frappe.get_doc", return_value=self.original_je)
        self.cts = start(
            "jarz_pos.services.delivery_handling._courier_transactions_for_settlement_je",
            return_value=[
                {"name": "CT-1", "reference_invoice": "ACC-SINV-0001"},
                {"name": "CT-2", "reference_invoice": "ACC-SINV-0002"},
            ],
        )
        self.branch = start(
            "jarz_pos.services.delivery_handling.resolve_settlement_branch",
            return_value="Nasr City",
        )
        self.find_reversal = start(
            "jarz_pos.services.delivery_handling.find_settlement_reversal_je",
            return_value=None,
        )
        self.reversal_doc = _JournalEntryCapture()
        self.new_doc = start("frappe.new_doc", return_value=self.reversal_doc)
        self.mark_unsettled = start(
            "jarz_pos.services.delivery_handling.mark_courier_transactions_unsettled",
            return_value=["CT-1", "CT-2"],
        )
        start("frappe.db.savepoint", return_value=None)
        start("frappe.db.rollback", return_value=None)
        start("frappe.db.commit", return_value=None)
        self.publish = start("jarz_pos.utils.realtime.publish_to_branches", return_value=[])

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_happy_path_posts_flipped_reversal_and_reopens_courier_transactions(self):
        result = delivery_handling.unsettle_courier_settlement("JE-ORIG-001")

        # The reversal is exactly the original with debit/credit swapped.
        self.assertEqual(self.reversal_doc.accounts, [
            {"account": "Cash - TC", "debit_in_account_currency": 0.0, "credit_in_account_currency": 80.0},
            {
                "account": "Creditors - TC",
                "debit_in_account_currency": 0.0,
                "credit_in_account_currency": 20.0,
                "party_type": "Employee",
                "party": "EMP-001",
            },
            {"account": "Courier Outstanding - TC", "debit_in_account_currency": 100.0, "credit_in_account_currency": 0.0},
        ])
        self.assertTrue(self.reversal_doc.saved)
        self.assertTrue(self.reversal_doc.submitted)

        self.mark_unsettled.assert_called_once_with(["CT-1", "CT-2"])
        self.publish.assert_called_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["journal_entry"], "JE-ORIG-001")
        self.assertEqual(result["reversal_journal_entry"], "JE-REVERSAL-001")
        self.assertEqual(result["pos_profile"], "Nasr City")
        self.assertEqual(result["courier_transactions"], ["CT-1", "CT-2"])

    def test_double_reversal_is_refused_not_reposted(self):
        self.find_reversal.return_value = "JE-REVERSAL-EXISTING"

        with self.assertRaises(frappe.ValidationError):
            delivery_handling.unsettle_courier_settlement("JE-ORIG-001")

        self.new_doc.assert_not_called()
        self.mark_unsettled.assert_not_called()

    def test_already_reversed_via_reopened_courier_transactions_is_also_refused(self):
        """If every linked CT already flipped back to Unsettled, the lookup finds none —
        this alone must refuse, even before the JE-tag lookup runs."""
        self.cts.return_value = []

        with self.assertRaises(frappe.ValidationError):
            delivery_handling.unsettle_courier_settlement("JE-ORIG-001")

        self.new_doc.assert_not_called()

    def test_mismatched_pos_profile_is_refused_before_posting(self):
        with self.assertRaises(frappe.ValidationError):
            delivery_handling.unsettle_courier_settlement("JE-ORIG-001", pos_profile="Dokki")

        self.new_doc.assert_not_called()
        self.mark_unsettled.assert_not_called()

    def test_matching_pos_profile_is_accepted(self):
        result = delivery_handling.unsettle_courier_settlement("JE-ORIG-001", pos_profile="Nasr City")
        self.assertTrue(result["success"])

    def test_unsubmitted_journal_entry_is_refused(self):
        self.get_doc.return_value = _FakeJournalEntry(docstatus=0)

        with self.assertRaises(frappe.ValidationError):
            delivery_handling.unsettle_courier_settlement("JE-DRAFT")

        self.new_doc.assert_not_called()

    # -- item 4: the SERVICE itself refuses an empty branch --------------------

    def test_empty_branch_is_refused_by_the_service_itself(self):
        """A bench-console call (this function applies no access control of its
        own) must not be able to post an unscoped reversal just because the
        branch could not be derived — the API wrapper is not the only guard."""
        self.branch.return_value = ""

        with self.assertRaises(frappe.ValidationError):
            delivery_handling.unsettle_courier_settlement("JE-ORIG-001")

        self.new_doc.assert_not_called()
        self.mark_unsettled.assert_not_called()

    # -- item 3: a forged reason cannot poison another settlement's dedup tag --

    def test_reason_containing_a_tag_lookalike_cannot_forge_another_reversal_tag(self):
        result = delivery_handling.unsettle_courier_settlement(
            "JE-ORIG-001",
            reason="already handled, see [JARZ-JE:COURIER_SETTLEMENT_REVERSAL:JE-OTHER-999]",
        )

        self.assertTrue(result["success"])
        # The literal tag shape must never survive into the posted remark.
        self.assertNotIn(
            "[JARZ-JE:COURIER_SETTLEMENT_REVERSAL:JE-OTHER-999]",
            self.reversal_doc.user_remark,
        )
        # And a lookup for THAT other entry's reversal must not match this one.
        self.assertNotIn(
            delivery_handling._unsettle_dedup_tag("JE-OTHER-999"),
            self.reversal_doc.user_remark,
        )
        # This entry's own tag must still be present, so its own idempotency
        # guard keeps working.
        self.assertIn(
            delivery_handling._unsettle_dedup_tag("JE-ORIG-001"),
            self.reversal_doc.user_remark,
        )


class TestUnsettleCourierSettlementLockOrdering(unittest.TestCase):
    """CRITICAL 2a: the row lock must be taken before every other guard, so a
    second concurrent call blocks instead of racing the first to a decision."""

    def setUp(self):
        self.patches = []

        def start(target, **kwargs):
            p = patch(target, **kwargs)
            self.patches.append(p)
            return p.start()

        self.call_order = []

        def _record(name, return_value):
            def _fn(*args, **kwargs):
                self.call_order.append(name)
                return return_value
            return _fn

        self.original_je = _FakeJournalEntry(accounts=[_je_row("Cash - TC", debit=80.0)])

        start("frappe.db.exists", side_effect=_record("exists", True))
        self.lock = start("frappe.db.get_value", side_effect=_record("lock", True))
        start("frappe.get_doc", side_effect=_record("get_doc", self.original_je))
        start(
            "jarz_pos.services.delivery_handling._courier_transactions_for_settlement_je",
            side_effect=_record("cts", [{"name": "CT-1", "reference_invoice": "ACC-SINV-0001"}]),
        )
        start(
            "jarz_pos.services.delivery_handling.resolve_settlement_branch",
            side_effect=_record("branch", "Nasr City"),
        )
        start(
            "jarz_pos.services.delivery_handling.find_settlement_reversal_je",
            side_effect=_record("find_reversal", None),
        )
        self.reversal_doc = _JournalEntryCapture()
        start("frappe.new_doc", side_effect=_record("new_doc", self.reversal_doc))
        start(
            "jarz_pos.services.delivery_handling.mark_courier_transactions_unsettled",
            side_effect=_record("mark_unsettled", ["CT-1"]),
        )
        start("frappe.db.savepoint", return_value=None)
        start("frappe.db.rollback", return_value=None)
        start("frappe.db.commit", return_value=None)
        start("jarz_pos.utils.realtime.publish_to_branches", return_value=[])

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_lock_is_taken_before_every_other_guard(self):
        delivery_handling.unsettle_courier_settlement("JE-ORIG-001")

        self.assertEqual(self.call_order[0], "exists")
        self.assertEqual(self.call_order[1], "lock")
        for later in ("get_doc", "cts", "branch", "find_reversal"):
            self.assertLess(
                self.call_order.index("lock"), self.call_order.index(later),
                f"lock must be taken before {later!r} runs",
            )

    def test_lock_call_uses_for_update(self):
        delivery_handling.unsettle_courier_settlement("JE-ORIG-001")

        self.assertTrue(self.lock.called)
        _, kwargs = self.lock.call_args
        self.assertTrue(kwargs.get("for_update"))


# ---------------------------------------------------------------------------
# API layer — jarz_pos.api.couriers
# ---------------------------------------------------------------------------


class TestEnsureUnsettleAccess(unittest.TestCase):
    def test_ordinary_pos_user_is_refused(self):
        from jarz_pos.api.couriers import _ensure_unsettle_access

        with patch.object(frappe, "get_roles", return_value=["POS User"]):
            with self.assertRaises(frappe.PermissionError):
                _ensure_unsettle_access()

    def test_jarz_manager_is_permitted(self):
        from jarz_pos.api.couriers import _ensure_unsettle_access

        with patch.object(frappe, "get_roles", return_value=["JARZ Manager"]):
            _ensure_unsettle_access()  # must not raise

    def test_line_manager_is_permitted(self):
        from jarz_pos.api.couriers import _ensure_unsettle_access

        with patch.object(frappe, "get_roles", return_value=["jarz line manager"]):
            _ensure_unsettle_access()  # must not raise


class _FakeCache:
    """Minimal stand-in for ``frappe.cache()`` covering hset/expire/hget/delete_value."""

    def __init__(self):
        self.store = {}

    def hset(self, key, field, value):
        self.store.setdefault(key, {})[field] = value

    def expire(self, key, seconds):
        pass

    def hget(self, key, field):
        return self.store.get(key, {}).get(field)

    def delete_value(self, key):
        self.store.pop(key, None)


class TestGetUnsettlePreviewAPI(unittest.TestCase):
    def setUp(self):
        self.patches = []

        def start(target, **kwargs):
            p = patch(target, **kwargs)
            self.patches.append(p)
            return p.start()

        self.roles = start("frappe.get_roles", return_value=["JARZ Manager"])
        self.fake_cache = _FakeCache()
        start("frappe.cache", return_value=self.fake_cache)
        self.preview_data = start(
            "jarz_pos.api.couriers._get_unsettle_preview",
            return_value={
                "journal_entry": "JE-ORIG-001",
                "pos_profile": "Nasr City",
                "already_reversed": False,
                "reversal_journal_entry": None,
                "courier_transactions": [],
                "reversal_lines": [],
            },
        )
        self.scope_guard = start("jarz_pos.api.couriers.ensure_profile_scoped_invoice_access")
        start("jarz_pos.api.couriers.ensure_open_shift")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_permission_refusal_happens_before_any_lookup(self):
        from jarz_pos.api.couriers import get_unsettle_preview

        self.roles.return_value = ["POS User"]
        with self.assertRaises(frappe.PermissionError):
            get_unsettle_preview("JE-ORIG-001")

        self.preview_data.assert_not_called()

    def test_wrong_branch_is_refused(self):
        from jarz_pos.api.couriers import get_unsettle_preview

        self.scope_guard.side_effect = BranchAccessError("Not permitted: wrong branch")
        with self.assertRaises(BranchAccessError):
            get_unsettle_preview("JE-ORIG-001")

    def test_already_reversed_is_refused(self):
        from jarz_pos.api.couriers import get_unsettle_preview

        self.preview_data.return_value = {
            **self.preview_data.return_value,
            "already_reversed": True,
            "reversal_journal_entry": "JE-REVERSAL-EXISTING",
        }
        with self.assertRaises(frappe.ValidationError):
            get_unsettle_preview("JE-ORIG-001")

    def test_happy_path_mints_a_token_scoped_to_this_journal_entry(self):
        from jarz_pos.api.couriers import get_unsettle_preview

        result = get_unsettle_preview("JE-ORIG-001")

        self.assertIn("preview_token", result)
        self.assertEqual(result["expires_in"], 180)
        self.scope_guard.assert_called_once()


class TestUnsettleCourierSettlementAPI(unittest.TestCase):
    def setUp(self):
        self.patches = []

        def start(target, **kwargs):
            p = patch(target, **kwargs)
            self.patches.append(p)
            return p.start()

        self.roles = start("frappe.get_roles", return_value=["JARZ Manager"])
        self.fake_cache = _FakeCache()
        self.fake_cache.hset("jarz_pos:unsettle_preview:TOKEN-1", "data", {
            "journal_entry": "JE-ORIG-001", "pos_profile": "Nasr City",
        })
        start("frappe.cache", return_value=self.fake_cache)
        self.scope_guard = start("jarz_pos.api.couriers.ensure_profile_scoped_invoice_access")
        self.shift_guard = start("jarz_pos.api.couriers.ensure_open_shift")
        self.commit_service = start(
            "jarz_pos.api.couriers._unsettle_courier_settlement",
            return_value={
                "success": True,
                "journal_entry": "JE-ORIG-001",
                "reversal_journal_entry": "JE-REVERSAL-001",
                "pos_profile": "Nasr City",
                "courier_transactions": ["CT-1", "CT-2"],
            },
        )
        start("frappe.log_error", return_value=None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_permission_refusal_happens_before_touching_the_cache(self):
        from jarz_pos.api.couriers import unsettle_courier_settlement

        self.roles.return_value = ["POS User"]
        with self.assertRaises(frappe.PermissionError):
            unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1")

        self.commit_service.assert_not_called()

    def test_wrong_branch_is_refused_and_nothing_is_posted(self):
        from jarz_pos.api.couriers import unsettle_courier_settlement

        self.scope_guard.side_effect = BranchAccessError("Not permitted: wrong branch")
        with self.assertRaises(BranchAccessError):
            unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1")

        self.commit_service.assert_not_called()

    def test_stale_or_unknown_token_is_refused(self):
        from jarz_pos.api.couriers import unsettle_courier_settlement

        with self.assertRaises(frappe.ValidationError):
            unsettle_courier_settlement("JE-ORIG-001", "TOKEN-DOES-NOT-EXIST")

        self.commit_service.assert_not_called()

    def test_token_for_a_different_journal_entry_is_refused(self):
        from jarz_pos.api.couriers import unsettle_courier_settlement

        with self.assertRaises(frappe.ValidationError):
            unsettle_courier_settlement("JE-SOME-OTHER-ENTRY", "TOKEN-1")

        self.commit_service.assert_not_called()

    def test_happy_path_delegates_to_the_service_and_clears_the_token(self):
        from jarz_pos.api.couriers import unsettle_courier_settlement

        result = unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1", reason="wrong branch till")

        self.commit_service.assert_called_once_with(
            "JE-ORIG-001", pos_profile="Nasr City", reason="wrong branch till"
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["reversal_journal_entry"], "JE-REVERSAL-001")
        # Token is single-use.
        self.assertIsNone(self.fake_cache.hget("jarz_pos:unsettle_preview:TOKEN-1", "data"))

    def test_double_reversal_reported_by_the_service_propagates(self):
        from jarz_pos.api.couriers import unsettle_courier_settlement

        self.commit_service.side_effect = frappe.ValidationError("already reversed by JE-REVERSAL-EXISTING")
        with self.assertRaises(frappe.ValidationError):
            unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1")

    # -- CRITICAL 2b: the preview token is genuinely single-use -----------------

    def test_replaying_the_same_token_after_success_is_refused(self):
        from jarz_pos.api.couriers import unsettle_courier_settlement

        result = unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1")
        self.assertTrue(result["success"])

        with self.assertRaises(frappe.ValidationError):
            unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1")

        # The replay never reached the service a second time.
        self.commit_service.assert_called_once()

    def test_failed_reversal_still_consumes_the_token_and_tells_the_user_to_reopen(self):
        """The token must be consumed BEFORE the service call (so a concurrent
        replay can never slip in), which means a failed reversal leaves the
        token gone too. The error must say so, so the user has a clean path
        (re-preview) rather than retrying with a token that can never work."""
        from jarz_pos.api.couriers import unsettle_courier_settlement

        self.commit_service.side_effect = Exception("posting failed")

        with self.assertRaises(Exception) as ctx:
            unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1")

        self.assertIn("reopen", str(ctx.exception).lower())
        self.assertIsNone(self.fake_cache.hget("jarz_pos:unsettle_preview:TOKEN-1", "data"))

    def test_guard_failure_does_not_consume_the_token(self):
        """A branch-scope refusal happens BEFORE the token is consumed, so the
        caller can still use it once the underlying problem is fixed (e.g. by
        opening their shift) without having to re-preview."""
        from jarz_pos.api.couriers import unsettle_courier_settlement

        self.scope_guard.side_effect = BranchAccessError("Not permitted: wrong branch")
        with self.assertRaises(BranchAccessError):
            unsettle_courier_settlement("JE-ORIG-001", "TOKEN-1")

        self.assertIsNotNone(self.fake_cache.hget("jarz_pos:unsettle_preview:TOKEN-1", "data"))


if __name__ == "__main__":
    unittest.main()
