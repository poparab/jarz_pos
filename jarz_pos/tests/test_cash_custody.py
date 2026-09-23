"""Cash custody (عهدة): the ledger guard, the endpoints, and the two spend paths.

Pure mocks: every module's ``frappe`` handle is replaced, so nothing here
touches the site. What is pinned, and why each matters:

* The guard's arithmetic. It decides whether money may leave a custody
  account, so the per-voucher net credit (JE rows, PE paid_from / paid_to, PI
  paid on the document) and the "exact balance is fine, one piastre over is
  not" boundary are asserted directly.
* The guard is CHEAP and LOCK-FREE when no custody account is credited: it
  runs on every Journal Entry, Payment Entry and Purchase Invoice submit.
* The balance read under lock is a LOCKING read — a plain read after a
  FOR UPDATE answers from the transaction's older snapshot.
* The issue/return permission matrix: manager, own holder, other user,
  disabled holder, a holder drawing on a branch that is not theirs, and a
  return into another branch (allowed).
* create_expense routes a holder's custody spend and refuses someone else's.
* The purchase ``custody:<holder>`` option resolves and is permission-gated.
* The statement's kind classification and running balance.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _Refused(Exception):
    def __init__(self, msg, exc=None):
        super().__init__(str(msg))
        self.exc = exc


def _throw(msg, exc=None, *args, **kwargs):
    raise _Refused(msg, exc)


class _PermissionError(Exception):
    pass


class _DoesNotExist(Exception):
    pass


def _mock_frappe(user="holder@example.com", roles=()):
    mock = MagicMock()
    mock.session.user = user
    mock.throw.side_effect = _throw
    mock.flags = SimpleNamespace()
    mock.PermissionError = _PermissionError
    mock.DoesNotExistError = _DoesNotExist
    mock.get_roles.return_value = list(roles)
    return mock


class _Doc(dict):
    """A voucher stand-in: ``.doctype`` plus ``.get`` like a Document."""

    def __init__(self, doctype, **fields):
        super().__init__(doctype=doctype, **fields)
        self.doctype = doctype


class _Holder(dict):
    """A Jarz Custody Holder stand-in with attribute AND ``.get`` access."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _holder(**overrides):
    data = {
        "name": "EMP-0001",
        "employee": "EMP-0001",
        "employee_name": "Ali",
        "user": "holder@example.com",
        "company": "Jarz",
        "account": "Custody - Ali - J",
        "enabled": 1,
    }
    data.update(overrides)
    return _Holder(data)


def _src(account, category="pos_profile", pos_profile=None):
    return SimpleNamespace(
        account=account,
        label=account,
        label_en=account,
        label_ar=account,
        category=category,
        balance=0.0,
        pos_profile=pos_profile,
    )


CUSTODY = "Custody - Ali - J"


# ── the ledger guard ───────────────────────────────────────────────────────


class TestNetCredits(unittest.TestCase):
    def test_journal_entry_credit_is_offset_by_debit_on_the_same_account(self):
        from jarz_pos.services import cash_custody

        doc = _Doc(
            "Journal Entry",
            accounts=[
                {"account": CUSTODY, "credit_in_account_currency": 100, "debit_in_account_currency": 0},
                {"account": CUSTODY, "credit_in_account_currency": 0, "debit_in_account_currency": 30},
                {"account": "Rent - J", "credit_in_account_currency": 0, "debit_in_account_currency": 70},
            ],
        )
        moves = cash_custody.net_credits(doc)
        self.assertAlmostEqual(moves[CUSTODY], 70.0)
        self.assertAlmostEqual(moves["Rent - J"], -70.0)

    def test_payment_entry_pay_credits_paid_from(self):
        from jarz_pos.services import cash_custody

        doc = _Doc(
            "Payment Entry",
            payment_type="Pay",
            paid_from=CUSTODY,
            paid_amount=50,
            paid_to="Creditors - J",
            received_amount=50,
        )
        self.assertAlmostEqual(cash_custody.net_credits(doc)[CUSTODY], 50.0)

    def test_payment_entry_receive_into_custody_is_a_debit(self):
        from jarz_pos.services import cash_custody

        doc = _Doc(
            "Payment Entry",
            payment_type="Receive",
            paid_from="Debtors - J",
            paid_amount=40,
            paid_to=CUSTODY,
            received_amount=40,
        )
        moves = cash_custody.net_credits(doc)
        self.assertAlmostEqual(moves[CUSTODY], -40.0)
        self.assertNotIn("Debtors - J", moves)

    def test_purchase_invoice_counts_only_when_paid_on_the_document(self):
        from jarz_pos.services import cash_custody

        unpaid = _Doc("Purchase Invoice", is_paid=0, cash_bank_account=CUSTODY, paid_amount=90)
        self.assertEqual(cash_custody.net_credits(unpaid), {})

        paid = _Doc(
            "Purchase Invoice",
            is_paid=1,
            cash_bank_account=CUSTODY,
            paid_amount=90,
            base_paid_amount=90,
        )
        self.assertAlmostEqual(cash_custody.net_credits(paid)[CUSTODY], 90.0)


class TestGuard(unittest.TestCase):
    def _run(self, doc, balance, holders=None, cancel=False):
        from jarz_pos.services import cash_custody

        holders = {CUSTODY: "EMP-0001"} if holders is None else holders
        mock_frappe = _mock_frappe()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cash_custody, "frappe", mock_frappe))
            accounts = stack.enter_context(
                patch.object(cash_custody, "custody_accounts", return_value=holders)
            )
            bal = stack.enter_context(
                patch.object(cash_custody, "custody_balance", return_value=balance)
            )
            stack.enter_context(
                patch.object(cash_custody, "holder_employee_name", return_value="Ali")
            )
            if cancel:
                cash_custody.guard_custody_balance_on_cancel(doc)
            else:
                cash_custody.guard_custody_balance(doc)
        return accounts, bal

    @staticmethod
    def _spend(amount, account=CUSTODY):
        return _Doc(
            "Journal Entry",
            company="Jarz",
            accounts=[
                {"account": "Rent - J", "debit_in_account_currency": amount, "credit_in_account_currency": 0},
                {"account": account, "credit_in_account_currency": amount, "debit_in_account_currency": 0},
            ],
        )

    def test_overdraw_is_refused_with_the_holder_named(self):
        with self.assertRaises(_Refused) as ctx:
            self._run(self._spend(80), balance=50)
        message = str(ctx.exception)
        self.assertIn("Ali", message)
        self.assertIn("50.00", message)
        self.assertIn("80.00", message)

    def test_exact_balance_is_allowed_and_read_under_lock(self):
        _accounts, bal = self._run(self._spend(80), balance=80)
        bal.assert_called_once_with(CUSTODY, "Jarz", for_update=True)

    def test_debit_rows_offset_the_credit(self):
        doc = _Doc(
            "Journal Entry",
            company="Jarz",
            accounts=[
                {"account": CUSTODY, "credit_in_account_currency": 100, "debit_in_account_currency": 0},
                {"account": CUSTODY, "credit_in_account_currency": 0, "debit_in_account_currency": 40},
                {"account": "Rent - J", "credit_in_account_currency": 0, "debit_in_account_currency": 60},
            ],
        )
        self._run(doc, balance=60)  # net 60 out of 60 -> fine
        with self.assertRaises(_Refused):
            self._run(doc, balance=59.9)

    def test_non_custody_voucher_returns_without_locking(self):
        _accounts, bal = self._run(self._spend(500, account="Cash - J"), balance=0)
        bal.assert_not_called()

    def test_voucher_with_no_credit_never_looks_up_custody(self):
        doc = _Doc(
            "Journal Entry",
            company="Jarz",
            accounts=[
                {"account": CUSTODY, "debit_in_account_currency": 100, "credit_in_account_currency": 0},
                {"account": "Cash - J", "debit_in_account_currency": 0, "credit_in_account_currency": 100},
            ],
        )
        # Cash - J IS credited, so custody_accounts is consulted; but a pure
        # debit into custody never locks it.
        _accounts, bal = self._run(doc, balance=0)
        bal.assert_not_called()

    def test_payment_entry_and_paid_purchase_invoice_are_guarded(self):
        pe = _Doc("Payment Entry", company="Jarz", payment_type="Pay", paid_from=CUSTODY,
                  paid_amount=120, paid_to="Creditors - J", received_amount=120)
        with self.assertRaises(_Refused):
            self._run(pe, balance=100)
        pi = _Doc("Purchase Invoice", company="Jarz", is_paid=1, cash_bank_account=CUSTODY,
                  paid_amount=120, base_paid_amount=120)
        with self.assertRaises(_Refused):
            self._run(pi, balance=100)
        self._run(pi, balance=120)

    def test_cancelling_an_issue_after_spending_is_refused(self):
        issue = _Doc(
            "Journal Entry",
            company="Jarz",
            accounts=[
                {"account": CUSTODY, "debit_in_account_currency": 100, "credit_in_account_currency": 0},
                {"account": "Nasr - J", "debit_in_account_currency": 0, "credit_in_account_currency": 100},
            ],
        )
        with self.assertRaises(_Refused):
            self._run(issue, balance=20, cancel=True)
        self._run(issue, balance=100, cancel=True)

    def test_cancelling_a_spend_is_never_blocked(self):
        _accounts, bal = self._run(self._spend(80), balance=0, cancel=True)
        bal.assert_not_called()


class TestCustodyBalance(unittest.TestCase):
    def test_for_update_locks_the_holder_then_reads_with_a_locking_read(self):
        from jarz_pos.services import cash_custody

        mock_frappe = _mock_frappe()
        mock_frappe.db.sql.side_effect = [(), ((70.0,),)]
        with patch.object(cash_custody, "frappe", mock_frappe):
            balance = cash_custody.custody_balance(CUSTODY, "Jarz", for_update=True)

        self.assertEqual(balance, 70.0)
        first, second = [c.args[0] for c in mock_frappe.db.sql.call_args_list]
        self.assertIn("tabJarz Custody Holder", first)
        self.assertIn("FOR UPDATE", first)
        self.assertIn("tabGL Entry", second)
        self.assertIn("is_cancelled = 0", second)
        self.assertIn("FOR UPDATE", second)

    def test_plain_read_takes_no_lock(self):
        from jarz_pos.services import cash_custody

        mock_frappe = _mock_frappe()
        mock_frappe.db.sql.return_value = ((12.5,),)
        with patch.object(cash_custody, "frappe", mock_frappe):
            balance = cash_custody.custody_balance(CUSTODY, "Jarz")
        self.assertEqual(balance, 12.5)
        self.assertEqual(mock_frappe.db.sql.call_count, 1)
        self.assertNotIn("FOR UPDATE", mock_frappe.db.sql.call_args.args[0])

    def test_custody_accounts_is_cached_per_request(self):
        from jarz_pos.services import cash_custody

        mock_frappe = _mock_frappe()
        mock_frappe.db.table_exists.return_value = True
        mock_frappe.get_all.return_value = [{"name": "EMP-0001", "account": CUSTODY}]
        with patch.object(cash_custody, "frappe", mock_frappe):
            first = cash_custody.custody_accounts()
            second = cash_custody.custody_accounts()
        self.assertEqual(first, {CUSTODY: "EMP-0001"})
        self.assertEqual(second, first)
        self.assertEqual(mock_frappe.get_all.call_count, 1)

    def test_missing_table_means_no_custody_accounts(self):
        from jarz_pos.services import cash_custody

        mock_frappe = _mock_frappe()
        mock_frappe.db.table_exists.return_value = False
        with patch.object(cash_custody, "frappe", mock_frappe):
            self.assertEqual(cash_custody.custody_accounts(), {})
        mock_frappe.get_all.assert_not_called()


# ── issue / return permission matrix ───────────────────────────────────────


class _ApiCase(unittest.TestCase):
    MANAGER_SOURCES = ["Nasr - J", "Dokki - J", "Cash - J"]
    OWN_SOURCES = ["Nasr - J"]

    def _call(self, fn_name, *, user="holder@example.com", can_manage=False,
              holder=None, can_cover=None, **kwargs):
        from jarz_pos.api import cash_custody as api

        holder = holder or _holder()
        mock_frappe = _mock_frappe(user=user)
        posted = MagicMock(return_value=SimpleNamespace(name="ACC-JV-0001"))

        def _returns(company, manage):
            branches = ["Nasr - J", "Dokki - J", "Maadi - J"]
            return [_src(a) for a in branches + (["Cash - J", "CIB - J"] if manage else [])]

        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "frappe", mock_frappe))
            stack.enter_context(patch.object(api, "_load_holder", return_value=holder))
            stack.enter_context(patch.object(api, "_can_manage", return_value=can_manage))
            stack.enter_context(patch.object(
                api, "_manager_source_accounts",
                return_value=[_src(a) for a in self.MANAGER_SOURCES],
            ))
            stack.enter_context(patch.object(
                api, "_holder_source_accounts",
                return_value=[_src(a) for a in self.OWN_SOURCES],
            ))
            stack.enter_context(patch.object(api, "_return_accounts", side_effect=_returns))
            stack.enter_context(patch.object(api, "_validate_counter_account"))
            stack.enter_context(patch.object(api, "_post_custody_je", posted))
            stack.enter_context(patch.object(api, "_serialize_holder", return_value={"name": holder["name"]}))
            cover = stack.enter_context(patch.object(api.cash_custody, "ensure_custody_can_cover"))
            if can_cover is not None:
                cover.side_effect = can_cover
            result = getattr(api, fn_name)(holder=holder["name"], **kwargs)
        return result, posted, cover


class TestIssueCustody(_ApiCase):
    def test_manager_issues_from_any_source(self):
        result, posted, _cover = self._call(
            "issue_custody", user="boss@example.com", can_manage=True,
            from_account="Dokki - J", amount=500,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["journal_entry"], "ACC-JV-0001")
        kwargs = posted.call_args.kwargs
        self.assertEqual(kwargs["debit_account"], CUSTODY)
        self.assertEqual(kwargs["credit_account"], "Dokki - J")
        self.assertEqual(kwargs["amount"], 500.0)
        self.assertEqual(kwargs["tag_type"], "CUSTODY_ISSUE")

    def test_holder_issues_from_own_branch(self):
        result, posted, _cover = self._call(
            "issue_custody", from_account="Nasr - J", amount="250",
        )
        self.assertTrue(result["success"])
        posted.assert_called_once()

    def test_holder_cannot_draw_on_a_branch_that_is_not_theirs(self):
        with self.assertRaises(_Refused) as ctx:
            self._call("issue_custody", from_account="Dokki - J", amount=100)
        self.assertIs(ctx.exception.exc, _PermissionError)

    def test_other_user_is_refused(self):
        with self.assertRaises(_Refused) as ctx:
            self._call("issue_custody", user="someone@example.com",
                       from_account="Nasr - J", amount=100)
        self.assertIs(ctx.exception.exc, _PermissionError)

    def test_disabled_holder_is_refused_even_for_a_manager(self):
        with self.assertRaises(_Refused):
            self._call("issue_custody", user="boss@example.com", can_manage=True,
                       holder=_holder(enabled=0), from_account="Cash - J", amount=100)

    def test_manager_cannot_issue_from_an_unlisted_account(self):
        with self.assertRaises(_Refused):
            self._call("issue_custody", user="boss@example.com", can_manage=True,
                       from_account="Custody - Omar - J", amount=100)

    def test_zero_amount_is_refused(self):
        with self.assertRaises(_Refused):
            self._call("issue_custody", from_account="Nasr - J", amount=0)


class TestReturnCustody(_ApiCase):
    def test_holder_returns_to_another_branch(self):
        result, posted, cover = self._call(
            "return_custody", to_account="Maadi - J", amount=60,
        )
        self.assertTrue(result["success"])
        kwargs = posted.call_args.kwargs
        self.assertEqual(kwargs["debit_account"], "Maadi - J")
        self.assertEqual(kwargs["credit_account"], CUSTODY)
        self.assertEqual(kwargs["tag_type"], "CUSTODY_RETURN")
        cover.assert_called_once()
        self.assertTrue(cover.call_args.kwargs.get("for_update"))

    def test_return_beyond_balance_is_refused_before_posting(self):
        def _refuse(*args, **kwargs):
            raise _Refused("Custody Ali holds 50.00; this entry would take 60.00 out of it.")

        with self.assertRaises(_Refused):
            self._call("return_custody", to_account="Maadi - J", amount=60, can_cover=_refuse)

    def test_holder_cannot_return_into_a_bank_account(self):
        with self.assertRaises(_Refused) as ctx:
            self._call("return_custody", to_account="CIB - J", amount=10)
        self.assertIs(ctx.exception.exc, _PermissionError)

    def test_manager_may_return_into_a_bank_account(self):
        result, posted, _cover = self._call(
            "return_custody", user="boss@example.com", can_manage=True,
            to_account="CIB - J", amount=10,
        )
        self.assertTrue(result["success"])
        posted.assert_called_once()

    def test_other_user_is_refused(self):
        with self.assertRaises(_Refused) as ctx:
            self._call("return_custody", user="someone@example.com",
                       to_account="Nasr - J", amount=10)
        self.assertIs(ctx.exception.exc, _PermissionError)


# ── create_expense routing ─────────────────────────────────────────────────


class _FakeExpenseDoc:
    def __init__(self):
        self.flags = SimpleNamespace(ignore_permissions=False)
        self.insert = MagicMock()
        self.submit = MagicMock()
        self.reload = MagicMock()

    def as_dict(self):
        return {"name": "JEXP-00001"}


class TestCreateExpenseCustody(unittest.TestCase):
    def _create(self, *, is_manager=False, holder=None, custody_map=None, cover=None, **payload):
        from jarz_pos.api import expenses

        mock_frappe = _mock_frappe(user="holder@example.com")
        captured = {}
        fake_doc = _FakeExpenseDoc()

        def _get_doc(doc_dict):
            captured.update(doc_dict)
            return fake_doc

        mock_frappe.get_doc.side_effect = _get_doc
        mock_frappe.db.get_value.return_value = "Custody - Ali"

        custody = MagicMock()
        custody.custody_accounts.return_value = (
            {CUSTODY: "EMP-0001", "Custody - Omar - J": "EMP-0002"} if custody_map is None else custody_map
        )
        custody.holder_for_user.return_value = holder
        custody.custody_account_labels.return_value = {"label_en": "Custody - Ali", "label_ar": "عهدة - Ali"}
        if cover is not None:
            custody.ensure_custody_can_cover.side_effect = cover

        with ExitStack() as stack:
            stack.enter_context(patch.object(expenses, "frappe", mock_frappe))
            stack.enter_context(patch.object(expenses, "cash_custody", custody))
            stack.enter_context(patch.object(expenses, "_is_manager", return_value=is_manager))
            stack.enter_context(patch.object(expenses, "_default_company", return_value="Jarz"))
            stack.enter_context(patch.object(
                expenses, "_current_user_pos_profile_names", return_value=["Nasr"]
            ))
            stack.enter_context(patch.object(expenses, "_resolve_named_account", return_value="Nasr - J"))
            stack.enter_context(patch.object(expenses, "_serialize_expense", return_value={"name": "JEXP-00001"}))
            stack.enter_context(patch.object(expenses, "now_datetime", return_value="2026-09-23 10:00:00"))
            data = {"amount": 40, "reason_account": "Transport - J", "expense_date": "2026-09-23"}
            data.update(payload)
            result = expenses.create_expense(**data)
        return result, captured, custody, fake_doc

    def test_holder_spends_from_own_custody_pending_approval(self):
        result, captured, custody, fake_doc = self._create(
            holder=_holder(), payment_source_type="custody",
        )
        self.assertTrue(result["success"])
        self.assertEqual(captured["paying_account"], CUSTODY)
        self.assertEqual(captured["payment_source_type"], "Custody")
        self.assertEqual(captured["payment_source_label"], "Custody - Ali")
        self.assertIsNone(captured["pos_profile"])
        self.assertEqual(captured["requires_approval"], 1)
        custody.ensure_custody_can_cover.assert_called_once()
        fake_doc.submit.assert_not_called()

    def test_naming_own_custody_account_routes_to_custody(self):
        _result, captured, _custody, _doc = self._create(holder=_holder(), paying_account=CUSTODY)
        self.assertEqual(captured["payment_source_type"], "Custody")
        self.assertEqual(captured["paying_account"], CUSTODY)

    def test_non_holder_asking_for_custody_is_refused(self):
        with self.assertRaises(_Refused) as ctx:
            self._create(holder=None, payment_source_type="Custody")
        self.assertIs(ctx.exception.exc, _PermissionError)

    def test_holder_naming_another_holders_custody_is_refused(self):
        with self.assertRaises(_Refused) as ctx:
            self._create(holder=_holder(), paying_account="Custody - Omar - J")
        self.assertIs(ctx.exception.exc, _PermissionError)

    def test_non_holder_naming_a_custody_account_is_refused(self):
        with self.assertRaises(_Refused):
            self._create(holder=None, paying_account=CUSTODY)

    def test_amount_over_balance_is_refused_before_insert(self):
        def _refuse(*args, **kwargs):
            raise _Refused("Custody Ali holds 10.00; this entry would take 40.00 out of it.")

        with self.assertRaises(_Refused):
            self._create(holder=_holder(), payment_source_type="custody", cover=_refuse)

    def test_pos_profile_path_is_unchanged(self):
        _result, captured, custody, _doc = self._create(
            holder=_holder(), pos_profile="Nasr", paying_account="Nasr - J",
        )
        self.assertEqual(captured["paying_account"], "Nasr - J")
        self.assertEqual(captured["payment_source_type"], "POS Profile")
        self.assertEqual(captured["pos_profile"], "Nasr")
        custody.ensure_custody_can_cover.assert_not_called()

    def test_manager_paying_from_custody_is_typed_and_balance_checked(self):
        _result, captured, custody, fake_doc = self._create(
            is_manager=True, paying_account=CUSTODY, payment_source_type="custody",
        )
        self.assertEqual(captured["payment_source_type"], "Custody")
        self.assertEqual(captured["requires_approval"], 0)
        custody.ensure_custody_can_cover.assert_called_once()
        fake_doc.submit.assert_called_once()


# ── purchase custody option ────────────────────────────────────────────────


class TestPurchaseCustodyOption(unittest.TestCase):
    ROW = {
        "name": "EMP-0001",
        "user": "holder@example.com",
        "enabled": 1,
        "account": CUSTODY,
        "company": "Jarz",
        "employee_name": "Ali",
    }

    def _resolve(self, option, *, user="holder@example.com", roles=(), row=None, fn="_resolve_custody_option"):
        from jarz_pos.api import purchase

        mock_frappe = _mock_frappe(user=user, roles=roles)
        mock_frappe.db.get_value.return_value = dict(self.ROW if row is None else row)
        mock_frappe.db.exists.return_value = False
        with patch.object(purchase, "frappe", mock_frappe):
            return getattr(purchase, fn)(option, "Jarz")

    def test_plain_options_are_not_custody(self):
        from jarz_pos.api import purchase

        mock_frappe = _mock_frappe()
        with patch.object(purchase, "frappe", mock_frappe):
            self.assertIsNone(purchase._resolve_custody_option("cash", "Jarz"))
            self.assertIsNone(purchase._resolve_custody_option("Nasr", "Jarz"))
        mock_frappe.db.get_value.assert_not_called()

    def test_holder_resolves_own_custody(self):
        self.assertEqual(self._resolve("custody:EMP-0001"), CUSTODY)

    def test_manager_resolves_any_custody(self):
        self.assertEqual(
            self._resolve("custody:EMP-0001", user="boss@example.com", roles=("Accounts Manager",)),
            CUSTODY,
        )

    def test_other_user_is_refused(self):
        with self.assertRaises(_Refused) as ctx:
            self._resolve("custody:EMP-0001", user="someone@example.com", roles=("Purchase User",))
        self.assertIs(ctx.exception.exc, _PermissionError)

    def test_disabled_holder_is_refused(self):
        with self.assertRaises(_Refused):
            self._resolve("custody:EMP-0001", row=dict(self.ROW, enabled=0))

    def test_pay_purchase_invoice_resolver_uses_custody(self):
        from jarz_pos.api import purchase
        from jarz_pos.constants import PAYMENT_MODES

        self.assertEqual(self._resolve("custody:EMP-0001", fn="_resolve_payment_account"), CUSTODY)
        self.assertEqual(purchase._resolve_payment_mode("custody:EMP-0001", "Jarz"), PAYMENT_MODES.CASH)


# ── statement ──────────────────────────────────────────────────────────────


class TestStatement(unittest.TestCase):
    ROWS = [
        {"posting_date": "2026-09-01", "voucher_type": "Journal Entry", "voucher_no": "JE-1",
         "debit": 1000, "credit": 0, "remarks": "Custody issue to Ali [JARZ-JE:CUSTODY_ISSUE:EMP-0001:a1]",
         "against": "Nasr - J"},
        {"posting_date": "2026-09-02", "voucher_type": "Journal Entry", "voucher_no": "JE-2",
         "debit": 0, "credit": 150, "remarks": "Taxi", "against": "Transport - J"},
        {"posting_date": "2026-09-03", "voucher_type": "Purchase Invoice", "voucher_no": "PI-1",
         "debit": 0, "credit": 300, "remarks": "", "against": "Supplier A"},
        {"posting_date": "2026-09-04", "voucher_type": "Payment Entry", "voucher_no": "PE-1",
         "debit": 0, "credit": 100, "remarks": "", "against": "Supplier B"},
        {"posting_date": "2026-09-05", "voucher_type": "Journal Entry", "voucher_no": "JE-3",
         "debit": 0, "credit": 200, "remarks": "[JARZ-JE:CUSTODY_RETURN:EMP-0001:b2]",
         "against": "Maadi - J"},
        {"posting_date": "2026-09-06", "voucher_type": "Journal Entry", "voucher_no": "JE-4",
         "debit": 25, "credit": 0, "remarks": "Desk top-up", "against": "Cash - J"},
        {"posting_date": "2026-09-07", "voucher_type": "Journal Entry", "voucher_no": "JE-5",
         "debit": 0, "credit": 5, "remarks": "Desk", "against": "Cash - J"},
    ]

    def _kinds(self):
        from jarz_pos.api import cash_custody as api

        mock_frappe = _mock_frappe()

        def _get_all(doctype, *args, **kwargs):
            if doctype == "Jarz Expense Request":
                return ["JE-2"]
            if doctype == "Payment Entry Reference":
                return ["PE-1"]
            return []

        mock_frappe.get_all.side_effect = _get_all
        tags = {
            "JE-1": "[JARZ-JE:CUSTODY_ISSUE:EMP-0001:a1]",
            "JE-2": None,
            "JE-3": "[JARZ-JE:CUSTODY_RETURN:EMP-0001:b2]",
            "JE-4": None,
            "JE-5": None,
        }
        with patch.object(api, "frappe", mock_frappe), patch.object(api, "_je_tags", return_value=tags):
            return api._voucher_kinds(self.ROWS)

    def test_kind_classification(self):
        from jarz_pos.api import cash_custody as api

        entries = api.build_statement_entries(self.ROWS, 0, self._kinds())
        self.assertEqual(
            [e["kind"] for e in entries],
            ["issue", "expense", "purchase", "purchase", "return", "transfer_in", "transfer_out"],
        )

    def test_running_balance_and_clean_remarks(self):
        from jarz_pos.api import cash_custody as api

        entries = api.build_statement_entries(
            self.ROWS, 100, self._kinds(), {"Nasr - J": {"label": "Nasr"}}
        )
        self.assertEqual(
            [e["balance"] for e in entries],
            [1100.0, 950.0, 650.0, 550.0, 350.0, 375.0, 370.0],
        )
        self.assertEqual(entries[0]["remark"], "Custody issue to Ali")
        self.assertEqual(entries[0]["counter_account"], "Nasr - J")
        self.assertEqual(entries[0]["counter_label"], "Nasr")
        self.assertEqual(entries[2]["counter_label"], "Supplier A")

    def _statement(self, *, user, can_manage):
        from jarz_pos.api import cash_custody as api

        mock_frappe = _mock_frappe(user=user)
        mock_frappe.db.sql.side_effect = [((100.0,),), list(self.ROWS)]
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "frappe", mock_frappe))
            stack.enter_context(patch.object(api, "_load_holder", return_value=_holder()))
            stack.enter_context(patch.object(api, "_can_manage", return_value=can_manage))
            stack.enter_context(patch.object(api, "_voucher_kinds", return_value={}))
            stack.enter_context(patch.object(api._expenses, "_account_label_map", return_value={}))
            stack.enter_context(patch.object(api, "_serialize_holder", return_value={"name": "EMP-0001"}))
            return api.get_custody_statement(
                "EMP-0001", from_date="2026-09-01", to_date="2026-09-30", limit=3
            )

    def test_statement_opening_closing_and_newest_first(self):
        result = self._statement(user="holder@example.com", can_manage=False)
        self.assertTrue(result["success"])
        self.assertEqual(result["opening_balance"], 100.0)
        self.assertEqual(result["closing_balance"], 370.0)
        self.assertEqual(result["from_date"], "2026-09-01")
        self.assertEqual(len(result["entries"]), 3)
        self.assertEqual(result["entries"][0]["voucher_no"], "JE-5")
        self.assertEqual(result["entries"][0]["balance"], 370.0)

    def test_statement_refused_for_another_user(self):
        with self.assertRaises(_Refused) as ctx:
            self._statement(user="someone@example.com", can_manage=False)
        self.assertIs(ctx.exception.exc, _PermissionError)


# ── holder controller: disabling ───────────────────────────────────────────


class TestHolderDisable(unittest.TestCase):
    def _disable(self, balance, was_enabled=1):
        from jarz_pos.doctype.jarz_custody_holder import jarz_custody_holder as mod

        doc = mod.JarzCustodyHolder.__new__(mod.JarzCustodyHolder)
        doc.__dict__.update(
            {
                "doctype": "Jarz Custody Holder",
                "name": "EMP-0001",
                "employee": "EMP-0001",
                "employee_name": "Ali",
                "company": "Jarz",
                "account": CUSTODY,
                "enabled": 0,
            }
        )
        doc.get_doc_before_save = lambda: {"enabled": was_enabled}
        mock_frappe = _mock_frappe()
        with patch.object(mod, "frappe", mock_frappe), patch.object(
            mod.cash_custody, "custody_balance", return_value=balance
        ) as bal:
            doc._guard_disable()
        return bal

    def test_disabling_with_money_held_is_refused(self):
        with self.assertRaises(_Refused):
            self._disable(balance=10)

    def test_disabling_an_empty_custody_is_allowed(self):
        bal = self._disable(balance=0)
        bal.assert_called_once_with(CUSTODY, "Jarz", for_update=True)

    def test_saving_an_already_disabled_holder_does_not_recheck(self):
        bal = self._disable(balance=10, was_enabled=0)
        bal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
