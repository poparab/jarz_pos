"""Tests for the Jarz Employee Penalty controller.

The controller is exercised without a database: instances are built with
``__new__`` and the module's ``frappe`` handle is patched, the same shape
``jarz_pos/tests/test_doctype_jarz_sop.py`` uses. ``convert_penalty`` is a pure
module-level function and is tested directly, because it is the ONE definition
of the days <-> money conversion that the API, the controller and the app's live
preview all have to agree on.

What is deliberately pinned here:

* both directions are always stored — a Money penalty still yields
  ``equivalent_days``, a Days penalty still yields ``amount``;
* a zero ``day_rate`` on a Money penalty reports 0 days rather than raising;
* a *time* penalty with no ``day_rate`` is REFUSED, because storing it at 0 EGP
  produces a record that looks applied and deducts nothing;
* ``period_month`` is a queried key, so a malformed one is refused at entry
  rather than silently matching no month;
* a settled penalty cannot be cancelled — that money has already moved;
* the ``naming_series`` field is a Select with a default and is NOT read-only
  Data, which is the shape that broke every non-Desk insert of
  ``Jarz Recurring Expense``.
"""

import datetime
import json
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


# ── frappe stub, installed only when there is no bench ────────────────────


def _flt(value, precision=None):
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        num = 0.0
    return num if precision is None else round(num, precision)


def _getdate(value=None):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not value:
        return datetime.date(2026, 9, 12)
    return datetime.date.fromisoformat(str(value)[:10])


def _make_utils_stub():
    utils = types.ModuleType("frappe.utils")
    utils.flt = _flt
    utils.getdate = _getdate
    utils.today = lambda: "2026-09-12"
    utils.now_datetime = lambda: datetime.datetime(2026, 9, 12, 12, 0, 0)
    return utils


def _fake_throw(message, exc=Exception, **kwargs):
    raise exc(message)


def _ensure_document_base():
    """Make ``from frappe.model.document import Document`` importable."""
    try:
        import frappe.model.document  # noqa: F401

        return
    except Exception:
        pass

    import frappe

    class _Document:
        pass

    model = types.ModuleType("frappe.model")
    model.__path__ = []
    document = types.ModuleType("frappe.model.document")
    document.Document = _Document
    model.document = document
    frappe.model = model
    sys.modules["frappe.model"] = model
    sys.modules["frappe.model.document"] = document


# The stub is installed ADDITIVELY, exactly like jarz_pos/tests/test_purchase_setup.py:
# a plain "frappe is importable" check makes this module order-dependent, because
# whichever sibling test loaded first may own a partial fake that is missing the
# pieces the controller imports at module level.
try:  # pragma: no cover - depends on whether the runner has a bench
    import frappe as _frappe
except Exception:  # pragma: no cover
    _frappe = types.ModuleType("frappe")
    sys.modules["frappe"] = _frappe


def _ensure(name, value):
    if not hasattr(_frappe, name):
        setattr(_frappe, name, value)


_ensure("_", lambda message: message)
_ensure("throw", _fake_throw)
_ensure("whitelist", lambda *a, **k: (lambda fn: fn))
_ensure("session", SimpleNamespace(user="Administrator"))
_ensure(
    "defaults",
    SimpleNamespace(
        get_user_default=lambda *a, **k: None,
        get_global_default=lambda *a, **k: None,
    ),
)
_ensure(
    "db",
    SimpleNamespace(
        get_value=lambda *a, **k: None,
        exists=lambda *a, **k: None,
        add_index=lambda *a, **k: None,
    ),
)
_ensure("utils", _make_utils_stub())
sys.modules.setdefault("frappe.utils", _frappe.utils)
for _name, _value in (("flt", _flt), ("getdate", _getdate), ("today", lambda: "2026-09-12")):
    if not hasattr(_frappe.utils, _name):
        setattr(_frappe.utils, _name, _value)

_ensure_document_base()

from jarz_pos.doctype.jarz_employee_penalty import jarz_employee_penalty as module  # noqa: E402


HERE = os.path.dirname(os.path.abspath(__file__))
# This module lives in `jarz_pos/tests/`, not next to the DocType, because the
# CI runner in `.github/workflows/backend-tests.yml` invokes every module as
# `jarz_pos.tests.<name>`. A test placed in the DocType's own folder is never
# run there — the exact way 29 recurring-expense tests went unnoticed for a
# month. Resolve the schema off the module instead of off this file's folder.
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(module.__file__)), "jarz_employee_penalty.json"
)


class PenaltyRefused(Exception):
    """What the patched ``frappe.throw`` raises."""


def passthrough_translate():
    return patch.object(module, "_", new=lambda message: message)


def _raise_refused(message, *args, **kwargs):
    raise PenaltyRefused(str(message))


def patched_frappe():
    """``frappe`` patched so ``throw`` actually stops execution.

    A MagicMock ``throw`` returns a mock and lets the code carry on, which makes
    every "this is refused" test pass for the wrong reason.
    """
    ctx = patch.object(module, "frappe")
    mock = ctx.__enter__()
    mock.throw.side_effect = _raise_refused
    mock.db.get_value.return_value = "EGP"
    mock.defaults.get_user_default.return_value = "JARZ"
    mock.defaults.get_global_default.return_value = "EGP"
    return ctx, mock


def make_penalty(**fields):
    penalty = module.JarzEmployeePenalty.__new__(module.JarzEmployeePenalty)
    penalty.name = "JPEN-00001"
    penalty.naming_series = module.DEFAULT_NAMING_SERIES
    penalty.employee = "HR-EMP-00004"
    penalty.employee_name = "Belal"
    penalty.company = "JARZ"
    penalty.currency = "EGP"
    penalty.penalty_date = "2026-09-03"
    penalty.period_month = "2026-09"
    penalty.unit = module.UNIT_DAYS
    penalty.quantity = 1.0
    penalty.amount = 0.0
    penalty.day_rate = 300.0
    penalty.equivalent_days = 0.0
    penalty.reason = "Left the branch unattended"
    penalty.settled = 0
    penalty.settled_via = None
    for key, value in fields.items():
        setattr(penalty, key, value)
    return penalty


def run_validate(penalty):
    ctx, _mock = patched_frappe()
    try:
        with passthrough_translate():
            penalty.validate()
    finally:
        ctx.__exit__(None, None, None)
    return penalty


# ── the conversion, both directions ───────────────────────────────────────


class TestConvertPenalty(unittest.TestCase):
    def test_days_price_at_the_day_rate(self):
        self.assertEqual((600.0, 2.0), module.convert_penalty("Days", 2, None, 300))

    def test_half_days_price_at_half_the_day_rate(self):
        self.assertEqual((450.0, 1.5), module.convert_penalty("Half Days", 3, None, 300))

    def test_money_is_taken_as_given_and_expressed_in_days(self):
        self.assertEqual((600.0, 2.0), module.convert_penalty("Money", 0, 600, 300))

    def test_money_on_a_zero_day_rate_reports_zero_days_not_a_crash(self):
        # The employee has no Salary Structure Assignment, so the penalty cannot
        # be expressed in days. Saying "0 days" is honest; ZeroDivisionError is
        # a 500 on a manager pressing Save.
        self.assertEqual((600.0, 0.0), module.convert_penalty("Money", 0, 600, 0))

    def test_a_missing_unit_is_treated_as_days(self):
        self.assertEqual((300.0, 1.0), module.convert_penalty(None, 1, None, 300))

    def test_a_fractional_day_survives_the_round_trip(self):
        # Days is a Float, not an Int: half a day entered as Days must not be
        # truncated to 0 or promoted to 1.
        amount, days = module.convert_penalty("Days", 0.5, None, 300)
        self.assertEqual(150.0, amount)
        self.assertEqual(0.5, days)


# ── validate() stores BOTH directions ─────────────────────────────────────


class TestValidateStoresBothDirections(unittest.TestCase):
    def test_a_days_penalty_still_records_money(self):
        penalty = run_validate(
            make_penalty(unit="Days", quantity=2, amount=0, day_rate=300)
        )
        self.assertEqual(600.0, penalty.amount)
        self.assertEqual(2.0, penalty.equivalent_days)

    def test_a_half_day_penalty_records_half_the_rate_and_half_a_day(self):
        penalty = run_validate(
            make_penalty(unit="Half Days", quantity=1, amount=0, day_rate=300)
        )
        self.assertEqual(150.0, penalty.amount)
        self.assertEqual(0.5, penalty.equivalent_days)

    def test_a_money_penalty_still_records_days(self):
        penalty = run_validate(
            make_penalty(unit="Money", quantity=0, amount=600, day_rate=300)
        )
        self.assertEqual(600.0, penalty.amount)
        self.assertEqual(2.0, penalty.equivalent_days)

    def test_a_money_penalty_on_a_zero_day_rate_is_allowed_and_reports_zero_days(self):
        # An employee with no salary on file can still be fined in money — this
        # is the escape hatch the time units are refused in favour of.
        penalty = run_validate(
            make_penalty(unit="Money", quantity=0, amount=600, day_rate=0)
        )
        self.assertEqual(600.0, penalty.amount)
        self.assertEqual(0.0, penalty.equivalent_days)

    def test_a_money_penalty_clears_a_stale_quantity(self):
        penalty = run_validate(
            make_penalty(unit="Money", quantity=7, amount=600, day_rate=300)
        )
        self.assertEqual(0.0, penalty.quantity)


# ── refusals ──────────────────────────────────────────────────────────────


class TestRefusals(unittest.TestCase):
    def _refused(self, penalty):
        with self.assertRaises(PenaltyRefused) as caught:
            run_validate(penalty)
        return str(caught.exception)

    def test_days_with_no_day_rate_is_refused(self):
        # Never priced at zero: a 0 EGP penalty looks applied on every screen
        # and deducts nothing from the salary.
        message = self._refused(
            make_penalty(unit="Days", quantity=2, amount=0, day_rate=0)
        )
        self.assertIn("day rate", message)

    def test_half_days_with_no_day_rate_is_refused(self):
        self._refused(make_penalty(unit="Half Days", quantity=2, amount=0, day_rate=None))

    def test_days_with_no_quantity_is_refused(self):
        self._refused(make_penalty(unit="Days", quantity=0, amount=0, day_rate=300))

    def test_money_with_no_amount_is_refused(self):
        self._refused(make_penalty(unit="Money", quantity=0, amount=0, day_rate=300))

    def test_a_malformed_period_month_is_refused(self):
        for bad in ("2026-9", "2026-13", "Sept 2026", "2026/09", "26-09"):
            with self.subTest(period_month=bad):
                self._refused(make_penalty(period_month=bad))

    def test_a_well_formed_period_month_is_accepted(self):
        for good in ("2026-01", "2026-09", "2026-12", " 2026-09 "):
            with self.subTest(period_month=good):
                penalty = run_validate(make_penalty(period_month=good))
                self.assertEqual(good.strip(), penalty.period_month)

    def test_a_blank_period_month_falls_back_to_the_penalty_month(self):
        penalty = run_validate(make_penalty(period_month="", penalty_date="2026-08-31"))
        self.assertEqual("2026-08", penalty.period_month)

    def test_an_unknown_unit_is_refused(self):
        self._refused(make_penalty(unit="Weeks"))

    def test_a_penalty_with_no_reason_is_refused(self):
        # Not defensible to the employee, and not auditable three months later.
        self._refused(make_penalty(reason="   "))


# ── cancel ────────────────────────────────────────────────────────────────


class TestCancel(unittest.TestCase):
    def _cancel(self, penalty):
        ctx, _mock = patched_frappe()
        try:
            with passthrough_translate():
                penalty.on_cancel()
        finally:
            ctx.__exit__(None, None, None)

    def test_a_settled_penalty_cannot_be_cancelled(self):
        # The salary payment has already been reduced by it; cancelling here
        # leaves the employee short with nothing on record saying why.
        penalty = make_penalty(settled=1, settled_via="JEXP-00021")
        with self.assertRaises(PenaltyRefused) as caught:
            self._cancel(penalty)
        self.assertIn("JEXP-00021", str(caught.exception))

    def test_an_unsettled_penalty_cancels(self):
        self._cancel(make_penalty(settled=0))

    def test_settled_is_read_as_a_flag_not_a_truthy_string(self):
        penalty = make_penalty(settled="1", settled_via=None)
        with self.assertRaises(PenaltyRefused):
            self._cancel(penalty)


# ── naming series ─────────────────────────────────────────────────────────


class TestNamingSeries(unittest.TestCase):
    def test_before_insert_fills_a_blank_series(self):
        # API inserts and Data Import never apply the field's default, so
        # without this every non-Desk insert dies on "Naming Series mandatory".
        penalty = make_penalty(naming_series=None)
        ctx, _mock = patched_frappe()
        try:
            penalty.before_insert()
        finally:
            ctx.__exit__(None, None, None)
        self.assertEqual(module.DEFAULT_NAMING_SERIES, penalty.naming_series)

    def test_before_insert_does_not_overwrite_a_chosen_series(self):
        penalty = make_penalty(naming_series="JPEN-2026-.#####")
        ctx, _mock = patched_frappe()
        try:
            penalty.before_insert()
        finally:
            ctx.__exit__(None, None, None)
        self.assertEqual("JPEN-2026-.#####", penalty.naming_series)


# ── schema fences ─────────────────────────────────────────────────────────


class TestSchema(unittest.TestCase):
    """Properties of the JSON that the Python cannot enforce."""

    @classmethod
    def setUpClass(cls):
        with open(SCHEMA_PATH, encoding="utf-8") as handle:
            cls.schema = json.load(handle)
        cls.fields = {f["fieldname"]: f for f in cls.schema["fields"]}

    def test_naming_series_is_a_select_with_a_default_not_read_only_data(self):
        field = self.fields["naming_series"]
        self.assertEqual("Select", field["fieldtype"])
        self.assertEqual(module.DEFAULT_NAMING_SERIES, field["options"])
        self.assertEqual(module.DEFAULT_NAMING_SERIES, field["default"])
        self.assertFalse(field.get("read_only"))

    def test_the_doctype_is_submittable(self):
        self.assertEqual(1, self.schema["is_submittable"])

    def test_unit_options_match_the_controller(self):
        self.assertEqual(
            list(module.PENALTY_UNITS), self.fields["unit"]["options"].split("\n")
        )

    def test_settlement_fields_are_read_only_and_writable_after_submit(self):
        for fieldname in ("settled", "settled_via"):
            with self.subTest(field=fieldname):
                field = self.fields[fieldname]
                self.assertEqual(1, field.get("read_only"))
                self.assertEqual(1, field.get("allow_on_submit"))

    def test_the_snapshot_and_derived_columns_are_read_only(self):
        for fieldname in ("day_rate", "equivalent_days", "employee_name"):
            with self.subTest(field=fieldname):
                self.assertEqual(1, self.fields[fieldname].get("read_only"))

    def test_the_fields_the_board_filters_on_are_mandatory(self):
        for fieldname in ("employee", "company", "penalty_date", "period_month", "reason"):
            with self.subTest(field=fieldname):
                self.assertEqual(1, self.fields[fieldname].get("reqd"))

    def test_every_field_is_in_the_field_order(self):
        self.assertEqual(sorted(self.fields), sorted(self.schema["field_order"]))


if __name__ == "__main__":
    unittest.main()
