"""Jarz POS - Kanban board API endpoints.
This module provides API endpoints for the Sales Invoice Kanban board functionality. 
Primary state field: 'custom_sales_invoice_state' (legacy fallback: 'sales_invoice_state').
"""
from __future__ import annotations
import frappe
import json
import traceback
import datetime
from frappe.query_builder.functions import Count
from typing import Dict, List, Any, Optional, Union, Tuple
from jarz_pos.constants import ACCOUNTS, PAYMENT_MODES, STATUS, WS_EVENTS, QUERY_LIMITS, ROLES
from jarz_pos.utils.access_control import (
    ClosedShiftError,
    ShiftRequiredError,
    assert_vouchers_not_in_closed_shift,
    ensure_open_shift_for_invoice,
    ensure_profile_scoped_invoice_access,
    get_invoice_branch,
    get_user_pos_profiles,
    get_users_for_pos_profiles,
)
from jarz_pos.utils.realtime import publish_invoice_event
from jarz_pos.services import ofd_pin_gate
from jarz_pos.services.delivery_handling import (
    DN_LOGIC_VERSION,
    build_ofd_shortage_field_values,
    ensure_delivery_note_for_invoice,
    get_ofd_shortage_preview,
)

# Accounting helpers
try:
    from jarz_pos.utils.account_utils import (
        get_company_receivable_account,
        get_pos_cash_account,
        ensure_partner_receivable_subaccount,
    )
except Exception:
    # Fallback dummies (should not normally trigger)
    def get_company_receivable_account(company: str) -> str:  # type: ignore
        return frappe.get_cached_value("Company", company, "default_receivable_account")
    def get_pos_cash_account(pos_profile: str, company: str) -> str:  # type: ignore
        return frappe.get_cached_value("Company", company, "default_cash_account") or "Cash"  # type: ignore
    def ensure_partner_receivable_subaccount(company: str, partner: str) -> str:  # type: ignore
        return get_company_receivable_account(company)

# Import utility functions with fallback if they don't exist
try:
    from jarz_pos.utils.invoice_utils import (
        get_address_details,
        format_invoice_data,
        apply_invoice_filters,
        sanitize_printable_text,
        read_invoice_shipping_income,
        normalize_woo_order_id,
    )
except ImportError:
    # Fallback implementations if utils don't exist
    def normalize_woo_order_id(value: Any) -> Optional[int]:
        try:
            return int(value or 0) or None
        except (TypeError, ValueError):
            return None

    def read_invoice_shipping_income(invoice: Any) -> float:
        try:
            for row in invoice.get("taxes") or []:
                if str(row.get("description") or "").strip().lower().startswith("shipping income"):
                    return float(row.get("tax_amount") or 0)
        except Exception:
            pass
        return 0.0

    def get_address_details(address_name: str) -> str:
        if not address_name:
            return ""
        try:
            address_doc = frappe.get_doc("Address", address_name)
            return f"{address_doc.address_line1 or ''}, {address_doc.city or ''}".strip(", ")
        except Exception:
            return ""
    
    def format_invoice_data(invoice: frappe.Document) -> Dict[str, Any]:
        address_name = invoice.get("shipping_address_name") or invoice.get("customer_address")
        items = [{"item_code": item.item_code, "item_name": item.item_name, 
                 "qty": float(item.qty), "rate": float(item.rate), "amount": float(item.amount)}
                for item in invoice.items]
        state_val = invoice.get("custom_sales_invoice_state") or invoice.get("sales_invoice_state") or "Received"
        return {
            "name": invoice.name,
            "invoice_id_short": invoice.name.split('-')[-1] if '-' in invoice.name else invoice.name,
            "customer_name": invoice.customer_name or invoice.customer,
            "customer": invoice.customer,
            "territory": invoice.territory or "",
            "sales_partner": getattr(invoice, "sales_partner", None),
            "required_delivery_date": invoice.get("required_delivery_datetime"),
            "status": state_val,
            "posting_date": str(invoice.posting_date),
            "grand_total": float(invoice.grand_total or 0),
            "net_total": float(invoice.net_total or 0),
            "total_taxes_and_charges": float(invoice.total_taxes_and_charges or 0),
            "full_address": get_address_details(address_name),
            # New delivery slot fields (date + time range)
            "delivery_date": getattr(invoice, "custom_delivery_date", None),
            "delivery_time_from": getattr(invoice, "custom_delivery_time_from", None),
            "delivery_duration": getattr(invoice, "custom_delivery_duration", None),
            "items": items
        }

    def sanitize_printable_text(value: Any) -> str:
        if value is None:
            return ""
        return " ".join(str(value).split())
    
    def apply_invoice_filters(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
        # Mirrors utils.invoice_utils.apply_invoice_filters — is_return excludes
        # credit notes, which inherit is_pos from the invoice they reverse.
        filter_conditions = {"docstatus": 1, "is_pos": 1, "is_return": 0}
        if not filters:
            return filter_conditions
        
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except json.JSONDecodeError:
                return filter_conditions
        
        if filters.get('dateFrom'):
            filter_conditions["posting_date"] = [">=", filters['dateFrom']]
        if filters.get('dateTo'):
            if "posting_date" in filter_conditions:
                filter_conditions["posting_date"] = ["between", [filters['dateFrom'], filters['dateTo']]]
            else:
                filter_conditions["posting_date"] = ["<=", filters['dateTo']]
        if filters.get('customer'):
            filter_conditions["customer"] = filters['customer']
        if filters.get('amountFrom'):
            filter_conditions["grand_total"] = [">=", filters['amountFrom']]
        if filters.get('amountTo'):
            if "grand_total" in filter_conditions:
                filter_conditions["grand_total"] = ["between", [filters['amountFrom'], filters['amountTo']]]
            else:
                filter_conditions["grand_total"] = ["<=", filters['amountTo']]
        
        return filter_conditions

try:
    from jarz_pos.api.manager import (
        get_invoice_amendment_eligibility,
        get_invoice_cancellation_eligibility,
        get_invoice_hard_mutation_blocker,
    )
except Exception:
    def get_invoice_amendment_eligibility(inv: Any) -> Dict[str, Any]:  # type: ignore
        return {
            "can_amend": False,
            "amendment_block_code": "unavailable",
            "amendment_block_reason": "Invoice amendment eligibility is unavailable.",
        }

    def get_invoice_cancellation_eligibility(inv: Any) -> Dict[str, Any]:  # type: ignore
        return {
            "can_cancel": False,
            "cancellation_block_code": "unavailable",
            "cancellation_block_reason": "Invoice cancellation eligibility is unavailable.",
            "cancellation_suggests_return": False,
        }

    def get_invoice_hard_mutation_blocker(inv: Any) -> Optional[Dict[str, Any]]:  # type: ignore
        return None

# Optional notification helper import (fail-safe)
try:
    from jarz_pos.api.notifications import notify_invoice_cancellation  # type: ignore
except Exception:
    def notify_invoice_cancellation(*args, **kwargs):  # type: ignore
        return None

# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

# REPLACED: direct Custom Field doc fetch (requires permissions) with meta-based access
# which is available to all authenticated users and avoids 403 on restricted roles.

def _get_state_field_options() -> List[str]:
    """Return list of state options from Sales Invoice meta without reading Custom Field doc.
    Prefers 'custom_sales_invoice_state', falls back to legacy names.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        # Prefer new canonical field first
        field_names = ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]
        for field_name in field_names:
            field = meta.get_field(field_name)
            if field and getattr(field, 'options', None):
                options = [opt.strip() for opt in field.options.split('\n') if opt.strip()]
                if options:
                    frappe.logger().info(f"Found state field: {field_name} with options: {options}")
                    return options
        frappe.logger().warning("No state field found, using default states")
        return ["Received", "In Progress", "Ready", "Out for Delivery", "Delivered", "Cancelled"]
    except Exception as e:
        frappe.logger().error(f"Error getting state field options: {str(e)}")
        return ["Received", "In Progress", "Ready", "Out for Delivery", "Delivered", "Cancelled"]

def _coerce_bool(val: Any) -> bool:
    try:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return float(val) != 0.0
        s = str(val).strip().lower()
        return s in {"1", "true", "yes", "y", "on"}
    except Exception:
        return False

def _is_pickup_invoice(inv: Union[Dict[str, Any], frappe.Document]) -> bool:
    """Detect pickup flags across the standardized and legacy Sales Invoice fields."""
    try:
        getter = inv.get if isinstance(inv, dict) else getattr
        for fieldname in ("custom_is_pickup", "is_pickup", "pickup", "custom_pickup"):
            try:
                val = getter(inv, fieldname) if getter is getattr else getter(fieldname)
                if _coerce_bool(val):
                    return True
            except Exception:
                pass
        
        # Fallback: Check remarks marker for legacy orders
        try:
            remarks = getter(inv, "remarks") if getter is getattr else getter("remarks")
            if isinstance(remarks, str) and "[pickup]" in remarks.lower():
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False

def _get_current_user_pos_profiles() -> List[str]:
    """Return names of POS Profiles linked to the current session user (and not disabled).

    Thin alias over the shared resolver so the board and the manager feed can no
    longer disagree about which branches a user owns.
    """
    return get_user_pos_profiles()

def _parse_filter_payload(filters: Optional[Union[str, Dict[str, Any]]]) -> Dict[str, Any]:
    if isinstance(filters, dict):
        return dict(filters)
    if isinstance(filters, str):
        try:
            parsed = json.loads(filters)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}

#: Filter keys that narrow the board to a deliberate subset. When any of these
#: is set the caller is hunting for specific orders, so the convenience defaults
#: that keep the *unfiltered* board fast (a recent-posting-date window and the
#: Delivered-column trim) must step aside — otherwise a search for a three-month
#: old order silently returns nothing and reads as "the filter is broken".
_NARROWING_FILTER_KEYS = (
    "searchTerm",
    "customer",
    "status",
    "dateFrom",
    "dateTo",
    "amountFrom",
    "amountTo",
)


def _has_narrowing_filters(raw_filters: Dict[str, Any]) -> bool:
    for key in _NARROWING_FILTER_KEYS:
        value = raw_filters.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _like_pattern(search: str) -> str:
    """Build a contains-pattern with SQL wildcards in the input neutralised.

    Without this a customer typing "%" matches every order, and "_" quietly
    matches any single character — the results look arbitrary rather than wrong.
    """
    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


#: How many Customer/Contact/Address rows a single search term may resolve to.
#:
#: These caps used to be 50, unordered. A search for a common name ("Ahmed",
#: "01001") matches far more than 50 customers, so the board was filtered by an
#: arbitrary 50 of them and orders that plainly matched what was typed simply
#: were not there — the single loudest way this search reads as broken. The caps
#: are now wide enough that ordinary searches are never truncated, and every
#: query is ordered `modified desc` so that if one ever is, what survives is the
#: recently-active customers who actually have orders on the board rather than a
#: set chosen by whatever order the database happened to return.
_SEARCH_MATCH_LIMIT = 500
_SEARCH_LINK_LIMIT = 1000


def _find_customer_search_matches(search_term: str) -> List[str]:
    search = sanitize_printable_text(search_term)
    if not search:
        return []

    like = _like_pattern(search)
    customer_ids = set()

    try:
        customer_or_filters: List[Dict[str, Any]] = [
            {"name": ["like", like]},
            {"customer_name": ["like", like]},
        ]
        try:
            if frappe.db.has_column("Customer", "mobile_no"):
                customer_or_filters.append({"mobile_no": ["like", like]})
        except Exception:
            pass

        customer_ids.update(
            frappe.get_all(
                "Customer",
                or_filters=customer_or_filters,
                pluck="name",
                order_by="modified desc",
                limit=_SEARCH_MATCH_LIMIT,
            ) or []
        )
    except Exception:
        pass

    if not any(ch.isdigit() for ch in search):
        return sorted(str(customer_id) for customer_id in customer_ids if str(customer_id).strip())

    try:
        contacts = frappe.get_all(
            "Contact",
            or_filters=[
                {"mobile_no": ["like", like]},
                {"phone": ["like", like]},
            ],
            pluck="name",
            order_by="modified desc",
            limit=_SEARCH_MATCH_LIMIT,
        ) or []
        if contacts:
            customer_ids.update(
                frappe.get_all(
                    "Dynamic Link",
                    filters={
                        "parenttype": "Contact",
                        "parent": ["in", contacts],
                        "link_doctype": "Customer",
                    },
                    pluck="link_name",
                    limit=_SEARCH_LINK_LIMIT,
                ) or []
            )
    except Exception:
        pass

    try:
        address_fields = []
        for fieldname in ("phone", "phone_number", "phone_no", "mobile_no"):
            try:
                if frappe.db.has_column("Address", fieldname):
                    address_fields.append(fieldname)
            except Exception:
                continue
        if address_fields:
            addresses = frappe.get_all(
                "Address",
                or_filters=[{fieldname: ["like", like]} for fieldname in address_fields],
                pluck="name",
                order_by="modified desc",
                limit=_SEARCH_MATCH_LIMIT,
            ) or []
            if addresses:
                customer_ids.update(
                    frappe.get_all(
                        "Dynamic Link",
                        filters={
                            "parenttype": "Address",
                            "parent": ["in", addresses],
                            "link_doctype": "Customer",
                        },
                        pluck="link_name",
                        limit=_SEARCH_LINK_LIMIT,
                    ) or []
                )
    except Exception:
        pass

    return sorted(str(customer_id) for customer_id in customer_ids if str(customer_id).strip())

def _build_invoice_search_or_filters(
    search_term: str,
    customer_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    search = sanitize_printable_text(search_term)
    if not search:
        return []

    like = _like_pattern(search)
    or_filters: List[Dict[str, Any]] = [
        {"name": ["like", like]},
        {"customer_name": ["like", like]},
        {"customer": ["like", like]},
    ]

    # The cards are labelled with the WooCommerce number, so that is what staff
    # type in. Exact match, not LIKE: woo_order_id is an Int column and a
    # substring match on it would surface unrelated orders.
    woo_search = normalize_woo_order_id(search.lstrip("#").strip())
    if woo_search is not None:
        or_filters.append({"woo_order_id": woo_search})

    if customer_ids:
        or_filters.append({"customer": ["in", customer_ids]})

    return or_filters

def _resolve_customer_phone(customer: str) -> str:
    """Resolve customer phone from Customer -> primary Contact -> primary Address -> any Contact."""
    if not customer:
        return ""
    try:
        cache = getattr(frappe.local, "cache", None)
        if isinstance(cache, dict):
            cache_key = f"cust_phone::{customer}"
            if cache_key in cache:
                return cache[cache_key]
        else:
            cache = None

        phone = ""

        # 1) Customer fields
        for field in ("mobile_no", "phone"):
            try:
                if frappe.db.has_column("Customer", field):
                    val = frappe.db.get_value("Customer", customer, field)
                    if val:
                        phone = str(val)
                        break
            except Exception:
                continue

        # 2) Primary Contact
        if not phone:
            try:
                primary_contact = frappe.db.get_value("Customer", customer, "customer_primary_contact")
                if primary_contact:
                    val = frappe.db.get_value("Contact", primary_contact, "mobile_no") or frappe.db.get_value("Contact", primary_contact, "phone")
                    if val:
                        phone = str(val)
            except Exception:
                pass

        # 3) Primary Address
        if not phone:
            try:
                primary_address = frappe.db.get_value("Customer", customer, "customer_primary_address")
                if primary_address:
                    for field in ("phone", "phone_number", "phone_no", "mobile_no"):
                        if frappe.db.has_column("Address", field):
                            val = frappe.db.get_value("Address", primary_address, field)
                            if val:
                                phone = str(val)
                                break
            except Exception:
                pass

        # 4) Any linked Contact (fallback)
        if not phone:
            try:
                contact_name = frappe.db.get_value(
                    "Dynamic Link",
                    {"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
                    "parent",
                )
                if contact_name:
                    val = frappe.db.get_value("Contact", contact_name, "mobile_no") or frappe.db.get_value("Contact", contact_name, "phone")
                    if val:
                        phone = str(val)
            except Exception:
                pass

        if isinstance(cache, dict):
            cache[cache_key] = phone
        return phone
    except Exception:
        return ""



#: Ledger classifications returned by :func:`_classify_collection_account`.
#: ``None`` means "this ledger does not, on its own, say how the customer paid" --
#: which is a real and common answer, not a failure.
_COLLECTION_LEDGER_UNKNOWN = None


def _classify_collection_account(
    account: str,
    meta: Optional[Dict[str, Any]],
    sales_partner: Optional[str] = None,
) -> Optional[str]:
    """Map a Payment Entry ``paid_to`` ledger onto a POS collection method.

    Returns ``None`` when the ledger does not identify a customer collection.

    This deliberately has no catch-all. It used to end in ``else: "Cash"``, i.e.
    *every* unrecognised ledger was reported as cash -- and the ledger an ONLINE
    Sales Partner order lands in is the partner AR subaccount ("Talabat - J"),
    which matches neither the Mobile Wallet nor the Bank Account name test. So an
    order paid online through a delivery partner displayed and printed "Cash", and
    so did every Payment Gateway collection, because guessing was the fallback.
    Ledgers we cannot read now fall through to the declared method on the invoice
    instead of being asserted as cash.
    """
    name = str(account or "").strip().lower()
    if not name:
        return _COLLECTION_LEDGER_UNKNOWN

    meta = meta or {}
    account_type = str(meta.get("account_type") or "").strip().lower()
    parent = str(meta.get("parent_account") or "").strip().lower()

    # Name tests first: these two ledgers are named after the collection method
    # that books into them (see delivery_handling._get_online_collection_account
    # and api/invoices.pay_invoice, which must keep agreeing with this mapping).
    if ACCOUNTS.MOBILE_WALLET.lower() in name:
        return ACCOUNTS.MOBILE_WALLET
    if "bank account" in name:
        return ACCOUNTS.INSTAPAY

    # The courier is holding the customer's cash until settlement. This is the one
    # receivable-typed ledger that genuinely means "the customer paid cash".
    if ACCOUNTS.COURIER_OUTSTANDING.lower() in name:
        return PAYMENT_MODES.CASH

    # A branch till. _get_cash_account only ever returns a Cash/Bank ledger under
    # "Cash In Hand", so both signals are checked rather than the account name --
    # a till is named after its POS profile ("Nasr City - J"), never "Cash".
    if account_type == "cash" or ACCOUNTS.CASH_IN_HAND.lower() in parent:
        return PAYMENT_MODES.CASH

    # A Sales Partner subaccount is an AR *reclass*, not a collection: the money
    # sits with the partner. It says nothing about how the customer paid. Checked
    # AFTER the till above, because a partner-attributed order really can be
    # collected into a branch drawer (three such Talabat orders exist on staging)
    # and the type-backed till signal must win over this name match.
    #
    # Matched by NAME against the invoice's own partner, not by account_type alone.
    # ensure_partner_receivable_subaccount creates these with account_type
    # "Receivable", but the ledgers that actually exist on staging/production do
    # not all come from it: "Talabat - J" is typed "Current Asset". A type test on
    # its own is therefore dead against the very data this fix was written for.
    partner = str(sales_partner or "").strip().lower()
    if partner and (name == partner or name.startswith(partner + " -")):
        return _COLLECTION_LEDGER_UNKNOWN
    if account_type == "receivable":
        return _COLLECTION_LEDGER_UNKNOWN

    # Any other Bank ledger (a payment gateway's, typically) is online money, but
    # not necessarily InstaPay. Let the declared method name it.
    return _COLLECTION_LEDGER_UNKNOWN


def _get_collection_change_map(invoice_names: List[str]) -> Dict[str, str]:
    """Per invoice, the collection method it was switched to after dispatch.

    ``change_payment_collection_method`` posts a Journal Entry and rewrites the
    Courier Transaction; it never touches the Payment Entry that settled the order
    against Courier Outstanding at Out-for-Delivery. The Courier Transaction note is
    therefore the ONLY trace of the switch, and it is newer than the Payment Entry,
    so it outranks it. It used to be consulted last, behind a ``not in method_map``
    guard that the Payment Entry had always already satisfied -- which made this
    signal dead for exactly the dispatched COD orders it exists to describe.

    Settled rows count too. The note is a permanent record of how the customer
    paid, and settling the courier does not revise it -- while the stale Payment
    Entry it outranks lives forever. Filtering to unsettled rows (as this did) meant
    a switched order read correctly until the courier settled and then silently
    reverted to "Cash". The note text is written by nothing but
    ``_append_collection_change_note``, so matching it is specific enough.
    """
    change_map: Dict[str, str] = {}
    if not invoice_names:
        return change_map
    try:
        ct_rows = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": ["in", invoice_names],
                "payment_mode": ["not in", [None, ""]],
                "notes": ["like", "%Payment collection changed on%"],
            },
            fields=["reference_invoice", "payment_mode", "modified"],
            order_by="modified desc",
            limit=QUERY_LIMITS.KANBAN_INVOICES,
        )
        for row in ct_rows:
            invoice_name = row.get("reference_invoice")
            # Rows arrive newest-first; the first one per invoice is the live method.
            if invoice_name and invoice_name not in change_map:
                change_map[invoice_name] = sanitize_printable_text(row.get("payment_mode"))
    except Exception:
        pass
    return change_map


def _get_payment_entry_method_map(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Per PAID invoice, the collection method its Payment Entry ledger implies.

    Only invoices with nothing outstanding are considered: before that, a Payment
    Entry either does not exist or does not stand for the whole order.
    """
    method_map: Dict[str, str] = {}
    paid_inv_names = [
        str(r.get("name")).strip()
        for r in rows
        if float(r.get("outstanding_amount") or 0) < 0.01
        and str(r.get("status") or "").strip().lower() in ("paid", "return", "credit note issued")
    ]
    if not paid_inv_names:
        return method_map

    try:
        pe_refs = frappe.get_all(
            "Payment Entry Reference",
            filters={
                "reference_doctype": "Sales Invoice",
                "reference_name": ["in", paid_inv_names],
            },
            fields=["reference_name", "parent"],
        )
        pe_names = list({r.parent for r in pe_refs})
        if not pe_names:
            return method_map

        pes = frappe.get_all(
            "Payment Entry",
            filters={"name": ["in", pe_names], "docstatus": 1, "payment_type": "Receive"},
            fields=["name", "paid_to"],
        )
        pe_account_map = {pe.name: pe.paid_to or "" for pe in pes}

        # One query for every ledger involved, rather than one lookup per invoice.
        account_names = sorted({acc for acc in pe_account_map.values() if acc})
        account_meta: Dict[str, Dict[str, Any]] = {}
        if account_names:
            for acc in frappe.get_all(
                "Account",
                filters={"name": ["in", account_names]},
                fields=["name", "account_type", "parent_account"],
            ):
                account_meta[acc.name] = acc

        partner_by_invoice = {
            str(r.get("name") or "").strip(): r.get("sales_partner") for r in rows
        }
        for ref in pe_refs:
            account = pe_account_map.get(ref.parent)
            if not account:
                continue
            method = _classify_collection_account(
                account,
                account_meta.get(account),
                partner_by_invoice.get(ref.reference_name),
            )
            if method and ref.reference_name not in method_map:
                method_map[ref.reference_name] = method
    except Exception:
        pass

    return method_map


def _get_actual_payment_method_map(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Resolve the payment method the customer ACTUALLY paid with, per invoice.

    ``rows`` are dicts carrying ``name``, ``outstanding_amount``, ``status`` and --
    for the fallback below to work -- ``custom_payment_method`` and ``sales_partner``.

    Sources, in priority order:
      1. An unsettled Courier Transaction whose notes record a collection-method
         change. This is the newest evidence and the only trace of a post-dispatch
         COD -> online switch, so it outranks the Payment Entry.
      2. The submitted Payment Entry ``paid_to`` ledger, classified by
         :func:`_classify_collection_account` -- which returns nothing rather than
         guessing when the ledger is not a customer collection.
      3. The method declared on the invoice (``custom_payment_method``). For a Sales
         Partner order settled into the partner AR subaccount this is the only
         honest answer, and ``change_payment_collection_method`` keeps it current.
      4. ``Online`` for a Sales Partner order carrying no declared method -- the
         partner collected, so it was certainly not our cash drawer.

    An invoice matching none of these is simply absent from the map: the board then
    shows its status badge and the receipt falls back to the requested method.
    Answering "Cash" for an unknown is what produced the original defect (an online
    Talabat order badged Cash on the Delivered column) and must not come back.

    Shared by the board (``get_kanban_invoices``) and the single-invoice details
    endpoint so a card and its printed receipt can never disagree about the method.
    """
    method_map: Dict[str, str] = {}
    invoice_names = [str(r.get("name") or "").strip() for r in rows if str(r.get("name") or "").strip()]
    if not invoice_names:
        return method_map

    # 1. Post-dispatch collection change (newest evidence wins).
    for name, method in _get_collection_change_map(invoice_names).items():
        if method:
            method_map[name] = method

    # 2. The ledger the money actually landed in.
    for name, method in _get_payment_entry_method_map(rows).items():
        if name not in method_map:
            method_map[name] = method

    # 3/4. The declared method, for the collections no ledger can name.
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name or name in method_map:
            continue
        declared = sanitize_printable_text(row.get("custom_payment_method"))
        if declared:
            method_map[name] = declared
        elif str(row.get("sales_partner") or "").strip():
            method_map[name] = PAYMENT_MODES.ONLINE

    return method_map



def _get_unsettled_customer_amount_map(invoice_names: List[str]) -> Dict[str, float]:
    """Customer money still to be collected on this order, per invoice.

    Two shapes hold that money in two different places, and reading only one of them
    gets both wrong:

    * **COD** — the money is the CUSTOMER leg of an unsettled Courier Transaction
      (``amount``), which is NOT the same thing as "the courier transaction is
      unsettled": a COD order switched to an online method keeps an unsettled row for
      the SHIPPING leg while its customer ``amount`` is zeroed, because the customer has
      already paid.
    * **unpaid online-intent** — the courier row is a FREIGHT row (``amount`` 0) whose
      settled flag is about HIS FEE, so it never reports the customer's money at all.
      There the receivable is still on Debtors and ``outstanding_amount`` is the answer.
      Missing this is what let the board hide "Change collection method" the moment a
      shift close settled the freight, on orders whose customer still owed every pound.

    ``outstanding_amount`` alone cannot answer the first one — a COD order is settled
    against Courier Outstanding at Out-for-Delivery, so it reads as fully paid while the
    customer has not handed over a single pound yet.
    """
    amount_map: Dict[str, float] = {}
    cleaned = [str(name or "").strip() for name in invoice_names if str(name or "").strip()]
    if not cleaned:
        return amount_map
    try:
        rows = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": ["in", cleaned],
                "status": ["!=", "Settled"],
            },
            fields=["reference_invoice", "amount"],
            limit=QUERY_LIMITS.KANBAN_INVOICES,
        )
        for row in rows:
            invoice_name = row.get("reference_invoice")
            if not invoice_name:
                continue
            amount_map[invoice_name] = amount_map.get(invoice_name, 0.0) + float(row.get("amount") or 0.0)
    except Exception:
        return {}

    # "Awaiting Payment" is stamped only by handle_unpaid_online_deliver_unconfirmed and
    # cleared the moment the transfer is confirmed or the order is converted to cash, so
    # it identifies exactly the shape above. ``max`` rather than ``+``: the two shapes
    # are mutually exclusive, and adding them would double an order that somehow carried
    # both.
    try:
        invoice_rows = frappe.get_all(
            "Sales Invoice",
            filters={
                "name": ["in", cleaned],
                "docstatus": 1,
                "custom_payment_confirmation_status": "Awaiting Payment",
            },
            fields=["name", "outstanding_amount"],
            limit=QUERY_LIMITS.KANBAN_INVOICES,
        )
        for row in invoice_rows:
            invoice_name = row.get("name")
            if not invoice_name:
                continue
            outstanding = float(row.get("outstanding_amount") or 0.0)
            if outstanding > amount_map.get(invoice_name, 0.0):
                amount_map[invoice_name] = outstanding
    except Exception:
        pass

    # Third shape: a COD order switched to an online method after dispatch and
    # not settled yet. Its row carries amount 0 and the invoice reads as paid,
    # but the switch's journal entry holds the customer's money in an online
    # ledger, and it can still move back to cash (or to another ledger) until the
    # courier settles. Read the amount off that entry's Courier Outstanding credit.
    try:
        switched_rows = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": ["in", cleaned],
                "status": ["!=", "Settled"],
                "amount": ["<=", 0.0001],
                "journal_entry": ["not in", [None, ""]],
                "payment_mode": ["not in", [None, "", "Deferred", "later", "cash_now", "Cash"]],
            },
            fields=["reference_invoice", "journal_entry"],
            limit=QUERY_LIMITS.KANBAN_INVOICES,
        )
        je_names = [r.get("journal_entry") for r in switched_rows if r.get("journal_entry")]
        if je_names:
            credits: Dict[str, float] = {}
            for acc in frappe.get_all(
                "Journal Entry Account",
                filters={
                    "parent": ["in", je_names],
                    "parenttype": "Journal Entry",
                    "account": ["like", f"{ACCOUNTS.COURIER_OUTSTANDING}%"],
                    "docstatus": 1,
                },
                fields=["parent", "credit_in_account_currency"],
            ):
                credits[acc.get("parent")] = credits.get(acc.get("parent"), 0.0) + float(acc.get("credit_in_account_currency") or 0.0)
            for row in switched_rows:
                invoice_name = row.get("reference_invoice")
                amount = credits.get(row.get("journal_entry"), 0.0)
                if invoice_name and amount > amount_map.get(invoice_name, 0.0):
                    amount_map[invoice_name] = amount
    except Exception:
        pass

    return amount_map


def _get_active_payment_receipt_map(invoice_names: List[str]) -> Dict[str, Dict[str, Any]]:
    cleaned_names = [str(name or "").strip() for name in invoice_names if str(name or "").strip()]
    if not cleaned_names:
        return {}

    receipt_map: Dict[str, Dict[str, Any]] = {}
    try:
        receipt_rows = frappe.get_all(
            "POS Payment Receipt",
            filters={
                "sales_invoice": ["in", cleaned_names],
                "status": ["!=", "Changed"],
            },
            fields=[
                "name",
                "sales_invoice",
                "payment_method",
                "status",
                "receipt_image_url",
                "receipt_image",
                "amount",
                "modified",
            ],
            order_by="modified desc",
            limit=QUERY_LIMITS.KANBAN_INVOICES * 3,
        )
        for row in receipt_rows:
            invoice_name = str(row.get("sales_invoice") or "").strip()
            if not invoice_name or invoice_name in receipt_map:
                continue
            image_url = sanitize_printable_text(
                row.get("receipt_image_url") or row.get("receipt_image") or ""
            )
            receipt_map[invoice_name] = {
                "payment_receipt_name": sanitize_printable_text(row.get("name")),
                "payment_receipt_method": sanitize_printable_text(row.get("payment_method")),
                "payment_receipt_status": sanitize_printable_text(row.get("status")),
                "payment_receipt_image_url": image_url,
                "payment_receipt_amount": float(row.get("amount") or 0.0),
            }
    except Exception as e:
        # Same policy as the note helpers: degrade the badge, log the cause.
        frappe.log_error(
            f"Payment Receipt Map Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}",
            "Kanban API",
        )
        return {}

    return receipt_map


#: Maximum length of the note preview surfaced on Kanban cards. Clients handle overflow.
INVOICE_NOTE_PREVIEW_MAX_LENGTH = 300


def _format_invoice_note_preview(note: Any) -> Optional[str]:
    """Sanitize + truncate a note body for card/preview payloads.

    Returns None when the note is empty so consumers can distinguish
    "no notes" from "an empty note".
    """
    text = sanitize_printable_text(note)
    if not text:
        return None
    return text[:INVOICE_NOTE_PREVIEW_MAX_LENGTH]


def _get_invoice_note_counts(invoice_names: List[str]) -> Dict[str, int]:
    """Batch-resolve the number of notes per invoice.

    Built with the query builder rather than a ``"count(name) as note_count"``
    string in ``fields``: ERPNext v16 rejects SQL-function strings in SELECT
    ("SQL functions are not allowed as strings in SELECT ... Use dict syntax"),
    and this helper's old bare ``except`` turned that hard rejection into an
    empty dict — so every Kanban card silently reported ``note_count == 0``
    from the v16 upgrade onward. See also ``price_lists._pricing_categories``,
    which sidesteps the same v16 restriction.

    A real SQL ``COUNT``/``GROUP BY`` (rather than counting fetched rows in
    Python) keeps the count exact regardless of how many notes an invoice has:
    the result is one row per invoice, so no row cap can truncate a count.
    """
    cleaned_names = [str(name or "").strip() for name in invoice_names if str(name or "").strip()]
    if not cleaned_names:
        return {}

    note_counts: Dict[str, int] = {}
    try:
        note = frappe.qb.DocType("Jarz Invoice Note")
        # One batched, grouped query for the whole page of invoices (no N+1).
        # Naturally bounded to <= len(cleaned_names) rows, so no limit needed.
        rows = (
            frappe.qb.from_(note)
            .select(note.sales_invoice, Count(note.name).as_("note_count"))
            .where(note.sales_invoice.isin(cleaned_names))
            .groupby(note.sales_invoice)
        ).run(as_dict=True)
        for row in rows:
            invoice_name = str(row.get("sales_invoice") or "").strip()
            if not invoice_name:
                continue
            note_counts[invoice_name] = int(row.get("note_count") or 0)
    except Exception as e:
        # Degrade to "no badges" rather than taking down the whole board, but
        # never silently: swallowing this is exactly what hid the v16 SELECT
        # rejection in production for months.
        frappe.log_error(
            f"Invoice Note Counts Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}",
            "Kanban API",
        )
        return {}

    return note_counts


def _get_invoice_latest_notes(invoice_names: List[str]) -> Dict[str, str]:
    """Batch-resolve the most recent note text per invoice.

    Uses a single ordered query and reduces to the first row seen per invoice
    in Python, mirroring `_get_active_payment_receipt_map`. A grouped
    "latest per group" query cannot carry the note body along with
    max(added_on), so the reduce happens here rather than in SQL.
    """
    cleaned_names = [str(name or "").strip() for name in invoice_names if str(name or "").strip()]
    if not cleaned_names:
        return {}

    latest_notes: Dict[str, str] = {}
    try:
        rows = frappe.get_all(
            "Jarz Invoice Note",
            filters={"sales_invoice": ["in", cleaned_names]},
            fields=["sales_invoice", "note", "added_on", "creation"],
            order_by="added_on desc, creation desc",
            limit=min(max(len(cleaned_names) * 20, 200), QUERY_LIMITS.KANBAN_INVOICES * 3),
        )
        for row in rows:
            invoice_name = str(row.get("sales_invoice") or "").strip()
            if not invoice_name or invoice_name in latest_notes:
                continue
            preview = _format_invoice_note_preview(row.get("note"))
            if preview:
                latest_notes[invoice_name] = preview
    except Exception as e:
        # Same policy as `_get_invoice_note_counts`: a note preview must never
        # break the board, but it must never fail invisibly either.
        frappe.log_error(
            f"Invoice Latest Notes Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}",
            "Kanban API",
        )
        return {}

    return latest_notes


def _serialize_invoice_note_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": sanitize_printable_text(row.get("name")),
        "sales_invoice": sanitize_printable_text(row.get("sales_invoice")),
        "pos_profile": sanitize_printable_text(row.get("pos_profile")),
        "note": str(row.get("note") or "").replace("\r\n", "\n").replace("\r", "\n").strip(),
        "added_by": sanitize_printable_text(row.get("added_by")),
        "added_by_full_name": sanitize_printable_text(row.get("added_by_full_name") or row.get("added_by")),
        "added_on": str(row.get("added_on") or row.get("creation") or ""),
    }


def _ensure_invoice_detail_access(invoice: frappe.Document) -> None:
    """Restrict invoice details to the current user's assigned POS Profiles."""
    ensure_profile_scoped_invoice_access(invoice, action_label="viewing this order")


def _get_territory_shipping_values(territory_name: str) -> Dict[str, float]:
    """Resolve configured delivery income/expense defaults for a territory."""
    if not territory_name:
        return {"income": 0.0, "expense": 0.0}

    income = 0.0
    expense = 0.0
    try:
        terr = frappe.get_doc("Territory", territory_name)
        for field_name in ["shipping_income", "delivery_income", "courier_income", "shipping_income_amount"]:
            if field_name in terr.as_dict():
                try:
                    income = float(terr.get(field_name) or 0)
                    break
                except Exception:
                    pass
        for field_name in ["shipping_expense", "delivery_expense", "courier_expense", "shipping_expense_amount"]:
            if field_name in terr.as_dict():
                try:
                    expense = float(terr.get(field_name) or 0)
                    break
                except Exception:
                    pass
    except Exception:
        pass
    return {"income": income, "expense": expense}


def _read_shipping_income_from_taxes(invoice: frappe.Document) -> float:
    """Read the actual shipping-income amount from the SI taxes table.

    Thin alias over the shared helper so the board, the order details and the
    amendment flow all answer "what did this invoice actually charge?" the same
    way.  See :func:`jarz_pos.utils.invoice_utils.read_invoice_shipping_income`.
    """
    return read_invoice_shipping_income(invoice)


def _get_invoice_shipping_values(invoice: frappe.Document) -> Dict[str, Any]:
    """Return effective shipping values after override and pickup rules.

    Reads the *actual* shipping income that the source invoice carried rather
    than always re-deriving from the territory default.  This is critical for
    amendment: an invoice that was free-shipping must not be re-charged on the
    replacement.

    Returns a dict with keys: income (float), expense (float),
    was_free_shipping (bool).
    """
    # Pickup and no-courier purposes (Employee / Sample-No-Courier) carry zero income
    # AND zero courier expense. B2B Supply keeps normal expense (custom_no_courier == 0).
    if _is_pickup_invoice(invoice) or bool(invoice.get("custom_no_courier")):
        return {"income": 0.0, "expense": 0.0, "was_free_shipping": False}

    shipping = _get_territory_shipping_values(invoice.get("territory") or "")
    override_status = str(invoice.get("custom_shipping_override_status") or "").strip()
    override_amount = float(invoice.get("custom_shipping_override") or 0)
    persisted_expense = float(invoice.get("custom_shipping_expense") or 0)

    if override_status == "Approved" and override_amount > 0:
        shipping["expense"] = override_amount
    elif persisted_expense > 0:
        shipping["expense"] = persisted_expense

    # Income side: what the SI actually carried, never the territory default —
    # the same value the Kanban card shows.
    shipping["income"] = _read_shipping_income_from_taxes(invoice)

    # was_free_shipping: income is zero AND at least one linked Jarz Bundle
    # has free_shipping=1.  This flag is propagated to the Flutter client so
    # the amendment flow does not re-inject the territory shipping charge.
    was_free_shipping = False
    if float(shipping.get("income") or 0) == 0.0:
        try:
            bundle_codes: List[str] = []
            for row in invoice.get("items") or []:
                bc = str(row.get("bundle_code") or "").strip()
                pb = str(row.get("parent_bundle") or "").strip()
                if bc and bc not in bundle_codes:
                    bundle_codes.append(bc)
                if pb and pb not in bundle_codes:
                    bundle_codes.append(pb)
            if bundle_codes:
                cols = set(frappe.db.get_table_columns("Jarz Bundle") or [])
                if "free_shipping" in cols:
                    any_free = frappe.get_all(
                        "Jarz Bundle",
                        filters={"name": ["in", bundle_codes], "free_shipping": 1},
                        pluck="name",
                    )
                    was_free_shipping = bool(any_free)
        except Exception:
            was_free_shipping = False

    shipping["was_free_shipping"] = was_free_shipping
    return shipping

# Backwards compatibility wrappers (kept in case referenced elsewhere in file)

def _get_state_custom_field():  # noqa: intentionally returns None now
    return None

def _get_allowed_states() -> List[str]:  # override previous implementation
    return _get_state_field_options()

def _state_key(label: str) -> str:
    return (label or "").strip().lower().replace(' ', '_')


# The board's forward sequence. Moves are one stage at a time; the only
# backward move is the kitchen correction Ready -> In Progress. Cancelled and
# Returned are reached through their own actions, never by moving the card.
# Until 2026-09-05 only the client enforced this, so Ready -> Delivered was
# accepted server-side and produced orders with no Delivery Note that could
# neither be cancelled nor returned.
_STATE_SEQUENCE = ["recieved", "in_progress", "ready", "out_for_delivery", "delivered"]
_STATE_ALIASES = {"received": "recieved", "processing": "in_progress"}
_STATE_LABELS = {
    "recieved": "Received",
    "in_progress": "In Progress",
    "ready": "Ready",
    "out_for_delivery": "Out for Delivery",
    "delivered": "Delivered",
}


def _sequence_index(label: Any) -> Optional[int]:
    key = _state_key(str(label or ""))
    key = _STATE_ALIASES.get(key, key)
    return _STATE_SEQUENCE.index(key) if key in _STATE_SEQUENCE else None


def _transition_block_reason(old_state: Any, new_state: Any) -> Optional[str]:
    """Why a board move is not allowed, or None when it is.

    Unknown labels on either side (legacy values, empty state) are left to the
    caller's other checks rather than refused here.
    """
    new_key = _state_key(str(new_state or ""))
    if new_key == "cancelled":
        return "Orders are cancelled through the Cancel Order action, not by moving the card."
    if new_key in {"returned", "return"}:
        return "Orders reach Returned through the Return Order action, not by moving the card."
    old_idx = 0 if old_state in (None, "") else _sequence_index(old_state)
    new_idx = _sequence_index(new_state)
    if old_idx is None or new_idx is None:
        return None
    if new_idx == old_idx + 1:
        return None
    if old_idx == 2 and new_idx == 1:
        return None
    old_label = _STATE_LABELS[_STATE_SEQUENCE[old_idx]]
    new_label = _STATE_LABELS[_STATE_SEQUENCE[new_idx]]
    if new_idx <= old_idx:
        return (
            f"Cannot move an order backward from '{old_label}' to '{new_label}'. "
            "Only Ready can go back to In Progress."
        )
    next_label = _STATE_LABELS[_STATE_SEQUENCE[old_idx + 1]]
    return (
        f"Move one stage at a time: from '{old_label}' the next stage is '{next_label}', "
        f"not '{new_label}'."
    )


def _safe_datetime(value: Any) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return frappe.utils.get_datetime(value)
    except Exception:
        return None


def _posting_datetime(card: Dict[str, Any]) -> Optional[datetime.datetime]:
    posting_date = card.get("posting_date")
    if not posting_date:
        return None
    posting_time = card.get("posting_time") or "00:00:00"
    return _safe_datetime(f"{posting_date} {posting_time}")


def _creation_datetime(card: Dict[str, Any]) -> Optional[datetime.datetime]:
    return _safe_datetime(card.get("creation"))


def _delivery_sort_key(card: Dict[str, Any]) -> datetime.datetime:
    delivery_date = card.get("delivery_date")
    if delivery_date:
        delivery_time = card.get("delivery_time_from") or "00:00:00"
        delivery_dt = _safe_datetime(f"{delivery_date} {delivery_time}")
        if delivery_dt:
            return delivery_dt
    posting_dt = _posting_datetime(card)
    if posting_dt:
        return posting_dt
    return frappe.utils.get_datetime("2099-12-31 23:59:59")


def _state_transition_sort_key(card: Dict[str, Any]) -> datetime.datetime:
    state_dt = _safe_datetime(card.get("_state_timestamp"))
    if state_dt:
        return state_dt
    posting_dt = _posting_datetime(card)
    if posting_dt:
        return posting_dt
    return frappe.utils.get_datetime("1970-01-01 00:00:00")


def _received_sort_key(card: Dict[str, Any]) -> Tuple[datetime.datetime, datetime.datetime, str]:
    posting_dt = _posting_datetime(card) or frappe.utils.get_datetime("1970-01-01 00:00:00")
    creation_dt = _creation_datetime(card) or posting_dt
    return (
        posting_dt,
        creation_dt,
        str(card.get("name") or ""),
    )


def _sort_kanban_columns(data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    for state_key, cards in data.items():
        if not cards:
            continue
        if state_key == "received":
            cards.sort(key=_received_sort_key, reverse=True)
        elif state_key == "in_progress":
            cards.sort(key=_delivery_sort_key)
        else:
            cards.sort(key=_state_transition_sort_key, reverse=True)
        for card in cards:
            card.pop("_state_timestamp", None)
    return data

# Unified success / error builders

def _success(**kwargs):
    payload = {"success": True}
    payload.update(kwargs)
    return payload

def _failure(msg: str):
    return {"success": False, "error": msg}


def _format_qty(value: Any) -> str:
    try:
        qty = float(value or 0)
    except Exception:
        qty = 0.0
    if abs(qty - round(qty)) < 0.0001:
        return str(int(round(qty)))
    return f"{qty:.3f}".rstrip("0").rstrip(".")


def _build_ofd_preview_errors(preview: Dict[str, Any]) -> List[str]:
    errors = list(preview.get("validation_errors") or [])

    for mismatch in preview.get("warehouse_mismatches") or []:
        errors.append(
            (
                "Invoice {invoice}: item {item} still points to warehouse {warehouse} instead of {expected}."
            ).format(
                invoice=mismatch.get("invoice_name") or "?",
                item=mismatch.get("item_code") or "?",
                warehouse=mismatch.get("warehouse") or "blank",
                expected=mismatch.get("expected_warehouse") or "?",
            )
        )

    for shortage in preview.get("blocking_shortages") or []:
        errors.append(
            (
                "Invoice(s) {invoices}: item {item} needs {required} in {warehouse}, only {available} available, and negative stock is disabled."
            ).format(
                invoices=", ".join(shortage.get("invoice_names") or []) or "?",
                item=shortage.get("item_code") or "?",
                required=_format_qty(shortage.get("required_qty")),
                warehouse=shortage.get("warehouse") or "?",
                available=_format_qty(shortage.get("available_qty")),
            )
        )

    # A6 — the Out-for-Delivery pin gate. THE SAME LINE EXISTS IN
    # api/trips.py::_build_ofd_preview_errors and both are required: this
    # function is duplicated, so a rule added here alone leaves the Delivery
    # Trip bulk-send path dispatching pinless orders. The rule itself lives in
    # services/ofd_pin_gate so the two call sites cannot drift apart, and it
    # returns [] unless Jarz POS Settings.require_delivery_pin_for_ofd is on.
    errors.extend(ofd_pin_gate.build_missing_pin_errors(preview))

    return errors

# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def preview_invoice_out_for_delivery(invoice_id: str) -> Dict[str, Any]:
    """Return OFD blockers and shortage preview for a single invoice without mutating data."""
    frappe.has_permission("Sales Invoice", ptype="read", throw=True)

    invoice_id = (invoice_id or "").strip()
    if not invoice_id:
        return _failure("invoice_id is required")

    invoice = frappe.get_doc("Sales Invoice", invoice_id)
    ensure_profile_scoped_invoice_access(invoice, action_label="previewing this order")
    if invoice.docstatus != 1:
        return _failure("Only submitted (docstatus=1) Sales Invoices can move Out for Delivery")

    blocking_errors: List[str] = []

    try:
        from jarz_pos.api.territories import territory_has_children
        inv_territory = (invoice.get("territory") or "").strip()
        inv_sub_territory = (
            getattr(invoice, "custom_sub_territory", None)
            or invoice.get("custom_sub_territory")
            or ""
        )
        if inv_territory and territory_has_children(inv_territory) and not inv_sub_territory:
            blocking_errors.append(
                "Please select a sub-territory before sending out for delivery"
            )
    except ImportError:
        pass

    try:
        override_status = (
            getattr(invoice, "custom_shipping_override_status", None)
            or invoice.get("custom_shipping_override_status")
            or ""
        )
        if str(override_status).strip() == "Pending":
            blocking_errors.append(
                "Custom shipping request is pending manager approval. Cannot proceed to Out for Delivery."
            )
    except Exception:
        pass

    preview = get_ofd_shortage_preview([invoice])
    blocking_errors.extend(_build_ofd_preview_errors(preview))

    return _success(
        invoice_id=invoice_id,
        preview=preview,
        blocking_errors=blocking_errors,
        requires_shortage_reason=bool(preview.get("requires_reason")) and not bool(preview.get("blocking")),
    )

@frappe.whitelist(allow_guest=False)
def get_kanban_columns() -> Dict[str, Any]:
    """Get all available Kanban columns based on Sales Invoice State field options.
    
    Returns:
        Dict with success status and columns data
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_columns called by {0}".format(frappe.session.user))
        options = _get_state_field_options()
        if not options:
            return _failure("Field 'sales_invoice_state' not found or has no options on Sales Invoice")
        columns = []
        # Colour per state. NOTE: every key below except "Returned" is stale —
        # the real options are "Recieved" (sic) / In Progress / Ready / Out for
        # Delivery / Delivered / Cancelled, none of which match, so those columns
        # all fall through to the #F5F5F5 default. Left as-is rather than fixed
        # here: the client themes its own columns and re-colouring five live
        # columns is a UI change, not part of the return work.
        color_map = {
            "Received": "#E3F2FD",
            "Processing": "#FFF3E0",
            "Preparing": "#F3E5F5",
            "Out for delivery": "#E8F5E8",
            "Completed": "#E0F2F1",
            # Pink, and the only key that actually matches a real option, so the
            # terminal money-reversed column is the one that stands out.
            "Returned": "#FCE4EC",
        }
        for i, option in enumerate(options):
            column_id = _state_key(option)
            columns.append({
                "id": column_id,
                "name": option,
                "color": color_map.get(option, "#F5F5F5"),
                "order": i
            })
        return _success(columns=columns)
    except Exception as e:
        error_msg = f"Error getting kanban columns: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Columns Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_kanban_invoices(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
    """Get Sales Invoices organized by their state for Kanban display.
    
    Args:
        filters: Filter conditions for invoice selection
        
    Returns:
        Dict with success status and invoices organized by state
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_invoices called with filters: {0}".format(filters))

        raw_filters = _parse_filter_payload(filters)
        filter_conditions = apply_invoice_filters(raw_filters)
        is_filtered = _has_narrowing_filters(raw_filters)
        search_term = sanitize_printable_text(raw_filters.get("searchTerm") or "")
        search_customer_ids = _find_customer_search_matches(search_term) if search_term else []
        search_or_filters = _build_invoice_search_or_filters(
            search_term,
            customer_ids=search_customer_ids,
        )

        # Include both submitted (1) and cancelled (2) invoices by default so the Cancelled column stays populated.
        if not str(raw_filters.get("status") or "").strip():
            filter_conditions["docstatus"] = ["in", [1, 2]]

        # Performance guardrails:
        # - Default to POS invoices and a recent date window when client doesn't specify
        # - Allow overriding via explicit filters
        #
        # The recent-date window is a convenience for the *unfiltered* board only.
        # Once staff have narrowed the board — searched an order number, picked a
        # customer, set an amount range — they are deliberately looking outside
        # today's work, and clamping them to 60 days made those searches come
        # back empty. The row limit below is what actually bounds the payload.
        try:
            if isinstance(filter_conditions, dict):
                if "posting_date" not in filter_conditions and not is_filtered:
                    filter_conditions["posting_date"] = [">=", frappe.utils.add_days(frappe.utils.today(), -60)]
                # Default to POS only unless caller provided is_pos explicitly (True/False)
                if "is_pos" not in filter_conditions:
                    filter_conditions["is_pos"] = 1
        except Exception:
            pass

        # Restrict to POS Profile(s) assigned to the current user
        allowed_profiles = _get_current_user_pos_profiles()

        # Initialize columns up-front for possible early return
        all_states = _get_state_field_options()
        kanban_data: Dict[str, List[Dict[str, Any]]] = {}
        for state in all_states:
            st = (state or '').strip()
            if st:
                kanban_data[_state_key(st)] = []

        if not allowed_profiles:
            # An empty board and "you were never given a branch" look identical to
            # the user, so say which one this is.
            frappe.logger().info("KANBAN API: No POS Profile linked to user; returning empty board")
            return _success(
                data=kanban_data,
                notice_code="no_branch_assigned",
                notice=frappe._(
                    "You are not assigned to any branch (POS Profile). Ask a "
                    "manager to add you to a branch to see orders."
                ),
            )

        # Optional client-provided branches list (subset of allowed profiles)
        client_selected_branches: List[str] = []
        maybe = raw_filters.get("branches")
        if isinstance(maybe, list):
            client_selected_branches = [str(x) for x in maybe if str(x).strip()]

        # Compute enforced branch list = intersection(allowed, client_selected) if client provided any; otherwise allowed only
        enforced_branches = allowed_profiles
        if client_selected_branches:
            enforced_branches = [p for p in allowed_profiles if p in set(client_selected_branches)]
            # If intersection is empty, return empty board (no access to requested branches)
            if not enforced_branches:
                return _success(data=kanban_data)

        # Enforce branch filter using new source of truth when available
        try:
            si_meta = frappe.get_meta("Sales Invoice")
            if si_meta.get_field("custom_kanban_profile"):
                filter_conditions["custom_kanban_profile"] = ["in", enforced_branches]
            else:
                # Fallback to legacy field
                filter_conditions["pos_profile"] = ["in", enforced_branches]
        except Exception:
            # Safe fallback
            filter_conditions["pos_profile"] = ["in", enforced_branches]

        # Fetch all matching Sales Invoices
        # Start with a stable base set of fields that always exist (or are known fixtures)
        fields = [
            "name", "customer", "customer_name", "territory", "posting_date",
            "posting_time", "creation", "grand_total", "net_total", "total_taxes_and_charges",
            "status", "custom_sales_invoice_state", "sales_invoice_state",
            "sales_partner", "pos_profile", "custom_kanban_profile",
            "modified", "outstanding_amount", "docstatus", "is_return",
            "custom_acceptance_status", "custom_accepted_by", "custom_accepted_on",
            "custom_payment_method",
            # Online payment assurance (InstaPay/Mobile Wallet unpaid-on-delivery badge)
            "custom_payment_confirmation_status", "custom_ofd_unconfirmed_since",
            # New delivery slot fields (these are in our fixtures; safe to select)
            "custom_delivery_date", "custom_delivery_time_from", "custom_delivery_duration",
            "shipping_address_name", "customer_address",
            # Shipping / sub-territory / trip fields
            "custom_shipping_expense", "custom_sub_territory", "custom_delivery_trip",
            "custom_shipping_override", "custom_shipping_override_status",
            # Always-safe system field
            "remarks",
            # WooCommerce order ID for customer-facing display
            "woo_order_id",
        ]

        # Append pickup-related fields ONLY if they exist in meta to avoid SQL errors.
        # custom_no_courier rides along: the card zeroes the shipping expense for
        # no-courier orders, and without the field that check silently never fires.
        try:
            si_meta = frappe.get_meta("Sales Invoice")
            pickup_candidates = [
                "custom_is_pickup", "is_pickup", "pickup", "custom_pickup", "custom_no_courier",
            ]
            for fn in pickup_candidates:
                if si_meta.get_field(fn):
                    fields.append(fn)
            # Return badge fields, behind the same meta guard: a site that has not
            # migrated the return fixtures yet would otherwise take an "unknown
            # column" SQL error and lose the entire board.
            for fn in ("custom_return_status", "custom_returned_amount"):
                if si_meta.get_field(fn):
                    fields.append(fn)
        except Exception:
            # If meta access fails, do not add optional fields
            pass

        # Cap results to avoid large payloads; client can paginate via additional filters
        invoices = frappe.get_all(
            "Sales Invoice",
            filters=filter_conditions,
            or_filters=search_or_filters or None,
            fields=fields,
            order_by="posting_date desc, posting_time desc",
            limit=QUERY_LIMITS.KANBAN_INVOICES,
        )

        # The rows are ordered newest-first, so anything past the cap is the
        # *oldest* still-open work — exactly what a dispatcher must not lose.
        # Report the shortfall instead of quietly serving a partial board.
        board_truncated = len(invoices) >= QUERY_LIMITS.KANBAN_INVOICES
        total_matching = len(invoices)
        # frappe.db.count() takes `filters` only — it has no way to express the
        # search's OR group, so running it while a search is active counts every
        # invoice the *other* filters allow and reports a total that ignores what
        # was typed. Better to under-report the cap than to tell staff a search
        # for one order matched forty thousand.
        if board_truncated and not search_or_filters:
            try:
                total_matching = frappe.db.count("Sales Invoice", filters=filter_conditions)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "Kanban truncation count failed",
                )
            frappe.logger().warning(
                f"KANBAN API: board truncated at {QUERY_LIMITS.KANBAN_INVOICES} of "
                f"{total_matching} matching invoices for {frappe.session.user}"
            )

        # Territory shipping cache
        territory_cache: Dict[str, Dict[str, float]] = {}
        _sub_territory_cache: Dict[str, bool] = {}
        _territory_name_cache: Dict[str, str] = {}

        def _get_territory_shipping(territory_name: str) -> Dict[str, float]:
            if not territory_name:
                return {"income": 0.0, "expense": 0.0}
            if territory_name in territory_cache:
                return territory_cache[territory_name]
            income = 0.0
            expense = 0.0
            try:
                terr = frappe.get_doc("Territory", territory_name)
                # Try multiple possible custom field names for robustness
                income_field_candidates = [
                    "shipping_income", "delivery_income", "courier_income", "shipping_income_amount"
                ]
                expense_field_candidates = [
                    "shipping_expense", "delivery_expense", "courier_expense", "shipping_expense_amount"
                ]
                for f in income_field_candidates:
                    if f in terr.as_dict():
                        try:
                            income = float(terr.get(f) or 0)
                            break
                        except Exception:
                            pass
                for f in expense_field_candidates:
                    if f in terr.as_dict():
                        try:
                            expense = float(terr.get(f) or 0)
                            break
                        except Exception:
                            pass
            except Exception:
                pass
            territory_cache[territory_name] = {"income": income, "expense": expense}
            return territory_cache[territory_name]

        # Get address information for invoices (batch compute via helper on names)
        invoice_addresses: Dict[str, str] = {}
        try:
            addr_name_by_inv = {}
            for inv in invoices:
                addr_name_by_inv[inv.name] = inv.get("shipping_address_name") or inv.get("customer_address")
            for inv_name, addr_name in addr_name_by_inv.items():
                invoice_addresses[inv_name] = get_address_details(addr_name)
        except Exception:
            # Fallback: empty addresses
            invoice_addresses = {inv.name: "" for inv in invoices}

        # Batch fetch items for all invoices (avoid N+1 queries)
        invoice_items: Dict[str, List[Dict[str, Any]]] = {inv.name: [] for inv in invoices}
        try:
            if invoices:
                items_rows = frappe.get_all(
                    "Sales Invoice Item",
                    filters={"parent": ["in", [inv.name for inv in invoices]]},
                    fields=["parent", "item_code", "item_name", "qty", "rate", "amount"],
                    limit=QUERY_LIMITS.KANBAN_INVOICES,
                )
                for row in items_rows:
                    parent = row.get("parent")
                    if parent in invoice_items:
                        invoice_items[parent].append({
                            "item_code": row.get("item_code"),
                            "item_name": sanitize_printable_text(row.get("item_name")),
                            "qty": row.get("qty"),
                            "rate": row.get("rate"),
                            "amount": row.get("amount"),
                        })
        except Exception:
            # Fallback to per-invoice if batch fails
            for inv in invoices:
                try:
                    items = frappe.get_all(
                        "Sales Invoice Item",
                        filters={"parent": inv.name},
                        fields=["item_code", "item_name", "qty", "rate", "amount"],
                        limit=100,
                    )
                    for item in items:
                        item["item_name"] = sanitize_printable_text(item.get("item_name"))
                    invoice_items[inv.name] = items
                except Exception:
                    invoice_items[inv.name] = []

        # Batch fetch actual payment method + still-to-collect customer cash.
        # Both are shared with get_invoice_details so the board card and the printed
        # receipt read from exactly the same resolution.
        payment_receipts_by_invoice = _get_active_payment_receipt_map([inv.name for inv in invoices])
        invoice_note_counts = _get_invoice_note_counts([inv.name for inv in invoices])
        invoice_latest_notes = _get_invoice_latest_notes([inv.name for inv in invoices])
        actual_payment_methods = _get_actual_payment_method_map([
            {
                "name": inv.name,
                "outstanding_amount": inv.get("outstanding_amount"),
                "status": inv.status,
                # Both are the fallback the resolver needs when the Payment Entry
                # ledger cannot name the method - a Sales Partner AR reclass.
                "custom_payment_method": inv.get("custom_payment_method"),
                "sales_partner": inv.get("sales_partner"),
            }
            for inv in invoices
        ])
        unsettled_customer_amounts = _get_unsettled_customer_amount_map(
            [inv.name for inv in invoices]
        )

        # Batch-fetch the shipping income each invoice actually carried. An invoice with
        # no such row charged nothing — see read_invoice_shipping_income for why there is
        # no territory fallback.
        actual_shipping_income_map: Dict[str, float] = {}
        try:
            if invoices:
                si_tax_rows = frappe.get_all(
                    "Sales Taxes and Charges",
                    filters={
                        "parent": ["in", [inv.name for inv in invoices]],
                        "parenttype": "Sales Invoice",
                        "description": ["like", "Shipping Income%"],
                    },
                    fields=["parent", "tax_amount"],
                    limit=0,
                )
                for row in si_tax_rows:
                    actual_shipping_income_map[row["parent"]] = float(row.get("tax_amount") or 0)
        except Exception:
            pass

        # Organize invoices by their current state
        for inv in invoices:
            state = inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "Received"  # Default state
            state_key = state.lower().replace(' ', '_')
            terr_ship = _get_territory_shipping(inv.get("territory") or "")
            # Prefer approved override > persisted SI value > territory
            override_status = inv.get("custom_shipping_override_status") or ""
            override_amount = float(inv.get("custom_shipping_override") or 0)
            si_shipping_expense = float(inv.get("custom_shipping_expense") or 0)
            if override_status == "Approved" and override_amount > 0:
                terr_ship["expense"] = override_amount
            elif si_shipping_expense > 0:
                terr_ship["expense"] = si_shipping_expense
            # Detect pickup and zero shipping amounts accordingly. A no-courier order
            # (Employee / Sample - No Courier) will never be handed to a courier, so
            # there is no expense to forecast — same rule as _get_invoice_shipping_values.
            # Unlike income there is no invoice-side record to read here: until dispatch
            # writes custom_shipping_expense, the territory rate IS the expected payout.
            is_pickup = _is_pickup_invoice(inv)
            if is_pickup or bool(inv.get("custom_no_courier")):
                terr_ship = {"income": 0.0, "expense": 0.0}

            # Income shown on the card = what the invoice actually charged, so an
            # amended delivery income (including a deliberate 0) shows up immediately.
            # Mirrors _get_invoice_shipping_values, which backs the order-details
            # dialog — the two must never disagree. Pickup / no-courier / free-delivery
            # orders need no special case: they carry no shipping row, hence 0.
            card_shipping_income = actual_shipping_income_map.get(inv.name, 0.0)

            # Resolve customer phone (Customer -> primary Contact -> primary Address -> any Contact)
            customer_phone = _resolve_customer_phone(inv.get("customer") or "")

            # Determine if there exists any UNSETTLED courier transaction for this invoice
            has_unsettled = False
            try:
                has_unsettled = frappe.db.exists(
                    "Courier Transaction",
                    {
                        "reference_invoice": inv.name,
                        "status": ["!=", "Settled"],
                    },
                )
            except Exception:
                has_unsettled = False

            # Normalize ERPNext doc status for board (treat Overdue as Unpaid)
            doc_status_label = str(inv.status or "").strip()
            if doc_status_label.lower() == "overdue":
                doc_status_label = STATUS.UNPAID

            acceptance_status_raw = (
                inv.get("custom_acceptance_status")
                or getattr(inv, "custom_acceptance_status", None)
                or inv.get("acceptance_status")
                or getattr(inv, "acceptance_status", None)
                or "Pending"
            )
            acceptance_status = str(acceptance_status_raw or "").strip() or "Pending"
            acceptance_status_lower = acceptance_status.lower()

            state_change_ts = getattr(inv, "modified", None)

            # Return badge. A partially-returned order keeps moving through the
            # live columns, so the card is the only place staff can see that part
            # of it has already come back. Fully-returned orders sit in the
            # "Returned" column and carry the same pair for the amount.
            try:
                card_return_status = sanitize_printable_text(
                    inv.get("custom_return_status")
                    or getattr(inv, "custom_return_status", None)
                    or ""
                )
            except Exception:
                card_return_status = ""
            try:
                card_returned_amount = float(inv.get("custom_returned_amount") or 0.0)
            except Exception:
                card_returned_amount = 0.0

            invoice_card = {
                "name": inv.name,
                "invoice_id_short": inv.name.split('-')[-1] if '-' in inv.name else inv.name,
                "customer_name": sanitize_printable_text(inv.customer_name or inv.customer),
                "customer": sanitize_printable_text(inv.customer),
                "territory": sanitize_printable_text(inv.territory or ""),
                "sales_partner": inv.get("sales_partner"),
                # Delivery slot: date + start time + duration
                "delivery_date": getattr(inv, "custom_delivery_date", None),
                "delivery_time_from": getattr(inv, "custom_delivery_time_from", None),
                "delivery_duration": getattr(inv, "custom_delivery_duration", None),
                "delivery_slot_label": sanitize_printable_text(getattr(inv, "custom_delivery_slot_label", None)),
                "status": sanitize_printable_text(state),  # Kanban state (custom field)
                "doc_status": sanitize_printable_text(doc_status_label),  # ERPNext doc status, with Overdue normalized to Unpaid
                "posting_date": str(inv.posting_date),
                "posting_time": str(getattr(inv, "posting_time", None) or inv.get("posting_time") or ""),
                "creation": str(getattr(inv, "creation", None) or inv.get("creation") or ""),
                "grand_total": float(inv.grand_total or 0),
                "net_total": float(inv.net_total or 0),
                "total_taxes_and_charges": float(inv.total_taxes_and_charges or 0),
                "full_address": sanitize_printable_text(invoice_addresses.get(inv.name, "")),
                "items": invoice_items.get(inv.name, []),
                "shipping_income": card_shipping_income,
                "shipping_expense": terr_ship.get("expense", 0.0),
                "has_unsettled_courier_txn": bool(has_unsettled),
                # Whether the CUSTOMER still owes money on this order. Distinct from
                # the flag above in both directions: a COD order switched to an online
                # method keeps an unsettled SHIPPING leg while the customer has already
                # paid, and an unpaid online-intent order keeps owing its full total
                # after a shift close settles the courier's freight row. This is the
                # flag the collection-method action gates on.
                "has_unsettled_customer_amount": bool(
                    float(unsettled_customer_amounts.get(inv.name, 0.0)) > 0.005
                ),
                "customer_phone": sanitize_printable_text(customer_phone),
                "is_pickup": bool(is_pickup),
                "acceptance_status": sanitize_printable_text(acceptance_status),
                "requires_acceptance": acceptance_status_lower != "accepted",
                "accepted_by": inv.get("custom_accepted_by"),
                "accepted_on": str(inv.get("custom_accepted_on")) if inv.get("custom_accepted_on") else None,
                "payment_method": sanitize_printable_text(inv.get("custom_payment_method")),
                "payment_confirmation_status": sanitize_printable_text(inv.get("custom_payment_confirmation_status")),
                "ofd_unconfirmed_since": str(inv.get("custom_ofd_unconfirmed_since")) if inv.get("custom_ofd_unconfirmed_since") else None,
                "actual_payment_method": sanitize_printable_text(actual_payment_methods.get(inv.name)),
                "payment_receipt_name": sanitize_printable_text(payment_receipts_by_invoice.get(inv.name, {}).get("payment_receipt_name")),
                "payment_receipt_method": sanitize_printable_text(payment_receipts_by_invoice.get(inv.name, {}).get("payment_receipt_method")),
                "payment_receipt_status": sanitize_printable_text(payment_receipts_by_invoice.get(inv.name, {}).get("payment_receipt_status")),
                "payment_receipt_image_url": sanitize_printable_text(payment_receipts_by_invoice.get(inv.name, {}).get("payment_receipt_image_url")),
                "pos_profile": sanitize_printable_text(inv.get("custom_kanban_profile")),
                "note_count": int(invoice_note_counts.get(inv.name, 0)),
                "latest_note": invoice_latest_notes.get(inv.name),
                "outstanding_amount": float(inv.get("outstanding_amount") or 0.0),
                "docstatus_value": int(getattr(inv, "docstatus", 0) or 0),
                "is_return": int(getattr(inv, "is_return", 0) or 0),
                "return_status": card_return_status,
                "returned_amount": card_returned_amount,
                "woo_order_id": inv.get("woo_order_id") or None,
                "_state_timestamp": str(state_change_ts) if state_change_ts else None,
            }

            # ── Sub-territory, trip & shipping override fields ──────────
            inv_territory = inv.territory or ""
            sub_terr = getattr(inv, "custom_sub_territory", None) or inv.get("custom_sub_territory") or ""
            invoice_card["sub_territory"] = sanitize_printable_text(sub_terr)
            # Translated display names for territory / sub-territory
            if inv_territory and inv_territory not in _territory_name_cache:
                _ter_doc_vals = frappe.db.get_value("Territory", inv_territory, ["territory_name", "custom_territory_name_ar"], as_dict=True)
                _territory_name_cache[inv_territory] = frappe._(
                    (_ter_doc_vals or {}).get("territory_name") or inv_territory
                )
                _territory_name_ar_cache = getattr(get_kanban_invoices, '_territory_name_ar_cache', {})
                _territory_name_ar_cache[inv_territory] = (_ter_doc_vals or {}).get("custom_territory_name_ar") or ""
                get_kanban_invoices._territory_name_ar_cache = _territory_name_ar_cache
            invoice_card["territory_display"] = sanitize_printable_text(_territory_name_cache.get(inv_territory, inv_territory))
            _territory_name_ar_cache = getattr(get_kanban_invoices, '_territory_name_ar_cache', {})
            invoice_card["territory_name_ar"] = sanitize_printable_text(_territory_name_ar_cache.get(inv_territory, ""))
            if sub_terr and sub_terr not in _territory_name_cache:
                _territory_name_cache[sub_terr] = frappe._(
                    frappe.db.get_value("Territory", sub_terr, "territory_name") or sub_terr
                )
            invoice_card["sub_territory_display"] = sanitize_printable_text(_territory_name_cache.get(sub_terr, sub_terr) if sub_terr else "")
            # Cache territory→has_children lookup
            if inv_territory and inv_territory not in _sub_territory_cache:
                _sub_territory_cache[inv_territory] = bool(
                    frappe.db.exists("Territory", {"parent_territory": inv_territory})
                )
            invoice_card["has_sub_territories"] = _sub_territory_cache.get(inv_territory, False)
            invoice_card["delivery_trip"] = sanitize_printable_text(getattr(inv, "custom_delivery_trip", None) or inv.get("custom_delivery_trip") or "")
            invoice_card["shipping_override"] = float(
                getattr(inv, "custom_shipping_override", 0) or inv.get("custom_shipping_override") or 0
            )
            invoice_card["shipping_override_status"] = (
                getattr(inv, "custom_shipping_override_status", None)
                or inv.get("custom_shipping_override_status")
                or ""
            )

            # Add to appropriate state column
            if state_key not in kanban_data:
                kanban_data[state_key] = []
            kanban_data[state_key].append(invoice_card)

        kanban_data = _sort_kanban_columns(kanban_data)

        # Trim "Delivered" column: keep only last 2 days + today, max 30 cards.
        # Skipped once the board is filtered — the trim is there to stop a
        # finished-work column from dominating the *live* board, but applying it
        # to a search meant a delivered order older than two days could never be
        # found, no matter what was typed.
        delivered_key = "delivered"
        if not is_filtered and delivered_key in kanban_data and kanban_data[delivered_key]:
            cutoff = str(frappe.utils.add_days(frappe.utils.today(), -2))
            kanban_data[delivered_key] = [
                c for c in kanban_data[delivered_key]
                if (c.get("posting_date") or "") >= cutoff
            ][:30]

        # Return unified success. `card_count` is what the board actually shows
        # after column trimming, so a filtered board can honestly say how many
        # orders matched instead of leaving staff to count cards by eye.
        return _success(
            data=kanban_data,
            truncated=board_truncated,
            total_matching=total_matching,
            limit=QUERY_LIMITS.KANBAN_INVOICES,
            filtered=is_filtered,
            card_count=sum(len(cards) for cards in kanban_data.values()),
        )
    except Exception as e:
        error_msg = f"Error getting kanban invoices: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Invoices Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def update_invoice_state(
    invoice_id: str,
    new_state: str,
    shortage_approved: bool | int = False,
    shortage_reason: Optional[str] = None,
    expected_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Update the custom_sales_invoice_state of a Sales Invoice (legacy field kept for backward compatibility).

    Args:
        invoice_id: ID of the Sales Invoice to update
        new_state: New state value to set
        expected_state: State the caller believes the invoice is in. When supplied
            and the invoice has since moved, the change is rejected instead of
            silently overwriting whoever got there first. Optional so older
            clients keep working.

    Returns:
        Dict with success status and message
    """
    try:
        frappe.has_permission("Sales Invoice", ptype="write", throw=True)
        frappe.logger().debug(f"KANBAN API: update_invoice_state - Invoice: {invoice_id}, New state: {new_state}")
        print("\n" + "-"*90)
        print("KANBAN STATE CHANGE API CALL")
        print(f"Invoice: {invoice_id}")
        print(f"Requested New State: {new_state}")
        print(f"Timestamp: {frappe.utils.now()}")
        allowed_states = _get_allowed_states()
        if not allowed_states:
            return _failure("No allowed states configured (Custom Field missing or empty)")
        if new_state not in allowed_states:
            match_ci = next((s for s in allowed_states if s.lower() == (new_state or '').lower()), None)
            if match_ci:
                new_state = match_ci
            else:
                return _failure(f"'{new_state}' is not a valid state")
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        # Branch scoping: the board is filtered per branch on read, so this is the
        # gate that stops a crafted call (or a socket-sourced invoice id from
        # another branch) from moving somebody else's order.
        ensure_profile_scoped_invoice_access(invoice, action_label="moving this order")
        if invoice.docstatus != 1:
            return _failure("Only submitted (docstatus=1) Sales Invoices can change state")
        old_state = (
            invoice.get("custom_sales_invoice_state")
            or invoice.get("sales_invoice_state")
            or invoice.get("custom_state")
            or invoice.get("state")
        )

        # Optimistic concurrency: reject rather than clobber when two staff drag
        # the same card at once.
        if expected_state:
            if _state_key(str(expected_state)) != _state_key(str(old_state or "")):
                return _failure(
                    f"This order already moved to '{old_state}' (you were looking at "
                    f"'{expected_state}'). Refresh the board and try again."
                )

        if old_state == new_state:
            print(f"State unchanged; old_state == new_state == {new_state}")
            return _success(message="State unchanged (already set)", invoice_id=invoice_id, state=new_state)

        # A fully-returned order is finished work: the stock is back in the
        # warehouse, the revenue is reversed and the credit note is posted. Moving
        # it anywhere — forward out of "Returned" to "Delivered", or back into the
        # live columns — would put dead work on the board and invite a second
        # dispatch, so the card is frozen where the return left it.
        #
        # Placed AFTER the no-op check on purpose: a client re-sending the state
        # the card is already in (realtime echo, retry, offline replay) should get
        # the benign "State unchanged", not an error.
        #
        # Compared through _state_key (lower-cased, spaces -> underscores) rather
        # than a bare string equality: new_state above is matched
        # case-insensitively, so a case-only variant must not be able to slip past
        # this guard either. Partially-returned orders still have goods with the
        # customer and keep moving normally.
        if _state_key(str(invoice.get("custom_return_status") or "")) == "fully_returned":
            return _failure("This order was fully returned and can no longer be moved.")

        transition_block = _transition_block_reason(old_state, new_state)
        if transition_block:
            return _failure(transition_block)

        meta = frappe.get_meta("Sales Invoice")
        fields_to_update: List[str] = []
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                fields_to_update.append(candidate)
        if not fields_to_update:
            return _failure("No sales invoice state fields found (expected custom_sales_invoice_state or sales_invoice_state)")

        normalized_target = (new_state or "").strip().lower()
        create_dn = normalized_target in {"out for delivery", "out_for_delivery"}
        # Dispatch moves stock (Delivery Note) and can raise a cash Payment Entry
        # against the branch account, so it belongs inside a shift window. Plain
        # Received -> In Progress -> Ready moves stay open.
        if create_dn:
            ensure_open_shift_for_invoice(invoice, action_label="dispatching an order")
            # A pickup order is handed over at the counter: the goods leave with the
            # customer, so the money has to be in the till first. The app enforces
            # this; the server did not, so an unpaid pickup could be dispatched and
            # end up Delivered with its whole total still on Debtors.
            if _is_pickup_invoice(invoice):
                try:
                    pickup_outstanding = float(invoice.get("outstanding_amount") or 0)
                except Exception:
                    pickup_outstanding = 0.0
                if pickup_outstanding > 0.5:
                    return _failure(
                        "Pickup orders must be paid before they are handed over. "
                        "Register the payment first."
                    )
        dn_logic_version = DN_LOGIC_VERSION
        shortage_approved = _coerce_bool(shortage_approved)
        shortage_reason = (shortage_reason or "").strip() or None
        frappe.logger().info(
            f"KANBAN API: State change requested -> {invoice_id} to '{new_state}' (normalized='{normalized_target}'), create_dn={create_dn}, logic_version={dn_logic_version}"
        )
        print(f"Normalized Target: {normalized_target} | create_dn: {create_dn} | logic_version: {dn_logic_version}")

        created_delivery_note: Optional[str] = None
        created_cash_payment_entry: Optional[str] = None
        created_partner_txn: Optional[str] = None
        audit_field_values: Dict[str, Any] = {}

        # ------------------------------------------------------------------
        # Helper: Ensure CASH Payment Entry for Sales Partner invoices when
        # moving to Out For Delivery (business rule 2025-09). Only trigger if:
        #   - invoice has sales_partner
        #   - invoice still has outstanding_amount > 0
        #   - payment not already fully paid (no existing PE closing it)
        #   - new state is Out For Delivery
        # The Payment Entry will credit the company Receivable and debit
        # the POS Profile cash account (branch cash) – representing branch
        # taking cash from rider on dispatch.
        # Idempotency: if a PE already exists allocating full outstanding,
        # function returns gracefully.
        # ------------------------------------------------------------------
        def _ensure_cash_payment_entry_for_partner(si_doc) -> Optional[str]:
            try:
                if not getattr(si_doc, "sales_partner", None):
                    return None
                outstanding = float(getattr(si_doc, "outstanding_amount", 0) or 0)
                if outstanding <= 0.0001:
                    return None
                existing = frappe.get_all(
                    "Payment Entry Reference",
                    filters={
                        "reference_doctype": "Sales Invoice",
                        "reference_name": si_doc.name,
                    },
                    fields=["parent", "allocated_amount", "total_amount", "outstanding_amount"],
                    limit=20,
                )
                for ref in existing:
                    try:
                        if float(ref.get("allocated_amount") or 0) >= outstanding - 0.0001:
                            return None
                    except Exception:
                        continue
                company = si_doc.company
                # Source of truth: custom_kanban_profile; fallback to pos_profile
                pos_profile = getattr(si_doc, "custom_kanban_profile", None) or getattr(si_doc, "pos_profile", None)
                if not pos_profile:
                    return None
                try:
                    cash_account = get_pos_cash_account(pos_profile, company)
                except Exception:
                    return None
                receivable = get_company_receivable_account(company)
                pe = frappe.new_doc("Payment Entry")
                pe.payment_type = "Receive"
                pe.company = company
                pe.posting_date = frappe.utils.getdate()
                pe.posting_time = frappe.utils.nowtime()
                pe.mode_of_payment = PAYMENT_MODES.CASH
                pe.party_type = "Customer"
                pe.party = si_doc.customer
                pe.paid_from = receivable
                pe.paid_to = cash_account
                pe.party_account = receivable
                pe.paid_amount = outstanding
                pe.received_amount = outstanding
                # Propagate branch to Payment Entry if custom field exists
                try:
                    pe_meta = frappe.get_meta("Payment Entry")
                    if pe_meta.get_field("custom_kanban_profile"):
                        pe.custom_kanban_profile = pos_profile
                except Exception:
                    pass
                pe.append("references", {
                    "reference_doctype": "Sales Invoice",
                    "reference_name": si_doc.name,
                    "due_date": getattr(si_doc, "due_date", None),
                    "total_amount": float(getattr(si_doc, "grand_total", 0) or 0),
                    "outstanding_amount": outstanding,
                    "allocated_amount": outstanding,
                })
                pe.flags.ignore_permissions = True
                try:
                    pe.set_missing_values()
                except Exception:
                    pass
                pe.insert(ignore_permissions=True)
                pe.submit()
                frappe.logger().info(
                    f"KANBAN API: Cash Payment Entry {pe.name} created for partner invoice {si_doc.name} on OFD transition"
                )
                return pe.name
            except Exception as ce:
                frappe.logger().warning(f"KANBAN API: Cash PE creation skipped for {si_doc.name}: {ce}")
                return None

        def _create_delivery_note_from_invoice(si_doc) -> str:
            frappe.logger().info(f"KANBAN API: Attempting Delivery Note creation for {si_doc.name}")
            # Avoid filtering by remarks at SQL level (table name has spaces). Instead,
            # fetch recent Delivery Notes for this customer and inspect remarks in Python.
            try:
                candidates = frappe.get_all(
                    "Delivery Note",
                    filters={
                        "docstatus": 1,
                        "customer": si_doc.customer,
                        # Narrow by date window to keep list small; last 7 days
                        "posting_date": [">=", frappe.utils.add_days(frappe.utils.today(), -7)],
                    },
                    fields=["name", "posting_date", "posting_time"],
                    order_by="posting_date desc, posting_time desc",
                    limit=50,
                )
            except Exception:
                candidates = []
            for row in candidates:
                try:
                    dn_name_try = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
                    if not dn_name_try:
                        continue
                    dn_doc_try = frappe.get_doc("Delivery Note", dn_name_try)
                    remarks_text = (getattr(dn_doc_try, "remarks", None) or "").strip()
                    if remarks_text and si_doc.name in remarks_text:
                        frappe.logger().info(
                            f"KANBAN API: Reusing existing Delivery Note {dn_name_try} for invoice {si_doc.name} (found by remarks scan)"
                        )
                        # Ensure completed state on reuse
                        try:
                            if int(getattr(dn_doc_try, "docstatus", 0) or 0) == 1:
                                try:
                                    dn_doc_try.db_set("per_billed", 100, update_modified=False)
                                except Exception:
                                    pass
                                try:
                                    dn_doc_try.db_set("status", "Completed", update_modified=False)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        return dn_name_try
                except Exception:
                    # ignore a single candidate failure and continue
                    continue
            dn = frappe.new_doc("Delivery Note")
            dn.customer = si_doc.customer
            dn.company = si_doc.company
            dn.posting_date = frappe.utils.getdate()
            dn.posting_time = frappe.utils.nowtime()
            dn.remarks = f"Auto-created from Sales Invoice {si_doc.name} on state change to Out for Delivery"
            default_wh = None
            for it in si_doc.items:
                if it.get("warehouse"):
                    default_wh = it.get("warehouse")
                    break
            if default_wh:
                dn.set_warehouse = default_wh
            for it in si_doc.items:
                dn.append("items", {
                    "item_code": it.item_code,
                    "item_name": it.item_name,
                    "description": it.description,
                    "qty": it.qty,
                    "uom": it.uom,
                    "stock_uom": it.stock_uom,
                    "conversion_factor": getattr(it, "conversion_factor", 1) or 1,
                    "rate": it.rate,
                    "amount": it.amount,
                    "warehouse": it.get("warehouse") or default_wh,
                })
            # Propagate branch to Delivery Note when custom field exists
            try:
                dn_meta = frappe.get_meta("Delivery Note")
                if dn_meta.get_field("custom_kanban_profile"):
                    dn.custom_kanban_profile = getattr(si_doc, "custom_kanban_profile", None)
            except Exception:
                pass
            dn.flags.ignore_permissions = True
            dn.insert(ignore_permissions=True)
            dn.submit()
            # Mark completed (fully billed) per business rule
            try:
                dn.db_set("per_billed", 100, update_modified=False)
            except Exception:
                pass
            try:
                dn.db_set("status", "Completed", update_modified=False)
            except Exception:
                pass
            frappe.logger().info(f"KANBAN API: Delivery Note {dn.name} submitted successfully for {si_doc.name}")
            return dn.name

        if create_dn:
            # ── OFD gates: sub-territory & custom shipping ──────────────
            try:
                from jarz_pos.api.territories import territory_has_children
                inv_territory = (invoice.get("territory") or "").strip()
                inv_sub_territory = (
                    getattr(invoice, "custom_sub_territory", None)
                    or invoice.get("custom_sub_territory")
                    or ""
                )
                if inv_territory and territory_has_children(inv_territory) and not inv_sub_territory:
                    return _failure(
                        "Please select a sub-territory before sending out for delivery"
                    )
            except ImportError:
                pass

            try:
                override_status = (
                    getattr(invoice, "custom_shipping_override_status", None)
                    or invoice.get("custom_shipping_override_status")
                    or ""
                )
                if str(override_status).strip() == "Pending":
                    return _failure(
                        "Custom shipping request is pending manager approval. "
                        "Cannot proceed to Out for Delivery."
                    )
            except Exception:
                pass

            preview = get_ofd_shortage_preview([invoice])
            preview_errors = _build_ofd_preview_errors(preview)
            if preview_errors:
                fail_resp = _failure(" ".join(preview_errors))
                fail_resp["dn_logic_version"] = dn_logic_version
                fail_resp["ofd_preview"] = preview
                return fail_resp

            if preview.get("requires_reason") and (not shortage_approved or not shortage_reason):
                fail_resp = _failure(
                    "Stock shortage approval reason is required before moving this invoice Out for Delivery."
                )
                fail_resp["dn_logic_version"] = dn_logic_version
                fail_resp["ofd_preview"] = preview
                fail_resp["requires_shortage_reason"] = True
                return fail_resp

            audit_field_values = build_ofd_shortage_field_values(
                (preview.get("invoice_previews") or {}).get(invoice_id),
                shortage_reason=shortage_reason,
                shortage_approved=shortage_approved,
            )

            try:
                print(f"Attempting Delivery Note creation for invoice {invoice_id}")
                dn_result = ensure_delivery_note_for_invoice(invoice_id)
                if dn_result.get("error"):
                    raise Exception(dn_result["error"])
                created_delivery_note = dn_result.get("delivery_note")
                print(f"Delivery Note created: {created_delivery_note}")
                frappe.logger().info(
                    f"KANBAN API: Delivery Note created '{created_delivery_note}' for invoice {invoice_id}"
                )
            except Exception as dn_ex:
                print(f"Delivery Note creation FAILED: {dn_ex}")
                frappe.logger().error(
                    f"KANBAN API: Delivery Note creation failed for {invoice_id}: {dn_ex}\n{frappe.get_traceback()}"
                )
                fail_resp = _failure(
                    f"Failed creating Delivery Note for invoice {invoice_id}: {str(dn_ex)}"
                )
                fail_resp["dn_logic_version"] = dn_logic_version
                return fail_resp
            # After (or even if reusing) DN creation, ensure branch cash PE if partner invoice
            try:
                created_cash_payment_entry = _ensure_cash_payment_entry_for_partner(invoice)
                if created_cash_payment_entry:
                    print(f"Cash Payment Entry created: {created_cash_payment_entry}")
            except Exception as cash_ex:
                print(f"Cash Payment Entry creation FAILED (non-fatal): {cash_ex}")
                frappe.logger().warning(
                    f"KANBAN API: Cash Payment Entry creation failed for {invoice_id}: {cash_ex}"
                )
            # Create Sales Partner Transaction record (idempotent)
            try:
                sales_partner_val = getattr(invoice, 'sales_partner', None)
                if sales_partner_val:
                    # Idempotency token pattern: SPTRN::<invoice_name>
                    idem_token = f"SPTRN::{invoice.name}"
                    # One transaction per invoice. The paid-online partner hook already
                    # minted one at invoice creation under its own token; matching on
                    # THIS token alone added a second row here on every kanban dispatch,
                    # and the partner's commission then settled twice.
                    if not frappe.db.exists("Sales Partner Transactions", {"reference_invoice": invoice.name}):
                        txn = frappe.new_doc("Sales Partner Transactions")
                        txn.sales_partner = sales_partner_val
                        txn.status = "Unsettled"  # always unsettle on creation
                        # Use original invoice creation datetime (invoice.creation is str/datetime)
                        try:
                            txn.date = getattr(invoice, 'creation', frappe.utils.now())
                        except Exception:
                            txn.date = frappe.utils.now()
                        txn.reference_invoice = invoice.name
                        txn.amount = float(getattr(invoice, 'grand_total', 0) or 0)
                        # Determine payment mode: cash if cash PE created, else Online
                        payment_mode_val = PAYMENT_MODES.CASH if created_cash_payment_entry else PAYMENT_MODES.ONLINE
                        txn.payment_mode = payment_mode_val
                        # §5-E: compute + store the commission/VAT split (and combined total)
                        # so the "Settle Sales Partner" batch action can post the fee JE.
                        try:
                            from jarz_pos.services.delivery_handling import _compute_sales_partner_fees
                            fees = _compute_sales_partner_fees(
                                invoice, sales_partner_val,
                                online=(payment_mode_val == PAYMENT_MODES.ONLINE),
                            )
                            txn.partner_fees = fees.get("total_fees")
                            txn.base_fee = fees.get("base_fees")
                            txn.vat_amount = fees.get("vat")
                        except Exception as _fee_err:
                            frappe.logger().warning(
                                f"KANBAN API: SPT fee computation failed for {invoice.name}: {_fee_err}"
                            )
                        txn.idempotency_token = idem_token
                        txn.insert(ignore_permissions=True)
                        created_partner_txn = txn.name
                        print(f"Sales Partner Transaction created: {created_partner_txn} ({payment_mode_val})")
                        frappe.logger().info(
                            f"KANBAN API: Sales Partner Transaction {txn.name} created for invoice {invoice_id}"
                        )
                    else:
                        print("Sales Partner Transaction already exists (idempotent skip)")
            except Exception as sp_txn_err:
                print(f"Sales Partner Transaction creation FAILED (non-fatal): {sp_txn_err}")
                frappe.logger().warning(
                    f"KANBAN API: Sales Partner Transaction creation failed for {invoice_id}: {sp_txn_err}"
                )

        updated_fields: List[str] = []
        for f in fields_to_update:
            try:
                invoice.set(f, new_state)
                updated_fields.append(f)
                print(f"set success for field {f}")
            except Exception as inner_ex:
                print(f"Failed setting field {f}: {inner_ex}")
                frappe.logger().error(f"Failed setting field {f} on {invoice_id}: {inner_ex}")

        if not updated_fields:
            return _failure(f"Failed updating invoice state for {invoice_id}")

        for field_name, field_value in audit_field_values.items():
            if not meta.get_field(field_name):
                continue
            try:
                invoice.set(field_name, field_value)
                updated_fields.append(field_name)
                print(f"set success for field {field_name}")
            except Exception as inner_ex:
                print(f"Failed setting field {field_name}: {inner_ex}")
                frappe.logger().error(f"Failed setting field {field_name} on {invoice_id}: {inner_ex}")

        try:
            invoice.flags.ignore_validate_update_after_submit = True
            invoice.save(ignore_permissions=True, ignore_version=True)
            print("invoice save successful")
        except Exception as save_ex:
            print(f"Failed saving invoice {invoice_id}: {save_ex}")
            frappe.logger().error(f"Failed saving invoice {invoice_id}: {save_ex}")
            return _failure(f"Failed updating invoice state for {invoice_id}: {save_ex}")

        try:
            frappe.db.commit()
            print("DB commit successful")
        except Exception as commit_ex:
            frappe.logger().warning(f"Explicit DB commit after state update failed: {commit_ex}")
            print(f"DB commit FAILED: {commit_ex}")

        # ── Trip status sync: recompute trip status when invoice changes ──
        try:
            from jarz_pos.api.trips import sync_trip_status
            sync_trip_status(invoice_id)
        except Exception as trip_sync_ex:
            frappe.logger().warning(f"KANBAN API: Trip sync failed for {invoice_id}: {trip_sync_ex}")

        frappe.logger().info(
            f"KANBAN API: Invoice {invoice_id} state change {old_state} -> {new_state}; fields updated: {updated_fields}; delivery_note={created_delivery_note}; logic_version={dn_logic_version}"
        )
        payload = {
            "invoice_id": invoice_id,
            "old_state": old_state,
            "new_state": new_state,
            "old_state_key": _state_key(old_state or "") if old_state else None,
            "new_state_key": _state_key(new_state),
            "updated_by": frappe.session.user,
            "timestamp": frappe.utils.now(),
            "delivery_note": created_delivery_note if create_dn else None,
            "dn_logic_version": dn_logic_version,
            "cash_payment_entry": created_cash_payment_entry,
            "sales_partner_transaction": created_partner_txn,
            "kanban_profile": get_invoice_branch(invoice),
        }
        publish_invoice_event(WS_EVENTS.INVOICE_STATE_CHANGE, payload, invoice)
        publish_invoice_event(WS_EVENTS.KANBAN_UPDATE, payload, invoice)
        return _success(
            message=f"Invoice {invoice_id} state updated",
            invoice_id=invoice_id,
            state=new_state,
            updated_fields=updated_fields,
            final_state=new_state,
            delivery_note=created_delivery_note if create_dn else None,
            dn_logic_version=dn_logic_version,
            cash_payment_entry=created_cash_payment_entry,
            sales_partner_transaction=created_partner_txn,
        )
    except Exception as e:
        print(f"GENERAL FAILURE update_invoice_state: {e}")
        error_msg = f"Error updating invoice state: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Update Invoice State Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

def _release_operational_artifacts(invoice_name: str) -> Dict[str, Any]:
    """Free the operational records a cancelled invoice was still holding.

    Cancelling the accounting documents used to leave the *operational* ones
    dangling: an uploaded payment receipt stayed pending forever, the delivery
    trip kept counting the order towards its completion, and an approved-but-
    unused shipping request stayed active. None of these affect the ledger,
    which is why they went unnoticed — they affect what staff see on screen.

    Every step is independently guarded: releasing artifacts must never be able
    to fail a cancellation that has already committed its accounting.
    """
    released: Dict[str, Any] = {}

    # An UNSETTLED Sales Partner Transaction is a fee not yet posted: with the
    # order cancelled there is nothing to charge commission on, so the row goes.
    # (A settled one has a fee journal behind it and blocks the cancel upstream.)
    try:
        unsettled_partner_rows = frappe.get_all(
            "Sales Partner Transactions",
            filters={"reference_invoice": invoice_name, "status": "Unsettled"},
            pluck="name",
        ) or []
        removed: List[str] = []
        for row_name in unsettled_partner_rows:
            frappe.delete_doc(
                "Sales Partner Transactions", row_name, ignore_permissions=True, force=True
            )
            removed.append(row_name)
        if removed:
            released["sales_partner_transactions"] = removed
    except Exception:
        frappe.logger().warning(
            f"KANBAN API: partner transaction release failed for {invoice_name}"
        )

    try:
        from jarz_pos.api.payment_receipts import mark_payment_receipts_changed_for_invoice

        changed = mark_payment_receipts_changed_for_invoice(invoice_name)
        if changed:
            released["payment_receipts"] = changed
    except Exception:
        frappe.logger().warning(
            f"KANBAN API: payment receipt release failed for {invoice_name}"
        )

    try:
        from jarz_pos.api.trips import sync_trip_status

        sync_trip_status(invoice_name)
        released["trip_synced"] = True
    except Exception:
        frappe.logger().warning(f"KANBAN API: trip sync failed for {invoice_name}")

    try:
        from jarz_pos.api.manager import _find_active_custom_shipping_requests

        cancelled_requests: List[str] = []
        for csr_name in _find_active_custom_shipping_requests(invoice_name):
            try:
                csr = frappe.get_doc("Custom Shipping Request", csr_name)
                if int(getattr(csr, "docstatus", 0) or 0) == 1:
                    csr.flags.ignore_permissions = True
                    csr.cancel()
                    cancelled_requests.append(csr_name)
                elif int(getattr(csr, "docstatus", 0) or 0) == 0:
                    frappe.delete_doc(
                        "Custom Shipping Request", csr_name, ignore_permissions=True, force=True
                    )
                    cancelled_requests.append(csr_name)
            except Exception:
                frappe.logger().warning(
                    f"KANBAN API: could not release shipping request {csr_name}"
                )
        if cancelled_requests:
            released["custom_shipping_requests"] = cancelled_requests
    except Exception:
        frappe.logger().warning(
            f"KANBAN API: shipping request release failed for {invoice_name}"
        )

    return released


@frappe.whitelist(allow_guest=False)
def cancel_invoice(invoice_id: str, reason: str, notes: Optional[str] = None) -> Dict[str, Any]:
    """Cancel a Sales Invoice prior to dispatch with audit trail and notifications."""
    try:
        invoice_id = (invoice_id or "").strip()
        reason = (reason or "").strip()
        notes = (notes or "").strip() or None

        if not invoice_id:
            return _failure("invoice_id is required")
        if not reason:
            return _failure("Cancellation reason is required")

        roles = {str(r or "").strip().lower() for r in frappe.get_roles(frappe.session.user)}
        if roles.isdisjoint(ROLES.LINE_MANAGER_TIER_LOWER):
            return _failure("You are not permitted to cancel orders")

        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        # Role check above says *what* this user may do; this says *whose* orders.
        ensure_profile_scoped_invoice_access(invoice, action_label="cancelling this order")
        if int(getattr(invoice, "docstatus", 0) or 0) != 1:
            return _failure("Only submitted invoices can be cancelled")
        if int(getattr(invoice, "is_return", 0) or 0):
            return _failure("Return invoices cannot be cancelled")

        # An unsettled partner transaction is not a settlement artifact: no fee has
        # been posted, so a paid-online Talabat order can still be cancelled before
        # dispatch. _release_operational_artifacts drops the row after the cancel.
        mutation_blocker = get_invoice_hard_mutation_blocker(
            invoice, ignore_unsettled_partner_transactions=True
        )
        if mutation_blocker:
            return _failure(
                mutation_blocker.get("mutation_block_reason")
                or "This invoice cannot be changed from this workflow"
            )

        current_state = (
            invoice.get("custom_sales_invoice_state")
            or invoice.get("sales_invoice_state")
            or invoice.get("custom_state")
            or invoice.get("state")
            or ""
        )
        normalized_state = _state_key(current_state)
        blocked_states = {"out_for_delivery", "out-for-delivery", "delivered", "completed", "cancelled"}
        if normalized_state in blocked_states:
            return _failure("Invoice already dispatched; cancellation blocked")

        outstanding = float(invoice.outstanding_amount or 0.0)
        grand_total = float(invoice.grand_total or 0.0)
        
        # Use a more generous tolerance for floating-point comparisons (0.50 EGP)
        tolerance = 0.50

        is_unpaid = outstanding >= (grand_total - tolerance)
        is_paid = outstanding <= tolerance
        
        # Log for debugging
        frappe.logger().info(f"KANBAN CANCEL: Invoice={invoice.name} Outstanding={outstanding} GrandTotal={grand_total} IsUnpaid={is_unpaid} IsPaid={is_paid}")
        
        if not (is_unpaid or is_paid):
            return _failure(f"Invoice has partial payments ({outstanding:.2f} remaining of {grand_total:.2f}); settle or refund before cancelling")

        # ── Shift safety ────────────────────────────────────────────────
        # Cancelling a Payment Entry flips its GL rows to is_cancelled=1, which
        # removes them from any shift window that already counted them. If that
        # window is closed, its reconciliation and Cash-Over/Short JE silently
        # stop matching the ledger. Two guards, in order of specificity:
        #   1. the branch must be open right now (cash is about to move), and
        #   2. no payment we are about to cancel may belong to a CLOSED shift.
        # Unpaid invoices move no cash, so neither applies to them.
        linked_payment_entries: List[str] = []
        if is_paid:
            linked_payment_entries = sorted({
                pe for pe in frappe.get_all(
                    "Payment Entry Reference",
                    filters={
                        "reference_doctype": "Sales Invoice",
                        "reference_name": invoice.name,
                        "docstatus": 1,
                        "parenttype": "Payment Entry",
                    },
                    pluck="parent",
                ) if pe
            })
            try:
                ensure_open_shift_for_invoice(invoice, action_label="cancelling a paid order")
                assert_vouchers_not_in_closed_shift(
                    [("Payment Entry", pe) for pe in linked_payment_entries],
                    action_label="cancelling this order",
                )
            except (ShiftRequiredError, ClosedShiftError) as shift_err:
                return _failure(str(getattr(shift_err, "message", None) or shift_err))

        credit_note_name: Optional[str] = None
        cancelled_payment_entries: List[str] = []
        cancelled_docstatus = 1
        savepoint = "kanban_cancel_invoice"
        try:
            frappe.db.savepoint(savepoint)
        except Exception:
            savepoint = None

        try:
            invoice.flags.ignore_permissions = True

            if is_unpaid:
                invoice.cancel()
                cancelled_docstatus = 2
            else:
                # Resolved above so the closed-shift guard could inspect them.
                for pe_name in linked_payment_entries:
                    if not pe_name:
                        continue
                    pe_doc = frappe.get_doc("Payment Entry", pe_name)
                    if int(getattr(pe_doc, "docstatus", 0) or 0) != 1:
                        continue
                    pe_doc.flags.ignore_permissions = True
                    pe_doc.cancel()
                    cancelled_payment_entries.append(pe_doc.name)

                # Cancelling the Payment Entry updates the linked Sales Invoice's
                # outstanding_amount and `modified` timestamp in the DB. Re-read the
                # invoice before cancelling it, otherwise this stale in-memory doc trips
                # a TimestampMismatchError ("has been modified after you have opened it").
                invoice.reload()
                invoice.flags.ignore_permissions = True
                invoice.cancel()
                cancelled_docstatus = 2

            meta = frappe.get_meta("Sales Invoice")
            updated_fields: List[str] = []
            for candidate in [
                "custom_sales_invoice_state",
                "sales_invoice_state",
                "custom_state",
                "state",
            ]:
                if meta.get_field(candidate):
                    try:
                        frappe.db.set_value("Sales Invoice", invoice.name, candidate, STATUS.CANCELLED, update_modified=True)
                        updated_fields.append(candidate)
                    except Exception:
                        pass

            # Persist structured cancellation metadata to queryable fields
            try:
                if meta.get_field("custom_cancellation_type"):
                    reason_text = reason
                    if notes:
                        reason_text = f"{reason}\nNotes: {notes}"
                    frappe.db.set_value(
                        "Sales Invoice", invoice.name,
                        "custom_cancellation_type", "POS Cancellation",
                        update_modified=False,
                    )
                    frappe.db.set_value(
                        "Sales Invoice", invoice.name,
                        "custom_cancellation_reason", reason_text,
                        update_modified=False,
                    )
            except Exception:
                frappe.logger().warning(
                    f"KANBAN API: Unable to set cancellation fields for {invoice.name}"
                )

            comment_lines = [
                f"Order cancelled by {frappe.session.user}",
                f"Reason: {reason}",
            ]
            if notes:
                comment_lines.append(f"Notes: {notes}")
            try:
                refreshed = frappe.get_doc("Sales Invoice", invoice.name)
                refreshed.add_comment("Comment", "\n".join(comment_lines))
            except Exception:
                frappe.logger().warning(f"KANBAN API: Unable to add cancellation comment for {invoice.name}")

            released_artifacts = _release_operational_artifacts(invoice.name)

            payload = {
                "invoice_id": invoice.name,
                "old_state": current_state,
                "new_state": STATUS.CANCELLED,
                "old_state_key": _state_key(current_state) if current_state else None,
                "new_state_key": _state_key(STATUS.CANCELLED),
                "cancelled_by": frappe.session.user,
                "reason": reason,
                "notes": notes,
                "credit_note": credit_note_name,
                "timestamp": frappe.utils.now(),
                "docstatus": cancelled_docstatus,
                "paid_path": is_paid,
                "cancelled_payment_entries": cancelled_payment_entries or None,
                "kanban_profile": get_invoice_branch(invoice),
                "released_artifacts": released_artifacts or None,
            }

            publish_invoice_event(WS_EVENTS.INVOICE_STATE_CHANGE, payload, invoice)
            publish_invoice_event(WS_EVENTS.KANBAN_UPDATE, payload, invoice)

            try:
                notify_invoice_cancellation(invoice.name, reason, notes=notes, credit_note=credit_note_name)
            except Exception:
                frappe.logger().warning(
                    f"KANBAN API: notify_invoice_cancellation failed for {invoice.name}"
                )

            try:
                frappe.db.commit()
            except Exception:
                frappe.logger().warning("KANBAN API: explicit commit failed after cancellation")

            return _success(
                invoice_id=invoice.name,
                cancelled_docstatus=cancelled_docstatus,
                credit_note=credit_note_name,
                released_artifacts=released_artifacts,
                state=STATUS.CANCELLED,
                reason=reason,
                notes=notes,
                paid_path=is_paid,
                updated_fields=updated_fields,
                cancelled_payment_entries=cancelled_payment_entries,
            )
        except Exception as exc:
            if savepoint:
                try:
                    frappe.db.rollback(savepoint=savepoint)
                except Exception:
                    pass
            error_msg = f"Error cancelling invoice: {exc}"
            frappe.logger().error(error_msg)
            frappe.log_error(
                f"Cancel Invoice Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
                "Kanban API",
            )
            return _failure(error_msg)
    except Exception as outer:
        error_msg = f"Unexpected cancellation error: {outer}"
        frappe.logger().error(error_msg)
        frappe.log_error(
            f"Cancel Invoice Error: {outer}\n\nTraceback:\n{traceback.format_exc()}",
            "Kanban API",
        )
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_invoice_details(invoice_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific invoice.
    
    Args:
        invoice_id: ID of the Sales Invoice to retrieve
        
    Returns:
        Dict with success status and invoice details
    """
    try:
        frappe.logger().debug(f"KANBAN API: get_invoice_details - Invoice: {invoice_id}")
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        _ensure_invoice_detail_access(invoice)
        data = format_invoice_data(invoice)
        shipping = _get_invoice_shipping_values(invoice)
        data["shipping_income"] = float(shipping.get("income") or 0.0)
        data["shipping_expense"] = float(shipping.get("expense") or 0.0)
        data["was_free_shipping"] = bool(shipping.get("was_free_shipping", False))
        # Add is_pickup flag consistently
        try:
            data["is_pickup"] = _is_pickup_invoice(invoice)
            if data["is_pickup"]:
                # Ensure shipping fields are zeroed for pickup in details too
                data["shipping_income"] = 0.0
                data["shipping_expense"] = 0.0
        except Exception:
            pass
        # Enrich with customer_phone
        try:
            customer_phone = _resolve_customer_phone(invoice.get("customer") or "")
            if customer_phone:
                data["customer_phone"] = sanitize_printable_text(customer_phone)
        except Exception:
            pass
        # Augment with unsettled courier txn flag
        try:
            data["has_unsettled_courier_txn"] = bool(
                frappe.db.exists(
                    "Courier Transaction",
                    {"reference_invoice": invoice.name, "status": ["!=", "Settled"]},
                )
            )
        except Exception:
            data["has_unsettled_courier_txn"] = False
        # The board card resolves these two; the details payload must carry them as well
        # or the printed receipt silently loses the payment method and prints UNPAID for
        # an order whose collection method was switched to an online one after dispatch.
        data["has_unsettled_customer_amount"] = bool(
            float(_get_unsettled_customer_amount_map([invoice.name]).get(invoice.name, 0.0)) > 0.005
        )
        data["actual_payment_method"] = sanitize_printable_text(
            _get_actual_payment_method_map(
                [
                    {
                        "name": invoice.name,
                        "outstanding_amount": invoice.get("outstanding_amount"),
                        "status": invoice.get("status"),
                        "custom_payment_method": invoice.get("custom_payment_method"),
                        "sales_partner": invoice.get("sales_partner"),
                    }
                ]
            ).get(invoice.name)
        )
        try:
            receipt_data = _get_active_payment_receipt_map([invoice.name]).get(invoice.name, {})
            data.update(receipt_data)
        except Exception:
            pass
        try:
            data["note_count"] = int(_get_invoice_note_counts([invoice.name]).get(invoice.name, 0))
        except Exception:
            data["note_count"] = 0
        try:
            data["latest_note"] = _get_invoice_latest_notes([invoice.name]).get(invoice.name)
        except Exception:
            data["latest_note"] = None
        data.update(get_invoice_amendment_eligibility(invoice))
        data.update(get_invoice_cancellation_eligibility(invoice))
        try:
            from jarz_pos.services.invoice_return import get_invoice_return_eligibility

            data.update(get_invoice_return_eligibility(invoice))
        except Exception:
            data.update({
                "can_return": False,
                "return_block_code": "unavailable",
                "return_block_reason": None,
            })
        # What has already been returned, mirroring the kanban card so the board
        # and the details dialog can never disagree about a partial return.
        # Defensive on both reads: these fields are absent on an unmigrated site.
        try:
            data["return_status"] = sanitize_printable_text(
                invoice.get("custom_return_status") or ""
            )
        except Exception:
            data["return_status"] = ""
        try:
            data["returned_amount"] = float(invoice.get("custom_returned_amount") or 0.0)
        except Exception:
            data["returned_amount"] = 0.0
        return _success(data=data)
    except Exception as e:
        error_msg = f"Error getting invoice details: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Invoice Details Error: {str(e)}", "Kanban API")
        return _failure(error_msg)


@frappe.whitelist(allow_guest=False)
def get_invoice_notes(invoice_id: str) -> Dict[str, Any]:
    """Return operational Jarz notes for a Sales Invoice only."""
    try:
        frappe.has_permission("Sales Invoice", ptype="read", throw=True)
        invoice = frappe.get_doc("Sales Invoice", (invoice_id or "").strip())
        _ensure_invoice_detail_access(invoice)

        rows = frappe.get_all(
            "Jarz Invoice Note",
            filters={"sales_invoice": invoice.name},
            fields=[
                "name",
                "sales_invoice",
                "pos_profile",
                "note",
                "added_by",
                "added_by_full_name",
                "added_on",
                "creation",
            ],
            order_by="added_on asc, creation asc",
            limit=500,
        )
        notes = [_serialize_invoice_note_row(row) for row in rows]
        return _success(data=notes, note_count=len(notes))
    except Exception as e:
        error_msg = f"Error getting invoice notes: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Invoice Notes Error: {str(e)}", "Kanban API")
        return _failure(error_msg)


@frappe.whitelist(allow_guest=False)
def add_invoice_note(invoice_id: str, note: str) -> Dict[str, Any]:
    """Add an operational Jarz note to a Sales Invoice."""
    try:
        frappe.has_permission("Sales Invoice", ptype="read", throw=True)
        invoice = frappe.get_doc("Sales Invoice", (invoice_id or "").strip())
        _ensure_invoice_detail_access(invoice)

        note_text = str(note or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not note_text:
            return _failure("Note cannot be empty")

        note_doc = frappe.get_doc(
            {
                "doctype": "Jarz Invoice Note",
                "sales_invoice": invoice.name,
                "note": note_text,
            }
        )
        note_doc.insert(ignore_permissions=True)
        frappe.db.commit()

        note_count = int(_get_invoice_note_counts([invoice.name]).get(invoice.name, 0))
        payload = {
            "event": "invoice_note_added",
            "invoice_id": invoice.name,
            "invoice": invoice.name,
            "note_count": note_count,
            "latest_note": _format_invoice_note_preview(note_doc.note),
            "updated_by": frappe.session.user,
            "timestamp": frappe.utils.now(),
            "kanban_profile": get_invoice_branch(invoice),
        }
        publish_invoice_event(WS_EVENTS.KANBAN_UPDATE, payload, invoice)

        note_data = _serialize_invoice_note_row(
            {
                "name": note_doc.name,
                "sales_invoice": note_doc.sales_invoice,
                "pos_profile": getattr(note_doc, "pos_profile", None),
                "note": note_doc.note,
                "added_by": getattr(note_doc, "added_by", None),
                "added_by_full_name": getattr(note_doc, "added_by_full_name", None),
                "added_on": getattr(note_doc, "added_on", None),
            }
        )
        return _success(data=note_data, note_count=note_count)
    except Exception as e:
        error_msg = f"Error adding invoice note: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Add Invoice Note Error: {str(e)}", "Kanban API")
        return _failure(error_msg)


@frappe.whitelist(allow_guest=False)
def get_kanban_filters() -> Dict[str, Any]:
    """Get available filter options for the Kanban board.
    
    Returns:
        Dict with success status and filter options
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_filters called")

        # Scope the picker to the branches this user actually works and to the
        # last six months. Unbounded, this was a DISTINCT over every POS invoice
        # ever raised — slow enough on production to time the request out, which
        # left the customer filter permanently empty and looking broken.
        customer_filters: Dict[str, Any] = {
            "docstatus": 1,
            "is_pos": 1,
            "is_return": 0,
            "posting_date": [">=", frappe.utils.add_days(frappe.utils.today(), -180)],
        }
        allowed_profiles = _get_current_user_pos_profiles()
        if allowed_profiles:
            try:
                branch_field = (
                    "custom_kanban_profile"
                    if frappe.get_meta("Sales Invoice").get_field("custom_kanban_profile")
                    else "pos_profile"
                )
            except Exception:
                branch_field = "pos_profile"
            customer_filters[branch_field] = ["in", allowed_profiles]

        customers = frappe.get_all(
            "Sales Invoice",
            filters=customer_filters,
            fields=["customer", "customer_name"],
            distinct=True,
            order_by="customer_name",
            limit=QUERY_LIMITS.KANBAN_INVOICES,
        )
        # DISTINCT is over the pair, so one customer renamed mid-period yields two
        # rows; collapse on the id and keep the first (alphabetical) label.
        seen: Dict[str, str] = {}
        for c in customers:
            if c.customer and c.customer not in seen:
                seen[c.customer] = c.customer_name or c.customer
        customer_options = [{"value": k, "label": v} for k, v in seen.items()]
        state_options = [{"value": s, "label": s} for s in _get_state_field_options()]
        return _success(customers=customer_options, states=state_options)
    except Exception as e:
        error_msg = f"Error getting kanban filters: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Filters Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

# ---------------------------------------------------------------------------
# Fallback explicit whitelist enforcement (in case of edge caching/import issues)
# ---------------------------------------------------------------------------
try:
    _kanban_funcs = [
        get_kanban_columns,
        get_kanban_invoices,
        update_invoice_state,
        cancel_invoice,
        get_invoice_details,
        get_invoice_notes,
        add_invoice_note,
        get_kanban_filters,
    ]
    for _f in _kanban_funcs:
        if not getattr(_f, "is_whitelisted", False):
            frappe.logger().warning(f"KANBAN API: Forcing whitelist registration for {_f.__name__}")
            # Re-wrap with decorator (preserve allow_guest False)
            _wrapped = frappe.whitelist(allow_guest=False)(_f)
            globals()[_f.__name__] = _wrapped
except Exception:
    # Silent fail – we don't want import to abort
    pass
