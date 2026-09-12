"""Tests for the staff-order employee picker (one staff Customer per employee).

What is pinned, and why each one matters for money:

* **Resolution order** a/b/c. The customer a staff order is billed to is the
  customer the salary board later recovers from; picking the wrong one either
  invents staff debt or loses it.
* **Ambiguity never guesses.** Two same-named candidates, or two employees
  sharing a name, create a fresh customer instead of adopting one.
* **Stale links are reported, never modified.** Production carries customers
  wrongly linked to an employee; this feature must not "fix" them silently.
* **WooCommerce never sees a staff customer** (the outbound flags are set).
* **Inactive employees are refused**, and the role gate is the Employee Order
  policy's own gate.
* ``customers_for_employees`` is **deterministic** and agrees with the picker.
* The Employee doc-event wrapper **never raises**.
* Deliver-at-Branch orders are not filed as "Territory Unresolved", and the POS
  policy payload carries ``deliver_at_branch``.

Mock-level like ``test_territory_exceptions`` / ``test_employee_ledger``: the
module's ``frappe`` is replaced by a small in-memory site, so nothing touches a
database and the suite runs in the pre-migrate CI logic gate.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.api import employee_customers as api
from jarz_pos.services import employee_customers as svc
from jarz_pos.services import territory_exceptions as te
from jarz_pos.utils import employee_link as el


EMP = "HR-EMP-00001"
OTHER_EMP = "HR-EMP-00002"


class _Refused(Exception):
    """What the fake ``frappe.throw`` raises when no exception class is given."""


def _throw(message, exc=None, title=None):
    if isinstance(exc, type) and issubclass(exc, BaseException):
        raise exc(message)
    raise _Refused(message)


def _employee(name=EMP, employee_name="Sara Ahmed", status="Active", cell_number="+201111034268"):
    return {
        "name": name,
        "employee_name": employee_name,
        "status": status,
        "cell_number": cell_number,
    }


def _customer(name, *, customer_group="Employee", custom_employee=None, creation=1,
              customer_name=None, mobile_no=None):
    return {
        "name": name,
        "customer_name": customer_name if customer_name is not None else name,
        "customer_group": customer_group,
        "custom_employee": custom_employee,
        "creation": creation,
        "mobile_no": mobile_no,
    }


class _FakeDoc:
    def __init__(self, data, site):
        for key, value in dict(data).items():
            setattr(self, key, value)
        self.flags = SimpleNamespace()
        self._site = site
        if not hasattr(self, "name"):
            self.name = None

    def insert(self, ignore_permissions=False):
        self._site.on_insert(self)
        return self


class _Site:
    """An in-memory stand-in for the handful of tables this feature touches."""

    def __init__(self, *, employees=None, customers=None, contact_mobiles=None,
                 groups=("Employee", "All Customer Groups"), reject_mobile=False,
                 race_on_claim=None):
        self.employees = {e["name"]: dict(e) for e in (employees or [])}
        self.customers = [dict(c) for c in (customers or [])]
        self.contact_mobiles = list(contact_mobiles or [])
        self.groups = set(groups)
        self.reject_mobile = reject_mobile
        self.race_on_claim = race_on_claim
        self.queries = []
        self.updates = []
        self.inserted = []
        self._clock = max([c.get("creation") or 0 for c in self.customers] + [0])
        self.frappe = self._build_frappe()

    # -- frappe double -----------------------------------------------------

    def _build_frappe(self):
        fake = MagicMock()
        fake.throw.side_effect = _throw
        fake.PermissionError = PermissionError
        fake.flags = SimpleNamespace()
        fake.local = SimpleNamespace(message_log=[])
        fake.session = SimpleNamespace(user="manager@jarz.test")
        fake.utils = SimpleNamespace(now=lambda: "2026-09-13 10:00:00")
        fake.get_traceback.return_value = "traceback"
        fake.db.sql.side_effect = self.sql
        fake.db.has_column.return_value = True
        fake.db.exists.side_effect = self.exists
        fake.get_all.side_effect = self.get_all
        fake.get_doc.side_effect = self.get_doc
        return fake

    def _customer(self, name):
        for row in self.customers:
            if row["name"] == name:
                return row
        return None

    def _ordered(self):
        return sorted(self.customers, key=lambda c: (c.get("creation") or 0, c["name"]))

    def sql(self, query, params=None, as_dict=False, **kwargs):
        q = " ".join(str(query).split())
        self.queries.append(q)
        params = tuple(params or ())

        if "FROM `tabEmployee`" in q:
            row = self.employees.get(params[0])
            return [dict(row)] if row else []

        if q.startswith("UPDATE `tabCustomer`"):
            employee, _now, _user, name = params
            self.updates.append((name, employee))
            row = self._customer(name)
            if row is not None:
                if self.race_on_claim:
                    # Another employee's call committed first.
                    row["custom_employee"] = self.race_on_claim
                if not str(row.get("custom_employee") or "").strip():
                    row["custom_employee"] = employee
            return []

        if q.startswith("SELECT `custom_employee` FROM `tabCustomer`"):
            row = self._customer(params[0])
            return [{"custom_employee": row.get("custom_employee")}] if row else []

        if "LOWER(TRIM(`customer_name`))" in q:
            group, key = params
            rows = [
                c for c in self._ordered()
                if c.get("customer_group") == group
                and not str(c.get("custom_employee") or "").strip()
                and str(c.get("customer_name") or "").strip().lower() == key
            ]
            return [{"name": c["name"]} for c in rows[:2]]

        if "FROM `tabCustomer`" in q and "`custom_employee` = %s" in q:
            return [
                {
                    "name": c["name"],
                    "customer_name": c.get("customer_name"),
                    "customer_group": c.get("customer_group"),
                }
                for c in self._ordered()
                if c.get("custom_employee") == params[0]
            ]

        raise AssertionError(f"unexpected SQL: {q}")

    def exists(self, doctype, name=None, *args, **kwargs):
        if doctype == "Customer Group":
            return name if name in self.groups else None
        return None

    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
        filters = dict(filters or {})
        if doctype == "Employee":
            if "name" in filters:
                excluded = filters["name"][1]
                rows = [
                    e for e in self.employees.values()
                    if e.get("status") == filters.get("status")
                    and e["name"] != excluded
                    and e.get("employee_name") == filters.get("employee_name")
                ]
            else:
                rows = [
                    e for e in sorted(self.employees.values(), key=lambda e: e["name"])
                    if e.get("status") == filters.get("status")
                ]
        elif doctype == "Customer":
            if "mobile_no" in filters:
                rows = [c for c in self.customers if c.get("mobile_no") in filters["mobile_no"][1]]
            elif "phone" in filters:
                rows = [c for c in self.customers if c.get("phone") in filters["phone"][1]]
            elif "custom_employee" in filters:
                wanted = filters["custom_employee"][1]
                rows = [
                    c for c in self._ordered()
                    if c.get("custom_employee") in wanted
                    and c.get("customer_group") == filters.get("customer_group")
                ]
            else:
                rows = []
        elif doctype == "Contact":
            forms = filters["mobile_no"][1]
            rows = [{"name": f"CONTACT-{m}", "mobile_no": m} for m in self.contact_mobiles if m in forms]
        else:
            rows = []
        if pluck:
            return [r[pluck] for r in rows]
        return [dict(r) for r in rows]

    def get_doc(self, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict):
            return _FakeDoc(args[0], self)
        raise AssertionError("this feature never loads an existing document")

    def on_insert(self, doc):
        payload = {
            k: v for k, v in vars(doc).items() if not k.startswith("_") and k != "flags"
        }
        if doc.doctype == "Customer":
            if self.reject_mobile and payload.get("mobile_no"):
                raise _Refused("Invalid Mobile No")
            name = payload["customer_name"]
            if self._customer(name):
                name = f"{name} - 1"
            doc.name = name
            self._clock += 1
            self.customers.append(
                {
                    "name": name,
                    "customer_name": payload.get("customer_name"),
                    "customer_group": payload.get("customer_group"),
                    "customer_type": payload.get("customer_type"),
                    "custom_employee": payload.get("custom_employee"),
                    "mobile_no": payload.get("mobile_no"),
                    "creation": self._clock,
                }
            )
        elif doc.doctype == "Customer Group":
            self.groups.add(payload["customer_group_name"])
        self.inserted.append(
            SimpleNamespace(
                doctype=doc.doctype,
                payload=payload,
                doc_flags=doc.flags,
                global_woo_flag=getattr(self.frappe.flags, "ignore_woo_outbound", None),
            )
        )

    def inserted_of(self, doctype):
        return [i for i in self.inserted if i.doctype == doctype]


def _ensure(site, employee=EMP, *, hrms=True, field=True):
    with patch.object(svc, "frappe", site.frappe), \
         patch.object(svc, "hrms_available", return_value=hrms), \
         patch.object(svc, "customer_has_employee_field", return_value=field):
        return svc.ensure_customer_for_employee(employee)


# ─────────────────────────────────────────────────────────────────────────────
# ensure_customer_for_employee — resolution order
# ─────────────────────────────────────────────────────────────────────────────

class TestResolutionOrder(unittest.TestCase):

    def test_a_existing_employee_group_link_oldest_wins(self):
        site = _Site(
            employees=[_employee()],
            customers=[
                _customer("Sara Ahmed - 1", custom_employee=EMP, creation=5),
                _customer("Sara Ahmed", custom_employee=EMP, creation=2),
            ],
        )
        result = _ensure(site)

        self.assertEqual(result, {"customer": "Sara Ahmed", "action": "existing", "conflicts": []})
        self.assertEqual(site.inserted, [])
        self.assertEqual(site.updates, [])

    def test_a_link_read_is_ordered_and_locking(self):
        site = _Site(employees=[_employee()], customers=[_customer("Sara Ahmed", custom_employee=EMP)])
        _ensure(site)

        link_reads = [q for q in site.queries if "`custom_employee` = %s" in q and q.startswith("SELECT")]
        self.assertEqual(len(link_reads), 1)
        self.assertIn("ORDER BY `creation` ASC, `name` ASC", link_reads[0])
        self.assertTrue(link_reads[0].endswith("FOR UPDATE"))

    def test_employee_row_is_locked_before_any_customer_read(self):
        site = _Site(employees=[_employee()], customers=[_customer("Sara Ahmed", custom_employee=EMP)])
        _ensure(site)

        self.assertIn("FROM `tabEmployee`", site.queries[0])
        self.assertTrue(site.queries[0].endswith("FOR UPDATE"))

    def test_a_non_employee_group_link_is_not_existing_but_a_conflict(self):
        site = _Site(
            employees=[_employee()],
            customers=[
                _customer("Walk-in Mona", customer_group="Individual", custom_employee=EMP, creation=1),
                _customer("Sara Ahmed", custom_employee=EMP, creation=9),
            ],
        )
        result = _ensure(site)

        self.assertEqual(result["action"], "existing")
        self.assertEqual(result["customer"], "Sara Ahmed")
        self.assertEqual(result["conflicts"], ["Walk-in Mona"])

    def test_b_adopts_the_single_unlinked_name_match(self):
        site = _Site(
            employees=[_employee()],
            customers=[_customer("CUST-SARA", customer_name="  sara AHMED ")],
        )
        result = _ensure(site)

        self.assertEqual(result, {"customer": "CUST-SARA", "action": "adopted", "conflicts": []})
        self.assertEqual(site._customer("CUST-SARA")["custom_employee"], EMP)
        self.assertEqual(site.inserted_of("Customer"), [])
        self.assertEqual(len(site.inserted_of("Comment")), 1, "an adoption must leave an audit trail")

    def test_b_ambiguous_name_match_falls_through_to_create(self):
        site = _Site(
            employees=[_employee()],
            customers=[
                _customer("CUST-SARA-1", customer_name="Sara Ahmed", creation=1),
                _customer("CUST-SARA-2", customer_name="Sara Ahmed", creation=2),
            ],
        )
        result = _ensure(site)

        self.assertEqual(result["action"], "created")
        self.assertEqual(site.updates, [])
        self.assertIsNone(site._customer("CUST-SARA-1")["custom_employee"])
        self.assertIsNone(site._customer("CUST-SARA-2")["custom_employee"])

    def test_b_name_shared_by_another_active_employee_is_not_adopted(self):
        site = _Site(
            employees=[_employee(), _employee(name=OTHER_EMP, cell_number=None)],
            customers=[_customer("CUST-SARA", customer_name="Sara Ahmed")],
        )
        result = _ensure(site)

        self.assertEqual(result["action"], "created")
        self.assertEqual(site.updates, [])
        self.assertIsNone(site._customer("CUST-SARA")["custom_employee"])

    def test_b_name_match_outside_the_employee_group_is_never_adopted(self):
        site = _Site(
            employees=[_employee()],
            customers=[_customer("Retail Sara", customer_name="Sara Ahmed", customer_group="Individual")],
        )
        result = _ensure(site)

        self.assertEqual(result["action"], "created")
        self.assertIsNone(site._customer("Retail Sara")["custom_employee"])

    def test_b_lost_adoption_race_creates_instead_of_stealing(self):
        site = _Site(
            employees=[_employee()],
            customers=[_customer("CUST-SARA", customer_name="Sara Ahmed")],
            race_on_claim="HR-EMP-00099",
        )
        result = _ensure(site)

        self.assertEqual(result["action"], "created")
        self.assertEqual(site._customer("CUST-SARA")["custom_employee"], "HR-EMP-00099")
        claim = [q for q in site.queries if q.startswith("UPDATE `tabCustomer`")]
        self.assertIn("IFNULL(`custom_employee`, '') = ''", claim[0], "adoption must be compare-and-set")

    def test_conflicts_are_reported_and_never_modified(self):
        site = _Site(
            employees=[_employee()],
            customers=[
                _customer("Walk-in Mona", customer_group="Individual", custom_employee=EMP),
                _customer("Walk-in Hana", customer_group="B2B", custom_employee=EMP, creation=2),
            ],
        )
        before = [dict(c) for c in site.customers]
        result = _ensure(site)

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["conflicts"], ["Walk-in Mona", "Walk-in Hana"])
        self.assertEqual(site.updates, [])
        self.assertEqual(site.customers[:2], before)


# ─────────────────────────────────────────────────────────────────────────────
# ensure_customer_for_employee — creating
# ─────────────────────────────────────────────────────────────────────────────

class TestCreate(unittest.TestCase):

    def test_c_creates_an_employee_group_customer_with_woo_outbound_off(self):
        site = _Site(employees=[_employee()])
        result = _ensure(site)

        self.assertEqual(result, {"customer": "Sara Ahmed", "action": "created", "conflicts": []})
        [created] = site.inserted_of("Customer")
        self.assertEqual(created.payload["customer_name"], "Sara Ahmed")
        self.assertEqual(created.payload["customer_group"], "Employee")
        self.assertEqual(created.payload["customer_type"], "Individual")
        self.assertEqual(created.payload["custom_employee"], EMP)
        self.assertTrue(created.doc_flags.ignore_woo_outbound)
        self.assertTrue(created.global_woo_flag, "the Contact ERPNext creates must not sync either")
        self.assertIsNone(site.frappe.flags.ignore_woo_outbound, "global flag must be restored")

    def test_c_mobile_is_the_canonical_cell_number_when_free(self):
        site = _Site(employees=[_employee(cell_number="+201111034268")])
        _ensure(site)

        [created] = site.inserted_of("Customer")
        self.assertEqual(created.payload["mobile_no"], "01111034268")

    def test_c_mobile_left_blank_when_another_customer_holds_any_spelling(self):
        site = _Site(
            employees=[_employee(cell_number="+201111034268")],
            customers=[_customer("Retail", customer_group="Individual", mobile_no="01111034268")],
        )
        _ensure(site)

        [created] = site.inserted_of("Customer")
        self.assertNotIn("mobile_no", created.payload)

    def test_c_mobile_left_blank_when_a_contact_holds_it(self):
        site = _Site(employees=[_employee(cell_number="01111034268")], contact_mobiles=["+201111034268"])
        _ensure(site)

        [created] = site.inserted_of("Customer")
        self.assertNotIn("mobile_no", created.payload)

    def test_c_rejected_mobile_retries_without_it(self):
        site = _Site(employees=[_employee()], reject_mobile=True)
        result = _ensure(site)

        self.assertEqual(result["action"], "created")
        [created] = site.inserted_of("Customer")
        self.assertNotIn("mobile_no", created.payload)
        site.frappe.db.rollback.assert_any_call(save_point=svc._INSERT_SAVEPOINT)

    def test_c_creates_the_employee_group_when_missing(self):
        site = _Site(employees=[_employee()], groups=("All Customer Groups",))
        _ensure(site)

        self.assertEqual([i.doctype for i in site.inserted], ["Customer Group", "Customer"])
        self.assertEqual(site.inserted[0].payload["parent_customer_group"], "All Customer Groups")


# ─────────────────────────────────────────────────────────────────────────────
# ensure_customer_for_employee — refusals
# ─────────────────────────────────────────────────────────────────────────────

class TestRefusals(unittest.TestCase):

    def test_inactive_employee_is_refused_without_writing(self):
        site = _Site(employees=[_employee(status="Left")])
        with self.assertRaises(_Refused) as ctx:
            _ensure(site)

        self.assertIn("Left", str(ctx.exception))
        self.assertEqual(site.inserted, [])
        self.assertEqual(site.updates, [])
        self.assertEqual(len(site.queries), 1, "only the Employee lock may run")

    def test_unknown_employee_is_refused(self):
        site = _Site(employees=[])
        with self.assertRaises(_Refused):
            _ensure(site, "HR-EMP-NOPE")
        self.assertEqual(site.inserted, [])

    def test_blank_employee_is_refused(self):
        site = _Site(employees=[_employee()])
        with self.assertRaises(_Refused):
            _ensure(site, "  ")
        self.assertEqual(site.queries, [])

    def test_hrms_absent_is_refused_before_any_query(self):
        site = _Site(employees=[_employee()])
        with self.assertRaises(_Refused):
            _ensure(site, hrms=False)
        self.assertEqual(site.queries, [])

    def test_unmigrated_link_field_is_refused_before_any_query(self):
        site = _Site(employees=[_employee()])
        with self.assertRaises(_Refused):
            _ensure(site, field=False)
        self.assertEqual(site.queries, [])


# ─────────────────────────────────────────────────────────────────────────────
# ensure_customers_for_all_employees
# ─────────────────────────────────────────────────────────────────────────────

class TestEnsureAll(unittest.TestCase):

    def _run(self, site, side_effect, *, hrms=True):
        with patch.object(svc, "frappe", site.frappe), \
             patch.object(svc, "hrms_available", return_value=hrms), \
             patch.object(svc, "customer_has_employee_field", return_value=True), \
             patch.object(svc, "ensure_customer_for_employee", side_effect=side_effect) as ensure:
            return svc.ensure_customers_for_all_employees(), ensure

    def test_one_failure_does_not_stop_the_others(self):
        site = _Site(
            employees=[
                _employee(name=EMP),
                _employee(name=OTHER_EMP, employee_name="Omar Said"),
                _employee(name="HR-EMP-00003", employee_name="Gone", status="Left"),
            ]
        )

        def _side_effect(employee):
            if employee == EMP:
                raise _Refused("insert blew up")
            return {"customer": "Omar Said", "action": "created", "conflicts": ["Walk-in Omar"]}

        summary, ensure = self._run(site, _side_effect)

        self.assertEqual(ensure.call_count, 2, "only Active employees are visited")
        self.assertEqual(summary["skipped"], [{"employee": EMP, "reason": "insert blew up"}])
        self.assertEqual(
            summary["created"],
            [{"employee": OTHER_EMP, "employee_name": "Omar Said", "customer": "Omar Said"}],
        )
        self.assertEqual(summary["conflicts"], [{"employee": OTHER_EMP, "customers": ["Walk-in Omar"]}])
        self.assertEqual(summary["adopted"], [])
        self.assertEqual(summary["existing"], [])
        site.frappe.db.rollback.assert_called_once_with(save_point=svc._BULK_SAVEPOINT)
        site.frappe.db.commit.assert_called_once_with()

    def test_failed_employee_does_not_leave_an_error_dialog(self):
        site = _Site(employees=[_employee()])
        site.frappe.local.message_log.append("earlier")

        def _side_effect(employee):
            site.frappe.local.message_log.append("Employee is Left")
            raise _Refused("Employee is Left")

        self._run(site, _side_effect)
        self.assertEqual(site.frappe.local.message_log, ["earlier"])

    def test_hrms_absent_returns_an_empty_summary(self):
        site = _Site(employees=[_employee()])
        summary, ensure = self._run(site, None, hrms=False)

        ensure.assert_not_called()
        for key in ("created", "adopted", "existing", "conflicts"):
            self.assertEqual(summary[key], [])
        self.assertEqual(len(summary["skipped"]), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Employee doc-event wrapper
# ─────────────────────────────────────────────────────────────────────────────

class _EmployeeDoc(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


class TestEmployeeHook(unittest.TestCase):

    def _run(self, doc, side_effect=None, *, log_error_raises=False):
        site = _Site(employees=[_employee()])
        site.frappe.local.message_log.append("earlier")
        if log_error_raises:
            site.frappe.log_error.side_effect = RuntimeError("error log is down too")
        with patch.object(svc, "frappe", site.frappe), \
             patch.object(svc, "hrms_available", return_value=True), \
             patch.object(svc, "customer_has_employee_field", return_value=True), \
             patch.object(svc, "ensure_customer_for_employee", side_effect=side_effect) as ensure:
            outcome = svc.ensure_customer_on_employee_save(doc, "on_update")
        return outcome, ensure, site

    def test_active_employee_is_ensured(self):
        outcome, ensure, _site = self._run(
            _EmployeeDoc(name=EMP, status="Active"),
            side_effect=lambda e: {"customer": "Sara Ahmed", "action": "created", "conflicts": []},
        )
        self.assertIsNone(outcome)
        ensure.assert_called_once_with(EMP)

    def test_inactive_employee_is_skipped(self):
        _outcome, ensure, _site = self._run(_EmployeeDoc(name=EMP, status="Left"))
        ensure.assert_not_called()

    def test_never_raises_and_rolls_back_to_its_savepoint(self):
        def _boom(employee):
            svc.frappe.local.message_log.append("Customer Group clash")
            raise _Refused("Customer Group clash")

        outcome, _ensure_mock, site = self._run(_EmployeeDoc(name=EMP, status="Active"), side_effect=_boom)

        self.assertIsNone(outcome)
        site.frappe.db.rollback.assert_called_once_with(save_point=svc._HOOK_SAVEPOINT)
        self.assertTrue(site.frappe.log_error.called)
        self.assertEqual(site.frappe.local.message_log, ["earlier"], "no red dialog on a saved Employee")

    def test_never_raises_even_when_the_error_log_fails(self):
        outcome, _ensure_mock, _site = self._run(
            _EmployeeDoc(name=EMP, status="Active"),
            side_effect=RuntimeError("db gone"),
            log_error_raises=True,
        )
        self.assertIsNone(outcome)

    def test_never_raises_when_hrms_probe_explodes(self):
        site = _Site(employees=[_employee()])
        with patch.object(svc, "frappe", site.frappe), \
             patch.object(svc, "hrms_available", side_effect=RuntimeError("boom")), \
             patch.object(svc, "ensure_customer_for_employee") as ensure:
            self.assertIsNone(svc.ensure_customer_on_employee_save(_EmployeeDoc(name=EMP, status="Active")))
        ensure.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# customers_for_employees is deterministic and agrees with the picker
# ─────────────────────────────────────────────────────────────────────────────

class TestCustomersForEmployeesDeterministic(unittest.TestCase):

    def _run(self, rows):
        mock = MagicMock()
        mock.get_all.return_value = rows
        with patch.object(el, "frappe", mock), \
             patch.object(el, "customer_has_employee_field", return_value=True):
            mapping = el.customers_for_employees([EMP])
        return mapping, mock

    def test_employee_group_customer_beats_an_older_retail_link(self):
        # Rows as the database returns them: creation asc, name asc.
        mapping, mock = self._run(
            [
                {"name": "Walk-in Mona", "custom_employee": EMP, "customer_group": "Individual"},
                {"name": "Sara Ahmed", "custom_employee": EMP, "customer_group": "Employee"},
                {"name": "Sara Ahmed - 1", "custom_employee": EMP, "customer_group": "Employee"},
            ]
        )
        self.assertEqual(mapping, {EMP: "Sara Ahmed"})

        kwargs = mock.get_all.call_args.kwargs
        self.assertEqual(kwargs["order_by"], "creation asc, name asc")
        self.assertIn("customer_group", kwargs["fields"])

    def test_without_a_staff_customer_the_oldest_link_wins(self):
        mapping, _mock = self._run(
            [
                {"name": "Walk-in Mona", "custom_employee": EMP, "customer_group": "Individual"},
                {"name": "Walk-in Hana", "custom_employee": EMP, "customer_group": "B2B"},
            ]
        )
        self.assertEqual(mapping, {EMP: "Walk-in Mona"})


class TestCanonicalCustomers(unittest.TestCase):

    def test_only_employee_group_customers_oldest_first(self):
        site = _Site(
            customers=[
                _customer("Walk-in Mona", customer_group="Individual", custom_employee=EMP, creation=1),
                _customer("Sara Ahmed - 1", custom_employee=EMP, creation=7),
                _customer("Sara Ahmed", custom_employee=EMP, creation=3),
            ]
        )
        with patch.object(svc, "frappe", site.frappe), \
             patch.object(svc, "customer_has_employee_field", return_value=True):
            mapping = svc.canonical_customers_for_employees([EMP, OTHER_EMP])

        self.assertEqual(mapping, {EMP: {"customer": "Sara Ahmed", "customer_name": "Sara Ahmed"}})


# ─────────────────────────────────────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _api_frappe():
    mock = MagicMock()
    mock.throw.side_effect = _throw
    mock.PermissionError = PermissionError
    mock.local = SimpleNamespace(message_log=[])
    return mock


class TestRoleGate(unittest.TestCase):

    def test_gate_is_the_employee_policy_default_gate(self):
        with patch(
            "jarz_pos.services.invoice_creation._has_manager_pricing_access", return_value=False
        ):
            self.assertFalse(api._has_staff_order_access())
        with patch(
            "jarz_pos.services.invoice_creation._has_manager_pricing_access", return_value=True
        ):
            self.assertTrue(api._has_staff_order_access())

    def test_every_endpoint_refuses_without_manager_access(self):
        calls = [
            lambda: api.list_staff_for_orders(),
            lambda: api.ensure_staff_customer(EMP),
            lambda: api.sync_staff_customers(),
        ]
        for call in calls:
            with patch.object(api, "frappe", _api_frappe()), \
                 patch.object(api, "_has_staff_order_access", return_value=False), \
                 patch.object(api, "hrms_available", return_value=True), \
                 patch.object(api.staff_customers, "ensure_customer_for_employee") as ensure, \
                 patch.object(api.staff_customers, "ensure_customers_for_all_employees") as ensure_all:
                with self.assertRaises(PermissionError):
                    call()
                ensure.assert_not_called()
                ensure_all.assert_not_called()


class TestListStaffForOrders(unittest.TestCase):

    def test_hrms_absent(self):
        with patch.object(api, "frappe", _api_frappe()), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=False):
            self.assertEqual(
                api.list_staff_for_orders(),
                {"success": True, "hrms_available": False, "employees": []},
            )

    def test_rows_carry_the_canonical_customer_or_null(self):
        employees = [
            {"employee": OTHER_EMP, "employee_name": "Omar Said", "branch": "", "designation": "Baker"},
            {"employee": EMP, "employee_name": "Sara Ahmed", "branch": "Dokki", "designation": "Cashier"},
        ]
        with patch.object(api, "frappe", _api_frappe()), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=True), \
             patch.object(api, "list_active_employees", return_value=employees) as lister, \
             patch.object(
                 api.staff_customers,
                 "canonical_customers_for_employees",
                 return_value={EMP: {"customer": "Sara Ahmed", "customer_name": "Sara Ahmed"}},
             ):
            result = api.list_staff_for_orders(search="sa")

        lister.assert_called_once_with(search="sa")
        self.assertEqual(result["success"], True)
        self.assertEqual(result["hrms_available"], True)
        self.assertEqual(
            result["employees"],
            [
                {"employee": OTHER_EMP, "employee_name": "Omar Said", "branch": "",
                 "designation": "Baker", "customer": None, "customer_name": None},
                {"employee": EMP, "employee_name": "Sara Ahmed", "branch": "Dokki",
                 "designation": "Cashier", "customer": "Sara Ahmed", "customer_name": "Sara Ahmed"},
            ],
        )


class TestEnsureStaffCustomer(unittest.TestCase):

    def test_success_shape(self):
        fake = _api_frappe()
        fake.db.get_value.return_value = "Sara Ahmed"
        row = {
            "name": "Sara Ahmed", "customer_name": "Sara Ahmed", "mobile_no": "01111034268",
            "customer_primary_address": None, "customer_primary_contact": "Sara Ahmed",
            "territory": None, "customer_group": "Employee",
            "territory_name": "", "territory_name_ar": "",
        }
        with patch.object(api, "frappe", fake), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=True), \
             patch.object(api, "_customer_row", return_value=dict(row)), \
             patch.object(
                 api.staff_customers,
                 "ensure_customer_for_employee",
                 return_value={"customer": "Sara Ahmed", "action": "created", "conflicts": ["Walk-in Mona"]},
             ):
            result = api.ensure_staff_customer(EMP)

        self.assertTrue(result["success"])
        self.assertTrue(result["created"])
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["conflicts"], ["Walk-in Mona"])
        customer = result["customer"]
        for key in row:
            self.assertEqual(customer[key], row[key])
        self.assertEqual(customer["employee"], EMP)
        self.assertEqual(customer["employee_name"], "Sara Ahmed")
        self.assertIs(customer["is_staff_customer"], True)

    def test_existing_is_not_created(self):
        fake = _api_frappe()
        fake.db.get_value.return_value = "Sara Ahmed"
        with patch.object(api, "frappe", fake), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=True), \
             patch.object(api, "_customer_row", return_value={"name": "Sara Ahmed"}), \
             patch.object(
                 api.staff_customers,
                 "ensure_customer_for_employee",
                 return_value={"customer": "Sara Ahmed", "action": "existing", "conflicts": []},
             ):
            result = api.ensure_staff_customer(EMP)

        self.assertFalse(result["created"])
        self.assertEqual(result["action"], "existing")

    def test_failure_is_folded_into_the_payload(self):
        fake = _api_frappe()
        fake.local.message_log.append("Employee 'HR-EMP-00001' is Left.")
        with patch.object(api, "frappe", fake), \
             patch.object(api.staff_customers, "frappe", fake), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=True), \
             patch.object(api.staff_customers, "message_log_length", return_value=0), \
             patch.object(
                 api.staff_customers,
                 "ensure_customer_for_employee",
                 side_effect=_Refused("Employee 'HR-EMP-00001' is Left."),
             ):
            result = api.ensure_staff_customer(EMP)

        self.assertEqual(result, {"success": False, "error": "Employee 'HR-EMP-00001' is Left."})
        fake.db.rollback.assert_called_once_with()
        self.assertEqual(fake.local.message_log, [], "the error travels in the payload only")

    def test_a_deadlock_is_retried_once(self):
        fake = _api_frappe()
        fake.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
        fake.db.get_value.return_value = "Sara Ahmed"
        outcomes = [
            fake.QueryDeadlockError("Deadlock found when trying to get lock"),
            {"customer": "Sara Ahmed", "action": "created", "conflicts": []},
        ]
        with patch.object(api, "frappe", fake), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=True), \
             patch.object(api, "_customer_row", return_value={"name": "Sara Ahmed"}), \
             patch.object(
                 api.staff_customers, "ensure_customer_for_employee", side_effect=outcomes
             ) as ensure:
            result = api.ensure_staff_customer(EMP)

        self.assertTrue(result["success"])
        self.assertEqual(ensure.call_count, 2)
        fake.db.rollback.assert_called_once_with()

    def test_hrms_absent(self):
        with patch.object(api, "frappe", _api_frappe()), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=False), \
             patch.object(api.staff_customers, "ensure_customer_for_employee") as ensure:
            result = api.ensure_staff_customer(EMP)

        ensure.assert_not_called()
        self.assertEqual(result["success"], True)
        self.assertEqual(result["hrms_available"], False)
        self.assertIsNone(result["customer"])


class TestCustomerRow(unittest.TestCase):

    def test_row_has_every_search_customers_key_even_without_a_territory(self):
        fake = _api_frappe()
        fake.db.has_column.return_value = True
        fake.db.get_value.return_value = {
            "name": "Sara Ahmed", "customer_name": "Sara Ahmed", "mobile_no": None,
            "customer_primary_address": None, "customer_primary_contact": None,
            "territory": None, "customer_group": "Employee", "phone": None,
            "custom_credit_allowed": 0,
        }
        with patch.object(api, "frappe", fake), \
             patch("jarz_pos.api.customer._augment_customer_with_territory") as augment:
            row = api._customer_row("Sara Ahmed")

        augment.assert_called_once()
        for key in ("name", "customer_name", "mobile_no", "customer_primary_address",
                    "customer_primary_contact", "territory", "territory_name",
                    "territory_name_ar", "customer_group"):
            self.assertIn(key, row)
        self.assertIs(row["credit_allowed"], False)
        requested = fake.db.get_value.call_args.args[2]
        self.assertIn("custom_credit_allowed", requested)


class TestSyncStaffCustomers(unittest.TestCase):

    def test_shape(self):
        summary = {
            "created": [{"employee": EMP, "employee_name": "Sara Ahmed", "customer": "Sara Ahmed"}],
            "adopted": [], "existing": [], "skipped": [], "conflicts": [],
        }
        with patch.object(api, "frappe", _api_frappe()), \
             patch.object(api, "_has_staff_order_access", return_value=True), \
             patch.object(api, "hrms_available", return_value=True), \
             patch.object(api.staff_customers, "ensure_customers_for_all_employees", return_value=summary):
            result = api.sync_staff_customers()

        self.assertEqual(result, {"success": True, "hrms_available": True, **summary})


# ─────────────────────────────────────────────────────────────────────────────
# Deliver-at-Branch territory noise + POS policy payload
# ─────────────────────────────────────────────────────────────────────────────

class TestDeliverAtBranchTerritoryNoise(unittest.TestCase):

    def _counter(self, policy, behavior):
        mock = MagicMock()
        mock.db.get_value.return_value = behavior
        with patch.object(te, "frappe", mock):
            return te._is_counter_fulfilled({"commercial_policy": policy}), mock

    def test_deliver_at_branch_policy_is_counter_fulfilled(self):
        answer, _mock = self._counter("Employee Order", "Deliver at Branch")
        self.assertTrue(answer)

    def test_normal_policy_is_not(self):
        answer, _mock = self._counter("Sample - Courier", "Normal")
        self.assertFalse(answer)

    def test_no_policy_never_queries(self):
        answer, mock = self._counter(None, "Deliver at Branch")
        self.assertFalse(answer)
        mock.db.get_value.assert_not_called()

    def test_lookup_failure_keeps_the_old_behaviour(self):
        mock = MagicMock()
        mock.db.get_value.side_effect = RuntimeError("db gone")
        with patch.object(te, "frappe", mock):
            self.assertFalse(te._is_counter_fulfilled({"commercial_policy": "Employee Order"}))

    def _record(self, snapshot, behavior="Deliver at Branch"):
        mock = MagicMock()
        mock.db.get_value.return_value = behavior
        with patch.object(te, "frappe", mock), \
             patch.object(te, "_exception_doctype_ready", return_value=True), \
             patch.object(te, "build_snapshot", return_value=snapshot), \
             patch.object(te, "_insert_exception", return_value="TEXC-1") as insert:
            return te.record_invoice_territory_exception({"name": "SI-1"}), insert

    def test_unresolved_territory_on_a_counter_order_is_not_filed(self):
        result, insert = self._record(
            {"sales_invoice": "SI-1", "docstatus": 1, "raw_territory": "",
             "pos_profile_used": "Dokki", "territory_pos_profile": None,
             "commercial_policy": "Employee Order"}
        )
        self.assertIsNone(result)
        insert.assert_not_called()

    def test_unresolved_territory_on_a_normal_order_is_still_filed(self):
        result, insert = self._record(
            {"sales_invoice": "SI-1", "docstatus": 1, "raw_territory": "",
             "pos_profile_used": "Dokki", "territory_pos_profile": None,
             "commercial_policy": None}
        )
        self.assertEqual(result, "TEXC-1")
        insert.assert_called_once()

    def test_branch_mismatch_on_a_counter_order_is_still_filed(self):
        result, insert = self._record(
            {"sales_invoice": "SI-1", "docstatus": 1, "raw_territory": "EGDOKKI",
             "pos_profile_used": "Nasr city", "territory_pos_profile": "Dokki",
             "commercial_policy": "Employee Order"}
        )
        self.assertEqual(result, "TEXC-1")
        self.assertEqual(insert.call_args.args[1], te.TYPE_BRANCH_MISMATCH)


class TestCommercialPolicyPayload(unittest.TestCase):

    def _run(self, *, has_column):
        from jarz_pos.api import pos

        mock = MagicMock()
        mock.session.user = "manager@jarz.test"
        mock.get_roles.return_value = ["JARZ Manager"]
        mock.throw.side_effect = _throw
        mock.PermissionError = PermissionError
        mock.db.exists.return_value = True
        mock.db.has_column.return_value = has_column
        row = {
            "name": "Employee Order", "policy_name": "Employee Order", "order_purpose": "Employee",
            "price_list": "Employee", "discount_percentage": 0,
            "shipping_income_behavior": "Zero", "shipping_expense_behavior": "Zero",
            "courier_behavior": "No Courier", "require_role": None, "company": None, "pos_profile": None,
        }
        if has_column:
            row["fulfilment_behavior"] = "Deliver at Branch"
        mock.get_all.return_value = [row]
        with patch.object(pos, "frappe", mock):
            result = pos.get_commercial_policies()
        return result, mock

    def test_payload_carries_deliver_at_branch(self):
        result, mock = self._run(has_column=True)
        self.assertIs(result[0]["deliver_at_branch"], True)
        self.assertIn("fulfilment_behavior", mock.get_all.call_args.kwargs["fields"])

    def test_unmigrated_site_does_not_select_the_column(self):
        result, mock = self._run(has_column=False)
        self.assertIs(result[0]["deliver_at_branch"], False)
        self.assertNotIn("fulfilment_behavior", mock.get_all.call_args.kwargs["fields"])


if __name__ == "__main__":
    unittest.main()
