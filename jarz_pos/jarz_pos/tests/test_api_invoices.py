"""Lightweight sanity tests for the new API modules.

These tests do **not** hit the database – they merely ensure that the new
modules can be imported successfully inside the test runner environment.
"""


def test_api_modules_present():
    import importlib

    invoices = importlib.import_module("jarz_pos.jarz_pos.api.invoices")
    couriers = importlib.import_module("jarz_pos.jarz_pos.api.couriers")

    assert hasattr(invoices, "create_sales_invoice")
    assert hasattr(couriers, "get_courier_balances")


def test_placeholder_create_sales_invoice():
    """Functional tests will be added once a frappe test site fixture exists."""

    assert True  # placeholder 