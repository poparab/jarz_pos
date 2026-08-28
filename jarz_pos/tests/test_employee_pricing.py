"""Tests for the Employee price-list guarantee (``setup/employee_pricing``).

The rule is one sentence — every large jar is 92 on the Employee list, every
medium is 77 — and the ways to get it wrong are all silent:

  * enumerate only "Medium" and the real, typo-named "Meduim" group's items keep
    their old price with nothing in the log to say so;
  * invent a category row for a group that does not exist on the site and every
    later save of anything linking to it fails validation;
  * rewrite a rate that is already correct and every migrate churns ``modified``
    on rows nobody touched;
  * correct a rate WITHOUT saying so and the admin whose UI edit was reverted has
    no way to find out why.

This module deliberately overwrites drift (unlike the create-only
``b2b_master_data``), so the "was it reported" cases matter as much as the
"was it fixed" ones.

Mock-level, like ``test_purchase_setup``: the module's ``frappe`` is replaced, so
the suite never writes an Item Price to the site it runs against.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.setup import employee_pricing as ep


_CATEGORY_DOCTYPE = "Jarz Price List Category Rate"


class _Site:
    """A tiny in-memory stand-in for the pricing tables this module touches."""

    def __init__(self, *, item_groups=("Large", "Medium", "Meduim"), items=None,
                 category_rates=None, item_prices=None, price_list_exists=True):
        self.item_groups = set(item_groups)
        #: item_group -> [item_code]
        self.items = dict(items or {})
        self.category_rates = [dict(r) for r in (category_rates or [])]
        self.item_prices = [dict(r) for r in (item_prices or [])]
        self.price_list_exists = price_list_exists
        #: (doctype, name, field, value) for every db.set_value
        self.writes = []
        #: the dicts handed to frappe.get_doc(...).insert()
        self.inserts = []

    # --- frappe.db -------------------------------------------------------
    def exists(self, doctype, name=None):
        if doctype == "Price List":
            return self.price_list_exists and name == ep.PRICE_LIST
        if doctype == "Item Group":
            return name in self.item_groups
        return False

    def get_value(self, doctype, name=None, fieldname=None, **kwargs):
        if doctype == "Price List" and fieldname == "currency":
            return "EGP"
        if doctype == "Company" and fieldname == "default_currency":
            return "EGP"
        return None

    def set_value(self, doctype, name, field, value, update_modified=True):
        self.writes.append((doctype, name, field, value))
        for row in self.category_rates if doctype == _CATEGORY_DOCTYPE else self.item_prices:
            if row["name"] == name:
                row[field] = value

    # --- frappe.get_all --------------------------------------------------
    def get_all(self, doctype, **kwargs):
        filters = kwargs.get("filters") or {}
        if doctype == "Item":
            groups = (filters.get("item_group") or ["in", []])[1]
            codes = []
            for group in groups:
                codes.extend(self.items.get(group, []))
            return codes
        if doctype == _CATEGORY_DOCTYPE:
            return [
                {"name": r["name"], "rate": r["rate"]}
                for r in self.category_rates
                if r["price_list"] == filters.get("price_list")
                and r["item_group"] == filters.get("item_group")
            ]
        if doctype == "Item Price":
            return [
                {"name": r["name"], "price_list_rate": r["price_list_rate"]}
                for r in self.item_prices
                if r["price_list"] == filters.get("price_list")
                and r["item_code"] == filters.get("item_code")
                # The module asks for the GENERIC row only; a customer-scoped
                # Item Price is a deliberate override and must stay invisible here.
                and not (r.get("customer") or "")
            ]
        return []

    # --- frappe.get_doc --------------------------------------------------
    def get_doc(self, data):
        site = self

        class _Doc:
            def __init__(self):
                self.data = dict(data)

            def insert(self, ignore_permissions=False):
                site.inserts.append(dict(data))
                site._materialize(dict(data))

        return _Doc()

    def _materialize(self, data):
        """Make an inserted row visible to the next run (idempotency testing)."""
        if data.get("doctype") == _CATEGORY_DOCTYPE:
            data["name"] = f"CR-{len(self.category_rates) + 1}"
            self.category_rates.append(data)
        elif data.get("doctype") == "Item Price":
            data["name"] = f"IP-{len(self.item_prices) + 1}"
            data.setdefault("customer", None)
            self.item_prices.append(data)

    # --- assertions helpers ----------------------------------------------
    def inserted(self, doctype):
        return [row for row in self.inserts if row.get("doctype") == doctype]


def _mock_frappe(site: _Site) -> MagicMock:
    mock = MagicMock()
    mock.db.exists.side_effect = site.exists
    mock.db.get_value.side_effect = site.get_value
    mock.db.set_value.side_effect = site.set_value
    mock.get_all.side_effect = site.get_all
    mock.get_doc.side_effect = site.get_doc
    mock.defaults.get_global_default.return_value = None
    mock.logger.return_value = MagicMock()
    return mock


def _run(site: _Site):
    mock = _mock_frappe(site)
    with patch.object(ep, "frappe", mock):
        log = ep.ensure_employee_price_list_rates()
    return log, mock


def _category_rate(name, item_group, rate):
    return {
        "name": name,
        "price_list": ep.PRICE_LIST,
        "item_group": item_group,
        "rate": rate,
    }


def _item_price(name, item_code, rate, customer=None):
    return {
        "name": name,
        "item_code": item_code,
        "price_list": ep.PRICE_LIST,
        "customer": customer,
        "price_list_rate": rate,
    }


_ITEMS = {"Large": ["JAR-L-1"], "Medium": ["JAR-M-1"], "Meduim": ["JAR-M-2"]}


class TestEmployeePricingSeeding(unittest.TestCase):
    """A site with no Employee rates gets both layers, for every size group."""

    def test_both_layers_are_written_for_every_group(self):
        site = _Site(items=_ITEMS)
        log, _mock = _run(site)

        categories = {
            (row["item_group"], row["rate"]) for row in site.inserted(_CATEGORY_DOCTYPE)
        }
        self.assertEqual(
            categories,
            {("Large", ep.LARGE_RATE), ("Medium", ep.MEDIUM_RATE), ("Meduim", ep.MEDIUM_RATE)},
        )

        prices = {
            (row["item_code"], row["price_list_rate"]) for row in site.inserted("Item Price")
        }
        self.assertEqual(
            prices,
            {("JAR-L-1", ep.LARGE_RATE), ("JAR-M-1", ep.MEDIUM_RATE), ("JAR-M-2", ep.MEDIUM_RATE)},
        )
        self.assertEqual(log["updated"], [], "a fresh site has no drift to correct")

    def test_the_meduim_typo_group_is_included(self):
        # REGRESSION GUARD: "Meduim" is a real Item Group holding real items.
        # Enumerating only "Medium" leaves those jars at the wrong staff price
        # and produces no error anywhere.
        site = _Site(items=_ITEMS)
        log, _mock = _run(site)

        priced = {row["item_code"] for row in site.inserted("Item Price")}
        self.assertIn("JAR-M-2", priced)
        self.assertIn(
            ("Meduim", ep.MEDIUM_RATE),
            {(r["item_group"], r["rate"]) for r in site.inserted(_CATEGORY_DOCTYPE)},
        )
        # Both spellings count as "medium" in the resolved-count report.
        self.assertEqual(log["summary"]["medium_items"], 2)
        self.assertEqual(log["summary"]["large_items"], 1)

    def test_absent_typo_group_is_skipped_not_invented(self):
        # A category rate Link pointing at a non-existent Item Group would fail
        # validation on insert and poison later saves of anything touching it.
        site = _Site(item_groups=("Large", "Medium"),
                     items={"Large": ["JAR-L-1"], "Medium": ["JAR-M-1"]})
        log, _mock = _run(site)

        groups = {row["item_group"] for row in site.inserted(_CATEGORY_DOCTYPE)}
        self.assertEqual(groups, {"Large", "Medium"})
        self.assertTrue(
            any("Meduim" in entry for entry in log["summary"]["skipped"]),
            "an absent group must be reported, not silently dropped",
        )

    def test_second_run_writes_nothing(self):
        site = _Site(items=_ITEMS)
        _run(site)
        inserts_after_first = len(site.inserts)

        log, _mock = _run(site)

        self.assertEqual(len(site.inserts), inserts_after_first, "second run must not insert")
        self.assertEqual(site.writes, [], "second run must not write")
        self.assertEqual(log["created"], [])
        self.assertEqual(log["updated"], [])


class TestEmployeePricingLeavesCorrectValuesAlone(unittest.TestCase):
    """An already-correct rate must not be rewritten — no churn on ``modified``."""

    def test_correct_rates_produce_no_writes(self):
        site = _Site(
            items=_ITEMS,
            category_rates=[
                _category_rate("CR-L", "Large", ep.LARGE_RATE),
                _category_rate("CR-M", "Medium", ep.MEDIUM_RATE),
                _category_rate("CR-MD", "Meduim", ep.MEDIUM_RATE),
            ],
            item_prices=[
                _item_price("IP-L1", "JAR-L-1", ep.LARGE_RATE),
                _item_price("IP-M1", "JAR-M-1", ep.MEDIUM_RATE),
                _item_price("IP-M2", "JAR-M-2", ep.MEDIUM_RATE),
            ],
        )
        log, mock = _run(site)

        self.assertEqual(site.writes, [])
        self.assertEqual(site.inserts, [])
        self.assertEqual(log["updated"], [])
        self.assertEqual(log["summary"]["unchanged"], 6)
        mock.log_error.assert_not_called()

    def test_a_customer_scoped_price_is_left_alone(self):
        # invoice_creation._resolve_item_rate reads a customer-scoped Item Price
        # ahead of the generic one, so that row is a deliberate per-person rate.
        site = _Site(
            item_groups=("Large",),
            items={"Large": ["JAR-L-1"]},
            item_prices=[_item_price("IP-SPECIAL", "JAR-L-1", 60.0, customer="CUST-1")],
        )
        _log, _mock = _run(site)

        self.assertEqual(site.writes, [], "the customer-scoped row must not be touched")
        prices = {(r["item_code"], r["price_list_rate"]) for r in site.inserted("Item Price")}
        self.assertEqual(prices, {("JAR-L-1", ep.LARGE_RATE)})


class TestEmployeePricingCorrectsDrift(unittest.TestCase):
    """Drift IS corrected — and is always reported, never silently reverted."""

    def test_wrong_category_rate_is_corrected_and_reported(self):
        site = _Site(
            item_groups=("Large",),
            items={"Large": ["JAR-L-1"]},
            category_rates=[_category_rate("CR-L", "Large", 85.0)],
            item_prices=[_item_price("IP-L1", "JAR-L-1", ep.LARGE_RATE)],
        )
        log, _mock = _run(site)

        self.assertIn((_CATEGORY_DOCTYPE, "CR-L", "rate", ep.LARGE_RATE), site.writes)
        self.assertEqual(log["summary"]["category_rates_updated"], 1)
        self.assertEqual(len(log["updated"]), 1)
        entry = log["updated"][0]
        self.assertIn("85.0", entry, "the old value must be reported")
        self.assertIn("92.0", entry, "the new value must be reported")
        self.assertIn("Large", entry, "the thing that changed must be named")

    def test_wrong_item_price_is_corrected_and_reported(self):
        site = _Site(
            item_groups=("Large",),
            items={"Large": ["JAR-L-1"]},
            category_rates=[_category_rate("CR-L", "Large", ep.LARGE_RATE)],
            item_prices=[_item_price("IP-L1", "JAR-L-1", 80.0)],
        )
        log, _mock = _run(site)

        self.assertIn(("Item Price", "IP-L1", "price_list_rate", ep.LARGE_RATE), site.writes)
        self.assertEqual(log["summary"]["item_prices_updated"], 1)
        self.assertEqual(len(log["updated"]), 1)
        self.assertIn("JAR-L-1", log["updated"][0])

    def test_drift_is_recorded_in_the_error_log(self):
        # logger.info is effectively silent on the servers, and a reverted UI edit
        # is exactly what somebody comes asking about later.
        site = _Site(
            item_groups=("Large",),
            items={"Large": ["JAR-L-1"]},
            category_rates=[_category_rate("CR-L", "Large", 85.0)],
            item_prices=[_item_price("IP-L1", "JAR-L-1", ep.LARGE_RATE)],
        )
        _log, mock = _run(site)

        mock.log_error.assert_called_once()
        self.assertIn("drift", mock.log_error.call_args.args[1].lower())

    def test_a_failing_error_log_does_not_undo_the_correction(self):
        site = _Site(
            item_groups=("Large",),
            items={"Large": ["JAR-L-1"]},
            category_rates=[_category_rate("CR-L", "Large", 85.0)],
            item_prices=[_item_price("IP-L1", "JAR-L-1", ep.LARGE_RATE)],
        )
        mock = _mock_frappe(site)
        mock.log_error.side_effect = Exception("Error Log is itself broken")
        with patch.object(ep, "frappe", mock):
            log = ep.ensure_employee_price_list_rates()

        self.assertEqual(log["summary"]["category_rates_updated"], 1)
        self.assertIn((_CATEGORY_DOCTYPE, "CR-L", "rate", ep.LARGE_RATE), site.writes)

    def test_an_unset_rate_counts_as_drift(self):
        site = _Site(
            item_groups=("Large",),
            items={"Large": []},
            category_rates=[_category_rate("CR-L", "Large", None)],
        )
        log, _mock = _run(site)
        self.assertIn((_CATEGORY_DOCTYPE, "CR-L", "rate", ep.LARGE_RATE), site.writes)
        self.assertEqual(log["summary"]["category_rates_updated"], 1)


class TestEmployeePricingNeverBreaksMigrate(unittest.TestCase):
    """This runs on every ``bench migrate``; it may fail, but never loudly."""

    def test_missing_price_list_is_a_clean_skip(self):
        site = _Site(items=_ITEMS, price_list_exists=False)
        log, _mock = _run(site)

        self.assertEqual(site.writes, [])
        self.assertEqual(site.inserts, [])
        self.assertTrue(log["summary"]["skipped"])

    def test_a_failing_query_is_contained(self):
        site = _Site(items=_ITEMS)
        mock = _mock_frappe(site)
        mock.get_all.side_effect = Exception("db down")
        with patch.object(ep, "frappe", mock):
            log = ep.ensure_employee_price_list_rates()

        # Returned normally (a migrate must not die over a price rate) and the
        # failures are named rather than swallowed.
        self.assertTrue(log["summary"]["failed"])
        self.assertEqual(log["summary"]["large_items"], 0)

    def test_rates_are_the_documented_numbers(self):
        # The whole feature is these two constants; pin them so a "cleanup" edit
        # has to be deliberate.
        self.assertEqual(ep.LARGE_RATE, 92.0)
        self.assertEqual(ep.MEDIUM_RATE, 77.0)
        self.assertEqual(ep.PRICE_LIST, "Employee")
        self.assertIn("Meduim", ep._MEDIUM_GROUPS)


if __name__ == "__main__":
    unittest.main()
