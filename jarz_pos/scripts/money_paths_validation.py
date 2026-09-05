"""Ledger validation for the POS money paths no existing harness drives.

``full_lifecycle_validation`` covers create → dispatch → settle → cancel on the
happy paths.  ``b2b_accounting_validation`` covers commercial policies.
``return_flow_validation`` covers returns.  None of them ever touches:

* changing how an already-dispatched order will be collected (COD ⇄ InstaPay),
* an amendment of a **paid** order (payment cancelled, invoice superseded),
* an address/territory change on a **submitted** invoice,
* the unpaid-online (InstaPay) dispatch → confirm → convert lifecycle,
* batch courier settlement measured against the single-invoice preview,
* the preview-token settlement path the Flutter client actually calls,
* the cancel guards for partial payment and closed shifts.

Those are the seven cases here.  Each drives the REAL service entry point and
then asserts the ledger — never a mock, never a re-implementation of the rule
under test.

SKIPS ARE NOT PASSES
--------------------
Every case checks its own preconditions first.  When the environment cannot
satisfy one (no courier bound to the branch, no second company, no closed shift
to test against, a Territory schema without ``delivery_expense``) the case
records a **skip with the exact reason** and returns.  A skip is counted in its
own bucket and never inflates ``passed``.  ``ctx.record(name, True, "skipped")``
is a lie this module does not tell.

Fixtures
--------
Customers, territories, addresses and receipts all carry the ``_B2BVALID_``
prefix (inherited from ``b2b_accounting_validation._ensure_customer``), which is
what ``purge_test_fixtures`` sweeps.  Grep for ``_B2BVALID_`` — not for this
file's name — when hunting residue.  Money amounts introduced by this module are
small and deliberately odd (13.37 / 41.73 / 7.11 / 19.29 / 0.43 / 2.03) so a row
it leaves behind can never be mistaken for real traffic.

Line references were verified against the tree on 2026-08-19; the ones that had
drifted are called out in the header comment of the case that uses them.

Refuses to run against production.  Cleans up in ``finally``.

Run::

    bench --site frontend execute jarz_pos.scripts.money_paths_validation.run
    bench --site frontend execute jarz_pos.scripts.money_paths_validation.run \
        --kwargs "{'cleanup': False}"
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import frappe
from frappe.utils import flt, get_datetime

from jarz_pos.scripts.b2b_accounting_validation import (
    RunContext,
    assert_gl_balanced,
    _cleanup,
    _create_invoice,
    _ensure_customer,
    _ensure_customer_group,
    _ensure_item_prices,
    _ensure_stock,
    _ensure_territory,
    _pay_invoice_full_cash,
    _pick_courier_party,
    _pick_pos_profile,
    _pick_sellable_items,
)

_TOL = 0.01
MARKER_START = "MONEY_PATHS_JSON_START"
MARKER_END = "MONEY_PATHS_JSON_END"

#: Deliberately odd fixture rates so a stray row is unmistakable.
TERRITORY_A = "_B2BVALID_MPT_A"
TERRITORY_B = "_B2BVALID_MPT_B"
#: Same delivery INCOME as A, different expense: the one territory change a
#: submitted invoice may still make (the customer-facing total does not move).
TERRITORY_C = "_B2BVALID_MPT_C"
TERRITORY_FREE = "_B2BVALID_MPT_FREE"
TERR_A_INCOME, TERR_A_EXPENSE = 13.37, 7.11
TERR_B_INCOME, TERR_B_EXPENSE = 41.73, 19.29
TERR_C_INCOME, TERR_C_EXPENSE = TERR_A_INCOME, 23.57
TERR_FREE_INCOME, TERR_FREE_EXPENSE = 0.0, 5.05

#: Residues used by the cancel-tolerance case (kanban.py's tolerance is 0.50).
CANCEL_RESIDUE_INSIDE_TOLERANCE = 0.43
CANCEL_RESIDUE_OUTSIDE_TOLERANCE = 2.03


# ─────────────────────────────────────────────────────────────────────────────
# Environment guard
# ─────────────────────────────────────────────────────────────────────────────

def _guard_environment() -> None:
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""
    if "erp.orderjarz.com" in base_url:
        raise RuntimeError(
            f"money_paths_validation refuses to run on production ({base_url!r})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Result buckets — checks (pass/fail), skips, observations
# ─────────────────────────────────────────────────────────────────────────────

def _bucket(ctx: RunContext, attr: str) -> List[Dict[str, Any]]:
    rows = getattr(ctx, attr, None)
    if rows is None:
        rows = []
        setattr(ctx, attr, rows)
    return rows


def _skip(ctx: RunContext, name: str, reason: str) -> None:
    """Record a precondition the environment cannot satisfy.

    NEVER routed through ``ctx.record``: a skip is not a pass.
    """
    _bucket(ctx, "mp_skips").append({"name": name, "reason": reason})
    print(f"   [SKIP] {name} :: {reason}")


def _observe(ctx: RunContext, name: str, detail: str) -> None:
    """Record a measured fact whose correctness is a human decision.

    Kept out of pass/fail so a behaviour nobody has ruled on yet cannot be
    silently scored either way.
    """
    _bucket(ctx, "mp_observations").append({"name": name, "detail": detail})
    print(f"   [NOTE] {name} :: {detail}")


def _track(ctx: RunContext, attr: str, name: str) -> None:
    rows = _bucket(ctx, attr)
    if name and {"name": name} not in rows:
        rows.append({"name": name})


# ─────────────────────────────────────────────────────────────────────────────
# Ledger helpers
# ─────────────────────────────────────────────────────────────────────────────

def _gl_by_account(voucher_type: str, voucher_no: str) -> Dict[str, float]:
    """Net movement per account for one voucher (debit positive, live rows only)."""
    rows = frappe.db.sql(
        """
        SELECT account, ROUND(SUM(debit - credit), 2) AS net
        FROM `tabGL Entry`
        WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
        GROUP BY account
        """,
        (voucher_type, voucher_no),
        as_dict=True,
    )
    return {r["account"]: flt(r["net"]) for r in rows}


def _gl_net(voucher_type: str, voucher_no: str, account: str) -> float:
    return flt(_gl_by_account(voucher_type, voucher_no).get(account, 0.0))


def _gl_net_for_invoices(invoices: List[str], account: str) -> float:
    """Live net movement (debit positive) on *account* across several invoices."""
    invoices = [i for i in invoices if i]
    if not invoices:
        return 0.0
    placeholders = ", ".join(["%s"] * len(invoices))
    rows = frappe.db.sql(
        f"""
        SELECT ROUND(SUM(debit - credit), 2)
        FROM `tabGL Entry`
        WHERE voucher_type = 'Sales Invoice'
          AND voucher_no IN ({placeholders})
          AND account = %s
          AND is_cancelled = 0
        """,
        tuple(invoices) + (account,),
    )
    return flt((rows and rows[0][0]) or 0.0)


def _live_gl_rows(voucher_type: str, voucher_no: str) -> int:
    return frappe.db.count(
        "GL Entry",
        {"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
    )


def _cancelled_gl_rows(voucher_type: str, voucher_no: str) -> int:
    return frappe.db.count(
        "GL Entry",
        {"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 1},
    )


def _assert_voucher_balanced(ctx: RunContext, case: str, doctype: str, name: str) -> None:
    passed, detail = assert_gl_balanced(doctype, name)
    ctx.record(f"{case}.{doctype.replace(' ', '_').lower()}_balanced::{name}", passed, detail)


def _invoice_value(invoice: str, field: str) -> Any:
    return frappe.db.get_value("Sales Invoice", invoice, field)


def _outstanding(invoice: str) -> float:
    return flt(_invoice_value(invoice, "outstanding_amount"))


def _grand_total(invoice: str) -> float:
    return flt(_invoice_value(invoice, "grand_total"))


def _receivable_total(invoice: str) -> float:
    """The amount ERPNext actually books to the receivable for this invoice.

    With rounding enabled (``disable_rounded_total`` = 0) ERPNext posts the
    Debtors line -- and therefore ``outstanding_amount`` -- at ``rounded_total``,
    NOT at ``grand_total``. Comparing a receivable or an outstanding against
    ``grand_total`` on such an invoice reports a phantom delta of up to half a
    currency unit and accuses the product of losing money it never lost. That is
    exactly what C3 did (grand_total 361.73 vs a ledger and outstanding of
    362.00). Every receivable/outstanding assertion in this module goes through
    here; amounts the product itself derives from ``grand_total`` (Courier
    Transaction amounts, settlement previews) deliberately do NOT.
    """
    row = frappe.db.get_value(
        "Sales Invoice",
        invoice,
        ["grand_total", "rounded_total", "disable_rounded_total"],
        as_dict=True,
    ) or {}
    grand = flt(row.get("grand_total"))
    if int(row.get("disable_rounded_total") or 0):
        return grand
    rounded = flt(row.get("rounded_total"))
    return rounded if abs(rounded) > 0.0001 else grand


def _pe_gl_net(invoice: str, account: str) -> float:
    """Live net movement on *account* across the submitted Payment Entries of one invoice."""
    return round(
        sum(_gl_net("Payment Entry", pe, account) for pe in _submitted_payment_entries_for(invoice)),
        2,
    )


def _payment_entries_for(invoice: str) -> List[str]:
    return sorted(set(frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
        pluck="parent",
        limit_page_length=50,
    ) or []))


def _submitted_payment_entries_for(invoice: str) -> List[str]:
    names = _payment_entries_for(invoice)
    if not names:
        return []
    return sorted(frappe.get_all(
        "Payment Entry",
        filters={"name": ["in", names], "docstatus": 1},
        pluck="name",
    ) or [])


def _allocated_total(invoice: str) -> float:
    rows = frappe.db.sql(
        """
        SELECT ROUND(SUM(per.allocated_amount), 2)
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent AND pe.docstatus = 1
        WHERE per.reference_doctype = 'Sales Invoice' AND per.reference_name = %s
        """,
        (invoice,),
    )
    return flt((rows and rows[0][0]) or 0.0)


def _courier_transactions(invoice: str) -> List[Dict[str, Any]]:
    return frappe.get_all(
        "Courier Transaction",
        filters={"reference_invoice": invoice},
        fields=[
            "name", "amount", "shipping_amount", "status", "payment_mode",
            "party_type", "party", "journal_entry", "idempotency_token", "notes",
        ],
        order_by="creation asc",
        limit_page_length=50,
    ) or []


def _delivery_notes_for(invoice: str) -> List[str]:
    return sorted(set(frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": invoice, "docstatus": 1},
        pluck="parent",
        limit_page_length=50,
    ) or []))


def _shipping_income_row(invoice: str) -> Tuple[bool, float]:
    """Return (row_present, tax_amount) for the Shipping Income tax row.

    The tax row is the source of truth for shipping income — never
    ``custom_delivery_income`` (NOT NULL DEFAULT 0) and never the Territory rate.
    """
    rows = frappe.get_all(
        "Sales Taxes and Charges",
        filters={"parent": invoice, "parenttype": "Sales Invoice"},
        fields=["description", "tax_amount"],
        limit_page_length=50,
    ) or []
    for row in rows:
        if str(row.get("description") or "").lower().startswith("shipping income"):
            return True, flt(row.get("tax_amount"))
    return False, 0.0


def _expect_throw(fn: Callable, *args: Any, **kwargs: Any) -> Tuple[bool, str]:
    """Call *fn* expecting it to raise. Returns (raised, message)."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the message is the evidence
        return True, str(exc)[:400]
    return False, "call returned without raising"


def _shift_gate_reason(pos_profile: str) -> Optional[str]:
    """Return why the open-shift gate would refuse, or None when it would not.

    api/couriers.py:402 runs ensure_open_shift_for_invoice before any collection
    change, so "no open shift on this branch" is a precondition of that case, not
    a defect in it.
    """
    try:
        from jarz_pos.utils.access_control import (
            get_open_shift_for_profile,
            user_requires_pos_shift,
        )
    except Exception:
        return None
    try:
        if not user_requires_pos_shift():
            return None
        if get_open_shift_for_profile(pos_profile):
            return None
    except Exception:
        return None
    return (
        f"user {frappe.session.user} is shift-gated and branch {pos_profile} has no open "
        "shift, so ensure_open_shift_for_invoice would refuse before any accounting runs"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_courier(picked: Any) -> Optional[Dict[str, Any]]:
    if not picked:
        return None
    try:
        party_type, party = picked
    except Exception:
        return None
    if not party_type or not party:
        return None
    return {"party_type": party_type, "party": party}


def _new_invoice(ctx: RunContext, env: Dict[str, Any], case: str, *,
                 territory: Optional[str] = None, pickup: bool = False) -> str:
    customer = _ensure_customer(
        ctx, case, territory or env["territory"], env["customer_group"]
    )
    return _create_invoice(
        ctx, customer=customer, pos_profile=env["pos_profile"], items=env["items"],
        order_purpose=None, commercial_policy=None, policy_reason=None, pickup=pickup,
    )


def _pay_invoice_partial(ctx: RunContext, invoice: str, pos_profile: str,
                         amount: float) -> Optional[str]:
    """Pay exactly *amount* in cash to the branch account.

    _pay_invoice_full_cash always clears the whole outstanding, so the
    partial-payment cancel guard needs its own driver.
    """
    from jarz_pos.utils.account_utils import get_pos_cash_account

    amount = round(float(amount), 2)
    if amount <= 0:
        return None
    inv = frappe.get_doc("Sales Invoice", invoice)
    cash_acc = get_pos_cash_account(pos_profile, inv.company)
    receivable = getattr(inv, "debit_to", None) or frappe.db.get_value(
        "Company", inv.company, "default_receivable_account"
    )
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = inv.company
    pe.posting_date = frappe.utils.nowdate()
    pe.mode_of_payment = "Cash"
    pe.party_type = "Customer"
    pe.party = inv.customer
    pe.paid_from = receivable
    pe.paid_to = cash_acc
    pe.paid_amount = amount
    pe.received_amount = amount
    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": invoice,
        "allocated_amount": amount,
    })
    pe.flags.ignore_permissions = True
    pe.insert(ignore_permissions=True)
    pe.submit()
    ctx.payment_entries.append(pe.name)
    return pe.name


def _territory_columns() -> set:
    try:
        return set(frappe.db.get_table_columns("Territory") or [])
    except Exception:
        return set()


def _ensure_mp_territory(ctx: RunContext, name: str, income: float, expense: float) -> str:
    """Create/refresh a fixture Territory carrying an odd income/expense pair."""
    cols = _territory_columns()
    if not frappe.db.exists("Territory", name):
        doc = frappe.new_doc("Territory")
        doc.territory_name = name
        try:
            parent = frappe.db.get_value("Territory", {"is_group": 1}, "name")
            if parent:
                doc.parent_territory = parent
        except Exception:
            pass
        doc.is_group = 0
        doc.insert(ignore_permissions=True)
    _track(ctx, "mp_territories", name)
    updates: Dict[str, Any] = {}
    if "delivery_income" in cols:
        updates["delivery_income"] = income
    if "delivery_expense" in cols:
        updates["delivery_expense"] = expense
    if updates:
        frappe.db.set_value("Territory", name, updates, update_modified=False)
    return name


def _ensure_territory_pos_profile(territory: str, pos_profile: str) -> Tuple[bool, str]:
    """Point the fixture Territory at the POS profile these cases drive.

    api/manager.py:1252-1262 runs ``assert_pos_profile_matches_territory`` before
    ANY amendment write, and utils/invoice_utils.py:1177 only lets it pass when
    the ORDER territory's own ``pos_profile`` equals the selected profile.
    ``b2b_accounting_validation._ensure_territory`` never sets that link, so the
    real endpoint rejected every amendment with POS_PROFILE_TERRITORY_MISMATCH
    and the case crashed. Building the production shape (territory -> branch) is
    the honest fix; passing ``pos_profile_override`` would skip the very guard
    production traffic has to satisfy.

    Returns ``(satisfied, evidence)``. A site whose Territory has no
    ``pos_profile`` field at all cannot host this precondition -- that is a SKIP,
    never a pass.
    """
    from jarz_pos.utils.invoice_utils import resolve_pos_profile_for_territory

    try:
        field = frappe.get_meta("Territory").get_field("pos_profile")
    except Exception as exc:
        return False, f"could not read Territory meta: {exc}"
    if not field:
        return False, (
            "Territory has no pos_profile field on this site, so "
            "resolve_pos_profile_for_territory returns None for EVERY territory and "
            "assert_pos_profile_matches_territory can only be satisfied by an explicit "
            "manager pos_profile_override"
        )
    try:
        frappe.db.set_value(
            "Territory", territory, {"pos_profile": pos_profile}, update_modified=False
        )
        frappe.db.commit()
    except Exception as exc:
        return False, f"could not stamp pos_profile={pos_profile!r} on Territory {territory}: {exc}"
    resolved = resolve_pos_profile_for_territory(territory) or ""
    if resolved != pos_profile:
        return False, (
            f"Territory {territory} still resolves to POS profile {resolved!r} after "
            f"stamping {pos_profile!r}"
        )
    return True, f"Territory {territory} -> POS Profile {pos_profile}"


def _addresses_for_customer(customer: str) -> List[str]:
    return sorted(set(frappe.get_all(
        "Dynamic Link",
        filters={
            "link_doctype": "Customer",
            "link_name": customer,
            "parenttype": "Address",
        },
        pluck="parent",
        limit_page_length=50,
    ) or []))


def _ensure_mp_address(ctx: RunContext, customer: str, title: str,
                       territory: str) -> Optional[str]:
    """Create (or refresh) a fixture Address whose city IS the Territory name.

    Address.city is what resolve_address_territory reads first, and the POS lane
    writes the Territory into it verbatim -- so this mirrors the production
    shape rather than inventing one.
    """
    existing = frappe.get_all(
        "Address",
        filters={"address_title": title},
        pluck="name",
        limit_page_length=5,
    ) or []
    if existing:
        name = existing[0]
        frappe.db.set_value("Address", name, {"city": territory}, update_modified=False)
        _track(ctx, "mp_addresses", name)
        return name
    try:
        addr = frappe.new_doc("Address")
        addr.address_title = title
        addr.address_type = "Shipping"
        addr.address_line1 = "Money Paths Fixture"
        addr.city = territory
        addr.country = frappe.db.get_value(
            "Company", frappe.defaults.get_global_default("company"), "country"
        ) or "Egypt"
        addr.append("links", {"link_doctype": "Customer", "link_name": customer})
        addr.insert(ignore_permissions=True)
        _track(ctx, "mp_addresses", addr.name)
        return addr.name
    except Exception as exc:
        print(f"   (address create failed for {title}: {exc})")
        return None


def _make_receipt(ctx: RunContext, invoice: str, pos_profile: str, amount: float,
                  method: str = "InstaPay") -> Optional[str]:
    """Create a POS Payment Receipt through the real endpoint and stamp an image URL.

    ensure_uploaded_payment_receipt rejects a receipt without an image, and
    every online collection path goes through it.
    """
    from jarz_pos.api.payment_receipts import create_payment_receipt

    try:
        res = create_payment_receipt(
            sales_invoice=invoice,
            payment_method=method,
            amount=round(float(amount), 2),
            pos_profile=pos_profile,
        )
    except Exception as exc:
        print(f"   (receipt create failed for {invoice}: {exc})")
        return None
    name = (res or {}).get("receipt_name")
    if not name:
        return None
    _track(ctx, "mp_receipts", name)
    try:
        frappe.db.set_value(
            "POS Payment Receipt",
            name,
            {
                "receipt_image_url": "/files/_B2BVALID_MP_receipt.png",
                "receipt_image": "/files/_B2BVALID_MP_receipt.png",
                "status": "Unconfirmed",
            },
            update_modified=False,
        )
    except Exception as exc:
        print(f"   (receipt image stamp failed for {name}: {exc})")
        return None
    return name


def _absorb(ctx: RunContext, invoice: str) -> None:
    """Register every artifact an invoice spawned so cleanup can unwind it."""
    for dn in _delivery_notes_for(invoice):
        if dn not in ctx.delivery_notes:
            ctx.delivery_notes.append(dn)
    for pe in _payment_entries_for(invoice):
        if pe not in ctx.payment_entries:
            ctx.payment_entries.append(pe)
    for ct in frappe.get_all(
        "Courier Transaction", filters={"reference_invoice": invoice},
        pluck="name", limit_page_length=50,
    ) or []:
        if ct not in ctx.courier_transactions:
            ctx.courier_transactions.append(ct)
    for field in ("user_remark", "title"):
        try:
            for je in frappe.get_all(
                "Journal Entry",
                filters={"docstatus": 1, field: ["like", f"%{invoice}%"]},
                pluck="name", limit_page_length=50,
            ) or []:
                if je not in ctx.journal_entries:
                    ctx.journal_entries.append(je)
        except Exception:
            pass


def _teardown_dispatched(ctx: RunContext) -> None:
    """Undo dispatch state so the shared cleanup can cancel the fixtures.

    block_cancel_if_dispatched refuses to cancel a dispatched invoice, by
    design. Rather than weakening that guard, remove the conditions it inspects
    -- for this run's fixtures only.
    """
    for invoice in list(dict.fromkeys(ctx.invoices)):
        try:
            if not frappe.db.exists("Sales Invoice", invoice):
                continue
            for dn in _delivery_notes_for(invoice):
                try:
                    doc = frappe.get_doc("Delivery Note", dn)
                    if int(doc.docstatus or 0) == 1:
                        doc.flags.ignore_permissions = True
                        doc.flags.ignore_links = True
                        doc.cancel()
                except Exception:
                    pass
            frappe.db.set_value(
                "Sales Invoice", invoice,
                {"custom_was_out_for_delivery": 0, "custom_sales_invoice_state": "Recieved"},
                update_modified=False,
            )
        except Exception:
            pass
    frappe.db.commit()


def _teardown_extras(ctx: RunContext) -> None:
    """Delete receipts first: they link to the invoices _cleanup then removes."""
    for row in _bucket(ctx, "mp_receipts"):
        try:
            frappe.delete_doc("POS Payment Receipt", row["name"], force=True,
                              ignore_permissions=True, delete_permanently=True)
        except Exception as exc:
            print(f"   could not delete POS Payment Receipt {row['name']}: {exc}")
    frappe.db.commit()


def _teardown_taxonomy(ctx: RunContext) -> None:
    """Best-effort removal of fixture Addresses/Territories.

    Both carry the _B2BVALID_ prefix, so anything still referenced (and
    therefore undeletable here) is swept later by purge_test_fixtures.
    """
    for row in _bucket(ctx, "mp_addresses"):
        try:
            frappe.delete_doc("Address", row["name"], force=True,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    for row in _bucket(ctx, "mp_territories"):
        try:
            frappe.delete_doc("Territory", row["name"], force=True,
                              ignore_permissions=True, delete_permanently=True)
        except Exception:
            pass
    frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# C5 - Batch courier settlement vs the single-invoice preview
#
# services/delivery_handling.py:1933  settle_delivery_party            (verified)
# services/delivery_handling.py:1979  company from the GLOBAL default  (verified)
# services/delivery_handling.py:4394  _create_settlement_journal_entry (verified)
# api/couriers.py:575                 generate_settlement_preview      (verified)
#
# Runs FIRST on purpose: settle_delivery_party sweeps EVERY unsettled Courier
# Transaction for the party, so any case that parks one must run after it. The
# precondition check below still refuses to settle when rows this harness did
# not create are in scope.
# ─────────────────────────────────────────────────────────────────────────────

def _case_batch_settlement(ctx: RunContext, env: Dict[str, Any]) -> None:
    case = "C5-BATCH-SETTLEMENT"

    from jarz_pos.api.couriers import generate_settlement_preview
    from jarz_pos.services.delivery_handling import (
        handle_out_for_delivery_transition,
        settle_delivery_party,
        _get_courier_outstanding_account,
    )
    from jarz_pos.services.settlement_strategies import dispatch_settlement
    from jarz_pos.utils.account_utils import get_creditors_account, get_pos_cash_account

    courier = env["courier"]
    if not courier:
        _skip(ctx, case, f"no courier is bound to POS profile {env['pos_profile']}")
        return

    party_type, party = courier["party_type"], courier["party"]
    foreign = frappe.get_all(
        "Courier Transaction",
        filters={"party_type": party_type, "party": party, "status": ["!=", "Settled"]},
        fields=["name", "reference_invoice", "amount"],
        limit_page_length=50,
    ) or []
    if foreign:
        _skip(
            ctx, case,
            f"courier {party_type}:{party} already carries {len(foreign)} unsettled "
            f"courier transaction(s) ({[r['reference_invoice'] for r in foreign][:5]}); "
            "a batch settle would sweep rows this harness did not create",
        )
        return

    cod_invoices: List[str] = []
    for idx in (1, 2):
        inv = _new_invoice(ctx, env, f"{case}-COD{idx}")
        frappe.db.commit()
        handle_out_for_delivery_transition(
            invoice_name=inv, courier="", mode="pay_now", pos_profile=env["pos_profile"],
            party_type=party_type, party=party,
        )
        frappe.db.commit()
        _absorb(ctx, inv)
        cod_invoices.append(inv)

    paid_invoice = _new_invoice(ctx, env, f"{case}-PAIDFEE")
    _pay_invoice_full_cash(ctx, paid_invoice, env["pos_profile"])
    frappe.db.commit()
    dispatch_settlement(
        paid_invoice, mode="later", pos_profile=env["pos_profile"],
        payment_type="Cash", party_type=party_type, party=party,
    )
    frappe.db.commit()
    _absorb(ctx, paid_invoice)

    fixtures = cod_invoices + [paid_invoice]

    scope = frappe.get_all(
        "Courier Transaction",
        filters={"party_type": party_type, "party": party, "status": ["!=", "Settled"]},
        fields=["name", "reference_invoice", "amount", "shipping_amount"],
        limit_page_length=50,
    ) or []
    outside = sorted({
        r["reference_invoice"] for r in scope
        if r.get("reference_invoice") not in fixtures
    })
    if outside:
        _skip(
            ctx, case,
            f"unsettled courier transactions appeared for invoices outside this case "
            f"({outside}); refusing to post a batch settlement over them",
        )
        return
    if not scope:
        _skip(ctx, case, "dispatch produced no unsettled courier transactions to settle")
        return

    expected_order = round(sum(flt(r["amount"]) for r in scope), 2)
    expected_ship = round(sum(flt(r["shipping_amount"]) for r in scope), 2)
    expected_net = round(expected_order - expected_ship, 2)

    ctx.record(
        f"{case}.cod_rows_carry_the_order_amount",
        all(
            abs(flt(r["amount"]) - _grand_total(r["reference_invoice"])) <= _TOL
            for r in scope if r["reference_invoice"] in cod_invoices
        ),
        f"courier rows={[(r['reference_invoice'], r['amount'], r['shipping_amount']) for r in scope]}",
    )
    ctx.record(
        f"{case}.paid_fee_only_row_collects_nothing",
        all(
            abs(flt(r["amount"])) <= _TOL
            for r in scope if r["reference_invoice"] == paid_invoice
        ),
        f"paid-invoice courier rows="
        f"{[(r['amount'], r['shipping_amount']) for r in scope if r['reference_invoice'] == paid_invoice]}",
    )

    previews: Dict[str, Dict[str, Any]] = {}
    for inv in fixtures:
        try:
            previews[inv] = generate_settlement_preview(inv, party_type, party) or {}
        except Exception as exc:
            ctx.record(f"{case}.preview_generated::{inv}", False, f"{exc}")

    company = env["company"]
    global_company = env["global_default_company"]
    cash_acc = get_pos_cash_account(env["pos_profile"], company)
    creditors_acc = get_creditors_account(company)
    courier_outstanding_acc = _get_courier_outstanding_account(company)

    result = settle_delivery_party(
        party_type=party_type, party=party, pos_profile=env["pos_profile"]
    )
    frappe.db.commit()
    je_name = (result or {}).get("journal_entry")
    if je_name and je_name not in ctx.journal_entries:
        ctx.journal_entries.append(je_name)

    if not je_name:
        ctx.record(
            f"{case}.settlement_journal_entry_posted", False,
            f"settle_delivery_party returned no journal entry: {result}",
        )
        return

    _assert_voucher_balanced(ctx, case, "Journal Entry", je_name)
    gl = _gl_by_account("Journal Entry", je_name)

    ctx.record(
        f"{case}.branch_cash_debited_order_minus_shipping",
        abs(flt(gl.get(cash_acc, 0.0)) - expected_net) <= _TOL,
        f"cash({cash_acc}) net={gl.get(cash_acc, 0.0)} expected={expected_net} "
        f"(order={expected_order} - shipping={expected_ship})",
    )
    ctx.record(
        f"{case}.payable_debited_total_shipping",
        abs(flt(gl.get(creditors_acc, 0.0)) - expected_ship) <= _TOL,
        f"creditors({creditors_acc}) net={gl.get(creditors_acc, 0.0)} expected={expected_ship}",
    )
    ctx.record(
        f"{case}.courier_outstanding_credited_total_order",
        abs(flt(gl.get(courier_outstanding_acc, 0.0)) + expected_order) <= _TOL,
        f"courier outstanding({courier_outstanding_acc}) net="
        f"{gl.get(courier_outstanding_acc, 0.0)} expected={-expected_order}",
    )

    party_rows = frappe.get_all(
        "GL Entry",
        filters={"voucher_type": "Journal Entry", "voucher_no": je_name,
                 "account": creditors_acc, "is_cancelled": 0},
        fields=["party_type", "party", "debit", "credit"],
        limit_page_length=20,
    ) or []
    if expected_ship > _TOL:
        ctx.record(
            f"{case}.payable_line_carries_the_courier_party",
            bool(party_rows) and all(
                r.get("party_type") == party_type and r.get("party") == party
                for r in party_rows
            ),
            f"payable lines={party_rows} expected party={party_type}:{party}",
        )
    else:
        _skip(
            ctx, f"{case}.payable_line_carries_the_courier_party",
            "no shipping accrued on any courier transaction, so no payable line is posted",
        )

    still_open = frappe.get_all(
        "Courier Transaction",
        filters={"name": ["in", [r["name"] for r in scope]], "status": ["!=", "Settled"]},
        pluck="name",
    ) or []
    ctx.record(
        f"{case}.every_courier_row_flipped_to_settled",
        not still_open,
        f"still unsettled={still_open} of {[r['name'] for r in scope]}",
    )

    if len(previews) == len(fixtures):
        p_order = round(sum(flt(p.get("order_amount")) for p in previews.values()), 2)
        p_ship = round(sum(flt(p.get("shipping_amount")) for p in previews.values()), 2)
        p_net = round(sum(flt(p.get("net_amount")) for p in previews.values()), 2)
        ctx.record(
            f"{case}.preview_order_sum_matches_batch",
            abs(p_order - expected_order) <= _TOL,
            f"sum(preview.order)={p_order} batch order={expected_order} per-invoice="
            f"{[(k, v.get('order_amount')) for k, v in previews.items()]}",
        )
        ctx.record(
            f"{case}.preview_shipping_sum_matches_batch",
            abs(p_ship - expected_ship) <= _TOL,
            f"sum(preview.shipping)={p_ship} batch shipping={expected_ship}",
        )
        ctx.record(
            f"{case}.preview_net_sum_matches_batch",
            abs(p_net - expected_net) <= _TOL,
            f"sum(preview.net)={p_net} batch net={expected_net}",
        )
    else:
        _skip(
            ctx, f"{case}.preview_matches_batch",
            f"only {len(previews)} of {len(fixtures)} previews were generated",
        )

    je_company = frappe.db.get_value("Journal Entry", je_name, "company")
    if str(global_company or "") == str(company or ""):
        _skip(
            ctx, f"{case}.settlement_uses_the_invoices_company",
            f"the global default company ({global_company}) already equals the fixture "
            f"company ({company}), so the :1979 mis-resolution cannot be observed here",
        )
    else:
        ctx.record(
            f"{case}.settlement_uses_the_invoices_company",
            str(je_company) == str(company),
            f"settlement JE company={je_company} invoices' company={company} global "
            f"default={global_company} -- settle_delivery_party resolves the company from "
            "frappe.defaults.get_global_default('company') at :1979, not from the invoices",
        )

    companies = frappe.get_all("Company", filters={}, pluck="name", limit_page_length=10) or []
    if len(companies) < 2:
        _skip(
            ctx, f"{case}.cross_company_batch_refused",
            f"only one Company exists on this site ({companies}); a cross-company batch "
            "cannot be constructed",
        )
    else:
        other = [c for c in companies if c != company]
        profiles = frappe.get_all(
            "POS Profile", filters={"disabled": 0, "company": ["in", other]},
            fields=["name", "company"], limit_page_length=5,
        ) or []
        if not profiles:
            _skip(
                ctx, f"{case}.cross_company_batch_refused",
                f"no enabled POS Profile exists for the other compan(y/ies) {other}; a "
                "cross-company courier batch cannot be constructed",
            )
        else:
            _skip(
                ctx, f"{case}.cross_company_batch_refused",
                f"a second company with a POS Profile exists ({profiles}), but the same "
                "courier must ALSO be branch-matched to it (assert_courier_matches_pos_profile) "
                "and hold unsettled rows there; that fixture was not constructed rather than "
                "posting a cross-company settlement against real ledgers",
            )


# ─────────────────────────────────────────────────────────────────────────────
# C1 - Payment collection method change after dispatch (COD -> InstaPay/Wallet)
#
# api/couriers.py:382                     change_payment_collection_method (verified)
# services/delivery_handling.py:3002      service entry                    (verified)
# services/delivery_handling.py:3102      online requires a receipt        (verified)
# services/delivery_handling.py:3329      _get_online_collection_account   (verified)
# services/delivery_handling.py:3395      _apply_collection_change_to_cash (verified)
# services/delivery_handling.py:3447      _apply_collection_change_to_online (verified)
# services/delivery_handling.py:3450-3463 the two idempotency guards       (verified)
# services/delivery_handling.py:3471      the JE                           (verified)
# services/delivery_handling.py:3503      CT.amount -> 0                   (verified)
# ─────────────────────────────────────────────────────────────────────────────

def _collection_change_jes(invoice: str) -> List[str]:
    """Find the collection-change JEs by user_remark, not by title.

    v16 JournalEntry.validate overwrites title with the first account name, so a
    title-based lookup finds nothing. user_remark survives untouched.
    """
    return sorted(set(frappe.get_all(
        "Journal Entry",
        filters=[
            ["docstatus", "=", 1],
            ["user_remark", "like", "%Payment collection changed to%"],
            ["user_remark", "like", f"%{invoice}%"],
        ],
        pluck="name",
        limit_page_length=20,
    ) or []))


def _case_collection_method_change(ctx: RunContext, env: Dict[str, Any]) -> None:
    case = "C1-COLLECTION-CHANGE"

    from jarz_pos.api.couriers import change_payment_collection_method
    from jarz_pos.services.delivery_handling import (
        handle_out_for_delivery_transition,
        _get_courier_outstanding_account,
        _get_online_collection_account,
        _is_online_collection_method,
    )

    courier = env["courier"]
    if not courier:
        _skip(ctx, case, f"no courier is bound to POS profile {env['pos_profile']}")
        return

    shift_reason = _shift_gate_reason(env["pos_profile"])
    if shift_reason:
        _skip(ctx, case, shift_reason)
        return

    company = env["company"]
    try:
        online_acc = _get_online_collection_account("Instapay", company)
    except Exception as exc:
        _skip(ctx, case, f"no InstaPay collection ledger resolvable for {company}: {exc}")
        return
    courier_outstanding_acc = _get_courier_outstanding_account(company)

    invoice = _new_invoice(ctx, env, case)
    frappe.db.commit()
    handle_out_for_delivery_transition(
        invoice_name=invoice, courier="", mode="pay_now", pos_profile=env["pos_profile"],
        party_type=courier["party_type"], party=courier["party"],
    )
    frappe.db.commit()
    _absorb(ctx, invoice)

    cts = [c for c in _courier_transactions(invoice) if flt(c["amount"]) > _TOL]
    if not cts:
        _skip(ctx, case, f"COD dispatch produced no courier transaction carrying an order "
                         f"amount for {invoice}")
        return
    ct = cts[0]
    ship_before = flt(ct["shipping_amount"])
    order_amount = flt(ct["amount"])

    raised, message = _expect_throw(
        change_payment_collection_method,
        invoice_name=invoice, new_method="Instapay", pos_profile=env["pos_profile"],
        idempotency_token="_B2BVALID_MP_C1_NORECEIPT",
    )
    ctx.record(
        f"{case}.online_switch_without_receipt_refused",
        raised and "receipt" in message.lower(),
        message,
    )

    receipt = _make_receipt(ctx, invoice, env["pos_profile"], order_amount, "InstaPay")
    if not receipt:
        _skip(ctx, case, "could not create a POS Payment Receipt fixture, which "
                         "delivery_handling.py:3102 requires for any online change")
        return

    token = "_B2BVALID_MP_C1_TOKEN1"
    try:
        res = change_payment_collection_method(
            invoice_name=invoice, new_method="Instapay", pos_profile=env["pos_profile"],
            receipt_name=receipt, reference_no="_B2BVALID_MP_C1_REF",
            idempotency_token=token,
        )
    except Exception as exc:
        ctx.record(f"{case}.collection_change_succeeded", False, f"{exc}")
        return
    frappe.db.commit()
    data = (res or {}).get("data") or res or {}
    # The ENDPOINT contract is "payment_collection_changed": delivery_handling.py:3441-3460
    # builds its own payload and merges it LAST (``{**result, **payload}``), so the inner
    # strategy's "cod_to_online" (:3832) is overwritten on every fresh change. Only the
    # idempotent-replay short-circuit at :3378-3389 ever returns "cod_to_online". The
    # direction is carried by ``new_method``, so assert on that instead of the label --
    # a change that silently landed on a CASH method still fails here.
    changed_mode = str(data.get("mode") or "")
    changed_method = str(data.get("new_method") or data.get("actual_payment_method") or "")
    ctx.record(
        f"{case}.collection_change_succeeded",
        bool((res or {}).get("success"))
        and (
            (changed_mode == "payment_collection_changed"
             and bool(changed_method) and _is_online_collection_method(changed_method))
            or changed_mode == "cod_to_online"
        ),
        json.dumps({k: data.get(k) for k in
                    ("mode", "new_method", "payment_changed", "journal_entry",
                     "order_amount", "shipping_amount")},
                   default=str)[:300],
    )

    je_name = data.get("journal_entry")
    if je_name and je_name not in ctx.journal_entries:
        ctx.journal_entries.append(je_name)
    if not je_name:
        ctx.record(f"{case}.journal_entry_posted", False, "no journal entry returned")
        return

    _assert_voucher_balanced(ctx, case, "Journal Entry", je_name)
    gl = _gl_by_account("Journal Entry", je_name)
    ctx.record(
        f"{case}.online_account_debited_order_amount",
        abs(flt(gl.get(online_acc, 0.0)) - order_amount) <= _TOL,
        f"{online_acc} net={gl.get(online_acc, 0.0)} expected={order_amount}",
    )
    ctx.record(
        f"{case}.courier_outstanding_credited_order_amount",
        abs(flt(gl.get(courier_outstanding_acc, 0.0)) + order_amount) <= _TOL,
        f"{courier_outstanding_acc} net={gl.get(courier_outstanding_acc, 0.0)} "
        f"expected={-order_amount}",
    )
    ctx.record(
        f"{case}.journal_entry_touches_only_those_two_accounts",
        set(gl.keys()) == {online_acc, courier_outstanding_acc},
        f"accounts={sorted(gl.keys())}",
    )

    after = frappe.db.get_value(
        "Courier Transaction", ct["name"],
        ["amount", "shipping_amount", "payment_mode", "journal_entry", "status"],
        as_dict=True,
    ) or {}
    ctx.record(
        f"{case}.courier_amount_zeroed",
        abs(flt(after.get("amount"))) <= _TOL,
        f"CT.amount {order_amount} -> {after.get('amount')}",
    )
    ctx.record(
        f"{case}.courier_shipping_preserved",
        abs(flt(after.get("shipping_amount")) - ship_before) <= _TOL,
        f"CT.shipping_amount {ship_before} -> {after.get('shipping_amount')}",
    )
    ctx.record(
        f"{case}.invoice_outstanding_untouched",
        abs(_outstanding(invoice)) <= _TOL,
        f"outstanding={_outstanding(invoice)} (the COD payment entry already cleared it)",
    )

    stored_title = frappe.db.get_value("Journal Entry", je_name, "title") or ""
    expected_title = f"Payment Collection Change - {invoice} - {token}"
    _observe(
        ctx, f"{case}.title_based_dedup_guard_is_live",
        f"JE title stored as {stored_title!r}; the :3453-3463 fallback guard matches on "
        f"{expected_title!r} -- equal={stored_title == expected_title}. When unequal that "
        "fallback can never match, and replay safety rests solely on the courier "
        "idempotency_token branch at :3450.",
    )

    before_jes = _collection_change_jes(invoice)
    try:
        change_payment_collection_method(
            invoice_name=invoice, new_method="Instapay", pos_profile=env["pos_profile"],
            receipt_name=receipt, reference_no="_B2BVALID_MP_C1_REF",
            idempotency_token=token,
        )
        replay_error = ""
    except Exception as exc:
        replay_error = str(exc)[:200]
    frappe.db.commit()
    after_jes = _collection_change_jes(invoice)
    ctx.record(
        f"{case}.replay_posts_no_second_journal_entry",
        len(after_jes) == len(before_jes) == 1,
        f"collection-change JEs before={before_jes} after={after_jes} "
        f"replay_error={replay_error!r}",
    )
    ctx.record(
        f"{case}.replay_leaves_courier_amount_at_zero",
        abs(flt(frappe.db.get_value("Courier Transaction", ct["name"], "amount"))) <= _TOL,
        f"CT.amount={frappe.db.get_value('Courier Transaction', ct['name'], 'amount')}",
    )

    raised, message = _expect_throw(
        change_payment_collection_method,
        invoice_name=invoice, new_method="Cash", pos_profile=env["pos_profile"],
        idempotency_token="_B2BVALID_MP_C1_REVERT",
    )
    ctx.record(
        f"{case}.revert_after_online_flip_is_refused",
        raised,
        f"reverting to Cash after the flip: {message} -- the courier amount is 0 (:3503) "
        "and no compensating JE exists, so :3084 must refuse",
    )

    # The cash path (:3395) needs its own COD order: the flipped one now carries
    # amount=0 and is refused above.
    cash_invoice = _new_invoice(ctx, env, f"{case}-CASH")
    frappe.db.commit()
    handle_out_for_delivery_transition(
        invoice_name=cash_invoice, courier="", mode="pay_now",
        pos_profile=env["pos_profile"],
        party_type=courier["party_type"], party=courier["party"],
    )
    frappe.db.commit()
    _absorb(ctx, cash_invoice)
    cash_cts = [c for c in _courier_transactions(cash_invoice) if flt(c["amount"]) > _TOL]
    if not cash_cts:
        _skip(ctx, f"{case}.cash_path",
              "the second COD dispatch produced no courier transaction carrying an "
              "order amount")
        return
    cash_ct = cash_cts[0]
    je_before = frappe.db.count("Journal Entry", {"docstatus": 1})
    try:
        change_payment_collection_method(
            invoice_name=cash_invoice, new_method="Cash", pos_profile=env["pos_profile"],
            idempotency_token="_B2BVALID_MP_C1_TOKEN2",
        )
        cash_error = ""
    except Exception as exc:
        cash_error = str(exc)[:200]
    frappe.db.commit()
    je_after = frappe.db.count("Journal Entry", {"docstatus": 1})
    cash_after = frappe.db.get_value(
        "Courier Transaction", cash_ct["name"],
        ["amount", "shipping_amount", "payment_mode"], as_dict=True,
    ) or {}
    ctx.record(
        f"{case}.cash_path_posts_no_journal_entry",
        je_after == je_before and not cash_error,
        f"submitted JE count {je_before} -> {je_after} error={cash_error!r}",
    )
    ctx.record(
        f"{case}.cash_path_moves_only_the_payment_mode",
        str(cash_after.get("payment_mode") or "") == "Cash"
        and abs(flt(cash_after.get("amount")) - flt(cash_ct["amount"])) <= _TOL
        and abs(flt(cash_after.get("shipping_amount")) - flt(cash_ct["shipping_amount"])) <= _TOL,
        f"payment_mode={cash_ct['payment_mode']} -> {cash_after.get('payment_mode')} ; "
        f"amount {cash_ct['amount']} -> {cash_after.get('amount')} ; "
        f"shipping {cash_ct['shipping_amount']} -> {cash_after.get('shipping_amount')}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# C2 - Amendment, end to end, on a PAID order
#
# api/manager.py:1634  submit_invoice_amendment   (verified)
# api/manager.py:1156  _run_invoice_amendment_job (verified)
# api/manager.py:1476  payment_entry.cancel()     (verified)
# api/manager.py:1489  source_invoice.cancel()    (verified)
# ─────────────────────────────────────────────────────────────────────────────

_AMENDMENT_PRECONDITION_CODES = {
    "state_not_supported", "delivery_note_exists", "delivery_trip_exists",
    "courier_transaction_exists", "sales_partner_transaction_exists",
    "journal_entry_exists", "custom_shipping_request_exists", "invoice_locked",
    "woo_order_locked", "cart_rebuild_failed", "invoice_not_submitted",
    "return_invoice", "empty_cart", "bundle_selection_missing", "stale_source",
    "suspicious_diff",
}


def _case_amendment_paid(ctx: RunContext, env: Dict[str, Any]) -> None:
    case = "C2-AMENDMENT-PAID"

    from jarz_pos.api.manager import submit_invoice_amendment
    from jarz_pos.utils.account_utils import get_pos_cash_account

    # Precondition, checked BEFORE any fixture is created: the amendment endpoint
    # refuses outright when the order territory does not map to the selected POS
    # profile (api/manager.py:1252-1262). Build that mapping if the schema allows
    # it; skip -- with the reason -- when it does not.
    mapped, mapping_detail = _ensure_territory_pos_profile(env["territory"], env["pos_profile"])
    if not mapped:
        _skip(ctx, case, f"POS profile / territory mapping could not be built: {mapping_detail}")
        return

    invoice = _new_invoice(ctx, env, case)
    _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])
    frappe.db.commit()

    source_grand = _grand_total(invoice)
    receivable_acc = _invoice_value(invoice, "debit_to")
    cash_acc = get_pos_cash_account(env["pos_profile"], env["company"])
    paid_entries = _submitted_payment_entries_for(invoice)
    paid_amount = round(sum(_gl_net("Payment Entry", pe, cash_acc) for pe in paid_entries), 2)
    src_ship_expense = flt(_invoice_value(invoice, "custom_shipping_expense"))
    src_income_present, src_income = _shipping_income_row(invoice)
    dn_before = len(_delivery_notes_for(invoice))

    if not paid_entries:
        _skip(ctx, case, "the cash payment fixture produced no submitted Payment Entry, "
                         "so there is nothing for :1476 to cancel")
        return

    try:
        result = submit_invoice_amendment(
            invoice_id=invoice,
            reuse_source_cart=1,
            pos_profile_name=env["pos_profile"],
            expected_source_grand_total=source_grand,
        ) or {}
    except Exception as exc:
        # A guard that refuses on an environment fact (not on the accounting under
        # test) is a precondition, not a defect -- and never a crash that aborts
        # the rest of the case.
        message = str(exc)[:400]
        if "POS_PROFILE_TERRITORY_MISMATCH" in message:
            _skip(
                ctx, case,
                f"assert_pos_profile_matches_territory still refuses after mapping "
                f"[{mapping_detail}]: {message}",
            )
        else:
            ctx.record(f"{case}.amendment_succeeded", False, message)
        return
    frappe.db.commit()

    if not result.get("success"):
        code = str(result.get("amendment_block_code") or "")
        reason = str(result.get("error") or "")[:300]
        if code in _AMENDMENT_PRECONDITION_CODES:
            _skip(ctx, case, f"amendment blocked by precondition [{code}]: {reason}")
        else:
            ctx.record(f"{case}.amendment_succeeded", False, f"[{code}] {reason}")
        return

    replacement = result.get("replacement_invoice_id")
    ctx.record(
        f"{case}.amendment_succeeded",
        bool(replacement) and not result.get("already_processed"),
        f"replacement={replacement} cancelled_payment_entries="
        f"{result.get('cancelled_payment_entries')}",
    )
    if not replacement:
        return
    if replacement not in ctx.invoices:
        ctx.invoices.append(replacement)
    _absorb(ctx, replacement)

    for pe in paid_entries:
        live = _live_gl_rows("Payment Entry", pe)
        cancelled = _cancelled_gl_rows("Payment Entry", pe)
        docstatus = int(frappe.db.get_value("Payment Entry", pe, "docstatus") or 0)
        ctx.record(
            f"{case}.payment_entry_cancelled::{pe}",
            docstatus == 2 and live == 0 and cancelled > 0,
            f"docstatus={docstatus} live GL rows={live} is_cancelled=1 rows={cancelled}",
        )
    cash_after = round(sum(_gl_net("Payment Entry", pe, cash_acc) for pe in paid_entries), 2)
    ctx.record(
        f"{case}.branch_cash_returned",
        abs(cash_after) <= _TOL,
        f"live cash({cash_acc}) movement from the cancelled payment(s) {paid_amount} -> "
        f"{cash_after} (must be 0)",
    )

    src_docstatus = int(_invoice_value(invoice, "docstatus") or 0)
    src_live = _live_gl_rows("Sales Invoice", invoice)
    ctx.record(
        f"{case}.source_invoice_gl_fully_reversed",
        src_docstatus == 2 and src_live == 0,
        f"source docstatus={src_docstatus} live GL rows={src_live}",
    )

    new_grand = _grand_total(replacement)
    amended_from = _invoice_value(replacement, "amended_from")
    ctx.record(
        f"{case}.replacement_carries_amended_from",
        str(amended_from or "") == invoice,
        f"amended_from={amended_from} expected={invoice}",
    )
    new_receivable = _receivable_total(replacement)
    ctx.record(
        f"{case}.replacement_outstanding_is_its_grand_total",
        abs(_outstanding(replacement) - new_receivable) <= _TOL,
        f"outstanding={_outstanding(replacement)} receivable total={new_receivable} "
        f"(grand_total={new_grand}; ERPNext books the receivable at rounded_total "
        f"unless disable_rounded_total is set)",
    )
    _assert_voucher_balanced(ctx, case, "Sales Invoice", replacement)

    net_receivable = _gl_net_for_invoices([invoice, replacement], receivable_acc)
    ctx.record(
        f"{case}.net_receivable_equals_the_new_total",
        abs(net_receivable - new_receivable) <= _TOL,
        f"net {receivable_acc} across source+replacement={net_receivable} new "
        f"receivable total={new_receivable} (grand_total={new_grand}; source was "
        f"{source_grand})",
    )

    dn_after = len(_delivery_notes_for(invoice)) + len(_delivery_notes_for(replacement))
    ctx.record(
        f"{case}.no_extra_delivery_note",
        dn_after == dn_before,
        f"submitted delivery notes before={dn_before} after={dn_after} "
        f"(source={_delivery_notes_for(invoice)} "
        f"replacement={_delivery_notes_for(replacement)})",
    )
    sle_rows = sum(
        frappe.db.count("Stock Ledger Entry",
                        {"voucher_type": "Sales Invoice", "voucher_no": name,
                         "is_cancelled": 0})
        for name in (invoice, replacement)
    )
    ctx.record(
        f"{case}.invoices_move_no_stock_directly",
        sle_rows == 0,
        f"stock ledger rows booked against the invoices={sle_rows} (must be 0)",
    )

    new_ship_expense = flt(_invoice_value(replacement, "custom_shipping_expense"))
    if src_ship_expense <= 0:
        _skip(
            ctx, f"{case}.shipping_expense_survives_reuse_source_cart",
            f"the source invoice carries no custom_shipping_expense (={src_ship_expense}; "
            "it is stamped at dispatch, and an amendable invoice is by definition "
            "pre-dispatch), so preservation cannot be observed",
        )
    else:
        ctx.record(
            f"{case}.shipping_expense_survives_reuse_source_cart",
            abs(new_ship_expense - src_ship_expense) <= _TOL,
            f"custom_shipping_expense {src_ship_expense} -> {new_ship_expense}",
        )

    new_income_present, new_income = _shipping_income_row(replacement)
    if not src_income_present:
        _skip(
            ctx, f"{case}.shipping_income_row_survives_reuse_source_cart",
            f"the source invoice has no Shipping Income tax row (territory "
            f"{env['territory']} bills no delivery income), so preservation cannot be "
            "observed",
        )
    else:
        ctx.record(
            f"{case}.shipping_income_row_survives_reuse_source_cart",
            new_income_present and abs(new_income - src_income) <= _TOL,
            f"Shipping Income row {src_income} -> {new_income} "
            f"(present={new_income_present})",
        )


# ─────────────────────────────────────────────────────────────────────────────
# C3 - Address / territory change on a SUBMITTED invoice   [HIGHEST PRIORITY]
#
# api/customer.py:1135       change_invoice_shipping_address (verified)
# api/customer.py:1233       the Approved-override branch    (verified)
# api/customer.py:1253       had_shipping_income_row         (verified)
# api/customer.py:1258-1271  tax-row rewrite, calculate_taxes_and_totals(), and
#                            save(ignore_validate_update_after_submit) on a
#                            SUBMITTED invoice               (verified)
#
# The ledger does NOT follow that save. ERPNext's on_update_after_submit reposts
# only when a tax row's account_head (or an accounting dimension) changes, and the
# rebuilt Shipping Income row keeps the same account -- so the document moved
# 685 -> 700 on ACC-SINV-2026-18096 (Woo 17176, 2026-09-03) while Debtors stayed
# at 685, and every Payment Entry afterwards failed "Allocated Amount cannot be
# greater than outstanding amount". (The earlier "verified on 17999/18014" note
# in this header compared a receivable that had not actually been re-rated.)
#
# Since 2026-09-05 api/customer.py refuses a fee-changing territory move on a
# submitted invoice BEFORE its first write; a same-fee move (territory C below)
# is still allowed, and the free-shipping gate is unchanged. The assertions below
# compare against ``_receivable_total`` (rounded_total when rounding is enabled),
# not against grand_total.
# ─────────────────────────────────────────────────────────────────────────────

def _prepare_c3_customer(ctx: RunContext, env: Dict[str, Any], suffix: str,
                         territory: str) -> Tuple[str, Optional[str]]:
    """Return (customer, address) with the customer's ONLY address in *territory*.

    create_pos_invoice resolves the order territory from the auto-picked shipping
    address BEFORE it falls back to Customer.territory, so the address must carry
    the territory or the fixture proves nothing. A previous run's alternate
    address would win that auto-pick, so it is removed first.
    """
    customer = _ensure_customer(ctx, suffix, territory, env["customer_group"])
    keep: Optional[str] = None
    for name in _addresses_for_customer(customer):
        title = str(frappe.db.get_value("Address", name, "address_title") or "")
        if title.endswith("_ALT"):
            try:
                frappe.delete_doc("Address", name, force=True, ignore_permissions=True,
                                  delete_permanently=True)
            except Exception:
                pass
            continue
        if keep is None:
            keep = name
    if keep:
        frappe.db.set_value("Address", keep, {"city": territory}, update_modified=False)
        _track(ctx, "mp_addresses", keep)
    else:
        keep = _ensure_mp_address(ctx, customer, f"{customer}_HOME", territory)
    try:
        frappe.db.set_value("Customer", customer,
                            {"customer_primary_address": keep or ""}, update_modified=False)
    except Exception:
        pass
    frappe.db.commit()
    return customer, keep


def _case_address_territory_change(ctx: RunContext, env: Dict[str, Any]) -> None:
    case = "C3-ADDRESS-TERRITORY"

    from jarz_pos.api.customer import change_invoice_shipping_address

    cols = _territory_columns()
    if "delivery_income" not in cols:
        _skip(ctx, case, "Territory has no delivery_income column on this site, so a "
                         "territory change cannot change delivery income")
        return
    expense_supported = "delivery_expense" in cols

    _ensure_mp_territory(ctx, TERRITORY_A, TERR_A_INCOME, TERR_A_EXPENSE)
    _ensure_mp_territory(ctx, TERRITORY_B, TERR_B_INCOME, TERR_B_EXPENSE)
    frappe.db.commit()

    customer, home = _prepare_c3_customer(ctx, env, case, TERRITORY_A)
    if not home:
        _skip(ctx, case, f"could not build a shipping address in {TERRITORY_A}")
        return

    invoice = _create_invoice(
        ctx, customer=customer, pos_profile=env["pos_profile"], items=env["items"],
        order_purpose=None, commercial_policy=None, policy_reason=None, pickup=False,
    )
    frappe.db.commit()

    if str(_invoice_value(invoice, "territory") or "") != TERRITORY_A:
        _skip(
            ctx, case,
            f"invoice {invoice} resolved to territory "
            f"{_invoice_value(invoice, 'territory')!r} instead of {TERRITORY_A}; the "
            "precondition (an order billed at territory A's rate) is not in place",
        )
        return

    income_present, old_income = _shipping_income_row(invoice)
    if not income_present or abs(old_income - TERR_A_INCOME) > _TOL:
        _skip(
            ctx, case,
            f"invoice {invoice} did not acquire territory A's Shipping Income row "
            f"(present={income_present} amount={old_income} expected={TERR_A_INCOME})",
        )
        return

    old_grand = _grand_total(invoice)
    receivable_acc = _invoice_value(invoice, "debit_to")
    address_b = _ensure_mp_address(ctx, customer, f"{customer}_ALT", TERRITORY_B)
    if not address_b:
        _skip(ctx, case, f"could not build a shipping address in {TERRITORY_B}")
        return
    frappe.db.commit()

    # ---- a fee-changing move on a SUBMITTED invoice must be REFUSED ----------
    # ACC-SINV-2026-18096 (Woo 17176): the rebuild took the document 685 -> 700
    # while the ledger stayed at 685 (ERPNext reposts on_update_after_submit only
    # when an account head changes, and the rebuilt row keeps the same account),
    # so every later Payment Entry failed "Allocated Amount cannot be greater than
    # outstanding amount". The guard now runs before the first write.
    old_territory_on_invoice = str(_invoice_value(invoice, "territory") or "")
    old_expense_on_invoice = flt(_invoice_value(invoice, "custom_shipping_expense"))
    old_receivable = _receivable_total(invoice)
    old_receivable_debit = _gl_net_for_invoices([invoice], receivable_acc)
    try:
        res = change_invoice_shipping_address(invoice, address_b) or {}
        refused, reason = False, json.dumps(res, default=str)[:300]
        frappe.db.commit()
    except Exception as exc:
        refused, reason = True, str(exc)
        frappe.db.rollback()

    ctx.record(
        f"{case}.submitted_invoice_fee_change_is_refused",
        refused and "already submitted" in reason,
        f"income {TERR_A_INCOME} -> {TERR_B_INCOME} on a submitted invoice; "
        f"refused={refused}; {reason}",
    )
    ctx.record(
        f"{case}.refusal_reason_reaches_the_caller_unwrapped",
        refused and "Failed to change invoice shipping address" not in reason,
        reason,
    )

    new_grand = _grand_total(invoice)
    new_income_present, new_income = _shipping_income_row(invoice)
    ctx.record(
        f"{case}.shipping_income_row_untouched_by_the_refusal",
        new_income_present and abs(new_income - old_income) <= _TOL,
        f"Shipping Income {old_income} -> {new_income} (must stay {TERR_A_INCOME})",
    )
    ctx.record(
        f"{case}.grand_total_untouched_by_the_refusal",
        abs(new_grand - old_grand) <= _TOL,
        f"grand_total {old_grand} -> {new_grand}",
    )
    ctx.record(
        f"{case}.territory_and_expense_untouched_by_the_refusal",
        str(_invoice_value(invoice, "territory") or "") == old_territory_on_invoice
        and abs(flt(_invoice_value(invoice, "custom_shipping_expense"))
                - old_expense_on_invoice) <= _TOL,
        f"territory {old_territory_on_invoice!r} -> "
        f"{_invoice_value(invoice, 'territory')!r}; custom_shipping_expense "
        f"{old_expense_on_invoice} -> {_invoice_value(invoice, 'custom_shipping_expense')}",
    )

    new_receivable = _receivable_total(invoice)
    receivable_debit = _gl_net_for_invoices([invoice], receivable_acc)
    delta = round(receivable_debit - new_receivable, 2)
    ctx.record(
        f"{case}.receivable_gl_still_equals_the_document_total",
        abs(delta) <= _TOL and abs(receivable_debit - old_receivable_debit) <= _TOL,
        f"live SUM(debit-credit) on {receivable_acc} for {invoice} = {receivable_debit} "
        f"(was {old_receivable_debit}); document receivable total = {new_receivable} "
        f"(was {old_receivable}); DELTA = {delta}. This is the invariant 17176 broke.",
    )

    allocated = _allocated_total(invoice)
    outstanding = _outstanding(invoice)
    expected_outstanding = round(new_receivable - allocated, 2)
    ctx.record(
        f"{case}.outstanding_equals_total_minus_payments",
        abs(outstanding - expected_outstanding) <= _TOL,
        f"outstanding={outstanding} expected={expected_outstanding} "
        f"(receivable total={new_receivable} - allocated={allocated}); DELTA = "
        f"{round(outstanding - expected_outstanding, 2)}",
    )

    balanced, detail = assert_gl_balanced("Sales Invoice", invoice)
    _observe(ctx, f"{case}.invoice_gl_after_the_refusal", detail)

    # ---- a SAME-fee move is still allowed, and the ledger still matches -------
    # Territory C bills the same delivery income as A with a different courier
    # expense: the customer-facing total cannot move, so the change goes through
    # and only custom_shipping_expense follows the new territory.
    _ensure_mp_territory(ctx, TERRITORY_C, TERR_C_INCOME, TERR_C_EXPENSE)
    frappe.db.commit()
    address_c = _ensure_mp_address(ctx, customer, f"{customer}_ALT_C", TERRITORY_C)
    if not address_c:
        _skip(ctx, f"{case}.same_fee_change_is_allowed",
              f"could not build a shipping address in {TERRITORY_C}")
    else:
        frappe.db.commit()
        try:
            res_c = change_invoice_shipping_address(invoice, address_c) or {}
            frappe.db.commit()
            ctx.record(
                f"{case}.same_fee_change_is_allowed",
                bool(res_c.get("success")) and bool(res_c.get("territory_changed"))
                and str(res_c.get("new_territory")) == TERRITORY_C,
                json.dumps(res_c, default=str)[:300],
            )
        except Exception as exc:
            ctx.record(f"{case}.same_fee_change_is_allowed", False, f"{exc}")
            frappe.db.rollback()
            res_c = {}

        if res_c.get("territory_changed"):
            c_grand = _grand_total(invoice)
            c_present, c_income = _shipping_income_row(invoice)
            ctx.record(
                f"{case}.same_fee_change_keeps_the_shipping_income_row",
                c_present and abs(c_income - TERR_A_INCOME) <= _TOL,
                f"Shipping Income {old_income} -> {c_income} (territory {TERRITORY_C} "
                f"bills {TERR_C_INCOME})",
            )
            ctx.record(
                f"{case}.same_fee_change_keeps_grand_total",
                abs(c_grand - old_grand) <= _TOL,
                f"grand_total {old_grand} -> {c_grand}",
            )
            c_receivable = _receivable_total(invoice)
            c_debit = _gl_net_for_invoices([invoice], receivable_acc)
            ctx.record(
                f"{case}.same_fee_change_receivable_gl_equals_the_document_total",
                abs(c_debit - c_receivable) <= _TOL,
                f"live SUM(debit-credit) on {receivable_acc} = {c_debit}; document "
                f"receivable total = {c_receivable}; DELTA = {round(c_debit - c_receivable, 2)}",
            )
            if expense_supported:
                ctx.record(
                    f"{case}.shipping_expense_follows_the_new_territory",
                    abs(flt(_invoice_value(invoice, "custom_shipping_expense"))
                        - TERR_C_EXPENSE) <= _TOL,
                    f"custom_shipping_expense="
                    f"{_invoice_value(invoice, 'custom_shipping_expense')} "
                    f"expected={TERR_C_EXPENSE} (territory {TERRITORY_C})",
                )
            else:
                _skip(ctx, f"{case}.shipping_expense_follows_the_new_territory",
                      "Territory has no delivery_expense column on this site, so "
                      "_territory_values in api/customer.py always reads 0 for expense")

    # ---- an Approved shipping override must survive the territory change (:1233) ----
    if not frappe.get_meta("Sales Invoice").get_field("custom_shipping_override_status"):
        _skip(ctx, f"{case}.approved_override_is_respected",
              "Sales Invoice has no custom_shipping_override_status field on this site")
    elif not expense_supported:
        _skip(ctx, f"{case}.approved_override_is_respected",
              "Territory has no delivery_expense column, so the override branch cannot be "
              "distinguished from the default")
    else:
        ov_customer, ov_home = _prepare_c3_customer(ctx, env, f"{case}-OV", TERRITORY_A)
        if not ov_home:
            _skip(ctx, f"{case}.approved_override_is_respected",
                  f"could not build a shipping address in {TERRITORY_A} for the override "
                  "fixture")
        else:
            ov_invoice = _create_invoice(
                ctx, customer=ov_customer, pos_profile=env["pos_profile"],
                items=env["items"], order_purpose=None, commercial_policy=None,
                policy_reason=None, pickup=False,
            )
            frappe.db.commit()
            override_expense = 3.19
            frappe.db.set_value(
                "Sales Invoice", ov_invoice,
                {"custom_shipping_override_status": "Approved",
                 "custom_shipping_expense": override_expense},
                update_modified=False,
            )
            # Territory C, not B: the invoice is submitted, so a fee-changing move
            # is refused before the override branch is ever reached.
            _ensure_mp_territory(ctx, TERRITORY_C, TERR_C_INCOME, TERR_C_EXPENSE)
            ov_address = _ensure_mp_address(ctx, ov_customer, f"{ov_customer}_ALT",
                                            TERRITORY_C)
            frappe.db.commit()
            if not ov_address:
                _skip(ctx, f"{case}.approved_override_is_respected",
                      f"could not build a shipping address in {TERRITORY_C} for the "
                      "override fixture")
            else:
                try:
                    change_invoice_shipping_address(ov_invoice, ov_address)
                    frappe.db.commit()
                    after_expense = flt(_invoice_value(ov_invoice,
                                                       "custom_shipping_expense"))
                    ctx.record(
                        f"{case}.approved_override_is_respected",
                        abs(after_expense - override_expense) <= _TOL,
                        f"custom_shipping_expense stayed {after_expense} (override "
                        f"{override_expense}); territory {TERRITORY_C} would bill "
                        f"{TERR_C_EXPENSE}",
                    )
                except Exception as exc:
                    ctx.record(f"{case}.approved_override_is_respected", False, f"{exc}")
                    frappe.db.rollback()

    # ---- free shipping -> charged territory (the had_shipping_income_row gate) ----
    _ensure_mp_territory(ctx, TERRITORY_FREE, TERR_FREE_INCOME, TERR_FREE_EXPENSE)
    frappe.db.commit()
    free_customer, free_home = _prepare_c3_customer(ctx, env, f"{case}-FREE",
                                                    TERRITORY_FREE)
    if not free_home:
        _skip(ctx, f"{case}.free_shipping_invoice_moves_into_a_charged_territory",
              f"could not build a shipping address in {TERRITORY_FREE}")
        return
    free_invoice = _create_invoice(
        ctx, customer=free_customer, pos_profile=env["pos_profile"], items=env["items"],
        order_purpose=None, commercial_policy=None, policy_reason=None, pickup=False,
    )
    frappe.db.commit()
    had_row, _had_amount = _shipping_income_row(free_invoice)
    if had_row:
        _skip(
            ctx, f"{case}.free_shipping_invoice_moves_into_a_charged_territory",
            f"invoice {free_invoice} already carries a Shipping Income row despite "
            f"territory {TERRITORY_FREE} billing {TERR_FREE_INCOME}",
        )
        return

    free_old_grand = _grand_total(free_invoice)
    free_receivable = _invoice_value(free_invoice, "debit_to")
    free_address = _ensure_mp_address(ctx, free_customer, f"{free_customer}_ALT",
                                      TERRITORY_B)
    frappe.db.commit()
    if not free_address:
        _skip(ctx, f"{case}.free_shipping_invoice_moves_into_a_charged_territory",
              f"could not build a shipping address in {TERRITORY_B}")
        return
    try:
        free_res = change_invoice_shipping_address(free_invoice, free_address) or {}
    except Exception as exc:
        ctx.record(f"{case}.free_shipping_address_change_succeeded", False, f"{exc}")
        return
    frappe.db.commit()

    free_row, free_amount = _shipping_income_row(free_invoice)
    free_new_grand = _grand_total(free_invoice)
    _observe(
        ctx, f"{case}.free_shipping_invoice_moves_into_a_charged_territory",
        f"invoice {free_invoice} moved {TERRITORY_FREE} (income {TERR_FREE_INCOME}) -> "
        f"{free_res.get('new_territory')} (income {TERR_B_INCOME}). Shipping Income row "
        f"present after={free_row} amount={free_amount}; grand_total {free_old_grand} -> "
        f"{free_new_grand}; custom_shipping_expense="
        f"{_invoice_value(free_invoice, 'custom_shipping_expense')}. "
        "had_shipping_income_row at api/customer.py:1253 gates the whole rewrite block, so "
        "an invoice that started with no row cannot gain one. WHETHER THAT IS INTENDED IS A "
        "HUMAN CALL: the customer keeps free shipping in an area that bills for it, while "
        "the courier expense follows the new territory.",
    )
    free_receivable_debit = _gl_net_for_invoices([free_invoice], free_receivable)
    free_new_receivable = _receivable_total(free_invoice)
    ctx.record(
        f"{case}.free_shipping_receivable_still_matches_its_own_total",
        abs(free_receivable_debit - free_new_receivable) <= _TOL,
        f"live SUM(debit-credit) on {free_receivable} = {free_receivable_debit}; "
        f"receivable total = {free_new_receivable} (grand_total={free_new_grand}); "
        f"DELTA = {round(free_receivable_debit - free_new_receivable, 2)}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# C4 - Unpaid-online (InstaPay) dispatch -> confirm -> convert
#
# services/delivery_handling.py:1065 handle_unpaid_online_deliver_unconfirmed (verified)
# services/delivery_handling.py:1320 confirm_online_payment                   (verified)
# services/delivery_handling.py:1409 convert_online_order_to_cod              (verified)
#
# The preview branch that depends on the zero-amount courier row is at
# api/couriers.py:668-670, NOT 653-655 as briefed (653 is "if ct_shipping > 0:").
#
# Dispatch is driven through settlement_strategies.dispatch_settlement, which is
# the ONLY route to :1065 - handle_out_for_delivery_transition sends every
# unpaid invoice to mark_courier_outstanding regardless of payment method.
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch_unpaid_online(ctx: RunContext, env: Dict[str, Any], case: str,
                            suffix: str) -> Optional[Tuple[str, float]]:
    """Create an unpaid InstaPay-intent order and move it Out for Delivery."""
    from jarz_pos.services.settlement_strategies import dispatch_settlement

    invoice = _new_invoice(ctx, env, f"{case}-{suffix}")
    frappe.db.commit()
    try:
        frappe.db.set_value("Sales Invoice", invoice,
                            {"custom_payment_method": "Instapay"}, update_modified=False)
    except Exception as exc:
        _skip(ctx, case, f"could not stamp custom_payment_method on {invoice}: {exc}")
        return None
    frappe.db.commit()

    grand = _grand_total(invoice)
    res = dispatch_settlement(
        invoice, mode="later", pos_profile=env["pos_profile"], payment_type="Instapay",
        party_type=env["courier"]["party_type"], party=env["courier"]["party"],
    ) or {}
    frappe.db.commit()
    _absorb(ctx, invoice)
    ctx.record(
        f"{case}.{suffix}.routed_to_the_unpaid_online_handler",
        str(res.get("mode") or "") == "unpaid_online_deliver_unconfirmed",
        f"dispatch_settlement mode={res.get('mode')}",
    )
    return invoice, grand


def _case_online_unpaid_lifecycle(ctx: RunContext, env: Dict[str, Any]) -> None:
    case = "C4-ONLINE-UNPAID"

    from jarz_pos.api.couriers import (
        confirm_online_payment,
        convert_online_order_to_cod,
        generate_settlement_preview,
    )
    from jarz_pos.services.delivery_handling import (
        _get_courier_outstanding_account,
        _get_online_collection_account,
    )

    if not env["courier"]:
        _skip(ctx, case, f"no courier is bound to POS profile {env['pos_profile']}")
        return
    if not frappe.get_meta("Sales Invoice").get_field("custom_payment_method"):
        _skip(ctx, case, "Sales Invoice has no custom_payment_method field, so an "
                         "online-intent order cannot be expressed")
        return
    company = env["company"]
    try:
        online_acc = _get_online_collection_account("Instapay", company)
    except Exception as exc:
        _skip(ctx, case, f"no InstaPay collection ledger resolvable for {company}: {exc}")
        return

    dispatched = _dispatch_unpaid_online(ctx, env, case, "CONFIRM")
    if not dispatched:
        return
    invoice, grand = dispatched

    ctx.record(
        f"{case}.dispatch_posts_no_payment_entry",
        not _submitted_payment_entries_for(invoice),
        f"payment entries after dispatch={_submitted_payment_entries_for(invoice)}",
    )
    receivable = _receivable_total(invoice)
    ctx.record(
        f"{case}.dispatch_leaves_the_full_amount_outstanding",
        abs(_outstanding(invoice) - receivable) <= _TOL,
        f"outstanding={_outstanding(invoice)} receivable total={receivable} "
        f"(grand_total={grand})",
    )

    cts = _courier_transactions(invoice)
    freight = flt(_invoice_value(invoice, "custom_shipping_expense"))
    if not cts:
        if freight <= 0:
            _skip(
                ctx, f"{case}.dispatch_creates_a_zero_amount_courier_row",
                f"territory {env['territory']} accrues no delivery expense "
                f"(custom_shipping_expense={freight}); the freight courier row is only "
                "created when freight > 0",
            )
        else:
            ctx.record(
                f"{case}.dispatch_creates_a_zero_amount_courier_row", False,
                f"freight={freight} but no Courier Transaction was created",
            )
    else:
        ctx.record(
            f"{case}.dispatch_creates_a_zero_amount_courier_row",
            all(abs(flt(c["amount"])) <= _TOL for c in cts),
            f"courier rows="
            f"{[(c['amount'], c['shipping_amount'], c['status']) for c in cts]} -- "
            "api/couriers.py:668 zeroes the settlement preview off exactly this",
        )
        for ct in cts:
            if ct.get("journal_entry"):
                _assert_voucher_balanced(ctx, case, "Journal Entry", ct["journal_entry"])

    try:
        preview = generate_settlement_preview(
            invoice, env["courier"]["party_type"], env["courier"]["party"]
        ) or {}
        ctx.record(
            f"{case}.preview_collects_nothing_from_the_courier",
            abs(flt(preview.get("order_amount"))) <= _TOL
            and abs(flt(preview.get("net_amount"))) <= _TOL,
            f"preview order_amount={preview.get('order_amount')} "
            f"net_amount={preview.get('net_amount')} "
            f"is_online_unconfirmed={preview.get('is_online_unconfirmed')}",
        )
    except Exception as exc:
        ctx.record(f"{case}.preview_collects_nothing_from_the_courier", False, f"{exc}")

    receipt = _make_receipt(ctx, invoice, env["pos_profile"], grand, "InstaPay")
    if not receipt:
        _skip(ctx, f"{case}.confirm_online_payment",
              "could not create the POS Payment Receipt fixture that "
              "confirm_online_payment validates before booking")
    else:
        try:
            confirm_online_payment(
                invoice_name=invoice, pos_profile=env["pos_profile"],
                reference_no="_B2BVALID_MP_C4_REF", receipt_name=receipt,
            )
            confirm_error = ""
        except Exception as exc:
            confirm_error = str(exc)[:300]
        frappe.db.commit()
        _absorb(ctx, invoice)

        pes = _submitted_payment_entries_for(invoice)
        ctx.record(
            f"{case}.confirm_posts_exactly_one_payment_entry",
            len(pes) == 1 and not confirm_error,
            f"payment entries={pes} error={confirm_error!r}",
        )
        receivable_acc = _invoice_value(invoice, "debit_to")
        for pe in pes:
            _assert_voucher_balanced(ctx, case, "Payment Entry", pe)
            gl = _gl_by_account("Payment Entry", pe)
            # confirm_online_payment pays the OUTSTANDING (delivery_handling.py:1597),
            # which ERPNext carries at rounded_total, so the ledger is measured
            # against the receivable total rather than grand_total.
            ctx.record(
                f"{case}.confirm_debits_the_online_ledger::{pe}",
                abs(flt(gl.get(online_acc, 0.0)) - receivable) <= _TOL,
                f"{online_acc} net={gl.get(online_acc, 0.0)} expected={receivable} "
                f"(grand_total={grand})",
            )
            ctx.record(
                f"{case}.confirm_credits_the_receivable::{pe}",
                abs(flt(gl.get(receivable_acc, 0.0)) + receivable) <= _TOL,
                f"{receivable_acc} net={gl.get(receivable_acc, 0.0)} expected={-receivable}",
            )
        ctx.record(
            f"{case}.confirm_clears_the_outstanding",
            abs(_outstanding(invoice)) <= _TOL,
            f"outstanding={_outstanding(invoice)}",
        )

        try:
            replay = confirm_online_payment(
                invoice_name=invoice, pos_profile=env["pos_profile"],
                reference_no="_B2BVALID_MP_C4_REF", receipt_name=receipt,
            ) or {}
            replay_error = ""
        except Exception as exc:
            replay = {}
            replay_error = str(exc)[:200]
        frappe.db.commit()
        ctx.record(
            f"{case}.confirm_is_idempotent",
            bool(replay.get("already_confirmed"))
            and len(_submitted_payment_entries_for(invoice)) == len(pes),
            f"replay already_confirmed={replay.get('already_confirmed')} payment "
            f"entries={_submitted_payment_entries_for(invoice)} error={replay_error!r}",
        )

    converted = _dispatch_unpaid_online(ctx, env, case, "CONVERT")
    if not converted:
        return
    conv_invoice, conv_grand = converted
    courier_outstanding_acc = _get_courier_outstanding_account(company)

    try:
        convert_online_order_to_cod(
            invoice_name=conv_invoice, pos_profile=env["pos_profile"],
            party_type=env["courier"]["party_type"], party=env["courier"]["party"],
        )
        conv_error = ""
    except Exception as exc:
        conv_error = str(exc)[:300]
    frappe.db.commit()
    _absorb(ctx, conv_invoice)

    conv_pes = _submitted_payment_entries_for(conv_invoice)
    conv_receivable = _receivable_total(conv_invoice)
    moved = round(sum(_gl_net("Payment Entry", pe, courier_outstanding_acc)
                      for pe in conv_pes), 2)
    ctx.record(
        f"{case}.convert_moves_the_receivable_to_courier_outstanding",
        abs(moved - conv_receivable) <= _TOL and not conv_error,
        f"{courier_outstanding_acc} debit across {conv_pes} = {moved} "
        f"expected={conv_receivable} (the Payment Entry moves the OUTSTANDING; "
        f"grand_total={conv_grand}) error={conv_error!r}",
    )
    ctx.record(
        f"{case}.convert_clears_the_outstanding",
        abs(_outstanding(conv_invoice)) <= _TOL,
        f"outstanding={_outstanding(conv_invoice)}",
    )
    conv_cts = _courier_transactions(conv_invoice)
    ctx.record(
        f"{case}.convert_sets_the_courier_amount_to_the_grand_total",
        any(abs(flt(c["amount"]) - conv_grand) <= _TOL for c in conv_cts),
        f"courier rows="
        f"{[(c['amount'], c['shipping_amount'], c['notes']) for c in conv_cts]} "
        f"expected one carrying {conv_grand}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# C6 - Settlement preview-token path (what the Flutter client actually calls)
#
# api/couriers.py:751  confirm_settlement             (briefed as 736 - DRIFTED +15)
# api/couriers.py:775  invoice mismatch refusal       (briefed as 751 - DRIFTED +24)
# api/couriers.py:799  strat_mode mapping             (briefed as 776 - DRIFTED +23)
# api/couriers.py:813  token invalidation on success  (briefed as 790 - DRIFTED +23)
# ─────────────────────────────────────────────────────────────────────────────

def _case_settlement_preview_token(ctx: RunContext, env: Dict[str, Any]) -> None:
    case = "C6-PREVIEW-TOKEN"

    from jarz_pos.api.couriers import confirm_settlement, generate_settlement_preview
    from jarz_pos.services.delivery_handling import _get_courier_outstanding_account
    from jarz_pos.utils.account_utils import (
        get_freight_expense_account,
        get_pos_cash_account,
    )

    courier = env["courier"]
    if not courier:
        _skip(ctx, case, f"no courier is bound to POS profile {env['pos_profile']}")
        return
    party_type, party = courier["party_type"], courier["party"]

    invoice_a = _new_invoice(ctx, env, f"{case}-A")
    invoice_b = _new_invoice(ctx, env, f"{case}-B")
    frappe.db.commit()
    grand_a = _grand_total(invoice_a)

    preview = generate_settlement_preview(invoice_a, party_type, party,
                                          mode="pay_now") or {}
    token = preview.get("preview_token")
    if not token:
        _skip(ctx, case, f"generate_settlement_preview minted no token for {invoice_a}")
        return

    ctx.record(
        f"{case}.preview_prices_the_unpaid_order_at_its_total",
        abs(flt(preview.get("order_amount")) - grand_a) <= _TOL,
        f"preview order_amount={preview.get('order_amount')} grand_total={grand_a} "
        f"shipping={preview.get('shipping_amount')} net={preview.get('net_amount')}",
    )

    raised, message = _expect_throw(
        confirm_settlement, invoice=invoice_b, preview_token=token, mode="pay_now",
        pos_profile=env["pos_profile"], party_type=party_type, party=party,
    )
    ctx.record(
        f"{case}.token_for_a_is_rejected_against_b",
        raised and "does not match" in message.lower(),
        message,
    )

    try:
        res = confirm_settlement(
            invoice=invoice_a, preview_token=token, mode="pay_now",
            pos_profile=env["pos_profile"], party_type=party_type, party=party,
            payment_mode="Cash",
        ) or {}
        confirm_error = ""
    except Exception as exc:
        res = {}
        confirm_error = str(exc)[:300]
    frappe.db.commit()
    _absorb(ctx, invoice_a)

    # api/couriers.py:799 maps the request mode onto the strategy key ("pay_now"/"now"
    # -> "now", everything else -> "later"), but the response built at :817-831 sets
    # ``"mode": mode`` -- the REQUEST mode -- and then merges the strategy result with
    # ``if k not in base``, so the handler's own "unpaid_settle_now" label is dropped
    # and NEVER reaches the caller. Asserting on it could only ever fail. The ledger
    # is asked instead: handle_unpaid_settle_now (settlement_strategies.py:148) pays
    # the receivable into the POS cash account, while handle_unpaid_settle_later moves
    # it to Courier Outstanding -- so a confirm that silently took the "later" branch
    # still fails this check.
    cash_acc = get_pos_cash_account(env["pos_profile"], env["company"])
    courier_outstanding_acc = _get_courier_outstanding_account(env["company"])
    now_pe = str(res.get("payment_entry") or "")
    now_pe_row = frappe.db.get_value(
        "Payment Entry", now_pe, ["docstatus", "paid_to", "paid_amount"], as_dict=True
    ) if now_pe else None
    now_to_courier = _pe_gl_net(invoice_a, courier_outstanding_acc)
    ctx.record(
        f"{case}.confirm_selects_the_strategy_the_preview_promised",
        not confirm_error
        and bool(res.get("success"))
        and str(res.get("mode") or "") == "pay_now"
        and bool(now_pe_row)
        and int((now_pe_row or {}).get("docstatus") or 0) == 1
        and str((now_pe_row or {}).get("paid_to") or "") == cash_acc
        and abs(now_to_courier) <= _TOL,
        f"preview said is_unpaid_effective={preview.get('is_unpaid_effective')} "
        f"mode=pay_now; endpoint echoed mode={res.get('mode')!r} and returned "
        f"payment_entry={now_pe!r} -> {now_pe_row} (expected a submitted PE paid into "
        f"{cash_acc}); courier-outstanding movement across this invoice's payment "
        f"entries={now_to_courier} (must be 0) error={confirm_error!r}",
    )
    ctx.record(
        f"{case}.confirm_reports_the_cached_preview_amounts",
        abs(flt(res.get("order_amount")) - flt(preview.get("order_amount"))) <= _TOL
        and abs(flt(res.get("shipping_amount"))
                - flt(preview.get("shipping_amount"))) <= _TOL,
        f"confirm order={res.get('order_amount')} shipping={res.get('shipping_amount')} "
        f"vs preview order={preview.get('order_amount')} "
        f"shipping={preview.get('shipping_amount')}",
    )

    pes = _submitted_payment_entries_for(invoice_a)
    cash_in = round(sum(_gl_net("Payment Entry", pe, cash_acc) for pe in pes), 2)
    ctx.record(
        f"{case}.posted_cash_matches_the_preview_order_amount",
        abs(cash_in - flt(preview.get("order_amount"))) <= _TOL,
        f"cash({cash_acc}) debited {cash_in} across {pes}; preview order_amount="
        f"{preview.get('order_amount')}",
    )

    freight_acc = get_freight_expense_account(env["company"])
    je_name = res.get("journal_entry")
    if je_name:
        if je_name not in ctx.journal_entries:
            ctx.journal_entries.append(je_name)
        _assert_voucher_balanced(ctx, case, "Journal Entry", je_name)
        ctx.record(
            f"{case}.posted_freight_matches_the_preview_shipping_amount",
            abs(_gl_net("Journal Entry", je_name, freight_acc)
                - flt(preview.get("shipping_amount"))) <= _TOL,
            f"freight({freight_acc}) debited "
            f"{_gl_net('Journal Entry', je_name, freight_acc)}; preview shipping_amount="
            f"{preview.get('shipping_amount')}",
        )
    elif flt(preview.get("shipping_amount")) > _TOL:
        ctx.record(
            f"{case}.posted_freight_matches_the_preview_shipping_amount", False,
            f"preview promised shipping {preview.get('shipping_amount')} but no journal "
            "entry was returned",
        )
    else:
        _skip(ctx, f"{case}.posted_freight_matches_the_preview_shipping_amount",
              "the preview carried no shipping amount, so no freight entry is expected")

    raised, message = _expect_throw(
        confirm_settlement, invoice=invoice_a, preview_token=token, mode="pay_now",
        pos_profile=env["pos_profile"], party_type=party_type, party=party,
    )
    ctx.record(
        f"{case}.token_replay_after_success_is_refused",
        raised and ("expired" in message.lower() or "invalid" in message.lower()),
        message,
    )

    preview_b = generate_settlement_preview(invoice_b, party_type, party,
                                            mode="later") or {}
    token_b = preview_b.get("preview_token")
    if not token_b:
        _skip(ctx, f"{case}.settle_later_maps_to_the_later_strategy",
              f"generate_settlement_preview minted no token for {invoice_b}")
        return
    try:
        res_b = confirm_settlement(
            invoice=invoice_b, preview_token=token_b, mode="later",
            pos_profile=env["pos_profile"], party_type=party_type, party=party,
        ) or {}
        error_b = ""
    except Exception as exc:
        res_b = {}
        error_b = str(exc)[:300]
    frappe.db.commit()
    _absorb(ctx, invoice_b)
    # Same shadowing as above: the endpoint echoes the request mode ("later") and
    # drops the handler's "unpaid_settle_later". The proof that the LATER strategy
    # ran is that the receivable moved to Courier Outstanding through a Payment
    # Entry (mark_courier_outstanding, delivery_handling.py:1119-1148) and NO branch
    # cash was taken. A confirm that took the "now" branch by mistake debits cash
    # instead and fails here.
    later_pe = str(res_b.get("payment_entry") or "")
    later_pe_row = frappe.db.get_value(
        "Payment Entry", later_pe, ["docstatus", "paid_to", "paid_amount"], as_dict=True
    ) if later_pe else None
    later_to_courier = _pe_gl_net(invoice_b, courier_outstanding_acc)
    later_to_cash = _pe_gl_net(invoice_b, cash_acc)
    ctx.record(
        f"{case}.settle_later_maps_to_the_later_strategy",
        not error_b
        and bool(res_b.get("success"))
        and str(res_b.get("mode") or "") == "later"
        and bool(later_pe_row)
        and int((later_pe_row or {}).get("docstatus") or 0) == 1
        and str((later_pe_row or {}).get("paid_to") or "") == courier_outstanding_acc
        and abs(later_to_courier - _receivable_total(invoice_b)) <= _TOL
        and abs(later_to_cash) <= _TOL,
        f"endpoint echoed mode={res_b.get('mode')!r}; payment_entry={later_pe!r} -> "
        f"{later_pe_row} (expected a submitted PE paid into {courier_outstanding_acc}); "
        f"courier outstanding debited {later_to_courier} of a receivable total "
        f"{_receivable_total(invoice_b)}; branch cash movement={later_to_cash} "
        f"(must be 0) error={error_b!r}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# C7 - Cancel guards not covered today
#
# api/kanban.py:2120       cancel_invoice                       (verified)
# api/kanban.py:2163       outstanding/grand_total are read here (as briefed)
# api/kanban.py:2167       tolerance = 0.50
# api/kanban.py:2170       is_paid = outstanding <= tolerance
# api/kanban.py:2175-2176  the partial-payment refusal itself   (briefed as 2162-2163)
# api/kanban.py:2200-2206  ensure_open_shift + closed-shift guard (verified)
# ─────────────────────────────────────────────────────────────────────────────

def _case_cancel_guards(ctx: RunContext, env: Dict[str, Any]) -> None:
    case = "C7-CANCEL-GUARDS"

    from jarz_pos.api.kanban import cancel_invoice
    from jarz_pos.utils.access_control import (
        assert_vouchers_not_in_closed_shift,
        find_closed_shift_covering,
    )

    inside = _new_invoice(ctx, env, f"{case}-INSIDE")
    frappe.db.commit()
    grand_inside = _grand_total(inside)
    # The residue must be measured against what is ACTUALLY outstanding (the
    # rounded receivable), not against grand_total: on a rounded invoice the two
    # differ by up to half a unit, which is enough to push a residue meant to sit
    # inside kanban.py's 0.50 tolerance outside it and fail a healthy product.
    open_inside = _outstanding(inside)
    if open_inside <= CANCEL_RESIDUE_OUTSIDE_TOLERANCE + 1:
        _skip(ctx, case, f"fixture invoice outstanding {open_inside} (grand_total "
                         f"{grand_inside}) is too small to leave a meaningful partial "
                         "residue")
        return
    _pay_invoice_partial(ctx, inside, env["pos_profile"],
                         open_inside - CANCEL_RESIDUE_INSIDE_TOLERANCE)
    frappe.db.commit()
    residue_inside = _outstanding(inside)

    result = cancel_invoice(inside, "money paths validation") or {}
    frappe.db.commit()
    docstatus = int(_invoice_value(inside, "docstatus") or 0)
    message = str(result.get("error") or result.get("message") or "")
    if not result.get("success") and "shift" in message.lower():
        _skip(
            ctx, f"{case}.residue_inside_tolerance_counts_as_fully_paid",
            f"cancel was refused for a shift reason, not the tolerance: {message}",
        )
    else:
        ctx.record(
            f"{case}.residue_inside_tolerance_counts_as_fully_paid",
            bool(result.get("success")) and docstatus == 2,
            f"outstanding {residue_inside} of grand_total {grand_inside} was treated as "
            "FULLY PAID because kanban.py:2167 uses a 0.50 tolerance (is_paid = "
            f"outstanding <= 0.50); cancel success={result.get('success')} "
            f"docstatus={docstatus} message={message!r}. The "
            f"{CANCEL_RESIDUE_INSIDE_TOLERANCE} still sitting in Debtors is written off by "
            "the cancellation.",
        )
    _absorb(ctx, inside)

    outside = _new_invoice(ctx, env, f"{case}-OUTSIDE")
    frappe.db.commit()
    grand_outside = _grand_total(outside)
    open_outside = _outstanding(outside)
    _pay_invoice_partial(ctx, outside, env["pos_profile"],
                         open_outside - CANCEL_RESIDUE_OUTSIDE_TOLERANCE)
    frappe.db.commit()
    residue_outside = _outstanding(outside)
    result = cancel_invoice(outside, "money paths validation") or {}
    frappe.db.commit()
    message = str(result.get("error") or result.get("message") or "")
    ctx.record(
        f"{case}.residue_outside_tolerance_is_refused",
        not result.get("success") and "partial payment" in message.lower(),
        f"outstanding {residue_outside} of grand_total {grand_outside}: "
        f"success={result.get('success')} message={message!r}",
    )
    ctx.record(
        f"{case}.refused_cancel_leaves_the_invoice_submitted",
        int(_invoice_value(outside, "docstatus") or 0) == 1,
        f"docstatus={_invoice_value(outside, 'docstatus')}",
    )
    _absorb(ctx, outside)

    # ---- closed shift ----
    closing = frappe.get_all(
        "POS Closing Entry",
        filters={"docstatus": 1},
        fields=["name", "period_start_date", "period_end_date", "pos_profile"],
        order_by="period_end_date desc",
        limit=1,
    ) or []
    if not closing:
        _skip(
            ctx, f"{case}.closed_shift_refusal",
            "no submitted POS Closing Entry exists on this site, so there is no closed "
            "window for a payment to fall inside",
        )
        return
    window = closing[0]
    try:
        start = get_datetime(window["period_start_date"])
        end = get_datetime(window["period_end_date"])
        inside_window = start + (end - start) / 2
    except Exception as exc:
        _skip(ctx, f"{case}.closed_shift_refusal",
              f"could not compute a timestamp inside {window['name']}: {exc}")
        return

    closed_inv = _new_invoice(ctx, env, f"{case}-CLOSED")
    _pay_invoice_full_cash(ctx, closed_inv, env["pos_profile"])
    frappe.db.commit()
    pes = _submitted_payment_entries_for(closed_inv)
    if not pes:
        _skip(ctx, f"{case}.closed_shift_refusal",
              "the cash payment fixture produced no submitted Payment Entry")
        return
    try:
        for pe in pes:
            frappe.db.set_value("Payment Entry", pe, "creation", inside_window,
                                update_modified=False)
        frappe.db.commit()
    except Exception as exc:
        _skip(ctx, f"{case}.closed_shift_refusal",
              f"could not backdate the fixture payment into {window['name']}: {exc}")
        return

    covering = find_closed_shift_covering(inside_window)
    if not covering:
        _skip(
            ctx, f"{case}.closed_shift_refusal",
            f"find_closed_shift_covering({inside_window}) matched nothing even though "
            f"{window['name']} spans {window['period_start_date']} to "
            f"{window['period_end_date']}",
        )
        return

    raised, guard_message = _expect_throw(
        assert_vouchers_not_in_closed_shift,
        [("Payment Entry", pe) for pe in pes],
        action_label="cancelling this order",
    )
    ctx.record(
        f"{case}.closed_shift_guard_raises",
        raised and "closed" in guard_message.lower(),
        f"assert_vouchers_not_in_closed_shift on {pes} inside {covering.get('name')}: "
        f"{guard_message}",
    )

    result = cancel_invoice(closed_inv, "money paths validation") or {}
    frappe.db.commit()
    message = str(result.get("error") or result.get("message") or "")
    ctx.record(
        f"{case}.closed_shift_cancel_is_refused",
        not result.get("success") and "closed" in message.lower(),
        f"payment(s) {pes} backdated into closed shift {covering.get('name')}: "
        f"success={result.get('success')} message={message!r}",
    )
    ctx.record(
        f"{case}.closed_shift_invoice_stays_submitted",
        int(_invoice_value(closed_inv, "docstatus") or 0) == 1,
        f"docstatus={_invoice_value(closed_inv, 'docstatus')}",
    )
    _absorb(ctx, closed_inv)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

#: Execution order is deliberate. C5 batch-settles EVERY unsettled Courier
#: Transaction for the shared courier party, so it must run before any case that
#: parks one. It also refuses to run at all when rows it did not create are in
#: scope, so a polluted site skips rather than posting against real ledgers.
_CASES = [
    _case_batch_settlement,             # C5
    _case_collection_method_change,     # C1
    _case_amendment_paid,               # C2
    _case_address_territory_change,     # C3
    _case_online_unpaid_lifecycle,      # C4
    _case_settlement_preview_token,     # C6
    _case_cancel_guards,                # C7
]


def run(cleanup: bool = True) -> Dict[str, Any]:
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}

    _guard_environment()
    frappe.flags.in_test = True
    frappe.flags.ignore_woo_outbound = True

    ctx = RunContext()
    try:
        pos_profile = _pick_pos_profile()
        items = _pick_sellable_items(pos_profile, 2)
        company = frappe.db.get_value("POS Profile", pos_profile, "company")
        env = {
            "pos_profile": pos_profile,
            "items": items,
            "company": company,
            "global_default_company": frappe.defaults.get_global_default("company"),
            "territory": _ensure_territory(company),
            "customer_group": _ensure_customer_group(),
            "courier": _normalize_courier(_pick_courier_party(pos_profile)),
        }
        _ensure_item_prices(ctx, items)
        _ensure_stock(ctx, items, pos_profile)
        frappe.db.commit()

        for case in _CASES:
            try:
                case(ctx, env)
            except Exception as exc:
                ctx.record(f"{case.__name__}.crashed", False, f"{exc}")
                frappe.log_error(frappe.get_traceback(),
                                 f"money path case failed: {case.__name__}")
            frappe.db.commit()

        skips = _bucket(ctx, "mp_skips")
        observations = _bucket(ctx, "mp_observations")
        summary = {
            "passed": ctx.passed,
            "failed": ctx.failed,
            "skipped": len(skips),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in ctx.checks
            ],
            "skips": skips,
            "observations": observations,
        }
        print("")
        for check in ctx.checks:
            print(f"   [{'PASS' if check.passed else 'FAIL'}] {check.name} :: {check.detail}")
        for row in skips:
            print(f"   [SKIP] {row['name']} :: {row['reason']}")
        for row in observations:
            print(f"   [NOTE] {row['name']} :: {row['detail']}")
        print(
            f"money_paths_validation: {ctx.passed} passed, {ctx.failed} failed, "
            f"{len(skips)} skipped, {len(observations)} observation(s)"
        )
        print(MARKER_START)
        print(json.dumps(summary, indent=2, default=str))
        print(MARKER_END)
        return summary
    finally:
        if cleanup:
            try:
                _teardown_dispatched(ctx)
                _teardown_extras(ctx)
                _cleanup(ctx)
                _teardown_taxonomy(ctx)
            except Exception:
                frappe.log_error(frappe.get_traceback(),
                                 "money_paths_validation cleanup failed")
