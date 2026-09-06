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


def _je_row(name, company="Test Company", posting_date="2026-09-01"):
    return {"name": name, "company": company, "posting_date": posting_date}


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
        self.find_reversal = start(
            "jarz_pos.services.delivery_handling.find_settlement_reversal_je",
            return_value=None,
        )
        self.get_all = start("frappe.get_all")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _run(self, ct_rows, je_rows, **kwargs):
        from jarz_pos.services.delivery_handling import list_recent_courier_settlements

        self.get_all.side_effect = [ct_rows, je_rows]
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
        self.find_reversal.return_value = "JE-REVERSAL-001"

        result = self._run(ct_rows, je_rows, include_reversed=False)

        self.assertEqual(result, [])

    def test_reversed_settlement_included_and_flagged_when_requested(self):
        ct_rows = [_ct_row("CT-1", "JE-REVERSED", "ACC-SINV-0001")]
        je_rows = [_je_row("JE-REVERSED")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}
        self.find_reversal.return_value = "JE-REVERSAL-001"

        result = self._run(ct_rows, je_rows, include_reversed=True)

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["already_reversed"])
        self.assertEqual(result[0]["reversal_journal_entry"], "JE-REVERSAL-001")

    def test_unreversed_settlement_reports_already_reversed_false(self):
        ct_rows = [_ct_row("CT-1", "JE-LIVE", "ACC-SINV-0001")]
        je_rows = [_je_row("JE-LIVE")]
        self.map_branches.return_value = {"ACC-SINV-0001": "Nasr City"}
        self.find_reversal.return_value = None

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
        je_filter_call = self.get_all.call_args_list[1]
        operator, scoped_names = je_filter_call.kwargs["filters"]["name"]
        self.assertEqual(operator, "in")
        self.assertEqual(list(scoped_names), ["JE-REAL-SETTLEMENT"])
        names = {row["journal_entry"] for row in result}
        self.assertNotIn("JE-UNRELATED-EXPENSE-ENTRY", names)

    def test_a_courier_transaction_with_no_journal_entry_is_excluded_from_the_query(self):
        from jarz_pos.services.delivery_handling import list_recent_courier_settlements

        self.get_all.side_effect = [[], []]
        list_recent_courier_settlements()

        ct_filter_call = self.get_all.call_args_list[0]
        self.assertEqual(ct_filter_call.kwargs["filters"]["journal_entry"], ["not in", ["", None]])

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
            pos_profile="Nasr City", limit=10, include_reversed=True
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


if __name__ == "__main__":
    unittest.main()
