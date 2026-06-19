"""customer.search_customers — optional customer_type filter (pure mock / unittest).

Asserts that the new optional ``customer_type`` argument:
  - adds a parameterized ``customer_type = %(customer_type)s`` clause ONLY when passed,
  - validates the value (rejects anything other than Individual/Company),
  - leaves the query/params byte-identical when omitted (None),
  - is ANDed onto the name/phone OR-group (so it always constrains results).

The DB layer (``frappe.db.sql`` / ``frappe.db.has_column``) and territory augmentation
are fully mocked; no real query runs. We capture the SQL + params handed to ``db.sql``.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from jarz_pos.api import customer as cust_api


@contextmanager
def _patched_db(captured, *, has_columns=("phone", "customer_type")):
    """Patch the frappe surface search_customers touches, capturing sql() args."""

    def _fake_sql(query, params=None, as_dict=1):
        captured["query"] = query
        captured["params"] = dict(params or {})
        return []  # no rows -> augmentation loop is a no-op

    has_set = set(has_columns)
    with patch.object(cust_api.frappe.db, "sql", side_effect=_fake_sql), patch.object(
        cust_api.frappe.db, "has_column", side_effect=lambda dt, col: col in has_set
    ), patch.object(cust_api.frappe, "logger"), patch.object(
        cust_api, "_augment_customer_with_territory"
    ):
        yield


class TestCustomerTypeFilterAbsent(unittest.TestCase):
    """When customer_type is omitted, the query carries NO customer_type clause."""

    def test_name_search_no_type_clause(self):
        cap = {}
        with _patched_db(cap):
            cust_api.search_customers(name="acme")
        self.assertNotIn("customer_type", cap["query"])
        self.assertNotIn("customer_type", cap["params"])
        self.assertEqual(cap["params"].get("search_term"), "%acme%")

    def test_phone_search_no_type_clause(self):
        cap = {}
        with _patched_db(cap):
            cust_api.search_customers(phone="0100")
        self.assertNotIn("customer_type", cap["query"])
        self.assertNotIn("customer_type", cap["params"])


class TestCustomerTypeFilterPresent(unittest.TestCase):
    """When a valid customer_type is passed, an ANDed parameterized clause is added."""

    def test_company_filter_added_and_anded(self):
        cap = {}
        with _patched_db(cap):
            cust_api.search_customers(name="acme", customer_type="Company")
        self.assertEqual(cap["params"].get("customer_type"), "Company")
        # Parameterized (no inlined value) and ANDed onto the OR group.
        self.assertIn("%(customer_type)s", cap["query"])
        self.assertIn("AND", cap["query"].upper())
        # The name OR-group is still present.
        self.assertIn("search_term", cap["params"])

    def test_individual_filter_added(self):
        cap = {}
        with _patched_db(cap):
            cust_api.search_customers(phone="0100", customer_type="Individual")
        self.assertEqual(cap["params"].get("customer_type"), "Individual")
        self.assertIn("%(customer_type)s", cap["query"])

    def test_type_clause_skipped_when_column_missing(self):
        # If the Customer doctype lacks the customer_type column, the filter is
        # silently skipped (no clause, no param) rather than producing bad SQL.
        cap = {}
        with _patched_db(cap, has_columns=("phone",)):
            cust_api.search_customers(name="acme", customer_type="Company")
        self.assertNotIn("%(customer_type)s", cap["query"])
        self.assertNotIn("customer_type", cap["params"])


class TestCustomerTypeValidation(unittest.TestCase):
    """An invalid customer_type is rejected before any query runs."""

    def test_invalid_type_throws(self):
        cap = {}
        with _patched_db(cap):
            with self.assertRaises(Exception):
                cust_api.search_customers(name="acme", customer_type="Wholesale")
        # db.sql must not have been reached.
        self.assertNotIn("query", cap)


class TestSearchNoCriteria(unittest.TestCase):
    """No name and no phone -> empty list, no query (unchanged contract)."""

    def test_empty_returns_list_without_query(self):
        cap = {}
        with _patched_db(cap):
            self.assertEqual(cust_api.search_customers(), [])
        self.assertNotIn("query", cap)


if __name__ == "__main__":
    unittest.main()
