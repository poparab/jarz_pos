"""A backdated batch is measured against the stock it will be posted against.

Production, 2026-09-13: a Plan tab batch dated 2026-09-01 18:00 passed the
material pre-check on today's Bin (461 jar labels, 5.272 Kg pistachio spread)
and then failed inside ``submit()``.  ERPNext validates a backdated Stock Entry
against the ledger balance **at its posting datetime** — 11 labels had been
counted by then, the other 450 were received on 2026-09-03 — and the refusal
reached the operator as raw ``<strong>``/``<a href>`` markup.

These tests pin both halves:

* a past posting is checked against the ledger at that moment, and the message
  says the date is the reason and when enough stock existed;
* a posting at or after now reads the Bin exactly as before, with no ledger query;
* a per-line ``error`` returned by the batch routes is plain text.
"""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

NOW = datetime(2026, 9, 13, 10, 20)
PAST = "2026-09-01 18:00:00"

BOM_ITEMS = {
    "LABEL": {
        "item_code": "LABEL",
        "item_name": "Chocolate Hazelnut Jar Label 330",
        "qty": 32.0,
        "uom": "Nos",
        "stock_uom": "Nos",
        "source_warehouse": "Raw Material - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 1,
    }
}


def _ledger_sql(balance_at_posting, sufficient_from=None):
    """``frappe.db.sql`` answering the two ledger reads the pre-check makes."""

    def sql(query, values=None, as_dict=False):
        if "qty_after_transaction >=" in query:
            return [sufficient_from] if sufficient_from else []
        if "qty_after_transaction" in query:
            return [(balance_at_posting,)]
        return []

    return MagicMock(side_effect=sql)


class TestResolveBackdatedPosting(unittest.TestCase):
    def _call(self, value):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._resolve_now_datetime", return_value=NOW):
            return manufacturing._resolve_backdated_posting(value)

    def test_a_past_moment_is_backdated(self):
        self.assertEqual(datetime(2026, 9, 1, 18, 0), self._call(PAST))

    def test_now_the_future_and_blank_are_not(self):
        self.assertIsNone(self._call(NOW))
        self.assertIsNone(self._call("2026-09-14 09:00:00"))
        self.assertIsNone(self._call(None))
        self.assertIsNone(self._call(""))


class TestPrecheckReadsTheLedgerAtThePostingMoment(unittest.TestCase):
    def _issues(self, line, sql, live=461.0):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_get_bom_items_as_dict",
            return_value=MagicMock(return_value=BOM_ITEMS),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_get_latest_stock_qty",
            return_value=MagicMock(return_value=live),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime", return_value=NOW
        ), patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows",
            return_value=[],
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = 0
            mock_frappe.db.sql = sql
            return manufacturing._get_material_precheck_issues(line, "Jarz Co")

    def test_stock_that_arrived_after_the_batch_date_does_not_count(self):
        sql = _ledger_sql(
            11.0,
            {
                "posting_datetime": datetime(2026, 9, 3, 2, 15),
                "voucher_type": "Purchase Invoice",
                "voucher_no": "ACC-PINV-1",
            },
        )
        line = {"item_code": "CHOC-L", "bom_name": "BOM-CHOC-L", "item_qty": 32, "scheduled_at": PAST}

        issues = self._issues(line, sql)

        self.assertEqual(1, len(issues))
        issue = issues[0]
        self.assertAlmostEqual(11.0, issue["available_qty"])
        self.assertAlmostEqual(21.0, issue["missing_qty"])
        self.assertEqual("2026-09-01 18:00", issue["as_of"])
        self.assertAlmostEqual(461.0, issue["available_now"])
        self.assertEqual(
            {
                "posting_datetime": "2026-09-03 02:15",
                "voucher_type": "Purchase Invoice",
                "voucher_no": "ACC-PINV-1",
            },
            issue["sufficient_from"],
        )

    def test_a_batch_posted_now_reads_the_bin_and_never_the_ledger(self):
        sql = _ledger_sql(0.0)
        line = {"item_code": "CHOC-L", "bom_name": "BOM-CHOC-L", "item_qty": 32}

        issues = self._issues(line, sql)

        self.assertEqual([], issues)
        sql.assert_not_called()

    def test_a_shortage_that_is_real_today_is_not_blamed_on_the_date(self):
        sql = _ledger_sql(5.0)
        line = {"item_code": "CHOC-L", "bom_name": "BOM-CHOC-L", "item_qty": 32, "scheduled_at": PAST}

        issues = self._issues(line, sql, live=20.0)

        self.assertAlmostEqual(5.0, issues[0]["available_qty"])
        self.assertNotIn("as_of", issues[0])

    def test_an_unreadable_ledger_keeps_the_live_figure(self):
        line = {"item_code": "CHOC-L", "bom_name": "BOM-CHOC-L", "item_qty": 32, "scheduled_at": PAST}

        issues = self._issues(line, MagicMock(side_effect=Exception("db gone")))

        self.assertEqual([], issues)


class TestBackdatedShortageMessage(unittest.TestCase):
    def test_the_message_names_the_date_and_when_stock_was_enough(self):
        from jarz_pos.api import manufacturing

        issue = {
            "type": "insufficient_stock",
            "item_code": "LABEL",
            "item_name": "Chocolate Hazelnut Jar Label 330",
            "uom": "Nos",
            "required_qty": 32.0,
            "available_qty": 11.0,
            "missing_qty": 21.0,
            "source_warehouse": "Raw Material - J",
            "as_of": "2026-09-01 18:00",
            "available_now": 461.0,
            "sufficient_from": {
                "posting_datetime": "2026-09-03 02:15",
                "voucher_type": "Purchase Invoice",
                "voucher_no": "ACC-PINV-1",
            },
        }
        with patch(
            "jarz_pos.api.manufacturing._get_material_precheck_issues", return_value=[issue]
        ), patch("jarz_pos.api.manufacturing._", new=lambda msg: msg), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            manufacturing._assert_material_availability(
                {"item_code": "CHOC-L", "bom_name": "BOM-CHOC-L"}, "Jarz Co"
            )

        message = mock_frappe.throw.call_args.args[0]
        self.assertIn("is short by 21.000 Nos (required 32.000, available 11.000)", message)
        self.assertIn("that is the stock on 2026-09-01 18:00, the date of this batch (461 Nos is there now)", message)
        self.assertIn("Enough stock from 2026-09-03 02:15 (Purchase Invoice ACC-PINV-1)", message)


class TestBasketRollupReadsTheLedgerForBackdatedLines(unittest.TestCase):
    def test_a_backdated_basket_is_short_on_stock_that_arrived_later(self):
        from jarz_pos.services import production_planning as planning

        components = [
            {
                "item_code": "LABEL",
                "item_name": "Label",
                "stock_uom": "Nos",
                "required_qty": 32.0,
                "source_warehouse": "Raw Material - J",
            }
        ]
        qty_at = MagicMock(return_value=11.0)
        stamp = MagicMock()
        with patch(
            "jarz_pos.services.production_planning._resolve_required_material_rows",
            return_value=MagicMock(return_value=components),
        ), patch(
            "jarz_pos.services.production_planning._resolve_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.services.production_planning._resolve_bin_stock_map",
            return_value={("LABEL", "Raw Material - J"): 461.0},
        ), patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows", return_value=[]
        ), patch(
            "jarz_pos.services.production_planning._resolve_backdated_stock_helpers",
            return_value=(lambda value: datetime(2026, 9, 1, 18) if value else None, qty_at),
        ), patch(
            "jarz_pos.services.production_planning._resolve_backdated_shortage_stamper",
            return_value=stamp,
        ):
            rollup = planning.build_basket_rollup(
                [
                    {"item_code": "A", "bom_name": "BOM-A", "item_qty": 32, "scheduled_at": PAST},
                    {"item_code": "B", "bom_name": "BOM-B", "item_qty": 32, "scheduled_at": PAST},
                ],
                "Jarz Co",
            )

        self.assertEqual(1, len(rollup["shortages"]))
        shortage = rollup["shortages"][0]
        self.assertAlmostEqual(11.0, shortage["available_qty"])
        self.assertAlmostEqual(53.0, shortage["missing_qty"])
        # One ledger read for the pair, however many lines share it.
        qty_at.assert_called_once()
        stamp.assert_called_once()
        self.assertAlmostEqual(461.0, stamp.call_args.args[2])

    def test_an_undated_basket_never_reads_the_ledger(self):
        from jarz_pos.services import production_planning as planning

        components = [
            {
                "item_code": "LABEL",
                "item_name": "Label",
                "stock_uom": "Nos",
                "required_qty": 32.0,
                "source_warehouse": "Raw Material - J",
            }
        ]
        qty_at = MagicMock(return_value=0.0)
        with patch(
            "jarz_pos.services.production_planning._resolve_required_material_rows",
            return_value=MagicMock(return_value=components),
        ), patch(
            "jarz_pos.services.production_planning._resolve_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.services.production_planning._resolve_bin_stock_map",
            return_value={("LABEL", "Raw Material - J"): 461.0},
        ), patch(
            "jarz_pos.services.production_planning._resolve_backdated_stock_helpers",
            return_value=(lambda value: None, qty_at),
        ):
            rollup = planning.build_basket_rollup(
                [{"item_code": "A", "bom_name": "BOM-A", "item_qty": 32}], "Jarz Co"
            )

        self.assertEqual([], rollup["shortages"])
        qty_at.assert_not_called()


class TestPlainErrorText(unittest.TestCase):
    def test_erpnext_stock_markup_becomes_a_sentence(self):
        from jarz_pos.api import manufacturing

        raw = (
            '<strong>21.0</strong> units of <a href="/desk/item/Chocolate%20Hazelnut%20Jar%20Label%20330" '
            'style="font-weight: bold;">Item Chocolate Hazelnut Jar Label 330</a> needed in '
            '<a href="/desk/warehouse/Raw%20Material%20-%20J" style="font-weight: bold;">'
            "Warehouse Raw Material - J</a> to complete this transaction."
        )

        self.assertEqual(
            "21.0 units of Item Chocolate Hazelnut Jar Label 330 needed in "
            "Warehouse Raw Material - J to complete this transaction.",
            manufacturing._plain_error_text(Exception(raw)),
        )

    def test_entities_are_decoded(self):
        from jarz_pos.api import manufacturing

        self.assertEqual("Fish & Chips", manufacturing._plain_error_text("Fish &amp; Chips"))


if __name__ == "__main__":
    unittest.main()
