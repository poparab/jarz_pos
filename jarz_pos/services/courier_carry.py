"""Carry-over of unsettled courier money across shift boundaries.

THE CASE THIS EXISTS FOR
------------------------
A courier takes the last order of the day, delivers it, and goes straight home.
The cash is real, it is collected, and it is nowhere near the branch when the
till is counted.  The books already model this correctly — dispatch moves the
amount ``Debtors -> Courier Outstanding``, so the drawer count is unaffected —
but the *close* used to refuse outright while any Courier Transaction on the
branch was still unsettled, which locked the whole branch until a manager
force-closed it.

So the close no longer refuses.  It asks.  The closer is shown every unsettled
transaction, invoice by invoice, and must tick each one to state on the record
"this money is still out there".  The transaction stays ``Unsettled`` and is
stamped with the shift that let it walk, who let it walk, and how many closes it
has now survived.  When it finally settles, it is stamped with the shift that
received the cash.  Those two stamps are what makes the carry auditable instead
of merely tolerated: the shift dashboard can show what each shift handed
forward, what it collected from previous shifts, and which courier has been
holding money for how long.

Nothing here posts to the ledger.  Carrying money is not an accounting event —
the amount is already sitting in Courier Outstanding with the courier's name on
it, which is exactly where it belongs until the cash is physically handed over.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


# ---------------------------------------------------------------------------
# Reading the outstanding set
# ---------------------------------------------------------------------------

def carry_columns_available() -> bool:
    """Have this app's carry columns actually reached the database yet?

    Code lands on a server BEFORE ``bench migrate`` runs, and the CI logic gate
    runs pre-migrate by design — so between the two, every column added here is
    still missing. Reading or writing one unguarded turns an ordinary settlement
    into a 500 for the length of a deploy, which is exactly the window an
    end-of-day settlement is most likely to land in.

    Not cached: a migrate flips this mid-process, and the check is a metadata
    lookup that Frappe already caches internally.
    """
    try:
        return bool(frappe.db.has_column("Courier Transaction", "carried_at"))
    except Exception:
        return False


def _courier_profile_expr() -> str:
    """SQL expression for the branch that owns an invoice.

    ``custom_kanban_profile`` wins over ``pos_profile``: the latter goes stale
    when an order is transferred between branches, and the branch that must
    account for the courier's cash is the one currently holding the order.
    """
    try:
        if frappe.db.has_column("Sales Invoice", "custom_kanban_profile"):
            return "COALESCE(NULLIF(si.custom_kanban_profile, ''), si.pos_profile)"
    except Exception:
        pass
    return "si.pos_profile"


def party_label(party_type: Optional[str], party: Optional[str]) -> str:
    if not party:
        return _("Unknown courier")
    try:
        if party_type == "Employee":
            return frappe.db.get_value("Employee", party, "employee_name") or party
        if party_type == "Supplier":
            return frappe.db.get_value("Supplier", party, "supplier_name") or party
    except Exception:
        pass
    return party


def get_unsettled_transactions(pos_profile: str) -> List[Dict[str, Any]]:
    """Every non-Settled Courier Transaction whose invoice belongs to *pos_profile*.

    One row per transaction, carrying enough invoice context for the closer to
    recognise the order on screen — an operator ticking "the money is still with
    the courier" needs the customer and the amount, not a transaction id.
    """
    profile = str(pos_profile or "").strip()
    if not profile:
        return []

    carry_select = (
        """
            ct.carried_from_shift,
            ct.carried_at,
            COALESCE(ct.carry_count, 0) AS carry_count,
        """
        if carry_columns_available()
        else ""
    )
    rows = frappe.db.sql(
        f"""
        SELECT
            ct.name AS courier_transaction,
            ct.reference_invoice,
            COALESCE(ct.amount, 0) AS amount,
            COALESCE(ct.shipping_amount, 0) AS shipping_amount,
            COALESCE(ct.party_type, '') AS party_type,
            COALESCE(ct.party, '') AS party,
            ct.date AS dispatched_at,
            {carry_select}
            COALESCE(ct.is_partner_order, 0) AS is_partner_order,
            si.customer_name,
            si.grand_total,
            si.posting_date
        FROM `tabCourier Transaction` ct
        INNER JOIN `tabSales Invoice` si ON si.name = ct.reference_invoice
        WHERE ct.status != %s
          AND ct.reference_invoice IS NOT NULL
          AND COALESCE({_courier_profile_expr()}, '') = %s
        ORDER BY ct.date ASC, ct.creation ASC
        """,
        ("Settled", profile),
        as_dict=True,
    )
    if not isinstance(rows, list):
        return []

    now = now_datetime()
    for row in rows:
        amount = flt(row.get("amount"))
        shipping = flt(row.get("shipping_amount"))
        row["net_balance"] = flt(amount - shipping)
        row["display_name"] = party_label(row.get("party_type"), row.get("party"))
        row["carried"] = bool(row.get("carried_from_shift"))
        dispatched = row.get("dispatched_at")
        try:
            row["days_outstanding"] = max(0, (now - dispatched).days) if dispatched else 0
        except Exception:
            row["days_outstanding"] = 0
    return rows


def build_close_block(pos_profile: str, detail_limit: int = 5) -> Dict[str, Any]:
    """What the closing screen needs to know about money still with couriers.

    ``blocked`` is kept for wire compatibility with clients that predate the
    acknowledgement flow — to them it still reads "you cannot close yet", which
    is the safe interpretation for a client that has no way to acknowledge.
    Current clients read ``requires_acknowledgement`` and the ``transactions``
    list instead.
    """
    payload: Dict[str, Any] = {
        "blocked": False,
        "requires_acknowledgement": False,
        "pos_profile": pos_profile,
        "transaction_count": 0,
        "invoice_count": 0,
        "party_count": 0,
        "net_balance": 0.0,
        "carried_count": 0,
        "parties": [],
        "transactions": [],
    }

    rows = get_unsettled_transactions(pos_profile)
    if not rows:
        return payload

    parties: Dict[tuple, Dict[str, Any]] = {}
    invoice_names: set = set()
    net_balance = 0.0
    carried_count = 0

    for row in rows:
        key = (row.get("party_type") or "", row.get("party") or "")
        invoice_name = str(row.get("reference_invoice") or "")
        if invoice_name:
            invoice_names.add(invoice_name)
        net_balance += flt(row.get("net_balance"))
        if row.get("carried"):
            carried_count += 1

        group = parties.setdefault(
            key,
            {
                "party_type": key[0],
                "party": key[1],
                "display_name": row.get("display_name"),
                "transaction_count": 0,
                "invoice_count": 0,
                "net_balance": 0.0,
                "invoices": [],
            },
        )
        group["transaction_count"] += 1
        group["net_balance"] = flt(group["net_balance"] + flt(row.get("net_balance")))
        if invoice_name and invoice_name not in group["invoices"]:
            if len(group["invoices"]) < detail_limit:
                group["invoices"].append(invoice_name)
            group["invoice_count"] += 1

    sorted_parties = sorted(
        parties.values(),
        key=lambda r: (
            -abs(flt(r.get("net_balance"))),
            -int(r.get("transaction_count") or 0),
            str(r.get("display_name") or ""),
        ),
    )

    payload.update(
        {
            "blocked": True,
            "requires_acknowledgement": True,
            "transaction_count": len(rows),
            "invoice_count": len(invoice_names),
            "party_count": len(sorted_parties),
            "net_balance": flt(net_balance),
            "carried_count": carried_count,
            "parties": sorted_parties[:detail_limit],
            "transactions": [
                {
                    "courier_transaction": row.get("courier_transaction"),
                    "reference_invoice": row.get("reference_invoice"),
                    "customer_name": row.get("customer_name"),
                    "party_type": row.get("party_type"),
                    "party": row.get("party"),
                    "display_name": row.get("display_name"),
                    "amount": flt(row.get("amount")),
                    "shipping_amount": flt(row.get("shipping_amount")),
                    "net_balance": flt(row.get("net_balance")),
                    "dispatched_at": str(row.get("dispatched_at") or ""),
                    "carried": bool(row.get("carried")),
                    "carry_count": int(row.get("carry_count") or 0),
                    "days_outstanding": int(row.get("days_outstanding") or 0),
                    "is_partner_order": bool(row.get("is_partner_order")),
                }
                for row in rows
            ],
        }
    )
    return payload


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------

def normalize_acknowledgement(value: Any) -> List[str]:
    """Coerce the client's acknowledgement payload into a list of CT names.

    Accepts a JSON string (Frappe passes list arguments that way over HTTP), a
    list of names, or a list of ``{"courier_transaction": ...}`` dicts, so the
    close endpoint does not care which shape a client happens to send.
    """
    if not value:
        return []

    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except Exception:
            return [value.strip()] if value.strip() else []

    if isinstance(value, dict):
        value = [value]

    names: List[str] = []
    for item in value or []:
        if isinstance(item, dict):
            name = str(item.get("courier_transaction") or item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def stamp_carried(
    transactions: Iterable[Dict[str, Any]],
    *,
    opening_entry: str,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """Record that *transactions* were knowingly left unsettled at a close.

    ``carried_from_shift`` keeps the FIRST shift that let the money walk — that
    is the shift whose closer originally took responsibility, and overwriting it
    each night would erase exactly the fact worth auditing.  ``carry_count``
    carries the recurrence instead, and is what turns "still out" into an age.
    """
    user = user or frappe.session.user
    stamped_at = now_datetime()
    carried: List[str] = []
    total = 0.0
    if not carry_columns_available():
        # Mid-deploy: the close still succeeds and the money still shows as
        # Unsettled, it just is not attributed to this shift. Losing the stamp
        # is survivable; refusing the close is not.
        return {"transactions": [], "count": 0, "net_balance": 0.0, "unstamped": True}

    for row in transactions or []:
        name = str(row.get("courier_transaction") or row.get("name") or "").strip()
        if not name:
            continue
        values: Dict[str, Any] = {
            "carried_at": stamped_at,
            "carried_by": user,
            "carry_count": int(row.get("carry_count") or 0) + 1,
        }
        if not row.get("carried_from_shift"):
            values["carried_from_shift"] = opening_entry
        try:
            frappe.db.set_value("Courier Transaction", name, values, update_modified=False)
        except Exception:
            # One un-stampable row must not abort a close that is otherwise
            # complete; the money is still tracked by its Unsettled status.
            frappe.log_error(
                frappe.get_traceback(), "jarz_pos.courier_carry.stamp_carried"
            )
            continue
        carried.append(name)
        total += flt(row.get("net_balance"))

    return {"transactions": carried, "count": len(carried), "net_balance": flt(total)}


def mark_settled(
    names: Iterable[str],
    *,
    pos_profile: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    update_modified: bool = True,
) -> List[str]:
    """Flip *names* to Settled and record which shift received the cash.

    The single place courier money is allowed to become Settled.  Every
    settlement path routes through here so ``settled_in_shift`` is never a
    best-effort guess — a carried transaction settled three days later is
    attributed to the shift that actually took the money, not to the one that
    dispatched it.

    ``pos_profile`` resolves the receiving shift.  When it is unknown (a
    back-office or partner settlement with no till involved) the status still
    flips and the shift stamp is simply left empty.
    """
    settled_in_shift = None
    if pos_profile:
        try:
            from jarz_pos.utils.access_control import get_open_shift_for_profile

            open_shift = get_open_shift_for_profile(pos_profile)
            if open_shift:
                settled_in_shift = open_shift.get("name")
        except Exception:
            settled_in_shift = None

    values: Dict[str, Any] = {"status": "Settled"}
    if carry_columns_available():
        values["settled_at"] = now_datetime()
        values["settled_by"] = frappe.session.user
        if settled_in_shift:
            values["settled_in_shift"] = settled_in_shift
    if extra:
        values.update(extra)

    done: List[str] = []
    for name in names or []:
        name = str(name or "").strip()
        if not name:
            continue
        frappe.db.set_value(
            "Courier Transaction", name, values, update_modified=update_modified
        )
        done.append(name)
    return done


def settlement_stamp(pos_profile: Optional[str] = None) -> Dict[str, Any]:
    """The settlement fields to set on a Courier Transaction *document*.

    For the paths that build or save a ``Courier Transaction`` doc rather than
    writing through :func:`mark_settled` — same stamps, applied in the caller's
    own transaction.
    """
    if not carry_columns_available():
        return {}

    stamp: Dict[str, Any] = {
        "settled_at": now_datetime(),
        "settled_by": frappe.session.user,
    }
    if pos_profile:
        try:
            from jarz_pos.utils.access_control import get_open_shift_for_profile

            open_shift = get_open_shift_for_profile(pos_profile)
            if open_shift:
                stamp["settled_in_shift"] = open_shift.get("name")
        except Exception:
            pass
    return stamp


# ---------------------------------------------------------------------------
# Reporting — what a shift handed forward, and what it collected
# ---------------------------------------------------------------------------

EMPTY_CARRY_STATS: Dict[str, Any] = {
    "carried_out_count": 0,
    "carried_out_amount": 0.0,
    "settled_in_count": 0,
    "settled_in_amount": 0.0,
}


def get_shift_carry_stats_bulk(openings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Carried-out and collected-in totals for many shifts, in two queries.

    Deliberately bulk: the shift monitor renders up to a thousand shifts at a
    time, and a per-row lookup would turn one screen into thousands of queries.

    * carried_out — transactions the shift closed on while still unsettled.
      Bucketed by ``carried_at`` falling inside the shift window rather than by
      ``carried_from_shift``, so a transaction carried three nights running
      appears in all three closes, not only the first.
    * settled_in — transactions the shift settled that a PREVIOUS shift had
      carried: the money that walked out one night and came back on another,
      which is the figure a manager is actually looking for.
    """
    stats: Dict[str, Dict[str, Any]] = {}
    windows = []
    for opening in openings or []:
        name = str(opening.get("name") or "").strip()
        if not name:
            continue
        stats[name] = dict(EMPTY_CARRY_STATS)
        start = opening.get("period_start_date")
        if start:
            windows.append((name, start, opening.get("period_end_date")))

    if not stats:
        return stats

    names = list(stats.keys())

    # Carried out — one sweep over the range, bucketed in Python because shift
    # windows can legitimately overlap across branches.
    if windows:
        try:
            span_start = min(w[1] for w in windows)
            rows = frappe.db.sql(
                """
                SELECT carried_at,
                       COALESCE(amount, 0) - COALESCE(shipping_amount, 0) AS net
                FROM `tabCourier Transaction`
                WHERE carried_at IS NOT NULL AND carried_at >= %(start)s
                """,
                {"start": span_start},
                as_dict=True,
            ) or []
            for name, start, end in windows:
                bucket = stats[name]
                for row in rows:
                    stamped = row.get("carried_at")
                    if not stamped or stamped < start:
                        continue
                    if end and stamped > end:
                        continue
                    bucket["carried_out_count"] += 1
                    bucket["carried_out_amount"] = flt(
                        bucket["carried_out_amount"] + flt(row.get("net"))
                    )
        except Exception:
            pass

    try:
        placeholders = ", ".join(["%s"] * len(names))
        settled = frappe.db.sql(
            f"""
            SELECT settled_in_shift AS opening,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(COALESCE(amount, 0) - COALESCE(shipping_amount, 0)), 0) AS total
            FROM `tabCourier Transaction`
            WHERE settled_in_shift IN ({placeholders})
              AND carried_from_shift IS NOT NULL
              AND carried_from_shift != settled_in_shift
            GROUP BY settled_in_shift
            """,
            tuple(names),
            as_dict=True,
        ) or []
        for row in settled:
            bucket = stats.get(str(row.get("opening") or ""))
            if bucket is None:
                continue
            bucket["settled_in_count"] = int(row.get("cnt") or 0)
            bucket["settled_in_amount"] = flt(row.get("total"))
    except Exception:
        pass

    return stats


def get_carried_balances(profiles: List[str]) -> Dict[str, Any]:
    """Money currently out with couriers across *profiles*, oldest first.

    The aging view the close acknowledgement is worth nothing without: a branch
    that ticks the same transaction every night for a week should be visible as
    exactly that, not as a fresh acknowledgement each time.
    """
    payload: Dict[str, Any] = {
        "couriers": [],
        "total_net_balance": 0.0,
        "transaction_count": 0,
        "carried_count": 0,
        "oldest_days_outstanding": 0,
    }
    if not profiles:
        return payload

    couriers: Dict[tuple, Dict[str, Any]] = {}
    for profile in profiles:
        for row in get_unsettled_transactions(profile):
            key = (row.get("party_type") or "", row.get("party") or "", profile)
            group = couriers.setdefault(
                key,
                {
                    "party_type": key[0],
                    "party": key[1],
                    "display_name": row.get("display_name"),
                    "pos_profile": profile,
                    "transaction_count": 0,
                    "carried_count": 0,
                    "net_balance": 0.0,
                    "max_days_outstanding": 0,
                    "max_carry_count": 0,
                    "transactions": [],
                },
            )
            group["transaction_count"] += 1
            group["net_balance"] = flt(group["net_balance"] + flt(row.get("net_balance")))
            if row.get("carried"):
                group["carried_count"] += 1
                payload["carried_count"] += 1
            days = int(row.get("days_outstanding") or 0)
            group["max_days_outstanding"] = max(group["max_days_outstanding"], days)
            group["max_carry_count"] = max(
                group["max_carry_count"], int(row.get("carry_count") or 0)
            )
            group["transactions"].append(
                {
                    "courier_transaction": row.get("courier_transaction"),
                    "reference_invoice": row.get("reference_invoice"),
                    "customer_name": row.get("customer_name"),
                    "net_balance": flt(row.get("net_balance")),
                    "dispatched_at": str(row.get("dispatched_at") or ""),
                    "carried_from_shift": row.get("carried_from_shift"),
                    "carry_count": int(row.get("carry_count") or 0),
                    "days_outstanding": days,
                }
            )
            payload["transaction_count"] += 1
            payload["total_net_balance"] = flt(
                payload["total_net_balance"] + flt(row.get("net_balance"))
            )
            payload["oldest_days_outstanding"] = max(
                payload["oldest_days_outstanding"], days
            )

    payload["couriers"] = sorted(
        couriers.values(),
        key=lambda r: (
            -int(r.get("max_days_outstanding") or 0),
            -abs(flt(r.get("net_balance"))),
            str(r.get("display_name") or ""),
        ),
    )
    payload["total_net_balance"] = flt(payload["total_net_balance"])
    return payload
