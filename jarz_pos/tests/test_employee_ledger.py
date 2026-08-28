"""Tests for the employee ledger (``api/manager.get_employee_ledger``).

The ledger adds two things that live in different party spaces — an HRMS
``Employee Advance`` (party = Employee) and an Employee-purpose POS order
(party = Customer, deliberately left unpaid because staff settle on account) —
into ONE balance per person. The properties worth pinning are the ones where a
plausible implementation quietly reports the wrong amount of money:

  * HRMS absent must degrade, never throw (this bench declares no required_apps).
  * An order whose Customer resolves to no Employee must still be counted.
  * A staff order that WAS paid at the till must contribute 0 to the balance
    while still appearing as history.
  * Branch scoping must reach the invoice query (managers are not exempt).
  * Truncation must be announced, because a total that silently stopped at row
    N is a wrong number a manager will act on.

Mock-level like ``test_commercial_policy`` and ``test_api_manager``: the module's
``frappe`` is replaced so nothing touches the site the suite runs against.

``api/manager.py`` binds ``add_days`` / ``flt`` / ``getdate`` / ``nowdate`` BY
NAME at import, so patching the module's ``frappe`` does not reach them — see
:func:`_ledger_env` for which are pinned and which are deliberately left real.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

from jarz_pos.api import manager


FROM_DATE = "2026-06-01"
TO_DATE = "2026-06-30"
#: What the patched ``nowdate`` reports, so the default-window case is exact.
TODAY = "2026-06-30"
#: TODAY minus 89 days: the inclusive 90-day window the endpoint documents.
DEFAULT_FROM_DATE = "2026-04-02"


def _raise_frappe(message, exc=None, title=None):
    if exc and isinstance(exc, type) and issubclass(exc, Exception):
        raise exc(message)
    raise Exception(message)


class _Site:
    """Rows the mocked ``frappe.get_all`` hands back, plus what it was asked.

    Sales Invoice and Employee Advance are each queried TWICE, and the two are
    not interchangeable:

    * WINDOWED (``posting_date`` in the filters) — the activity feed, what gets
      listed, row-capped.
    * ALL-TIME (no ``posting_date``) — the balance, uncapped.

    The fake tells them apart on exactly that, so a test can park money outside
    the window and prove it still counts. ``open_*`` default to what the real
    all-time queries would return for the same rows: every open advance, and only
    invoices that still owe something.
    """

    def __init__(self, *, advances=None, invoices=None, employees=None,
                 delivery_notes=None, pos_profiles=None,
                 open_advances=None, open_invoices=None):
        self.advances = advances or []
        self.invoices = invoices or []
        self.open_advances = (
            open_advances if open_advances is not None else list(self.advances)
        )
        self.open_invoices = (
            open_invoices if open_invoices is not None
            else [r for r in self.invoices if r.get("outstanding_amount")]
        )
        self.employees = employees or []
        self.delivery_notes = delivery_notes or []
        self.pos_profiles = pos_profiles if pos_profiles is not None else ["Dokki", "Nasr city"]
        #: doctype -> [kwargs] in call order, so BOTH passes can be asserted.
        self.calls = {}
        #: doctypes queried at all, so "never asked" can be asserted.
        self.queried = []

    def get_all(self, doctype, **kwargs):
        self.queried.append(doctype)
        self.calls.setdefault(doctype, []).append(kwargs)
        windowed = "posting_date" in (kwargs.get("filters") or {})
        if doctype == "Employee Advance":
            return list(self.advances if windowed else self.open_advances)
        if doctype == "Sales Invoice":
            return list(self.invoices if windowed else self.open_invoices)
        if doctype == "Employee":
            return list(self.employees)
        if doctype == "Delivery Note Item":
            return list(self.delivery_notes)
        if doctype == "POS Profile":
            return list(self.pos_profiles)
        return []

    def query(self, doctype, *, windowed):
        """The kwargs of the windowed (listing) or all-time (balance) query."""
        for kwargs in self.calls.get(doctype, []):
            if ("posting_date" in (kwargs.get("filters") or {})) is windowed:
                return kwargs
        raise AssertionError(
            f"no {'windowed' if windowed else 'all-time'} {doctype} query was made"
        )

    def filters_for(self, doctype, *, windowed):
        return self.query(doctype, windowed=windowed).get("filters") or {}


def _mock_frappe(site: _Site) -> MagicMock:
    mock = MagicMock()
    mock.session.user = "manager@example.com"
    # In LINE_MANAGER_TIER, so _ensure_manager_dashboard_access lets us through.
    mock.get_roles.return_value = ["JARZ Manager"]
    mock.throw.side_effect = _raise_frappe
    mock.PermissionError = PermissionError
    # No global default company -> _employee_ledger_currency falls back to EGP
    # instead of str()-ing a MagicMock into the payload.
    mock.defaults.get_global_default.return_value = None
    # _shift_monitor_user_details reads User.full_name / Employee via db.get_value.
    mock.db.get_value.return_value = None
    mock.get_all.side_effect = site.get_all
    return mock


def _flt(value, precision=None):
    """Pure stand-in for ``frappe.utils.flt``. NOT cosmetic.

    Without an initialised site ``flt(x, 2)`` calls ``rounded()``, which reads
    ``frappe.get_system_settings("rounding_method")``, which dereferences
    ``frappe.client_cache`` — ``None`` here — and raises. ``flt`` catches that in
    its own ``except Exception`` and returns **0.0**. Left real, every amount in
    this suite would silently read as zero and the failures would look like
    ledger arithmetic bugs instead of a missing site.

    Every value used here is exact to two decimals, so this and the real
    banker's rounding agree on all of them.
    """
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(num, precision) if precision is not None else num


@contextmanager
def _ledger_env(mock, *, profiles=("Dokki",), hrms=True,
                employees_for_customers=None, customers_for_employees=None):
    """Pin everything the endpoint reaches outside its own module.

    ``api/manager.py`` does ``from frappe.utils import add_days, flt, getdate,
    nowdate`` at import, so those four are attributes of ``manager``, not of the
    ``frappe`` object the tests replace:

    * ``nowdate`` — patched. Real, it needs the system timezone from System
      Settings and dies bench-less (this is what made 24 of these tests error).
      Pinned to a constant, which also makes the default-window case exact.
    * ``flt`` — patched, see :func:`_flt`: real, it degrades to 0.0 bench-less.
    * ``add_days`` / ``getdate`` — deliberately left REAL. They are pure
      (dateutil) for the inputs used here, so the 90-day default window is still
      computed by the production arithmetic rather than asserted against itself.

    The ``employee_link`` helpers are patched on their OWN module because the
    endpoint imports them inside the function body, which resolves at call time.
    """
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(manager, "frappe", mock))
        enter(patch.object(manager, "nowdate", lambda: TODAY))
        enter(patch.object(manager, "flt", _flt))
        enter(patch.object(manager, "_current_user_allowed_profiles",
                           return_value=list(profiles)))
        enter(patch("jarz_pos.utils.employee_link.hrms_available", return_value=hrms))
        enter(patch("jarz_pos.utils.employee_link.employees_for_customers",
                    return_value=dict(employees_for_customers or {})))
        enter(patch("jarz_pos.utils.employee_link.customers_for_employees",
                    return_value=dict(customers_for_employees or {})))
        yield


def _run(site, *, profiles=("Dokki",), hrms=True, employees_for_customers=None,
         customers_for_employees=None, from_date=FROM_DATE, to_date=TO_DATE,
         mock=None, **kwargs):
    """Call the endpoint under :func:`_ledger_env`; return ``(payload, mock)``."""
    mock = mock if mock is not None else _mock_frappe(site)
    with _ledger_env(mock, profiles=profiles, hrms=hrms,
                     employees_for_customers=employees_for_customers,
                     customers_for_employees=customers_for_employees):
        result = manager.get_employee_ledger(from_date=from_date, to_date=to_date, **kwargs)
    return result, mock


def _invoice(name, customer, outstanding, *, grand_total=None, status="Unpaid",
             branch="Dokki", customer_name=None):
    return {
        "name": name,
        "customer": customer,
        "customer_name": customer_name or customer,
        "posting_date": "2026-06-10",
        "grand_total": grand_total if grand_total is not None else outstanding,
        "outstanding_amount": outstanding,
        "status": status,
        "custom_kanban_profile": branch,
        "custom_sales_invoice_state": "Delivered",
    }


def _advance(name, employee, *, paid=0.0, claimed=0.0, returned=0.0,
             amount=None, status="Paid", employee_name=None):
    return {
        "name": name,
        "employee": employee,
        "employee_name": employee_name or employee,
        "posting_date": "2026-06-05",
        "advance_amount": amount if amount is not None else paid,
        "paid_amount": paid,
        "claimed_amount": claimed,
        "return_amount": returned,
        "status": status,
        "purpose": "Cash advance",
        "advance_account": "Employee Advances - J",
        "currency": "EGP",
    }


class TestEmployeeLedgerHrmsDegradation(unittest.TestCase):
    """HRMS is not a required_app: its absence must cost the advances, not the page."""

    def test_hrms_absent_returns_empty_advances_and_zeroed_totals(self):
        site = _Site(invoices=[_invoice("SINV-1", "CUST-1", 100.0)])
        result, _mock = _run(site, hrms=False)

        self.assertTrue(result["success"])
        self.assertFalse(result["hrms_available"])
        self.assertEqual(result["advances"], [])
        self.assertEqual(result["summary"]["advance_outstanding"], 0.0)
        self.assertEqual(result["summary"]["advance_count"], 0)
        # The orders half still works — that is the point of degrading.
        self.assertEqual(result["summary"]["order_outstanding"], 100.0)
        self.assertEqual(result["summary"]["total_outstanding"], 100.0)
        self.assertEqual(result["notice_code"], "hrms_unavailable")

    def test_hrms_absent_never_queries_the_advance_table(self):
        # Querying a DocType that does not exist is exactly how "degrade
        # gracefully" turns back into a 500.
        site = _Site(invoices=[_invoice("SINV-1", "CUST-1", 10.0)])
        _result, _mock = _run(site, hrms=False)
        self.assertNotIn("Employee Advance", site.queried)

    def test_advance_query_failure_leaves_the_orders_half_intact(self):
        site = _Site(invoices=[_invoice("SINV-1", "CUST-1", 40.0)])

        def _boom(doctype, **kwargs):
            if doctype == "Employee Advance":
                raise Exception("Unknown table 'tabEmployee Advance'")
            return site.get_all(doctype, **kwargs)

        mock = _mock_frappe(site)
        mock.get_all.side_effect = _boom
        result, _mock = _run(site, mock=mock)

        self.assertTrue(result["success"])
        self.assertEqual(result["advances"], [])
        self.assertEqual(result["summary"]["order_outstanding"], 40.0)


class TestEmployeeLedgerOrders(unittest.TestCase):
    """Order money must never disappear on its way into the total."""

    def test_customer_with_no_employee_link_still_counts(self):
        # REGRESSION GUARD: joining Customer -> Employee and keeping only the
        # matches is the obvious implementation, and it silently understates the
        # amount owed by exactly the orders of everyone not linked yet.
        site = _Site(invoices=[_invoice("SINV-1", "CUST-9", 250.0, customer_name="Mona Staff")])
        result, _mock = _run(site, employees_for_customers={})

        self.assertEqual(result["summary"]["order_outstanding"], 250.0)
        self.assertEqual(result["summary"]["total_outstanding"], 250.0)

        order = result["orders"][0]
        self.assertEqual(order["employee"], "")
        self.assertEqual(order["employee_name"], "Mona Staff")

        self.assertEqual(len(result["employees"]), 1)
        row = result["employees"][0]
        self.assertEqual(row["employee"], "")
        self.assertEqual(row["employee_name"], "Mona Staff")
        self.assertEqual(row["customer"], "CUST-9")
        self.assertEqual(row["order_outstanding"], 250.0)

    def test_two_unlinked_customers_do_not_collapse_into_one_row(self):
        site = _Site(invoices=[
            _invoice("SINV-1", "CUST-9", 100.0, customer_name="Mona Staff"),
            _invoice("SINV-2", "CUST-8", 60.0, customer_name="Sara Staff"),
        ])
        result, _mock = _run(site, employees_for_customers={})

        self.assertEqual(len(result["employees"]), 2)
        self.assertEqual(result["summary"]["order_outstanding"], 160.0)
        self.assertEqual(
            {r["customer"] for r in result["employees"]}, {"CUST-9", "CUST-8"}
        )

    def test_paid_order_contributes_zero_but_is_still_listed(self):
        # A staff order paid at the till is history, not debt. It must show in
        # the feed and add nothing to the balance.
        site = _Site(
            invoices=[
                _invoice("SINV-PAID", "CUST-1", 0.0, grand_total=180.0, status="Paid"),
                _invoice("SINV-OPEN", "CUST-1", 150.0, status="Unpaid"),
            ],
            employees=[{"name": "EMP-1", "employee_name": "Ali", "branch": "", "user_id": ""}],
        )
        result, _mock = _run(site, employees_for_customers={"CUST-1": "EMP-1"})

        self.assertEqual(result["summary"]["order_count"], 2)
        self.assertEqual(len(result["orders"]), 2)
        self.assertEqual(result["summary"]["order_outstanding"], 150.0)

        paid = next(o for o in result["orders"] if o["invoice"] == "SINV-PAID")
        self.assertEqual(paid["grand_total"], 180.0)
        self.assertEqual(paid["outstanding_amount"], 0.0)

    def test_delivery_note_is_attached_per_invoice(self):
        site = _Site(
            invoices=[_invoice("SINV-1", "CUST-1", 20.0), _invoice("SINV-2", "CUST-1", 30.0)],
            delivery_notes=[{"parent": "DN-001", "against_sales_invoice": "SINV-2"}],
        )
        result, _mock = _run(site)
        by_invoice = {o["invoice"]: o["delivery_note"] for o in result["orders"]}
        self.assertEqual(by_invoice["SINV-2"], "DN-001")
        self.assertIsNone(by_invoice["SINV-1"])


class TestEmployeeLedgerRollup(unittest.TestCase):
    """One person, one balance — advances and orders on the same row."""

    def _employee_rows(self):
        return [{"name": "EMP-1", "employee_name": "Ali Staff", "branch": "", "user_id": ""}]

    def test_advance_balance_uses_the_hrms_arithmetic(self):
        site = _Site(
            advances=[_advance("EA-1", "EMP-1", paid=500.0, claimed=200.0, returned=50.0)],
            employees=self._employee_rows(),
        )
        result, _mock = _run(site)

        # paid - (claimed + return) = 500 - 250 = 250
        self.assertEqual(result["advances"][0]["balance"], 250.0)
        self.assertEqual(result["summary"]["advance_outstanding"], 250.0)

    def test_approved_but_undisbursed_advance_adds_nothing(self):
        # Status "Unpaid" means no money has left the company yet. It is listed
        # for visibility and must contribute 0.
        site = _Site(
            advances=[_advance("EA-2", "EMP-1", paid=0.0, amount=400.0, status="Unpaid")],
            employees=self._employee_rows(),
        )
        result, _mock = _run(site)

        self.assertEqual(len(result["advances"]), 1)
        self.assertEqual(result["advances"][0]["amount"], 400.0)
        self.assertEqual(result["summary"]["advance_outstanding"], 0.0)

    def test_advances_and_orders_merge_into_one_person(self):
        site = _Site(
            advances=[_advance("EA-1", "EMP-1", paid=500.0, claimed=200.0)],
            invoices=[_invoice("SINV-1", "CUST-1", 150.0)],
            employees=self._employee_rows(),
        )
        result, _mock = _run(
            site,
            employees_for_customers={"CUST-1": "EMP-1"},
            customers_for_employees={"EMP-1": "CUST-1"},
        )

        self.assertEqual(len(result["employees"]), 1)
        row = result["employees"][0]
        self.assertEqual(row["employee"], "EMP-1")
        self.assertEqual(row["customer"], "CUST-1")
        self.assertEqual(row["advance_outstanding"], 300.0)
        self.assertEqual(row["order_outstanding"], 150.0)
        self.assertEqual(row["total_outstanding"], 450.0)
        self.assertEqual(row["advance_count"], 1)
        self.assertEqual(row["order_count"], 1)
        self.assertEqual(result["summary"]["total_outstanding"], 450.0)
        self.assertEqual(result["summary"]["employee_count"], 1)

    def test_employees_are_sorted_by_total_outstanding_desc(self):
        site = _Site(invoices=[
            _invoice("SINV-1", "CUST-A", 50.0),
            _invoice("SINV-2", "CUST-B", 900.0),
            _invoice("SINV-3", "CUST-C", 400.0),
        ])
        result, _mock = _run(site, employees_for_customers={})
        totals = [r["total_outstanding"] for r in result["employees"]]
        self.assertEqual(totals, [900.0, 400.0, 50.0])


class TestEmployeeLedgerOutstandingIsAllTime(unittest.TestCase):
    """The window says what is LISTED. It must never limit what is OWED.

    This is the whole reason the endpoint runs two passes. Under a windowed
    total, the older and more delinquent a debt is the more certainly it
    disappears from a figure the screen labels "total outstanding" — which is
    exactly backwards, and is the number a manager acts on.
    """

    def test_unpaid_order_outside_the_window_still_counts(self):
        site = _Site(
            invoices=[],  # nothing was invoiced in the last 90 days
            open_invoices=[_invoice("SINV-MARCH", "CUST-1", 300.0, customer_name="Mona Staff")],
        )
        result, _mock = _run(site, employees_for_customers={})

        self.assertEqual(result["orders"], [], "nothing to LIST in this window")
        self.assertEqual(result["summary"]["order_outstanding"], 300.0)
        self.assertEqual(result["summary"]["total_outstanding"], 300.0)
        self.assertTrue(result["summary"]["outstanding_is_all_time"])

    def test_advance_outside_the_window_still_counts(self):
        site = _Site(
            advances=[],
            open_advances=[_advance("EA-JAN", "EMP-1", paid=500.0, claimed=100.0)],
            employees=[{"name": "EMP-1", "employee_name": "Ali Staff",
                        "branch": "", "user_id": ""}],
        )
        result, _mock = _run(site)

        self.assertEqual(result["advances"], [])
        self.assertEqual(result["summary"]["advance_outstanding"], 400.0)
        self.assertEqual(result["summary"]["total_outstanding"], 400.0)

    def test_person_with_a_balance_but_no_listed_rows_is_still_present(self):
        # The rollup is driven by what is OWED, not by what is listed. Filtering
        # this person out would hide the oldest debts from the per-person table
        # while still counting them in the headline — the worst of both.
        site = _Site(
            advances=[],
            invoices=[],
            open_advances=[_advance("EA-JAN", "EMP-1", paid=500.0)],
            open_invoices=[_invoice("SINV-MARCH", "CUST-1", 300.0)],
            employees=[{"name": "EMP-1", "employee_name": "Ali Staff",
                        "branch": "", "user_id": ""}],
        )
        result, _mock = _run(
            site,
            employees_for_customers={"CUST-1": "EMP-1"},
            customers_for_employees={"EMP-1": "CUST-1"},
        )

        self.assertEqual(result["advances"], [])
        self.assertEqual(result["orders"], [])
        self.assertEqual(len(result["employees"]), 1)

        row = result["employees"][0]
        self.assertEqual(row["employee"], "EMP-1")
        self.assertEqual(row["employee_name"], "Ali Staff")
        self.assertEqual(row["advance_outstanding"], 500.0)
        self.assertEqual(row["order_outstanding"], 300.0)
        self.assertEqual(row["total_outstanding"], 800.0)
        # Counts describe the WINDOW, so zero here is correct, not a bug.
        self.assertEqual(row["advance_count"], 0)
        self.assertEqual(row["order_count"], 0)
        self.assertEqual(result["summary"]["employee_count"], 1)

    def test_money_inside_the_window_is_counted_once_not_twice(self):
        # Both passes see the same open invoice. The listing pass must add
        # counts only — if it also added money, everything recent would double.
        site = _Site(invoices=[_invoice("SINV-1", "CUST-1", 150.0)])
        result, _mock = _run(site, employees_for_customers={})

        self.assertEqual(result["summary"]["order_outstanding"], 150.0)
        self.assertEqual(result["employees"][0]["order_outstanding"], 150.0)
        self.assertEqual(result["employees"][0]["order_count"], 1)

    def test_activity_with_no_balance_is_listed_but_not_counted_as_a_debtor(self):
        # A staff order paid at the till: the person stays visible with a zero
        # balance, but employee_count answers "how many owe us money".
        site = _Site(
            invoices=[_invoice("SINV-PAID", "CUST-1", 0.0, grand_total=180.0, status="Paid")],
            open_invoices=[],
        )
        result, _mock = _run(site, employees_for_customers={})

        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(len(result["employees"]), 1)
        self.assertEqual(result["employees"][0]["total_outstanding"], 0.0)
        self.assertEqual(result["employees"][0]["order_count"], 1)
        self.assertEqual(result["summary"]["employee_count"], 0)
        self.assertEqual(result["summary"]["total_outstanding"], 0.0)

    def test_outstanding_is_all_time_is_present_on_every_response(self):
        populated, _mock = _run(_Site(invoices=[_invoice("SINV-1", "CUST-1", 10.0)]),
                                employees_for_customers={})
        no_branch, _mock = _run(_Site(), profiles=())
        wrong_branch, _mock = _run(_Site(), profiles=("Dokki",), branch="Nasr city")

        for payload in (populated, no_branch, wrong_branch):
            self.assertIs(payload["summary"]["outstanding_is_all_time"], True)


class TestEmployeeLedgerBranchScoping(unittest.TestCase):
    """Managers are branch-scoped like everyone else; only Administrator is not."""

    def test_both_order_queries_are_filtered_to_the_assigned_profiles(self):
        # BOTH passes, deliberately: "all-time" widens the date range, and must
        # never quietly widen whose money this user can see.
        site = _Site(invoices=[])
        _result, _mock = _run(site, profiles=("Dokki",))
        for windowed in (True, False):
            self.assertEqual(
                site.filters_for("Sales Invoice", windowed=windowed)["custom_kanban_profile"],
                ["in", ["Dokki"]],
                f"windowed={windowed} query lost its branch scope",
            )

    def test_explicit_branch_narrows_both_queries(self):
        site = _Site(invoices=[])
        _result, _mock = _run(site, profiles=("Dokki", "Nasr city"), branch="Nasr city")
        for windowed in (True, False):
            self.assertEqual(
                site.filters_for("Sales Invoice", windowed=windowed)["custom_kanban_profile"],
                ["in", ["Nasr city"]],
            )

    def test_all_time_order_query_asks_only_for_open_invoices(self):
        # The balance query is uncapped, so it has to be narrow: paid staff
        # orders are history and must not be dragged into it.
        site = _Site(invoices=[])
        _result, _mock = _run(site)
        self.assertEqual(
            site.filters_for("Sales Invoice", windowed=False)["outstanding_amount"], ["!=", 0]
        )

    def test_all_time_advance_from_another_branch_is_excluded_from_the_balance(self):
        # Same branch rule as the listing pass — proven against the balance,
        # which is the figure that would leak.
        site = _Site(
            open_advances=[_advance("EA-OLD", "EMP-2", paid=300.0)],
            employees=[{"name": "EMP-2", "employee_name": "Other Branch",
                        "branch": "Nasr city", "user_id": ""}],
            pos_profiles=["Dokki", "Nasr city"],
        )
        result, _mock = _run(site, profiles=("Dokki",))
        self.assertEqual(result["summary"]["advance_outstanding"], 0.0)
        self.assertEqual(result["employees"], [])

    def test_unassigned_branch_returns_an_empty_ledger_with_a_reason(self):
        site = _Site(invoices=[_invoice("SINV-1", "CUST-1", 100.0)])
        result, _mock = _run(site, profiles=("Dokki",), branch="Nasr city")

        self.assertTrue(result["success"])
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["summary"]["total_outstanding"], 0.0)
        self.assertEqual(result["notice_code"], "branch_not_permitted")
        # Nothing was read; the refusal happens before any query.
        self.assertNotIn("Sales Invoice", site.queried)

    def test_no_branch_assigned_says_so_instead_of_looking_empty(self):
        site = _Site()
        result, _mock = _run(site, profiles=())

        self.assertTrue(result["success"])
        self.assertEqual(result["employees"], [])
        self.assertEqual(result["notice_code"], "no_branch_assigned")
        self.assertTrue(result["notice"])

    def test_advance_of_an_employee_in_another_branch_is_excluded(self):
        # Employee Advance carries no POS Profile, so the employee's own branch
        # is the only attribution available. When it names a branch this manager
        # does not run, the advance is not theirs to see.
        site = _Site(
            advances=[_advance("EA-1", "EMP-2", paid=300.0)],
            employees=[{"name": "EMP-2", "employee_name": "Other Branch",
                        "branch": "Nasr city", "user_id": ""}],
            pos_profiles=["Dokki", "Nasr city"],
        )
        result, _mock = _run(site, profiles=("Dokki",))

        self.assertEqual(result["advances"], [])
        self.assertEqual(result["summary"]["advance_outstanding"], 0.0)

    def test_advance_of_an_employee_with_no_branch_is_kept(self):
        # Employee.branch is blank for most staff on this site. Filtering those
        # out would empty the ledger of exactly the money worth chasing.
        site = _Site(
            advances=[_advance("EA-1", "EMP-3", paid=300.0)],
            employees=[{"name": "EMP-3", "employee_name": "No Branch",
                        "branch": "", "user_id": ""}],
            pos_profiles=["Dokki", "Nasr city"],
        )
        result, _mock = _run(site, profiles=("Dokki",))

        self.assertEqual(len(result["advances"]), 1)
        self.assertEqual(result["summary"]["advance_outstanding"], 300.0)


class TestEmployeeLedgerContract(unittest.TestCase):
    """Payload shape, echoed filters, and the truncation announcement."""

    def test_payload_is_flat_with_every_section_present(self):
        site = _Site()
        result, _mock = _run(site)
        for key in ("success", "hrms_available", "filters", "summary",
                    "employees", "advances", "orders"):
            self.assertIn(key, result)
        for key in ("advance_outstanding", "order_outstanding", "total_outstanding",
                    "advance_count", "order_count", "employee_count", "currency",
                    "outstanding_is_all_time"):
            self.assertIn(key, result["summary"])

    def test_filters_are_echoed_as_strings(self):
        site = _Site()
        result, _mock = _run(site, branch="Dokki", employee="EMP-1")
        self.assertEqual(
            result["filters"],
            {"from_date": FROM_DATE, "to_date": TO_DATE, "branch": "Dokki", "employee": "EMP-1"},
        )

    def test_dates_default_to_a_ninety_day_window_not_to_today(self):
        # A one-day default on a balance view hides the six-week-old advance the
        # screen exists to surface. The window is asserted exactly (add_days and
        # getdate are the REAL ones here — only nowdate is pinned), so shrinking
        # it back to "today" or to 30 days cannot pass quietly.
        site = _Site()
        result, _mock = _run(site, from_date=None, to_date=None)
        self.assertEqual(result["filters"]["to_date"], TODAY)
        self.assertEqual(result["filters"]["from_date"], DEFAULT_FROM_DATE)

    def test_reversed_dates_are_rejected(self):
        # Asserted through the mocked frappe.throw rather than a bare
        # assertRaises(Exception): this case passed VACUOUSLY while the harness
        # was blowing up in nowdate(), because an AttributeError is an Exception
        # too. Counting the throw pins that OUR guard is what fired, and does not
        # depend on the message surviving translation.
        site = _Site()
        mock = _mock_frappe(site)
        with self.assertRaises(Exception) as ctx:
            _run(site, mock=mock, from_date=TO_DATE, to_date=FROM_DATE)

        self.assertNotIsInstance(ctx.exception, AttributeError)
        self.assertEqual(mock.throw.call_count, 1)
        # The refusal happens before any data is read.
        self.assertEqual(site.queried, [])

    def test_truncation_is_announced_rather_than_silent(self):
        site = _Site(invoices=[
            _invoice("SINV-1", "CUST-1", 10.0),
            _invoice("SINV-2", "CUST-2", 10.0),
            _invoice("SINV-3", "CUST-3", 10.0),
        ])
        result, _mock = _run(site, employees_for_customers={}, limit=2)

        self.assertEqual(len(result["orders"]), 2)
        self.assertEqual(result["notice_code"], "results_truncated")
        self.assertTrue(result["notice"])

    def test_limit_is_capped_at_the_house_maximum(self):
        # Same cap idiom as the rest of api/manager.py: max(1, min(limit, 500)).
        # The query asks for one row MORE than the cap, which is how truncation
        # is detected rather than guessed from a full page.
        site = _Site()
        _result, _mock = _run(site, limit=99999)
        self.assertEqual(
            site.query("Sales Invoice", windowed=True)["limit_page_length"], 501
        )

    def test_garbage_limit_falls_back_to_the_default(self):
        site = _Site()
        _result, _mock = _run(site, limit="not-a-number")
        self.assertEqual(
            site.query("Sales Invoice", windowed=True)["limit_page_length"], 201
        )

    def test_the_balance_queries_are_never_capped(self):
        # Truncating a list loses rows; truncating a balance states the wrong
        # amount of money. The cap must reach the feed only.
        site = _Site()
        _result, _mock = _run(site, limit=1)
        self.assertEqual(site.query("Sales Invoice", windowed=False)["limit_page_length"], 0)
        self.assertEqual(site.query("Employee Advance", windowed=False)["limit_page_length"], 0)

    def test_role_gate_is_the_first_thing_that_runs(self):
        # Deliberately NOT wrapped in _ledger_env: only `frappe` is replaced, so
        # nothing else the endpoint would touch is pinned. That is the assertion.
        # If the gate ever moves below the date handling, this fails — with an
        # AttributeError from the real nowdate() bench-less, or by returning a
        # payload instead of raising on a live site. Either way it cannot pass.
        site = _Site(invoices=[_invoice("SINV-1", "CUST-1", 10.0)])
        mock = _mock_frappe(site)
        mock.get_roles.return_value = ["Sales User"]
        with patch.object(manager, "frappe", mock):
            with self.assertRaises(PermissionError):
                manager.get_employee_ledger(from_date=FROM_DATE, to_date=TO_DATE)
        # Refused before any data was read.
        self.assertEqual(site.queried, [])
        self.assertEqual(mock.throw.call_count, 1)


if __name__ == "__main__":
    unittest.main()
