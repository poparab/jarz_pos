"""Customer deals: time-limited special prices for one B2B customer (pure mock).

What is pinned here, and why each matters:

* the deal lookup prefers a row naming the item over a category row, and
  answers ``None`` outside the window -- that "no match" IS the revert to the
  normal price, there is no job that undoes a deal;
* the resolver puts a live deal above every list row (customer Item Price
  included), and ``include_deals=False`` answers "what is the normal price";
* an amendment is priced on its SOURCE order's date only when the source is a
  cancelled invoice of the same customer with no live replacement, because
  ``amended_from`` arrives from the client;
* price-list coverage counts a deal as a price, or a deal-only item on a B2B
  list with no rate would be refused at checkout;
* the DocType refuses ambiguous rows and two live deals pricing the same
  thing on overlapping dates (the resolver would pick one silently);
* ending a deal that already priced orders moves its end to yesterday instead
  of cancelling it, so amending one of those orders still finds the deal.
"""

from __future__ import annotations

import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import customer_deals as api
from jarz_pos.services import customer_deals as deals
from jarz_pos.services import invoice_creation as ic

D = datetime.date
TODAY = D(2026, 10, 2)


def _row(deal, rate, item_code=None, item_group=None, valid_from=TODAY):
    return frappe._dict(
        deal=deal, rate=rate, item_code=item_code, item_group=item_group, valid_from=valid_from
    )


class TestFindDealRate(unittest.TestCase):
    def _find(self, rows, item_group="Large", code="Molten Large"):
        with patch.object(deals, "_active_deal_rows", return_value=rows), patch.object(
            deals.frappe.db, "get_value", return_value=item_group
        ):
            return deals.find_deal_rate("Cafe A", code, on_date=TODAY)

    def test_item_row_beats_category_row(self):
        rows = [
            _row("DEAL-1", 80, item_group="Large"),
            _row("DEAL-2", 70, item_code="Molten Large"),
        ]
        self.assertEqual(self._find(rows), (70.0, "DEAL-2"))

    def test_category_row_matches_item_group(self):
        self.assertEqual(self._find([_row("DEAL-1", 80, item_group="Large")]), (80.0, "DEAL-1"))

    def test_other_category_does_not_match(self):
        self.assertIsNone(self._find([_row("DEAL-1", 80, item_group="Medium")]))

    def test_no_live_deal_is_none(self):
        self.assertIsNone(self._find([]))

    def test_non_list_result_is_ignored(self):
        # A mocked frappe.db.sql in another module's test returns a MagicMock.
        self.assertIsNone(self._find(MagicMock()))

    def test_missing_customer_or_item_skips_lookup(self):
        with patch.object(deals, "_active_deal_rows") as rows:
            self.assertIsNone(deals.find_deal_rate("", "X"))
            self.assertIsNone(deals.find_deal_rate("Cafe A", ""))
        rows.assert_not_called()

    def test_missing_table_means_no_deals(self):
        err = Exception("Table 'tabJarz Customer Deal' doesn't exist")
        with patch.object(deals.frappe.db, "sql", side_effect=err), patch.object(
            deals.frappe.db, "is_table_missing", return_value=True
        ):
            self.assertEqual(deals._active_deal_rows("Cafe A", TODAY), [])

    def test_other_sql_errors_propagate(self):
        with patch.object(deals.frappe.db, "sql", side_effect=RuntimeError("boom")), patch.object(
            deals.frappe.db, "is_table_missing", return_value=False
        ):
            with self.assertRaises(RuntimeError):
                deals._active_deal_rows("Cafe A", TODAY)

    def test_window_is_inclusive_in_the_query(self):
        with patch.object(deals.frappe.db, "sql", return_value=[]) as sql:
            deals._active_deal_rows("Cafe A", TODAY)
        query = sql.call_args.args[0]
        self.assertIn("d.valid_from <= %(on_date)s", query)
        self.assertIn("d.valid_upto >= %(on_date)s", query)
        self.assertIn("IFNULL(d.disabled, 0) = 0", query)


class TestDealStatus(unittest.TestCase):
    def test_statuses(self):
        base = {"valid_from": D(2026, 10, 1), "valid_upto": D(2026, 10, 5), "disabled": 0}
        self.assertEqual(deals.deal_status(base, D(2026, 9, 30)), "upcoming")
        self.assertEqual(deals.deal_status(base, D(2026, 10, 1)), "active")
        self.assertEqual(deals.deal_status(base, D(2026, 10, 5)), "active")
        self.assertEqual(deals.deal_status(base, D(2026, 10, 6)), "expired")
        self.assertEqual(deals.deal_status({**base, "disabled": 1}, D(2026, 10, 2)), "cancelled")


class TestResolverPrecedence(unittest.TestCase):
    def test_deal_beats_customer_item_price(self):
        with patch.object(ic._customer_deals, "find_deal_rate", return_value=(80.0, "DEAL-1")), patch.object(
            ic.frappe.db, "get_value", return_value=95
        ) as gv:
            rate, prov = ic._resolve_item_rate_with_provenance(
                "Molten Large", "B2B Selling", customer="Cafe A", on_date=TODAY
            )
        self.assertEqual((rate, prov), (80.0, "deal"))
        gv.assert_not_called()

    def test_include_deals_false_returns_normal_price(self):
        with patch.object(ic._customer_deals, "find_deal_rate", return_value=(80.0, "DEAL-1")) as fd, patch.object(
            ic.frappe.db, "get_value", return_value=92
        ):
            rate, prov = ic._resolve_item_rate_with_provenance(
                "Molten Large", "B2B Selling", customer="Cafe A", include_deals=False
            )
        self.assertEqual((rate, prov), (92.0, "customer_price"))
        fd.assert_not_called()

    def test_no_customer_never_looks_for_a_deal(self):
        with patch.object(ic._customer_deals, "find_deal_rate") as fd, patch.object(
            ic.frappe.db, "get_value", return_value=160
        ):
            rate, prov = ic._resolve_item_rate_with_provenance("Molten Large", "Standard Selling")
        self.assertEqual((rate, prov), (160.0, "item_price"))
        fd.assert_not_called()

    def test_no_deal_falls_through_unchanged(self):
        with patch.object(ic._customer_deals, "find_deal_rate", return_value=None), patch.object(
            ic.frappe.db, "get_value", side_effect=[None, 92]
        ):
            rate, prov = ic._resolve_item_rate_with_provenance(
                "Molten Large", "B2B Selling", customer="Cafe A"
            )
        self.assertEqual((rate, prov), (92.0, "item_price"))

    def test_wrapper_passes_the_date(self):
        with patch.object(ic._customer_deals, "find_deal_rate", return_value=(80.0, "DEAL-1")) as fd:
            self.assertEqual(
                ic._resolve_item_rate("Molten Large", "B2B Selling", customer="Cafe A", on_date=TODAY),
                80.0,
            )
        self.assertEqual(fd.call_args.kwargs["on_date"], TODAY)


class TestDealPricingDate(unittest.TestCase):
    def _date(self, source_row, replacement=False, amended_from="SINV-1", customer="Cafe A"):
        with patch.object(ic.frappe.utils, "today", return_value=str(TODAY)), patch.object(
            ic.frappe.db, "get_value", return_value=source_row
        ), patch.object(ic.frappe.db, "exists", return_value=replacement):
            return ic._deal_pricing_date(amended_from, customer)

    def _src(self, **kw):
        row = {"docstatus": 2, "customer": "Cafe A", "posting_date": D(2026, 9, 20)}
        row.update(kw)
        return frappe._dict(row)

    def test_new_order_prices_today(self):
        self.assertEqual(self._date(None, amended_from=None), TODAY)

    def test_amendment_keeps_source_date(self):
        self.assertEqual(self._date(self._src()), D(2026, 9, 20))

    def test_other_customers_source_prices_today(self):
        self.assertEqual(self._date(self._src(customer="Cafe B")), TODAY)

    def test_live_source_prices_today(self):
        self.assertEqual(self._date(self._src(docstatus=1)), TODAY)

    def test_source_with_live_replacement_prices_today(self):
        self.assertEqual(self._date(self._src(), replacement=True), TODAY)

    def test_unknown_source_prices_today(self):
        self.assertEqual(self._date(None), TODAY)


class TestCoverageCountsDeals(unittest.TestCase):
    def test_deal_only_item_is_covered(self):
        decision = SimpleNamespace(matched=True, discount_percentage=0, order_purpose="B2B Supply")
        logger = MagicMock()
        with patch.object(ic._customer_deals, "find_deal_rate", return_value=(80.0, "DEAL-1")), patch.object(
            ic.frappe.db, "exists", return_value=False
        ), patch.object(ic.frappe.db, "get_value", return_value=None):
            ic._validate_policy_price_list_coverage(
                decision,
                "B2B Selling",
                [{"item_code": "Molten Large", "qty": 1}],
                logger,
                customer="Cafe A",
                pricing_date=TODAY,
            )

    def test_without_deal_still_refused(self):
        decision = SimpleNamespace(matched=True, discount_percentage=0, order_purpose="B2B Supply")
        with patch.object(ic._customer_deals, "find_deal_rate", return_value=None), patch.object(
            ic.frappe.db, "exists", return_value=False
        ), patch.object(ic.frappe.db, "get_value", return_value=None):
            with self.assertRaises(frappe.ValidationError):
                ic._validate_policy_price_list_coverage(
                    decision,
                    "B2B Selling",
                    [{"item_code": "Molten Large", "qty": 1}],
                    MagicMock(),
                    customer="Cafe A",
                )


class _Row(SimpleNamespace):
    pass


def _deal_doc(items, valid_from=TODAY, valid_upto=TODAY, disabled=0, new=True):
    from jarz_pos.doctype.jarz_customer_deal.jarz_customer_deal import JarzCustomerDeal

    doc = JarzCustomerDeal.__new__(JarzCustomerDeal)
    doc.__dict__.update(
        customer="Cafe A",
        valid_from=valid_from,
        valid_upto=valid_upto,
        disabled=disabled,
        name="DEAL-9",
        items=[_Row(idx=i + 1, **r) for i, r in enumerate(items)],
    )
    doc.is_new = lambda: new
    return doc


class TestDealValidation(unittest.TestCase):
    def _validate(self, doc, conflict=None):
        mod = "jarz_pos.doctype.jarz_customer_deal.jarz_customer_deal"
        with patch(f"{mod}.frappe.db.exists", return_value=True), patch(
            f"{mod}.find_conflicting_deal", return_value=conflict
        ) as fc:
            doc.validate()
        return fc

    def test_valid_deal_passes(self):
        fc = self._validate(
            _deal_doc(
                [
                    {"item_group": "Large", "item_code": None, "rate": 80},
                    {"item_group": None, "item_code": "Molten Medium", "rate": 60},
                ]
            )
        )
        targets = fc.call_args.args[3]
        self.assertEqual(targets, {("group", "Large"), ("item", "Molten Medium")})

    def test_row_with_both_targets_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._validate(_deal_doc([{"item_group": "Large", "item_code": "Molten Large", "rate": 80}]))

    def test_empty_row_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._validate(_deal_doc([{"item_group": None, "item_code": None, "rate": 80}]))

    def test_duplicate_target_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._validate(
                _deal_doc(
                    [
                        {"item_group": "Large", "item_code": None, "rate": 80},
                        {"item_group": "Large", "item_code": None, "rate": 75},
                    ]
                )
            )

    def test_negative_rate_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._validate(_deal_doc([{"item_group": "Large", "item_code": None, "rate": -1}]))

    def test_end_before_start_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._validate(
                _deal_doc(
                    [{"item_group": "Large", "item_code": None, "rate": 80}],
                    valid_from=D(2026, 10, 5),
                    valid_upto=D(2026, 10, 4),
                )
            )

    def test_overlapping_deal_on_same_target_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._validate(
                _deal_doc([{"item_group": "Large", "item_code": None, "rate": 80}]),
                conflict="DEAL-1",
            )

    def test_cancelled_deal_skips_overlap_check(self):
        fc = self._validate(
            _deal_doc([{"item_group": "Large", "item_code": None, "rate": 80}], disabled=1),
            conflict="DEAL-1",
        )
        fc.assert_not_called()


class TestEndDeal(unittest.TestCase):
    def _end(self, valid_from, valid_upto):
        doc = MagicMock()
        doc.valid_from = valid_from
        doc.valid_upto = valid_upto
        doc.disabled = 0
        doc.customer = "Cafe A"
        doc.as_dict.side_effect = lambda: {
            "valid_from": doc.valid_from,
            "valid_upto": doc.valid_upto,
            "disabled": doc.disabled,
        }
        with patch.object(api, "_ensure_full_manager_pricing_access"), patch.object(
            api.frappe, "get_doc", return_value=doc
        ), patch.object(api, "_today", return_value=TODAY), patch.object(
            api, "_serialize", return_value={}
        ), patch.object(api, "_normal_price_list", return_value="B2B Selling"):
            api.end_customer_deal("DEAL-1")
        doc.save.assert_called_once()
        return doc

    def test_running_deal_ends_yesterday_keeping_history(self):
        doc = self._end(D(2026, 9, 25), D(2026, 10, 10))
        self.assertEqual(frappe.utils.getdate(doc.valid_upto), D(2026, 10, 1))
        self.assertEqual(doc.disabled, 0)

    def test_deal_starting_today_is_cancelled(self):
        doc = self._end(TODAY, D(2026, 10, 10))
        self.assertEqual(doc.disabled, 1)

    def test_upcoming_deal_is_cancelled(self):
        doc = self._end(D(2026, 10, 5), D(2026, 10, 10))
        self.assertEqual(doc.disabled, 1)

    def test_expired_deal_cannot_be_ended(self):
        with self.assertRaises(frappe.ValidationError):
            self._end(D(2026, 9, 1), D(2026, 9, 10))


class TestSaveDealGuards(unittest.TestCase):
    def _save(self, existing_from, existing_upto, new_from, new_upto, disabled=0):
        doc = MagicMock()
        doc.customer = "Cafe A"
        doc.valid_from = existing_from
        doc.valid_upto = existing_upto
        doc.as_dict.return_value = {
            "valid_from": existing_from,
            "valid_upto": existing_upto,
            "disabled": disabled,
        }
        with patch.object(api, "_ensure_full_manager_pricing_access"), patch.object(
            api, "_require_customer", return_value="Cafe A"
        ), patch.object(api.frappe, "get_doc", return_value=doc), patch.object(
            api, "_today", return_value=TODAY
        ), patch.object(api, "_serialize", return_value={}), patch.object(
            api, "_normal_price_list", return_value="B2B Selling"
        ):
            api.save_customer_deal(
                "Cafe A",
                str(new_from),
                str(new_upto),
                '[{"item_group": "Large", "rate": 80}]',
                deal="DEAL-1",
            )
        return doc

    def test_expired_deal_cannot_be_edited(self):
        with self.assertRaises(frappe.ValidationError):
            self._save(D(2026, 9, 1), D(2026, 9, 10), D(2026, 9, 1), D(2026, 10, 10))

    def test_running_deal_keeps_its_start(self):
        with self.assertRaises(frappe.ValidationError):
            self._save(D(2026, 9, 25), D(2026, 10, 10), D(2026, 9, 28), D(2026, 10, 10))

    def test_running_deal_can_be_extended(self):
        doc = self._save(D(2026, 9, 25), D(2026, 10, 10), D(2026, 9, 25), D(2026, 10, 20))
        self.assertEqual(doc.valid_upto, D(2026, 10, 20))
        doc.save.assert_called_once()

    def test_items_must_be_a_list(self):
        with self.assertRaises(frappe.ValidationError):
            api._parse_items("[]")
        with self.assertRaises(frappe.ValidationError):
            api._parse_items('[{"item_group": "Large", "rate": "abc"}]')


if __name__ == "__main__":
    unittest.main()
