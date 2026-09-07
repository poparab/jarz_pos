"""Tests for listing recent courier settlements — how a manager FINDS one to reverse.

``unsettle_courier_settlement`` is keyed on a Journal Entry NAME, which nothing on a
phone ever surfaces: ``get_courier_balances`` only ever describes UNSETTLED positions,
and every settle call site discards the ``journal_entry`` its own response carries.
These tests pin down the replacement for the mobile client's on-device settlement
cache, which cannot help the exact case this exists for — on 2026-09-02 five Nasr City
Courier Transactions were settled from the wrong branch till, and whoever needs to
reverse that may not be on the device that did it:

* a settlement is identified the same way the un-settle preview identifies one — by
  the ``journal_entry`` a Courier Transaction carries — so the two never disagree;
* branch scope mirrors ``get_courier_balances``: only the caller's own branch(es);
* ``include_reversed`` defaults to hiding what can no longer be acted on;
* the limit is clamped, not trusted from the caller;
* browsing the list requires the same manager-tier access reversing one does.
"""

import unittest
from unittest.mock import patch

import frappe


def _ct_row(name, journal_entry, invoice, amount=100.0, shipping=20.0,
            party_type="Employee", party="EMP-001"):
    return {
        "name": name,
        "journal_entry": journal_entry,
        "reference_invoice": invoice,
        "amount": amount,
        "shipping_amount": shipping,
        "party_type": party_type,
        "party": party,
    }


def _je_row(name, company="Test Company", posting_date="2026-09-01", user_remark=None,
            creation="2026-08-01 00:00:00", tag=None):
    """A Journal Entry row as the batch fetch returns it.

    ``user_remark`` defaults to a real batch-settlement remark, because a JE is
    only listed if :func:`_is_settlement_je_remark` RECOGNISES it as a
    settlement (an allow-list — see ``SETTLEMENT_JE_TAG_TYPES``). A fixture with
    no remark is not a settlement and must not appear, so the default here has
    to be a genuine one rather than an empty string.

    ``creation`` and ``tag`` are what make the row honest since 2026-09-07: the
    listing classifies on ``_trusted_je_tag_source``, which reads the app-written
    ``custom_jarz_je_tag`` and falls back to the remark ONLY for entries that
    predate that field. The default is a legacy batch settlement — no stamp, a
    pre-cutoff ``creation`` — which is precisely the 2026-09-02 wrong-branch case
    this feature exists for, so the default fixture keeps proving it stays
    listable.
    """
    if user_remark is None:
        user_remark = "Order: 100.0, Shipping: 20.0, Net to branch: 80.0"
    return {
        "name": name,
        "company": company,
        "posting_date": posting_date,
        "user_remark": user_remark,
        "creation": creation,
        "custom_jarz_je_tag": tag,
    }


# ---------------------------------------------------------------------------
# Service layer — jarz_pos.services.delivery_handling.list_recent_courier_settlements
# ---------------------------------------------------------------------------


class TestListRecentCourierSettlementsService(unittest.TestCase):
    def setUp(self):
        self.patches = []

        def start(target, **kwargs):
            p = patch(target, **kwargs)
            self.patches.append(p)
            return p.start()

        self.visible_profiles = start(
            "jarz_pos.services.delivery_handling.get_visible_pos_profiles",
            return_value=["Nasr City"],
        )
        self.map_branches = start(
            "jarz_pos.services.delivery_handling.map_invoice_branches",
            return_value={},
        )
        self.global_access = start(
            "jarz_pos.services.delivery_handling.user_has_global_profile_access",
            return_value=False,
        )
        # Batched in one call instead of one `user_remark LIKE` per settlement
        # group — see _find_settlement_reversal_jes_bulk (item 5).
        self.find_reversal_bulk = start(
            "jarz_pos.services.delivery_handling._find_settlement_reversal_jes_bulk",
            return_value={},
        )
        # State the schema outright: `_je_tag_field_exists` would otherwise load
        # a real Meta through the very `frappe.get_all` these tests dispatch on.
        start(
            "jarz_pos.services.delivery_handling._je_tag_field_exists", return_value=True
        )
        start(
            "jarz_pos.services.delivery_handling._je_tag_legacy_cutoff",
            return_value="2026-09-07 00:00:00",
        )
        self.get_all = start("frappe.get_all")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _run(self, ct_rows, je_rows, **kwargs):
        from jarz_pos.services.delivery_handling import list_recent_courier_settlements

        # Dispatch on the DocType rather than on call order. A positional
        # side_effect list silently mis-feeds the function the moment the
        # implementation gains or loses a query — which is exactly how these
        # tests broke: the Journal Entry rows arrived where the Courier
        # Transaction rows were expected and every assertion died on a
        # KeyError instead of telling us anything about the behaviour.
        def _dispatch(doctype, *args, **kwargs):
            if doctype == "Courier Transaction":
                return ct_rows
            if doctype == "Journal Entry":
                return je_rows
            return []

        self.get_all.side_effect = _dispatch
        return list_recent_courier_settlements(**kwargs)

    # -- no visible profiles --------------------------------------------------

    def test_no_visible_profiles_returns_empty_without_a_query(self):
        from jarz_pos.services.delivery_handling import list_recent_courier_settlements

        self.visible_profiles.return_value = []
        result = list_recent_courier_settlements()

        self.assertEqual(result, [])
        self.get_all.assert_not_called()

    # -- branch scoping ---------------------------------------------------------

    def test_branch_scoping_excludes_another_branchs_settlement(self):
        ct_rows = [
            _ct_row("CT-1", "JE-NASR", "ACC-SINV-0001"),
            _ct_row("CT-2", "JE-DOKKI", "ACC-SINV-0002"),
        ]
        je_rows = [_je_row("JE-NASR"), _je_row("JE-DOKKI")]
        self.map_branches.return_value = {
            "ACC-SINV-0001": "Nasr City",
            "ACC-SINV-0002": "Dokki",
        }

        result = self._run(ct_rows, je_rows)

        names = {row["journal_entry"] for row in result}
        self.assertEqual(names, {"JE-NASR"})

    def test_unattributable_settlement_hidden_from_a_scoped_caller(self):
        """No invoice branch resolves at all -> hidden unless the caller has global access."""
        ct_rows = [_ct_row("CT-1", "JE-UNKNOWN", "ACC-SINV-0009")]
        je_rows = [_je_row("JE-UNKNOWN")]
        self.map_branches.return_value = {}  # unattributable
        self.global_access.return_value = False

        result = self._run(ct_rows, je_rows)

        self.assertEqual(result, [])

    def test_unattributable_settlement_shown_to_a_global_caller(self):
        ct_rows = [_ct_row("CT-1", "JE-UNKNOWN", "ACC-SINV-0009")]
        je_rows = [_je_row("JE-UNKNOWN")]
        self.map_branches.return_value = {}
        self.global_access.return_value = True

        result = self._run(ct_rows, je_rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pos_profile"], "")

    def test_settlement_spanning_two_branches_is_treated_as_unattributable(self):
        ct_rows = [
            _ct_row("CT-1", "JE-SPLIT", "ACC-SINV-0001"),
            _ct_row("CT-2", "JE-SPLIT", "ACC-SINV-0002"),
        ]
        je_rows = [_je_row("JE-SPLIT")]
        self.map_branches.return_value = {
            "ACC-SINV-0001": "Nasr City",
            "ACC-SINV-0002": "Dokki",
        }
        self.global_access.return_value = False

        result = self._run(ct_rows, je_rows)

        # Neither branch matches "visible_profiles=['Nasr City']" cleanly because the
        # settlement resolves to no single branch, and the caller is not global.
        self.assertEqual(result, [])

    # -- include_reversed / already_reversed -------------------------------------

    def test_reversed_settlement_hidden_by_default(self):
        ct_rows = [_ct_row("CT-1", "JE-REVERSED", "ACC-SINV-0001")]
        je_rows = [_je_row("JE-REVERSED")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}
        self.find_reversal_bulk.return_value = {"JE-REVERSED": "JE-REVERSAL-001"}

        result = self._run(ct_rows, je_rows, include_reversed=False)

        self.assertEqual(result, [])

    def test_reversed_settlement_included_and_flagged_when_requested(self):
        ct_rows = [_ct_row("CT-1", "JE-REVERSED", "ACC-SINV-0001")]
        je_rows = [_je_row("JE-REVERSED")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}
        self.find_reversal_bulk.return_value = {"JE-REVERSED": "JE-REVERSAL-001"}

        result = self._run(ct_rows, je_rows, include_reversed=True)

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["already_reversed"])
        self.assertEqual(result[0]["reversal_journal_entry"], "JE-REVERSAL-001")

    def test_unreversed_settlement_reports_already_reversed_false(self):
        ct_rows = [_ct_row("CT-1", "JE-LIVE", "ACC-SINV-0001")]
        je_rows = [_je_row("JE-LIVE")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}
        self.find_reversal_bulk.return_value = {}

        result = self._run(ct_rows, je_rows)

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["already_reversed"])
        self.assertIsNone(result[0]["reversal_journal_entry"])

    # -- limit clamp --------------------------------------------------------------

    def test_limit_is_clamped_to_the_upper_bound(self):
        ct_rows = [
            _ct_row(f"CT-{i}", f"JE-{i}", f"ACC-SINV-{i:04d}")
            for i in range(1, 6)
        ]
        je_rows = [_je_row(f"JE-{i}") for i in range(1, 6)]
        self.map_branches.return_value = {
            f"ACC-SINV-{i:04d}": "Nasr City" for i in range(1, 6)
        }

        result = self._run(ct_rows, je_rows, limit=2)

        self.assertEqual(len(result), 2)

    def test_negative_limit_is_clamped_up_to_one(self):
        ct_rows = [
            _ct_row("CT-1", "JE-1", "ACC-SINV-0001"),
            _ct_row("CT-2", "JE-2", "ACC-SINV-0002"),
        ]
        je_rows = [_je_row("JE-1"), _je_row("JE-2")]
        self.map_branches.return_value = {
            "ACC-SINV-0001": "Nasr City", "ACC-SINV-0002": "Nasr City",
        }

        result = self._run(ct_rows, je_rows, limit=-5)

        # A negative limit must still return at least one row (max(1, ...)) rather
        # than zero, even though two candidates were eligible.
        self.assertEqual(len(result), 1)

    def test_zero_limit_falls_back_to_the_default(self):
        ct_rows = [_ct_row("CT-1", "JE-1", "ACC-SINV-0001")]
        je_rows = [_je_row("JE-1")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}

        result = self._run(ct_rows, je_rows, limit=0)

        self.assertEqual(len(result), 1)  # only one candidate exists; default cap is 50

    # -- what counts as a settlement ------------------------------------------------

    def test_a_journal_entry_with_no_settling_courier_transaction_never_appears(self):
        """The Journal Entry query is scoped to names actually referenced by a CT —
        an unrelated JE (however legitimate) cannot sneak into the result."""
        ct_rows = [_ct_row("CT-1", "JE-REAL-SETTLEMENT", "ACC-SINV-0001")]
        je_rows = [_je_row("JE-REAL-SETTLEMENT")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}

        result = self._run(ct_rows, je_rows)

        # Frappe's filter form is ``["in", [...]]``, not a bare list — assert the
        # names the query is scoped to, not the literal filter spelling, so this
        # keeps testing the guarantee rather than the query's syntax.
        # Find the Journal Entry query by its DocType, never by call index:
        # Frappe makes its own internal `get_all("DocType Link", ...)` calls
        # whose presence depends on cache state, so any positional index here
        # is a test that fails for reasons unrelated to the code under test.
        je_filter_call = next(
            c for c in self.get_all.call_args_list
            if c.args and c.args[0] == "Journal Entry"
        )
        operator, scoped_names = je_filter_call.kwargs["filters"]["name"]
        self.assertEqual(operator, "in")
        self.assertEqual(list(scoped_names), ["JE-REAL-SETTLEMENT"])
        names = {row["journal_entry"] for row in result}
        self.assertNotIn("JE-UNRELATED-EXPENSE-ENTRY", names)

    def test_a_courier_transaction_with_no_journal_entry_is_excluded_from_the_query(self):
        from jarz_pos.services.delivery_handling import list_recent_courier_settlements

        # Return-value, not a positional list: Frappe makes its own internal
        # get_all calls (DocType Link / DocType Action on System Settings) that
        # would exhaust a fixed list and raise StopIteration.
        self.get_all.side_effect = None
        self.get_all.return_value = []
        list_recent_courier_settlements()

        ct_filter_call = next(
            c for c in self.get_all.call_args_list
            if c.args and c.args[0] == "Courier Transaction"
        )
        # `["is", "set"]`, NOT `["not in", ["", None]]`.
        #
        # This assertion used to pin the BROKEN shape as if it were correct, and
        # that is the whole reason the defect survived a green suite: a mock
        # records the filter dict without ever handing it to SQL, so the one
        # thing that mattered — what the database does with it — was never
        # exercised. Frappe renders the old shape as
        # `IFNULL(journal_entry,'') NOT IN ('', NULL)`, which is UNKNOWN (never
        # TRUE) for every row, so the query returned nothing on any real
        # database. See test_settlement_reversal_real_db for the check that
        # actually runs this against MariaDB.
        self.assertEqual(ct_filter_call.kwargs["filters"]["journal_entry"], ["is", "set"])

    def test_journal_entry_missing_or_unsubmitted_is_dropped(self):
        """A CT could point at a JE that no longer resolves as posted; skip it, don't crash."""
        ct_rows = [_ct_row("CT-1", "JE-GHOST", "ACC-SINV-0001")]
        je_rows = []  # docstatus filter (or deletion) excluded it from the batch fetch
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}

        result = self._run(ct_rows, je_rows)

        self.assertEqual(result, [])

    def test_empty_courier_transaction_table_short_circuits(self):
        result = self._run([], [])
        self.assertEqual(result, [])

    def test_partner_fee_accrual_je_is_excluded_from_the_list(self):
        """CRITICAL 1: a Courier Transaction born Settled with journal_entry pointing
        at a partner fee-accrual JE (settlement_strategies.create_partner_fee_accrual_je)
        must never surface as something a manager could reverse. This must fail
        loudly if the loose filter is ever reintroduced."""
        from jarz_pos.services import delivery_handling

        ct_rows = [_ct_row("CT-1", "JE-FEE-001", "ACC-SINV-0001")]
        tag = delivery_handling._je_dedup_tag(
            "ACC-SINV-0001", delivery_handling.PARTNER_FEE_ACCRUAL_JE_TAG_TYPE
        )
        je_rows = [_je_row(
            "JE-FEE-001",
            user_remark=f"Delivery fee owed to Talabat – ACC-SINV-0001 {tag}",
            tag=tag,
        )]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}

        result = self._run(ct_rows, je_rows)

        self.assertEqual(result, [])

    def test_a_foreign_entry_with_a_forged_settlement_remark_is_not_listed(self):
        """The read-side fix, at the listing.

        An entry this app never posted whose remark contains a well-formed batch
        settlement tag. Before 2026-09-07 the listing classified on
        ``user_remark`` alone, so this appeared in the reversal dialog as
        something a manager could reverse — and reversing it posts the inverse of
        an entry that was never a settlement. It carries no app stamp and was
        created after the cutoff, so there is nothing about it to trust.
        """
        from jarz_pos.services import delivery_handling

        forged = delivery_handling._je_dedup_tag(
            "COURIER-X", delivery_handling.BATCH_SETTLEMENT_JE_TAG_TYPE
        )
        ct_rows = [_ct_row("CT-1", "JE-FOREIGN-001", "ACC-SINV-0001")]
        je_rows = [_je_row(
            "JE-FOREIGN-001",
            user_remark=f"Cash transfer, note: {forged}",
            creation="2099-01-01 00:00:00",
            tag=None,
        )]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}

        self.assertEqual(self._run(ct_rows, je_rows), [])

    def test_a_stamped_settlement_is_listed_even_with_an_unhelpful_remark(self):
        """The other half: provenance is what counts, not the prose.

        The stamp says batch settlement; the remark says nothing recognisable.
        This is the shape an entry takes once the tag lives in its own field, and
        it must be listable — otherwise hardening the read side would have made
        every settlement posted after it unreversable.
        """
        from jarz_pos.services import delivery_handling

        tag = delivery_handling._je_dedup_tag(
            "EMP-001", delivery_handling.BATCH_SETTLEMENT_JE_TAG_TYPE
        )
        ct_rows = [_ct_row("CT-1", "JE-STAMPED-001", "ACC-SINV-0001")]
        je_rows = [_je_row(
            "JE-STAMPED-001",
            user_remark="nothing recognisable here",
            creation="2099-01-01 00:00:00",
            tag=tag,
        )]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}

        result = self._run(ct_rows, je_rows)

        self.assertEqual([r["journal_entry"] for r in result], ["JE-STAMPED-001"])

    def test_ct_query_applies_a_posting_date_window_and_a_sql_limit(self):
        """Item 5: the Courier Transaction scan must be bounded in SQL — a date
        window and a page-size cap — not pull every row that ever carried a
        journal_entry and rely on the Python-side `limit` at the very end."""
        self._run([], [])

        ct_filter_call = self.get_all.call_args_list[0]
        self.assertIn("date", ct_filter_call.kwargs["filters"])
        operator, cutoff = ct_filter_call.kwargs["filters"]["date"]
        self.assertEqual(operator, ">=")
        self.assertTrue(cutoff)
        self.assertIn("limit_page_length", ct_filter_call.kwargs)
        self.assertGreater(ct_filter_call.kwargs["limit_page_length"], 0)

    def test_reversal_lookup_is_batched_into_a_single_call(self):
        """Item 5: one call to the batched helper covers every settlement group in
        the page, instead of one `user_remark LIKE` query per group."""
        ct_rows = [
            _ct_row("CT-1", "JE-1", "ACC-SINV-0001"),
            _ct_row("CT-2", "JE-2", "ACC-SINV-0002"),
        ]
        je_rows = [_je_row("JE-1"), _je_row("JE-2")]
        self.map_branches.return_value = {
            "ACC-SINV-0001": "Nasr City", "ACC-SINV-0002": "Nasr City",
        }

        self._run(ct_rows, je_rows)

        self.find_reversal_bulk.assert_called_once()

    # -- row shape ------------------------------------------------------------------

    def test_row_shape_and_net_amount_computation(self):
        ct_rows = [
            _ct_row("CT-1", "JE-1", "ACC-SINV-0001", amount=500.0, shipping=50.0),
            _ct_row("CT-2", "JE-1", "ACC-SINV-0001", amount=100.0, shipping=10.0),
        ]
        je_rows = [_je_row("JE-1", posting_date="2026-09-05")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}

        with patch(
            "jarz_pos.services.delivery_handling._resolve_party_display_name",
            return_value="John Doe",
        ):
            result = self._run(ct_rows, je_rows)

        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["journal_entry"], "JE-1")
        self.assertEqual(row["posting_date"], "2026-09-05")
        self.assertEqual(row["party_type"], "Employee")
        self.assertEqual(row["party"], "EMP-001")
        self.assertEqual(row["display_name"], "John Doe")
        self.assertEqual(row["pos_profile"], "Nasr City")
        self.assertAlmostEqual(row["net_amount"], (500.0 - 50.0) + (100.0 - 10.0))
        self.assertEqual(row["transaction_count"], 2)
        self.assertFalse(row["already_reversed"])
        self.assertIsNone(row["reversal_journal_entry"])


# ---------------------------------------------------------------------------
# _find_settlement_reversal_jes_bulk — the batched replacement for one
# find_settlement_reversal_je call per settlement group
# ---------------------------------------------------------------------------


class TestFindSettlementReversalJesBulk(unittest.TestCase):
    """The batched reversal lookup.

    Both halves of it are stated explicitly here — the provenance scan and the
    bounded legacy-remark scan — because `_je_tag_field_exists` otherwise loads
    a real Meta, and loading a Meta issues `frappe.get_all` calls of its own
    straight into the counter these tests assert on.
    """

    def setUp(self):
        self.patches = [
            patch(
                "jarz_pos.services.delivery_handling._je_tag_field_exists",
                return_value=True,
            ),
            patch(
                "jarz_pos.services.delivery_handling._je_tag_legacy_cutoff",
                return_value="2026-09-07 00:00:00",
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_empty_input_short_circuits_without_a_query(self):
        from jarz_pos.services.delivery_handling import _find_settlement_reversal_jes_bulk

        with patch.object(frappe, "get_all") as mock_get_all:
            result = _find_settlement_reversal_jes_bulk({})

        self.assertEqual(result, {})
        mock_get_all.assert_not_called()

    def test_matches_are_keyed_by_original_je_name(self):
        from jarz_pos.services.delivery_handling import (
            _find_settlement_reversal_jes_bulk,
            _unsettle_dedup_tag,
        )

        je_by_name = {
            "JE-1": {"name": "JE-1", "company": "Test Company"},
            "JE-2": {"name": "JE-2", "company": "Test Company"},
        }
        candidates = [
            {"name": "JE-REV-1", "user_remark": f"Reversal note {_unsettle_dedup_tag('JE-1')}"},
        ]
        with patch.object(frappe, "get_all", return_value=candidates) as mock_get_all:
            result = _find_settlement_reversal_jes_bulk(je_by_name)

        # One company, so the query count is a small CONSTANT — the provenance
        # scan plus the bounded legacy-remark scan — never one per settlement,
        # which is the timeout this batching exists to avoid.
        self.assertEqual(mock_get_all.call_count, 2)
        self.assertEqual(result, {"JE-1": "JE-REV-1"})
        self.assertNotIn("JE-2", result)

    def test_query_count_does_not_grow_with_the_number_of_settlements(self):
        """The actual invariant: queries scale with COMPANIES, not settlements."""
        from jarz_pos.services.delivery_handling import _find_settlement_reversal_jes_bulk

        def _count(n):
            je_by_name = {
                f"JE-{i}": {"name": f"JE-{i}", "company": "Test Company"}
                for i in range(n)
            }
            with patch.object(frappe, "get_all", return_value=[]) as mock_get_all:
                _find_settlement_reversal_jes_bulk(je_by_name)
            return mock_get_all.call_count

        self.assertEqual(_count(2), _count(50))

    def test_one_query_per_distinct_company(self):
        from jarz_pos.services.delivery_handling import _find_settlement_reversal_jes_bulk

        je_by_name = {
            "JE-1": {"name": "JE-1", "company": "Company A"},
            "JE-2": {"name": "JE-2", "company": "Company B"},
        }
        with patch.object(frappe, "get_all", return_value=[]) as mock_get_all:
            _find_settlement_reversal_jes_bulk(je_by_name)

        with patch.object(frappe, "get_all", return_value=[]) as one_company:
            _find_settlement_reversal_jes_bulk(
                {"JE-1": {"name": "JE-1", "company": "Company A"}}
            )

        self.assertEqual(one_company.call_count, 2)
        self.assertEqual(mock_get_all.call_count, 4)


# ---------------------------------------------------------------------------
# API layer — jarz_pos.api.couriers.list_recent_settlements
# ---------------------------------------------------------------------------


class TestListRecentSettlementsAPI(unittest.TestCase):
    def setUp(self):
        self.patches = []

        def start(target, **kwargs):
            p = patch(target, **kwargs)
            self.patches.append(p)
            return p.start()

        self.roles = start("frappe.get_roles", return_value=["JARZ Manager"])
        self.service = start(
            "jarz_pos.api.couriers._list_recent_courier_settlements",
            return_value=[],
        )
        # The feature ships dark (couriers.UNSETTLE_RELEASED = False). These
        # cases describe how it behaves once released, so they lift the hold;
        # the hold itself is pinned by TestUnsettleReleaseHold below.
        start("jarz_pos.api.couriers.UNSETTLE_RELEASED", new=True)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_ordinary_pos_user_is_refused_before_any_query_runs(self):
        from jarz_pos.api.couriers import list_recent_settlements

        self.roles.return_value = ["POS User"]
        with self.assertRaises(frappe.PermissionError):
            list_recent_settlements()

        self.service.assert_not_called()

    def test_manager_is_permitted_and_call_is_forwarded(self):
        from jarz_pos.api.couriers import list_recent_settlements

        list_recent_settlements(pos_profile="Nasr City", limit=10, include_reversed=True)

        self.service.assert_called_once_with(
            pos_profile="Nasr City", limit=10, include_reversed=True, days=90
        )

    def test_line_manager_is_permitted(self):
        from jarz_pos.api.couriers import list_recent_settlements

        self.roles.return_value = ["jarz line manager"]
        list_recent_settlements()  # must not raise
        self.service.assert_called_once()

    def test_string_include_reversed_is_coerced_to_a_real_bool(self):
        from jarz_pos.api.couriers import list_recent_settlements

        list_recent_settlements(include_reversed="true")
        self.assertIs(self.service.call_args.kwargs["include_reversed"], True)

        self.service.reset_mock()
        list_recent_settlements(include_reversed="0")
        self.assertIs(self.service.call_args.kwargs["include_reversed"], False)

    def test_default_include_reversed_is_false(self):
        from jarz_pos.api.couriers import list_recent_settlements

        list_recent_settlements()
        self.assertIs(self.service.call_args.kwargs["include_reversed"], False)


class TestUnsettleReleaseHold(unittest.TestCase):
    """The kill switch itself, which outlived the release it was built for.

    Settlement reversal shipped dark from 944bf02 and was released on
    2026-09-07 once its three defects were fixed and verified against a real
    database and real concurrency. The flag stays, and stays tested, because it
    is the only way to take these endpoints away again without a deploy: they
    each accept an arbitrary Journal Entry name, so hiding the mobile button
    would leave them reachable by anyone who can call the API.

    These tests deliberately pin the MECHANISM rather than the flag's current
    value. A test asserting `UNSETTLE_RELEASED is False` says nothing about
    whether the hold works, and has to be deleted the day it matters most.
    """

    def test_the_hold_refuses_every_reversal_endpoint_when_it_is_off(self):
        from jarz_pos.api import couriers

        # An administrator — the widest caller there is. The hold must refuse
        # even them, and must refuse BEFORE the role check, so that lifting it
        # is the only way through.
        with patch("jarz_pos.api.couriers.UNSETTLE_RELEASED", new=False), patch(
            "frappe.get_roles", return_value=["System Manager"]
        ):
            for call in (
                lambda: couriers.list_recent_settlements(),
                lambda: couriers.get_unsettle_preview("ACC-JV-2026-00001"),
                lambda: couriers.unsettle_courier_settlement("ACC-JV-2026-00001", "tok"),
            ):
                with self.assertRaises(frappe.ValidationError):
                    call()

    def test_the_hold_is_checked_before_the_role_gate(self):
        """Order matters: the hold must be the outermost guard, so that while it
        is on there is no caller — of any role — who gets past it."""
        from jarz_pos.api import couriers

        with patch("jarz_pos.api.couriers.UNSETTLE_RELEASED", new=False), patch(
            "frappe.get_roles"
        ) as roles:
            with self.assertRaises(frappe.ValidationError):
                couriers.get_unsettle_preview("ACC-JV-2026-00001")

        roles.assert_not_called()

    def test_every_reversal_endpoint_consults_the_hold(self):
        """A fourth endpoint added later must not quietly bypass the switch."""
        from jarz_pos.api import couriers

        for call in (
            lambda: couriers.list_recent_settlements(),
            lambda: couriers.get_unsettle_preview("ACC-JV-2026-00001"),
            lambda: couriers.unsettle_courier_settlement("ACC-JV-2026-00001", "tok"),
        ):
            with patch(
                "jarz_pos.api.couriers._ensure_unsettle_released",
                side_effect=RuntimeError("hold consulted"),
            ):
                with self.assertRaises(RuntimeError):
                    call()


if __name__ == "__main__":
    unittest.main()
