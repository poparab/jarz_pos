"""Customer credit ("on account") APIs.

WHAT THIS IS FOR
----------------
B2B shops — coffee shops — sometimes take an order on credit: the goods are
delivered and nothing is paid at the door. The decision is made PER ORDER, not
per customer: the same shop pays cash on one order and takes the next on
account. Settlement is informal and ROLLING — in the owner's words, "a lot of
cases it is an invoice after invoice, so when I send them the second invoice
they pay the first invoice."

That single sentence is the whole design brief, and it rules out the obvious
implementation. What is needed is a RUNNING BALANCE per customer, not an
overdue alarm and not a per-invoice collections workflow:

* :func:`get_credit_ledger` — what each shop owes, all-time, oldest first.
* :func:`record_credit_payment` — ONE payment, allocated FIFO across whatever
  is open. The shop hands over money that clears "the previous invoice(s)";
  they do not name one.
* :func:`get_customer_credit_profile` — what the POS asks before it will offer
  the Credit button at checkout.

Nothing here arms a reminder or an escalation. A credit order deliberately does
NOT get ``custom_payment_confirmation_status = "Awaiting Payment"`` at dispatch
(see ``services/delivery_handling.handle_credit_deliver_on_account``), because
that stamp is what puts an order into the InstaPay verification queue and arms
the HOURLY alarm in ``api/escalations.py``. Thirty-day trade credit is not an
incident.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate

from jarz_pos.constants import ROLES
from jarz_pos.utils.credit_utils import (
    CREDIT_PAYMENT_METHOD,  # noqa: F401  (re-export; see the note below)
    apply_credit_invoice_match,
)

# ``CREDIT_PAYMENT_METHOD`` is re-exported here for the callers that already
# import it from this module. The definition — and the OR predicate that goes
# with it — lives in ``utils/credit_utils`` so this module, ``api/kanban`` and
# ``services/invoice_creation`` cannot drift apart about what a credit debt is.

#: Same fallback ``invoice_creation`` uses when a customer is allowed credit but
#: nobody typed a number of days.
DEFAULT_CREDIT_DAYS = 30

#: Default ACTIVITY window, matching ``manager.get_employee_ledger``. A ledger is
#: an activity feed, not a one-day object, so it does NOT default to today.
_CREDIT_LEDGER_DEFAULT_DAYS = 90


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _log_credit_error(summary: str) -> None:
    """Log a failure without ever *becoming* the failure.

    ``frappe.log_error`` writes an Error Log row, so it can itself raise (read-only
    replica, disk full, the DocType missing mid-migrate). Called from ``except``
    blocks that would otherwise turn a degraded section into a 500.
    """
    try:
        frappe.log_error(frappe.get_traceback(), summary)
    except Exception:
        pass


def _credit_currency() -> str:
    """Presentation currency for the ledger totals.

    Every amount here is company-currency, so one label for the whole payload is
    honest. Falls back to EGP exactly as ``manager._employee_ledger_currency``.
    """
    try:
        company = frappe.defaults.get_global_default("company")
        if company:
            currency = frappe.get_cached_value("Company", company, "default_currency")
            if currency:
                return str(currency)
    except Exception:
        pass
    return "EGP"


def _branch_field() -> str:
    """Sales Invoice field that carries the operational branch.

    Same resolution ``manager.get_manager_orders`` uses: ``custom_kanban_profile``
    is the branch an order currently belongs to (it moves on transfer),
    ``pos_profile`` is where it was created and is read-only after submit.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        return "custom_kanban_profile" if meta.get_field("custom_kanban_profile") else "pos_profile"
    except Exception:
        return "pos_profile"


def _ensure_credit_ledger_access() -> None:
    """Read gate for the credit screens.

    Same tier as the Manager Dashboard (``manager._ensure_manager_dashboard_access``):
    a line manager runs a branch and chases its debts, so they are in; a POS
    cashier is not.
    """
    from jarz_pos.api.manager import _ensure_manager_dashboard_access

    _ensure_manager_dashboard_access()


def _ensure_credit_payment_access() -> None:
    """Write gate for taking money against a credit balance.

    Deliberately the SAME set the other money endpoints use
    (``api/couriers._ensure_collection_change_access``) rather than a new one:
    this posts a submitted Payment Entry into a branch drawer, which is exactly
    the authority "change collection method" already carries.
    """
    roles = {str(role or "").strip() for role in (frappe.get_roles() or []) if str(role or "").strip()}
    allowed = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Manager access required"), frappe.PermissionError)


def _allowed_profiles() -> List[str]:
    from jarz_pos.api.manager import _current_user_allowed_profiles

    return _current_user_allowed_profiles() or []


def _customer_credit_settings(customer: str) -> Dict[str, Any]:
    """The three credit columns off a Customer, failing CLOSED.

    Every field is seeded by ``setup/credit_terms.py``, so on a bench that has
    not migrated the read raises rather than returning ``None``. That must
    degrade to "credit not allowed" and never to "allowed with no limit".
    """
    result: Dict[str, Any] = {"allowed": False, "days": 0, "limit": 0.0}
    name = str(customer or "").strip()
    if not name:
        return result
    try:
        row = frappe.db.get_value(
            "Customer",
            name,
            ["custom_credit_allowed", "custom_credit_days", "custom_credit_limit_amount"],
            as_dict=True,
        )
    except Exception:
        return result
    if not row:
        return result
    try:
        result["allowed"] = bool(int(row.get("custom_credit_allowed") or 0))
    except Exception:
        result["allowed"] = bool(row.get("custom_credit_allowed"))
    try:
        result["days"] = max(0, int(row.get("custom_credit_days") or 0))
    except Exception:
        result["days"] = 0
    try:
        result["limit"] = max(0.0, flt(row.get("custom_credit_limit_amount")))
    except Exception:
        result["limit"] = 0.0
    return result


def _open_credit_invoice_fields() -> List[str]:
    fields = [
        "name",
        "customer",
        "customer_name",
        "posting_date",
        "due_date",
        "grand_total",
        "outstanding_amount",
        "status",
        _branch_field(),
    ]
    try:
        if frappe.get_meta("Sales Invoice").get_field("custom_credit_terms_days"):
            fields.append("custom_credit_terms_days")
    except Exception:
        pass
    # de-dup while preserving order (branch field may already be in the list)
    seen: set = set()
    ordered: List[str] = []
    for field in fields:
        if field not in seen:
            seen.add(field)
            ordered.append(field)
    return ordered


def _open_credit_invoices(
    *,
    customers: Optional[List[str]] = None,
    profiles: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Every OPEN credit invoice, oldest first. Never date-filtered.

    This is the BALANCE query. It is deliberately uncapped and deliberately
    all-time: truncating a list loses rows, truncating a balance states the
    wrong amount of money — and the older the debt, the more certainly a date
    window would hide exactly the row somebody is hunting for.

    **The credit match is an OR, and that is load-bearing.**
    ``custom_payment_method`` is a MUTABLE column: ``change_payment_collection_method``
    rewrites it on every collection-method change, and its ``unpaid_online_retarget``
    branch posts NO voucher — the outstanding stays exactly where it was. Keyed on
    the method alone, a manager tapping "Change collection method -> Instapay" on a
    credit card made a real debt disappear from this query: out of the ledger, out of
    the customer's used limit, and unreachable by ``record_credit_payment``'s FIFO
    allocation. ``custom_credit_terms_days`` is the stamp
    ``_apply_credit_terms`` freezes at creation and nothing ever rewrites, so it is
    permanent provenance. ``api/kanban`` and ``invoice_creation.get_open_credit_balance``
    use the same predicate; see ``utils/credit_utils``.

    ``is_return: 0`` is KEPT deliberately. A credit note against an unpaid credit
    invoice does not sit here waiting to be netted off: ``services/invoice_return``
    posts a ``JE_AR_KNOCKOFF`` that credits Debtors against the ORIGINAL invoice, so
    the surviving row's ``outstanding_amount`` is already reduced by the return and
    the credit note's own outstanding is zero. Admitting returns would not correct a
    balance — it would hand ``record_credit_payment`` a negative-outstanding row to
    allocate a Receive Payment Entry against.
    """
    filters: Dict[str, Any] = {
        "docstatus": 1,
        "outstanding_amount": [">", 0.005],
        "is_return": 0,
    }
    or_filters = apply_credit_invoice_match(filters)
    if customers is not None:
        if not customers:
            return []
        filters["customer"] = ["in", customers]
    if profiles is not None:
        if not profiles:
            return []
        filters[_branch_field()] = ["in", profiles]
    try:
        return (
            frappe.get_all(
                "Sales Invoice",
                filters=filters,
                or_filters=or_filters,
                fields=_open_credit_invoice_fields(),
                # FIFO order, and the same order the allocation below walks in.
                order_by="posting_date asc, creation asc",
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        _log_credit_error("Credit ledger: open credit invoice query failed")
        return []


def _age_days(posting_date: Any) -> int:
    try:
        return max(0, int((getdate(nowdate()) - getdate(posting_date)).days))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=False)
def get_credit_ledger(
    customer: Optional[str] = None,
    pos_profile: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: Union[int, str, None] = 200,
) -> Dict[str, Any]:
    """What each customer owes on account, with their open invoices oldest-first.

    **THE DATE WINDOW DESCRIBES ACTIVITY. IT NEVER LIMITS THE BALANCE.**

    Read that twice before changing anything here, because collapsing the two
    back into one filtered query is a very natural "simplification" and it
    silently breaks the only number on the screen that matters. This is the
    customer analogue of :func:`jarz_pos.api.manager.get_employee_ledger` and it
    copies its semantics exactly:

    * ``invoices`` is the ROWS LISTED, and it honours ``from_date`` /
      ``to_date``. This is the activity feed — what was sold on credit in the
      window, paid or not.
    * ``summary.total_outstanding``, ``customers[].outstanding`` and
      ``customers[].open_invoices`` are the BALANCE, computed over EVERY open
      credit invoice regardless of date. An invoice unpaid since March is
      exactly the debt somebody is hunting for; under a windowed total it would
      contribute nothing while the client still labelled the figure "total
      outstanding". The older and more delinquent the debt, the more certainly
      it would disappear. ``summary.outstanding_is_all_time`` is always ``True``
      so the client can say so out loud.

    A consequence, and it is correct: a shop can appear in ``customers`` with a
    real balance and an EMPTY ``invoices`` list, because all their activity
    predates the window. The rollup is driven by what is owed, not by what is
    listed. The reverse also holds — a shop whose only credit order in the
    window was already paid appears with a zero balance.

    Dates default to the **last 90 days**, not to today: a rolling settlement
    pattern ("they pay the first when I hand them the second") has no meaningful
    single day. The listed section is capped at ``limit`` (200 by default, 500
    max); if it is truncated the payload says so via
    ``notice_code = "results_truncated"`` rather than dropping rows quietly. The
    balance queries are deliberately UNCAPPED.

    Branch scoping is the same as everywhere else: the caller sees only orders
    belonging to the POS Profiles they are assigned to (Administrator excepted).
    A shop that orders from two branches therefore shows a *different* balance
    to a manager of one branch than to an administrator — which is honest, and
    is why ``filters.branch`` is echoed back.

    Args:
        customer: Restrict to one Customer. Omitted means every customer with an
            open credit balance in the caller's branches.
        pos_profile: Branch to narrow to; omitted or "all" means every assigned
            branch. A branch the caller is not assigned to returns an empty
            ledger with ``notice_code = "branch_not_permitted"``.
        from_date: Start of the ACTIVITY window (default: 89 days before today).
            Does not affect any outstanding figure.
        to_date: End of the ACTIVITY window (default: today). Likewise.
        limit: Max rows in the LISTED ``invoices`` section. Capped at 500. The
            balance is never capped.

    Returns:
        ``success``, ``filters``, ``summary``, ``customers`` (rollup, biggest
        balance first, each with ``open_invoices`` oldest-first), ``invoices``
        (the activity feed), plus optional ``notice_code`` / ``notice``.
    """
    _ensure_credit_ledger_access()

    try:
        limit_value = max(1, min(int(limit or 200), 500))
    except Exception:
        limit_value = 200

    end_date = getdate(to_date) if to_date else getdate(nowdate())
    start_date = (
        getdate(from_date)
        if from_date
        else getdate(add_days(nowdate(), -(_CREDIT_LEDGER_DEFAULT_DAYS - 1)))
    )
    if start_date > end_date:
        frappe.throw(_("From date cannot be later than To date"))

    selected_customer = str(customer or "").strip()
    selected_branch = str(pos_profile or "").strip()
    currency = _credit_currency()
    filters_echo: Dict[str, Any] = {
        "from_date": str(start_date),
        "to_date": str(end_date),
        "customer": selected_customer or None,
        "branch": selected_branch or None,
    }

    def _empty(notice_code: str, notice: str) -> Dict[str, Any]:
        return {
            "success": True,
            "filters": filters_echo,
            "summary": {
                "total_outstanding": 0.0,
                "customer_count": 0,
                "invoice_count": 0,
                "listed_count": 0,
                "oldest_invoice_date": None,
                "currency": currency,
                "outstanding_is_all_time": True,
            },
            "customers": [],
            "invoices": [],
            "notice_code": notice_code,
            "notice": notice,
        }

    allowed = _allowed_profiles()
    if not allowed:
        # Passing the role gate while owning no branch is a setup problem, not an
        # empty ledger — say which, exactly as the other dashboards do.
        return _empty(
            "no_branch_assigned",
            _(
                "You are not assigned to any branch (POS Profile). Ask an "
                "administrator to add you to the branches you manage."
            ),
        )

    profiles = list(allowed)
    if selected_branch and selected_branch.lower() != "all":
        if selected_branch not in allowed:
            return _empty(
                "branch_not_permitted",
                _("You are not assigned to branch {0}.").format(selected_branch),
            )
        profiles = [selected_branch]

    customers_filter = [selected_customer] if selected_customer else None
    branch_field = _branch_field()

    # --- What is OWED: every open credit invoice, all time -------------------
    open_rows = _open_credit_invoices(customers=customers_filter, profiles=profiles)

    # --- What is LISTED: the activity window ---------------------------------
    listed_rows: List[Dict[str, Any]] = []
    listed_truncated = False
    try:
        activity_filters: Dict[str, Any] = {
            "docstatus": 1,
            "posting_date": ["between", [str(start_date), str(end_date)]],
            branch_field: ["in", profiles],
        }
        # Same OR as the balance query above, for the same reason: the payment
        # method is rewritten by a collection-method change, the frozen terms
        # stamp is not. Without it an order relabelled after dispatch vanishes
        # from the activity feed too, so the screen would not even show the row
        # a manager is trying to reconcile.
        activity_or_filters = apply_credit_invoice_match(activity_filters)
        if selected_customer:
            activity_filters["customer"] = selected_customer
        listed_rows = (
            frappe.get_all(
                "Sales Invoice",
                filters=activity_filters,
                or_filters=activity_or_filters,
                fields=_open_credit_invoice_fields(),
                order_by="posting_date desc, creation desc",
                # One extra row is asked for so truncation can be DETECTED rather
                # than guessed from "len == limit", which is wrong exactly when
                # the count lands on the cap.
                limit_page_length=limit_value + 1,
            )
            or []
        )
        if len(listed_rows) > limit_value:
            listed_truncated = True
            listed_rows = listed_rows[:limit_value]
    except Exception:
        _log_credit_error("Credit ledger: activity query failed")
        listed_rows = []

    # --- Rollup ---------------------------------------------------------------
    people: Dict[str, Dict[str, Any]] = {}

    def _bucket(name: str, display: str) -> Dict[str, Any]:
        row = people.get(name)
        if row is None:
            settings = _customer_credit_settings(name)
            row = {
                "customer": name,
                "customer_name": display or name,
                "outstanding": 0.0,
                "invoice_count": 0,
                "oldest_invoice_date": None,
                "oldest_age_days": 0,
                "credit_allowed": bool(settings["allowed"]),
                "credit_days": int(settings["days"] or DEFAULT_CREDIT_DAYS),
                "credit_limit": flt(settings["limit"], 2),
                "open_invoices": [],
            }
            people[name] = row
        return row

    total_outstanding = 0.0
    oldest_overall: Optional[str] = None

    for row in open_rows:
        name = str(row.get("customer") or "")
        if not name:
            continue
        outstanding = flt(row.get("outstanding_amount"))
        if outstanding <= 0.005:
            continue
        bucket = _bucket(name, str(row.get("customer_name") or name))
        bucket["outstanding"] += outstanding
        bucket["invoice_count"] += 1
        posting_date = str(row.get("posting_date") or "") or None
        # ``open_rows`` is already oldest-first, so the first one wins.
        if posting_date and not bucket["oldest_invoice_date"]:
            bucket["oldest_invoice_date"] = posting_date
            bucket["oldest_age_days"] = _age_days(posting_date)
        if posting_date and (oldest_overall is None or posting_date < oldest_overall):
            oldest_overall = posting_date
        bucket["open_invoices"].append(
            {
                "invoice": row.get("name"),
                "posting_date": posting_date,
                "due_date": str(row.get("due_date") or "") or None,
                "grand_total": flt(row.get("grand_total"), 2),
                "outstanding_amount": flt(outstanding, 2),
                "age_days": _age_days(posting_date),
                "credit_terms_days": int(row.get("custom_credit_terms_days") or 0),
                "branch": str(row.get(branch_field) or ""),
                "status": row.get("status"),
            }
        )
        total_outstanding += outstanding

    customers_payload: List[Dict[str, Any]] = []
    for row in people.values():
        row["outstanding"] = flt(row["outstanding"], 2)
        limit_amount = flt(row["credit_limit"])
        row["available_credit"] = (
            flt(max(0.0, limit_amount - row["outstanding"]), 2) if limit_amount > 0 else None
        )
        customers_payload.append(row)
    # Biggest debt first; the name breaks ties so the order is stable between
    # requests (a jumping table reads as data changing when it has not).
    customers_payload.sort(
        key=lambda r: (-r["outstanding"], str(r.get("customer_name") or ""))
    )

    invoices_payload: List[Dict[str, Any]] = []
    for row in listed_rows:
        posting_date = str(row.get("posting_date") or "") or None
        invoices_payload.append(
            {
                "invoice": row.get("name"),
                "customer": row.get("customer"),
                "customer_name": row.get("customer_name") or row.get("customer"),
                "posting_date": posting_date,
                "due_date": str(row.get("due_date") or "") or None,
                "grand_total": flt(row.get("grand_total"), 2),
                "outstanding_amount": flt(row.get("outstanding_amount"), 2),
                "age_days": _age_days(posting_date),
                "credit_terms_days": int(row.get("custom_credit_terms_days") or 0),
                "branch": str(row.get(branch_field) or ""),
                "status": row.get("status"),
            }
        )

    payload: Dict[str, Any] = {
        "success": True,
        "filters": filters_echo,
        "summary": {
            # All-time, by design. See the docstring.
            "total_outstanding": flt(total_outstanding, 2),
            "customer_count": sum(1 for r in customers_payload if r["outstanding"] > 0.005),
            "invoice_count": sum(int(r["invoice_count"]) for r in customers_payload),
            # Activity count: rows listed inside the window.
            "listed_count": len(invoices_payload),
            "oldest_invoice_date": oldest_overall,
            "currency": currency,
            "outstanding_is_all_time": True,
        },
        "customers": customers_payload,
        "invoices": invoices_payload,
    }

    if listed_truncated:
        # Truncation is reported, never silent. The wording is careful: only the
        # LIST is cut. The outstanding totals are all-time and uncapped, so they
        # remain correct — saying otherwise would send a manager chasing a
        # shortfall that does not exist.
        payload["notice_code"] = "results_truncated"
        payload["notice"] = _(
            "Showing the first {0} orders. The outstanding totals are complete; "
            "only this list is cut short. Narrow the date range, a branch or a "
            "customer to see the rest."
        ).format(limit_value)

    return payload


@frappe.whitelist(allow_guest=False)
def get_customer_credit_profile(customer: str) -> Dict[str, Any]:
    """Whether this shop may order on credit, and how much room is left.

    Called by the POS the moment a customer is selected, so the checkout screen
    can decide whether to offer the Credit button at all. Read-only and cheap.

    ``current_balance`` is ALL-TIME and NOT branch-scoped, unlike
    :func:`get_credit_ledger`: this number gates the next order against the
    customer's limit, and a branch-scoped exposure figure would let the same
    shop run up the limit once per branch. It matches exactly what
    ``services/invoice_creation._apply_credit_terms`` will check at submit, which
    is the point — the POS must not offer a button the server will refuse.

    ``available_credit`` is ``None`` (not 0) when no limit is set. Zero means
    "no room left"; ``None`` means "no ceiling", and the two must never be
    confused by a client that would grey out the button on either.
    """
    _ensure_credit_ledger_access()

    name = str(customer or "").strip()
    if not name:
        frappe.throw(_("customer is required"))
    if not frappe.db.exists("Customer", name):
        frappe.throw(_("Customer {0} was not found").format(name))
    frappe.has_permission("Customer", "read", doc=name, throw=True)

    settings = _customer_credit_settings(name)

    from jarz_pos.services.invoice_creation import get_open_credit_balance

    current_balance = flt(get_open_credit_balance(name), 2)
    limit_amount = flt(settings["limit"], 2)

    open_rows = _open_credit_invoices(customers=[name])
    oldest = str(open_rows[0].get("posting_date") or "") if open_rows else None

    return {
        "success": True,
        "customer": name,
        "customer_name": frappe.db.get_value("Customer", name, "customer_name") or name,
        "credit_allowed": bool(settings["allowed"]),
        "credit_days": int(settings["days"] or DEFAULT_CREDIT_DAYS),
        "credit_limit": limit_amount,
        "current_balance": current_balance,
        "available_credit": (
            flt(max(0.0, limit_amount - current_balance), 2) if limit_amount > 0 else None
        ),
        "open_invoice_count": len(open_rows),
        "oldest_invoice_date": oldest or None,
        "currency": _credit_currency(),
    }


def _existing_credit_payment(
    *, customer: str, reference_no: Optional[str], amount: float, paid_to: str
) -> Optional[str]:
    """A Payment Entry that already recorded THIS handover, or ``None``.

    Double submission is the realistic failure here: the operator taps "Record
    payment", the phone loses the connection while the server is committing, and
    the app retries. Without a guard that books the money twice and clears
    invoices nobody paid for.

    Two levels, strongest first:

    * an ``idempotency_token`` supplied by the client becomes the PE's
      ``reference_no``; an exact match is a definitive replay.
    * otherwise, an identical (customer, amount, destination account) PE
      submitted in the last two minutes. Deliberately narrow: a shop paying the
      same round figure twice in one minute is possible, so this window is short
      enough that a genuine second handover is not swallowed, and the caller is
      told which PE was reused either way.
    """
    try:
        if reference_no:
            existing = frappe.get_all(
                "Payment Entry",
                filters={
                    "docstatus": 1,
                    "party_type": "Customer",
                    "party": customer,
                    "reference_no": reference_no,
                },
                pluck="name",
                limit_page_length=1,
            )
            if existing:
                return str(existing[0])
            return None

        cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-120)
        recent = frappe.get_all(
            "Payment Entry",
            filters={
                "docstatus": 1,
                "party_type": "Customer",
                "party": customer,
                "paid_to": paid_to,
                "paid_amount": ["between", [amount - 0.005, amount + 0.005]],
                "creation": [">=", cutoff],
            },
            pluck="name",
            limit_page_length=1,
        )
        if recent:
            return str(recent[0])
    except Exception:
        # A failed idempotency probe must not block a legitimate payment; the
        # DB-level lock below still serialises concurrent callers.
        _log_credit_error("record_credit_payment: idempotency probe failed")
    return None


@frappe.whitelist(allow_guest=False)
def record_credit_payment(
    customer: str,
    amount: Union[float, str],
    pos_profile: str,
    payment_method: str = "Cash",
    posting_date: Optional[str] = None,
    remarks: Optional[str] = None,
    idempotency_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Take one payment against a shop's credit balance, allocated FIFO.

    THIS IS THE OWNER'S ACTUAL PATTERN, and it is why there is no
    ``invoice`` argument: "a lot of cases it is an invoice after invoice, so when
    I send them the second invoice they pay the first invoice." The shop hands
    over money that clears the previous invoice(s). They do not name one, and an
    endpoint that made them name one would be recording a fiction.

    So: ONE Receive Payment Entry, allocated oldest-invoice-first across every
    open credit invoice of that customer.

    * **Partial payment** part-allocates the oldest invoice and stops. The rest
      of that invoice stays outstanding and keeps its place at the front of the
      queue.
    * **Excess** beyond every open invoice is NOT refused. It stays unallocated
      on the Payment Entry, which in ERPNext is an advance against the customer —
      the honest record of "they paid more than they owed". The response says so
      explicitly (``unallocated_amount`` + ``notice_code = "recorded_as_advance"``)
      rather than letting it be discovered later.
    * **Nothing open at all** is still recorded, as a pure advance, for the same
      reason. Refusing would leave real money with nowhere to go.

    Allocation is NOT branch-scoped, although the endpoint is: the debt is one
    debt to one company, and a shop handing over cash is clearing "the last
    invoice", not "the last invoice from this branch". Each allocation reports
    its own branch so the payload stays auditable. The caller must still manage
    the branch whose drawer receives the money, and that branch's shift must be
    open — cash lands in the POS Profile's own drawer via ``get_pos_cash_account``
    so the shift close expects it.

    Args:
        customer: Customer whose balance is being paid down.
        amount: Money handed over. Must be > 0.
        pos_profile: Branch receiving it. Decides the drawer AND the shift.
        payment_method: "Cash" (default), "Instapay", "Mobile Wallet" or
            "Payment Gateway". Non-cash lands in the matching online ledger
            instead of the drawer.
        posting_date: Defaults to today. A back-dated payment posts into the day
            it names, which is what a reconciliation of last week's cash needs.
        remarks: Free text stored on the Payment Entry.
        idempotency_token: Optional client-supplied replay key. Stored as the
            PE's ``reference_no``; resending the same token returns the original
            entry instead of taking the money twice.

    Returns:
        ``success``, ``payment_entry``, ``amount``, ``allocated_amount``,
        ``unallocated_amount``, ``allocations`` (per invoice, oldest first),
        ``remaining_balance``, ``cash_account``, plus optional ``notice_code`` /
        ``notice`` and ``already_recorded`` on a replay.
    """
    from jarz_pos.services.delivery_handling import (
        _get_online_collection_account,
        _get_receivable_account,
        _is_cash_collection_method,
        _normalize_collection_method,
    )
    from jarz_pos.utils.access_control import ensure_open_shift
    from jarz_pos.utils.account_utils import get_pos_cash_account, validate_account_exists

    _ensure_credit_payment_access()

    customer = str(customer or "").strip()
    pos_profile = str(pos_profile or "").strip()
    remarks = (remarks or "").strip() or None
    idempotency_token = (idempotency_token or "").strip() or None

    if not customer:
        frappe.throw(_("customer is required"))
    if not pos_profile:
        frappe.throw(_("pos_profile is required"))
    try:
        amount = flt(amount)
    except Exception:
        amount = 0.0
    if amount <= 0.005:
        frappe.throw(_("Enter the amount the customer handed over."))

    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Customer {0} was not found").format(customer))

    # Note the shape: an EMPTY allowed list refuses, it does not wave through.
    # ``get_user_pos_profiles`` hands Administrator every enabled profile, so
    # empty genuinely means "assigned to no branch" — and the money is about to
    # land in a specific branch's drawer, which such a user cannot answer for.
    allowed = _allowed_profiles()
    if pos_profile not in allowed:
        frappe.throw(
            _("You are not assigned to branch {0}.").format(pos_profile),
            frappe.PermissionError,
        )
    # Cash is about to land in this branch's drawer, so the branch has to be open
    # — otherwise the payment falls outside every shift window and the next cash
    # count cannot possibly reconcile.
    ensure_open_shift(pos_profile, action_label="recording a credit payment")

    company = frappe.db.get_value("POS Profile", pos_profile, "company")
    if not company:
        frappe.throw(_("POS Profile {0} has no company").format(pos_profile))

    normalized_method = _normalize_collection_method(payment_method)
    if _is_cash_collection_method(normalized_method):
        paid_to = get_pos_cash_account(pos_profile, company)
    else:
        paid_to = _get_online_collection_account(normalized_method, company)
    validate_account_exists(paid_to)
    paid_from = _get_receivable_account(company)

    # Serialise concurrent callers on the customer row BEFORE reading the open
    # invoices: two operators recording the same handover at once would otherwise
    # both read the same outstanding and both allocate it.
    try:
        frappe.db.sql("SELECT name FROM `tabCustomer` WHERE name=%s FOR UPDATE", (customer,))
    except Exception:
        pass

    replay = _existing_credit_payment(
        customer=customer,
        reference_no=idempotency_token,
        amount=amount,
        paid_to=paid_to,
    )
    if replay:
        return {
            "success": True,
            "already_recorded": True,
            "payment_entry": replay,
            "customer": customer,
            "amount": flt(amount, 2),
            "cash_account": paid_to,
            "notice_code": "already_recorded",
            "notice": _(
                "This payment was already recorded ({0}); it has not been taken twice."
            ).format(replay),
        }

    open_rows = _open_credit_invoices(customers=[customer])

    posting = getdate(posting_date) if posting_date else getdate(nowdate())

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = company
    pe.posting_date = posting
    try:
        pe.posting_time = frappe.utils.nowtime()
    except Exception:
        pass
    pe.mode_of_payment = normalized_method if _is_cash_collection_method(normalized_method) else None
    pe.party_type = "Customer"
    pe.party = customer
    pe.paid_from = paid_from
    pe.party_account = paid_from
    pe.paid_to = paid_to
    pe.paid_amount = amount
    pe.received_amount = amount
    # Always set, not only when a token was supplied: ERPNext makes
    # reference_no/reference_date MANDATORY the moment ``paid_to`` is a Bank-type
    # account, so an Instapay/wallet settlement with no token would fail to
    # insert. When a token IS supplied it doubles as the replay key probed above.
    pe.reference_no = idempotency_token or f"CREDIT-{customer}"
    pe.reference_date = posting
    if remarks:
        pe.remarks = remarks

    # Branch provenance, when the column exists — the same optional stamp the
    # kanban's partner Payment Entry writes.
    try:
        if frappe.get_meta("Payment Entry").get_field("custom_kanban_profile"):
            pe.custom_kanban_profile = pos_profile
    except Exception:
        pass

    branch_field = _branch_field()
    remaining = amount
    allocations: List[Dict[str, Any]] = []
    for row in open_rows:
        if remaining <= 0.005:
            break
        outstanding = flt(row.get("outstanding_amount"))
        if outstanding <= 0.005:
            continue
        # FIFO: fill the oldest invoice completely, then move on. A partial
        # payment part-allocates this one and the loop stops on the next pass.
        allocated = min(remaining, outstanding)
        pe.append(
            "references",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": row.get("name"),
                "due_date": row.get("due_date"),
                "total_amount": flt(row.get("grand_total")),
                "outstanding_amount": outstanding,
                "allocated_amount": allocated,
            },
        )
        allocations.append(
            {
                "invoice": row.get("name"),
                "posting_date": str(row.get("posting_date") or "") or None,
                "due_date": str(row.get("due_date") or "") or None,
                "outstanding_before": flt(outstanding, 2),
                "allocated_amount": flt(allocated, 2),
                "fully_settled": bool(allocated >= outstanding - 0.005),
                "branch": str(row.get(branch_field) or ""),
            }
        )
        remaining = flt(remaining - allocated, 2)

    allocated_total = flt(amount - remaining, 2)
    unallocated = flt(max(0.0, remaining), 2)

    pe.flags.ignore_permissions = True
    try:
        pe.insert(ignore_permissions=True)
        pe.submit()
    except Exception:
        _log_credit_error(f"record_credit_payment: could not post payment for {customer}")
        raise

    remaining_balance = flt(
        max(0.0, sum(flt(r.get("outstanding_amount")) for r in open_rows) - allocated_total), 2
    )

    payload: Dict[str, Any] = {
        "success": True,
        "payment_entry": pe.name,
        "customer": customer,
        "customer_name": frappe.db.get_value("Customer", customer, "customer_name") or customer,
        "pos_profile": pos_profile,
        "payment_method": normalized_method,
        "posting_date": str(posting),
        "amount": flt(amount, 2),
        "allocated_amount": allocated_total,
        "unallocated_amount": unallocated,
        "allocations": allocations,
        "remaining_balance": remaining_balance,
        "cash_account": paid_to,
        "currency": _credit_currency(),
    }

    if unallocated > 0.005:
        # Said out loud rather than left to be discovered: the money IS recorded,
        # but part of it is now an advance sitting on the customer rather than a
        # cleared invoice, and the person at the counter should know before the
        # shop walks away.
        payload["notice_code"] = "recorded_as_advance"
        payload["notice"] = _(
            "{0} of this payment was more than {1} currently owes on credit. It "
            "is recorded as an advance on their account and will clear their next "
            "credit order."
        ).format(frappe.utils.fmt_money(unallocated, currency=_credit_currency()), customer)

    return payload
