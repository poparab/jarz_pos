"""Tests for the employee cash-advance request/approve flow.

What is covered here, and why each case earns its place:

  * **Role gating.** Requesting is ``ROLES.LINE_MANAGER_TIER``; approving is
    ``ROLES.ADMIN | {JARZ Manager}``. These are two DIFFERENT sets on purpose —
    the person who asks for cash must not be the person who releases it — and
    the failure mode of getting them wrong is silent: a line manager who can
    approve their own request, or a JARZ Manager locked out of a queue they own.
    Both spellings of the line-manager role are exercised, because both exist as
    real Role records on staging and production and a set carrying only one
    silently excludes half the people holding it.
  * **Graceful degradation without HRMS.** ``hooks.py`` declares no
    ``required_apps``, so the bootstrap has to answer, not throw, on a bench with
    no ``Employee Advance`` DocType.
  * **Amount validation**, because a zero or negative advance is a Payment Entry
    that moves money the wrong way.
  * **The non-draft guard on approve**, which is the only thing between one
    approval and two payouts for the same advance.
  * **The serializer's balance arithmetic**, which is the number a manager reads
    to decide whether an employee still owes money.

Deliberately mock/unittest rather than ``FrappeTestCase``: on ERPNext v16
``FrappeTestCase`` pulls in ``erpnext.tests.utils``, whose module-level
``BootStrapTestData()`` collides with the populated master data on the site CI
runs against. Same reasoning as ``tests/test_commercial_policy.py``.

NOT covered here, and deliberately so: the Payment Entry leg of
``approve_employee_advance``. Building it exercises HRMS's own controller, the
``advance_payment_payable_doctypes`` hook and real GL — mocking that would assert
only that the mocks were called, which is exactly the false green this suite has
been bitten by before. It needs a staging run against real data.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.constants import ROLES


MODULE = "jarz_pos.api.employee_advances"


def _raise_frappe(message, exc=None, title=None):
    """Stand-in for ``frappe.throw``: honours the exception class when given one."""
    if exc and isinstance(exc, type) and issubclass(exc, Exception):
        raise exc(message)
    raise ValueError(message)


def _mock_frappe(roles, user="someone@example.com"):
    """A ``frappe`` double whose ``throw`` actually throws.

    A bare ``MagicMock`` returns a mock from ``frappe.throw`` and keeps going,
    which would make every permission test pass regardless of the gate.
    """
    mock = MagicMock()
    mock.session.user = user
    mock.get_roles.return_value = list(roles)
    mock.PermissionError = PermissionError
    mock.throw.side_effect = _raise_frappe
    return mock


def _flt(value, precision=None):
    """Deterministic stand-in for ``frappe.utils.flt``. Patched in by name.

    This is NOT belt-and-braces — without it every money assertion in this file
    silently reads 0.0, and the module under test rejects a valid 1500 EGP
    request with "Amount must be greater than zero."

    Why: ``api/employee_advances`` binds ``flt`` at import
    (``from frappe.utils import flt``), so patching the ``frappe`` module object
    does not reach it — the REAL ``flt`` runs. And the real ``flt(x, 2)`` calls
    ``rounded()``, whose first statement is
    ``frappe.get_system_settings("rounding_method")``
    (frappe/utils/data.py:1244). Run without a site — which is how this module
    is executed via ``env/bin/python -m unittest`` — that read raises, and
    ``flt``'s own ``except Exception`` swallows it and returns 0.0
    (frappe/utils/data.py:1155-1158). Every currency value collapses to zero
    with no error anywhere.

    That failure mode belongs to the harness, not to the app: production always
    has a database. So the double is patched over ``flt`` by name, which also
    makes these assertions independent of the site's rounding-method setting.
    It agrees with the real ``flt`` on every value used below; the tests
    deliberately avoid exact ``.xx5`` midpoints, where the two rounding families
    legitimately differ.
    """
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.replace(",", "")
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(num, precision) if precision is not None else num


class _FakeAdvanceDoc:
    """Minimal ``Employee Advance`` stub: attribute, ``get``/``set`` and doc API."""

    def __init__(self, **data):
        self._data = dict(data)
        self.flags = SimpleNamespace(ignore_permissions=False)
        self.insert = MagicMock()
        self.submit = MagicMock()
        self.cancel = MagicMock()
        self.reload = MagicMock()
        self.db_set = MagicMock()
        self.add_comment = MagicMock()

    # -- doc-like access ---------------------------------------------------
    def __getattr__(self, key):
        # Only reached when normal attribute lookup fails, and ``_data`` is set
        # first in __init__, so this cannot recurse.
        if key in self._data:
            return self._data[key]
        raise AttributeError(key)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def as_dict(self):
        return dict(self._data)


# ─────────────────────────────────────────────────────────────────────────────
# Role gating
# ─────────────────────────────────────────────────────────────────────────────


class TestEmployeeAdvanceRoleSets(unittest.TestCase):
    """The two gates are different sets, and both line-manager spellings count."""

    def test_line_manager_tier_may_request_but_not_approve(self):
        from jarz_pos.api import employee_advances as ea

        for role in (ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT):
            with self.subTest(role=role):
                self.assertIn(role, ea.REQUEST_ROLES, "line manager must be able to request")
                self.assertNotIn(
                    role,
                    ea.APPROVE_ROLES,
                    "a line manager approving their own request defeats the whole flow",
                )

    def test_jarz_manager_may_do_both(self):
        from jarz_pos.api import employee_advances as ea

        self.assertIn(ROLES.JARZ_MANAGER, ea.REQUEST_ROLES)
        self.assertIn(ROLES.JARZ_MANAGER, ea.APPROVE_ROLES)

    def test_plain_staff_is_in_neither_set(self):
        from jarz_pos.api import employee_advances as ea

        for role in ("Jarz POS Staff", "POS User", "Sales User", "Employee"):
            with self.subTest(role=role):
                self.assertNotIn(role, ea.REQUEST_ROLES)
                self.assertNotIn(role, ea.APPROVE_ROLES)


class TestRequestGate(unittest.TestCase):
    def test_line_manager_is_allowed_to_request(self):
        """A line manager gets a DRAFT advance — created, never submitted, never paid."""
        from jarz_pos.api.employee_advances import (
            F_PAYING_ACCOUNT,
            F_POS_PROFILE,
            F_REQUESTED_BY,
            create_employee_advance_request,
        )

        captured = {}
        fake_doc = _FakeAdvanceDoc(name="HR-EAD-2026-00001")

        def _get_doc(doc_dict):
            captured.update(doc_dict)
            return fake_doc

        mock_frappe = _mock_frappe([ROLES.JARZ_LINE_MANAGER], user="line@example.com")
        mock_frappe.get_doc.side_effect = _get_doc

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.flt", _flt), \
                patch(f"{MODULE}.hrms_available", return_value=True), \
                patch(f"{MODULE}._advance_has_field", return_value=True), \
                patch(
                    f"{MODULE}._validate_employee",
                    return_value={
                        "name": "HR-EMP-00001",
                        "employee_name": "Ahmed",
                        "status": "Active",
                        "company": "Jarz",
                        "salary_currency": "EGP",
                    },
                ), \
                patch(f"{MODULE}._validate_paying_account", return_value={"name": "Dokki - J"}), \
                patch(f"{MODULE}._serialize_one", return_value={"name": "HR-EAD-2026-00001"}):
            result = create_employee_advance_request(
                employee="HR-EMP-00001",
                amount=1500,
                purpose="Petty cash float",
                paying_account="Dokki - J",
                pos_profile="",
                posting_date="2026-08-28",
            )

        self.assertTrue(result.get("success"))
        self.assertEqual(captured["doctype"], "Employee Advance")
        self.assertEqual(captured["advance_amount"], 1500.0)
        self.assertEqual(captured["currency"], "EGP")
        self.assertEqual(captured["company"], "Jarz")
        # status is read-only and derived by EmployeeAdvance.set_status(); writing
        # it by hand produces a row whose status disagrees with its own amounts.
        self.assertNotIn("status", captured)

        self.assertEqual(fake_doc.get(F_PAYING_ACCOUNT), "Dokki - J")
        self.assertIsNone(fake_doc.get(F_POS_PROFILE))
        self.assertEqual(fake_doc.get(F_REQUESTED_BY), "line@example.com")

        fake_doc.insert.assert_called_once()
        # The request must NOT submit and must NOT pay. Both are the approver's job.
        fake_doc.submit.assert_not_called()

    def test_plain_staff_is_denied(self):
        from jarz_pos.api.employee_advances import create_employee_advance_request

        mock_frappe = _mock_frappe(["Jarz POS Staff"], user="staff@example.com")

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=True):
            with self.assertRaises(PermissionError):
                create_employee_advance_request(
                    employee="HR-EMP-00001",
                    amount=1500,
                    purpose="Petty cash float",
                    paying_account="Dokki - J",
                )

        mock_frappe.get_doc.assert_not_called()


class TestApproveGate(unittest.TestCase):
    def test_line_manager_may_not_approve(self):
        """The requester tier must not be able to release the cash."""
        from jarz_pos.api.employee_advances import approve_employee_advance

        for role in (ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT):
            with self.subTest(role=role):
                mock_frappe = _mock_frappe([role], user="line@example.com")
                with patch(f"{MODULE}.frappe", mock_frappe), \
                        patch(f"{MODULE}.hrms_available", return_value=True):
                    with self.assertRaises(PermissionError):
                        approve_employee_advance("HR-EAD-2026-00001")
                mock_frappe.get_doc.assert_not_called()

    def test_jarz_manager_passes_the_gate(self):
        """A JARZ Manager gets past the permission check and on to the doc.

        Asserted by the exception it *does* raise: the advance handed to it is
        already submitted, so the non-draft guard fires. A PermissionError here
        would mean the gate rejected a manager.
        """
        from jarz_pos.api.employee_advances import approve_employee_advance

        submitted = _FakeAdvanceDoc(
            name="HR-EAD-2026-00001", docstatus=1, status="Paid", company="Jarz"
        )
        mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")
        mock_frappe.get_doc.return_value = submitted

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                approve_employee_advance("HR-EAD-2026-00001")

        self.assertIn("draft", str(ctx.exception).lower())
        submitted.submit.assert_not_called()

    def test_already_paid_advance_is_not_approved_twice(self):
        """The non-draft guard is the only thing between one approval and two payouts."""
        from jarz_pos.api.employee_advances import approve_employee_advance

        for docstatus, status in ((1, "Paid"), (2, "Cancelled")):
            with self.subTest(docstatus=docstatus):
                doc = _FakeAdvanceDoc(
                    name="HR-EAD-2026-00002",
                    docstatus=docstatus,
                    status=status,
                    company="Jarz",
                )
                mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")
                mock_frappe.get_doc.return_value = doc

                with patch(f"{MODULE}.frappe", mock_frappe), \
                        patch(f"{MODULE}.hrms_available", return_value=True):
                    with self.assertRaises(ValueError):
                        approve_employee_advance("HR-EAD-2026-00002")

                doc.submit.assert_not_called()

    def test_reject_is_gated_to_approvers(self):
        from jarz_pos.api.employee_advances import reject_employee_advance

        mock_frappe = _mock_frappe([ROLES.JARZ_LINE_MANAGER], user="line@example.com")
        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=True):
            with self.assertRaises(PermissionError):
                reject_employee_advance("HR-EAD-2026-00001", "Not budgeted")

        mock_frappe.get_doc.assert_not_called()
        mock_frappe.delete_doc.assert_not_called()


class TestReject(unittest.TestCase):
    def test_rejecting_a_draft_records_the_reason_before_deleting_it(self):
        """``purpose`` survives into the Deleted Document snapshot; a Comment does not."""
        from jarz_pos.api.employee_advances import reject_employee_advance

        draft = _FakeAdvanceDoc(
            name="HR-EAD-2026-00003", docstatus=0, status="Draft", purpose="Petty cash float"
        )
        mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")
        mock_frappe.get_doc.return_value = draft

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=True), \
                patch(f"{MODULE}.now_datetime", return_value="2026-08-28 12:00:00"):
            result = reject_employee_advance("HR-EAD-2026-00003", "Not budgeted this month")

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "deleted")

        draft.db_set.assert_called_once()
        annotated = draft.db_set.call_args[0][1]
        self.assertIn("Petty cash float", annotated, "the original purpose must not be lost")
        self.assertIn("Not budgeted this month", annotated)
        self.assertIn("manager@example.com", annotated)

        mock_frappe.delete_doc.assert_called_once()

    def test_reject_requires_a_reason(self):
        from jarz_pos.api.employee_advances import reject_employee_advance

        mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")
        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                reject_employee_advance("HR-EAD-2026-00003", "   ")

        self.assertIn("reason", str(ctx.exception).lower())
        mock_frappe.get_doc.assert_not_called()

    def test_reject_refuses_an_advance_that_was_already_paid(self):
        """Cancelling a paid advance would strand the payout in the ledger.

        HRMS's on_cancel silently UNLINKS the Payment Entry when
        ``HR Settings.unlink_payment_on_cancellation_of_employee_advance`` is on,
        so without this guard the cash would be gone from the drawer with no
        document justifying it.
        """
        from jarz_pos.api.employee_advances import reject_employee_advance

        submitted = _FakeAdvanceDoc(
            name="HR-EAD-2026-00004", docstatus=1, status="Paid", purpose="Float"
        )
        mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")
        mock_frappe.get_doc.return_value = submitted

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=True), \
                patch(f"{MODULE}.now_datetime", return_value="2026-08-28 12:00:00"), \
                patch(f"{MODULE}._linked_payment_entries", return_value=["ACC-PAY-2026-00042"]):
            with self.assertRaises(ValueError) as ctx:
                reject_employee_advance("HR-EAD-2026-00004", "Changed our mind")

        self.assertIn("ACC-PAY-2026-00042", str(ctx.exception))
        submitted.cancel.assert_not_called()
        mock_frappe.delete_doc.assert_not_called()

    def test_reject_cancels_a_submitted_but_unpaid_advance(self):
        from jarz_pos.api.employee_advances import reject_employee_advance

        submitted = _FakeAdvanceDoc(
            name="HR-EAD-2026-00005", docstatus=1, status="Unpaid", purpose="Float"
        )
        mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")
        mock_frappe.get_doc.return_value = submitted

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=True), \
                patch(f"{MODULE}.now_datetime", return_value="2026-08-28 12:00:00"), \
                patch(f"{MODULE}._linked_payment_entries", return_value=[]), \
                patch(f"{MODULE}._serialize_one", return_value={"name": "HR-EAD-2026-00005"}):
            result = reject_employee_advance("HR-EAD-2026-00005", "Duplicate request")

        self.assertEqual(result["action"], "cancelled")
        submitted.cancel.assert_called_once()
        mock_frappe.delete_doc.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


class TestRequestValidation(unittest.TestCase):
    def _attempt(self, **overrides):
        from jarz_pos.api.employee_advances import create_employee_advance_request

        payload = {
            "employee": "HR-EMP-00001",
            "amount": 1500,
            "purpose": "Petty cash float",
            "paying_account": "Dokki - J",
            "posting_date": "2026-08-28",
        }
        payload.update(overrides)

        mock_frappe = _mock_frappe([ROLES.JARZ_LINE_MANAGER], user="line@example.com")
        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.flt", _flt), \
                patch(f"{MODULE}.hrms_available", return_value=True), \
                patch(f"{MODULE}._advance_has_field", return_value=True), \
                patch(
                    f"{MODULE}._validate_employee",
                    return_value={"company": "Jarz", "salary_currency": "EGP"},
                ), \
                patch(f"{MODULE}._validate_paying_account", return_value={}), \
                patch(f"{MODULE}._serialize_one", return_value={}):
            create_employee_advance_request(**payload)

    def test_zero_amount_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._attempt(amount=0)
        self.assertIn("greater than zero", str(ctx.exception))

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._attempt(amount=-250)
        self.assertIn("greater than zero", str(ctx.exception))

    def test_missing_employee_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._attempt(employee="")
        self.assertIn("Employee", str(ctx.exception))

    def test_missing_purpose_is_rejected(self):
        """A missing purpose must be answered with "Purpose", not with the amount.

        The regression this pins: a chain of short-circuiting guards reports
        whichever field it happens to check FIRST, so a request whose only
        problem was an empty purpose came back as "Amount must be greater than
        zero" — sending the manager to fix a box that was already correct.
        """
        with self.assertRaises(ValueError) as ctx:
            self._attempt(purpose="   ")
        message = str(ctx.exception)
        self.assertIn("Purpose", message)
        self.assertNotIn("Amount", message)

    def test_missing_paying_account_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._attempt(paying_account="")
        message = str(ctx.exception)
        self.assertIn("Paying account", message)
        self.assertNotIn("Amount", message)

    def test_a_missing_amount_says_so_rather_than_saying_it_is_too_small(self):
        with self.assertRaises(ValueError) as ctx:
            self._attempt(amount=None)
        message = str(ctx.exception)
        self.assertIn("Amount", message)
        self.assertNotIn("greater than zero", message)

    def test_every_missing_field_is_named_in_one_reply(self):
        """One round trip, not one per empty box."""
        with self.assertRaises(ValueError) as ctx:
            self._attempt(employee="", purpose="", paying_account="", amount="")
        message = str(ctx.exception)
        for field in ("Employee", "Amount", "Purpose", "Paying account"):
            with self.subTest(field=field):
                self.assertIn(field, message)

    def test_request_refuses_when_the_jarz_column_has_not_migrated(self):
        """Better one clear error than a queue of unapprovable rows.

        Without ``custom_jarz_paying_account`` the approve step has no idea which
        drawer to pay out of, so a request created in that state could never be
        approved. Deliberately called with an EMPTY purpose as well: the schema
        guard must win over the field guards, because no amount of editing the
        form fixes a column that is not there — the action is a deploy.
        """
        from jarz_pos.api.employee_advances import create_employee_advance_request

        mock_frappe = _mock_frappe([ROLES.JARZ_LINE_MANAGER], user="line@example.com")
        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.flt", _flt), \
                patch(f"{MODULE}.hrms_available", return_value=True), \
                patch(f"{MODULE}._advance_has_field", return_value=False):
            with self.assertRaises(ValueError) as ctx:
                create_employee_advance_request(
                    employee="HR-EMP-00001",
                    amount=1500,
                    purpose="",
                    paying_account="Dokki - J",
                )

        message = str(ctx.exception)
        self.assertIn("bench migrate", message)
        self.assertNotIn("Purpose", message)
        mock_frappe.get_doc.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Graceful degradation
# ─────────────────────────────────────────────────────────────────────────────


class TestHrmsAbsent(unittest.TestCase):
    def test_bootstrap_answers_instead_of_throwing(self):
        from datetime import date

        from jarz_pos.api.employee_advances import get_employee_advance_bootstrap

        mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=False), \
                patch(f"{MODULE}.getdate", return_value=date(2026, 8, 28)):
            result = get_employee_advance_bootstrap()

        self.assertTrue(result["success"], "absence of HRMS is a normal answer, not an error")
        self.assertFalse(result["hrms_available"])
        self.assertTrue(result["notice"], "the client needs a string to render as an empty state")
        self.assertEqual(result["advances"], [])
        self.assertEqual(result["employees"], [])
        self.assertEqual(result["payment_sources"], [])
        self.assertEqual(result["months"], [])
        self.assertEqual(result["current_month"], "2026-08")
        self.assertEqual(result["summary"]["pending_count"], 0)
        self.assertEqual(result["summary"]["outstanding_amount"], 0.0)
        # The role flags still have to be truthful: the client uses them to decide
        # which buttons to render, and "HRMS missing" is not "you lost the role".
        self.assertTrue(result["can_request"])
        self.assertTrue(result["can_approve"])

    def test_write_paths_refuse_clearly(self):
        from jarz_pos.api.employee_advances import (
            approve_employee_advance,
            create_employee_advance_request,
            reject_employee_advance,
        )

        mock_frappe = _mock_frappe([ROLES.JARZ_MANAGER], user="manager@example.com")

        with patch(f"{MODULE}.frappe", mock_frappe), \
                patch(f"{MODULE}.hrms_available", return_value=False):
            for call in (
                lambda: create_employee_advance_request(
                    employee="HR-EMP-00001",
                    amount=100,
                    purpose="x",
                    paying_account="Dokki - J",
                ),
                lambda: approve_employee_advance("HR-EAD-2026-00001"),
                lambda: reject_employee_advance("HR-EAD-2026-00001", "no"),
            ):
                with self.assertRaises(ValueError) as ctx:
                    call()
                self.assertIn("HRMS", str(ctx.exception))

        mock_frappe.get_doc.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────────────────


class TestAdvanceSerializer(unittest.TestCase):
    """``_serialize_advance`` is pure once its lookup maps and ``flt`` are supplied."""

    ROW = {
        "name": "HR-EAD-2026-00001",
        "employee": "HR-EMP-00001",
        "employee_name": "Ahmed Fathy",
        "posting_date": "2026-08-28",
        "currency": "EGP",
        "advance_amount": 1000,
        "paid_amount": 1000,
        "claimed_amount": 250.50,
        "return_amount": 100,
        "purpose": "Petty cash float",
        "status": "Partially Paid",
        "docstatus": 1,
        "company": "Jarz",
        "creation": "2026-08-28 10:00:00",
        "modified": "2026-08-28 11:00:00",
        "custom_jarz_paying_account": "Dokki - J",
        "custom_jarz_pos_profile": "Dokki",
        "custom_jarz_requested_by": "line@example.com",
        "custom_jarz_approved_by": "manager@example.com",
        "custom_jarz_approved_on": "2026-08-28 11:00:00",
        "custom_jarz_payment_entry": "ACC-PAY-2026-00042",
    }

    def setUp(self):
        # Patched by NAME, not via the frappe mock: the module binds ``flt`` at
        # import, and the real one collapses every amount to 0.0 without a
        # database. See the _flt docstring — without this every money assertion
        # below reads 0.0 and looks like a serializer bug.
        patcher = patch(f"{MODULE}.flt", _flt)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _serialize(self, **overrides):
        from jarz_pos.api.employee_advances import _serialize_advance

        row = dict(self.ROW)
        row.update(overrides)
        return _serialize_advance(
            row,
            account_labels={"Dokki - J": {"label": "Dokki", "label_en": "Dokki", "label_ar": "الدقي"}},
            employee_meta={"HR-EMP-00001": {"employee_name": "Ahmed Fathy", "branch": "Dokki"}},
            user_names={"line@example.com": "Line Manager", "manager@example.com": "The Manager"},
        )

    def test_balance_is_paid_minus_claimed_and_returned(self):
        out = self._serialize()
        # 1000 paid, 250.50 claimed back via an Expense Claim, 100 returned
        # => 649.50 still sitting with the employee.
        self.assertEqual(out["balance"], 649.50)

    def test_balance_is_zero_before_anything_is_paid(self):
        out = self._serialize(paid_amount=0, claimed_amount=0, return_amount=0)
        self.assertEqual(out["balance"], 0.0)

    def test_balance_goes_negative_when_more_is_returned_than_paid(self):
        """Not clamped: a negative balance is a real data problem worth surfacing."""
        out = self._serialize(paid_amount=100, claimed_amount=0, return_amount=250)
        self.assertEqual(out["balance"], -150.0)

    def test_amount_maps_from_advance_amount(self):
        out = self._serialize()
        self.assertEqual(out["amount"], 1000.0)
        self.assertNotIn(
            "advance_amount", out, "the client must never see an HRMS fieldname"
        )

    def test_money_is_rounded_to_two_places(self):
        # Deliberately away from an exact .xx5 midpoint. The real ``flt`` applies
        # the site's rounding method (banker's rounding by default) while the
        # ``_flt`` double uses Python's ``round``; the two agree everywhere
        # EXCEPT at a midpoint. Staying off midpoints keeps the double honest
        # against the real thing and keeps this asserting the serializer's
        # contract — "two places" — rather than the site's System Settings.
        out = self._serialize(advance_amount=1000.567, paid_amount=333.333)
        self.assertEqual(out["amount"], 1000.57)
        self.assertEqual(out["paid_amount"], 333.33)

    def test_branch_comes_from_the_employee_and_pos_profile_from_the_advance(self):
        out = self._serialize()
        self.assertEqual(out["branch"], "Dokki")
        self.assertEqual(out["pos_profile"], "Dokki")

    def test_bilingual_payment_labels_are_preserved(self):
        """The Flutter client localises off the label/label_en/label_ar triple."""
        out = self._serialize()
        self.assertEqual(out["payment_label"], "Dokki")
        self.assertEqual(out["payment_label_en"], "Dokki")
        self.assertEqual(out["payment_label_ar"], "الدقي")

    def test_status_is_read_never_computed(self):
        out = self._serialize(status="Claimed")
        self.assertEqual(out["status"], "Claimed")

    def test_status_falls_back_to_docstatus_on_a_brand_new_doc(self):
        out = self._serialize(status="", docstatus=0)
        self.assertEqual(out["status"], "Draft")

    def test_shape_is_exactly_the_documented_contract(self):
        """The Flutter model is generated against this key set — additions are breaking."""
        out = self._serialize()
        self.assertEqual(
            set(out),
            {
                "name",
                "employee",
                "employee_name",
                "branch",
                "pos_profile",
                "posting_date",
                "currency",
                "amount",
                "paid_amount",
                "claimed_amount",
                "return_amount",
                "balance",
                "purpose",
                "status",
                "docstatus",
                "paying_account",
                "payment_label",
                "payment_label_en",
                "payment_label_ar",
                "requested_by",
                "requested_by_name",
                "approved_by",
                "approved_on",
                "payment_entry",
                "company",
                "creation",
                "modified",
            },
        )

    def test_dates_go_over_the_wire_as_strings(self):
        from datetime import date, datetime

        out = self._serialize(
            posting_date=date(2026, 8, 28),
            creation=datetime(2026, 8, 28, 10, 0, 0),
            custom_jarz_approved_on=datetime(2026, 8, 28, 11, 0, 0),
        )
        for key in ("posting_date", "creation", "approved_on", "modified"):
            with self.subTest(key=key):
                self.assertIsInstance(out[key], str)
        self.assertEqual(out["posting_date"], "2026-08-28")

    def test_requested_by_falls_back_to_owner_on_pre_migration_rows(self):
        row = dict(self.ROW)
        row.pop("custom_jarz_requested_by")
        row["owner"] = "legacy@example.com"

        from jarz_pos.api.employee_advances import _serialize_advance

        out = _serialize_advance(row)
        self.assertEqual(out["requested_by"], "legacy@example.com")


if __name__ == "__main__":
    unittest.main()
