import json

import frappe
from frappe import _
from typing import List, Dict, Any, Optional

from jarz_pos.constants import ACCOUNTS, PAYMENT_MODES, PRICE_LISTS, ROLES
from jarz_pos.utils.warehouse_utils import resolve_purchase_warehouse

STANDARD_BUYING = PRICE_LISTS.STANDARD_BUYING

SETTINGS_DOCTYPE = "Jarz POS Settings"

#: Jarz POS Settings field naming the site-wide default purchase VAT template.
#: Seeded by ``jarz_pos.setup.purchase_setup``; blank means "no default".
DEFAULT_ITEM_TAX_TEMPLATE_FIELD = "purchase_default_item_tax_template"


def _ensure_manager_access():
    roles = set(frappe.get_roles())
    allowed_roles = ROLES.PURCHASE
    if not roles.intersection(allowed_roles):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _coerce_rows(value: Any) -> List[Dict[str, Any]]:
    """Accept a list, or the JSON string Frappe hands us for form-encoded calls."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            frappe.throw(_("Malformed items payload"))
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        frappe.throw(_("Malformed items payload"))
    return [row for row in value if isinstance(row, dict)]


def _has_field(doctype: str, fieldname: str) -> bool:
    """True when the column actually exists on this site *right now*.

    Code reaches the server before ``bench migrate`` runs, so a query naming a
    not-yet-migrated field would 500 the endpoint for the whole window between
    deploy and migrate.
    """
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


def _default_item_tax_template(company: Optional[str] = None) -> Optional[str]:
    """The site-wide default VAT template configured on Jarz POS Settings.

    Returns ``None`` — i.e. "no default", never a throw — whenever the setting
    cannot be honoured: the field has not been migrated yet, it is blank, or the
    template it names has since been deleted, disabled, or belongs to another
    company. A purchase must never fail because a *default* went stale.

    ``company`` is checked only when supplied; the read-only listing endpoints
    have no company in hand and are just pre-filling a dropdown, whereas
    :func:`create_purchase_invoice` knows its company and must not attach a
    cross-company template that ERPNext would reject.
    """
    if not _has_field(SETTINGS_DOCTYPE, DEFAULT_ITEM_TAX_TEMPLATE_FIELD):
        return None
    try:
        configured = frappe.db.get_single_value(
            SETTINGS_DOCTYPE, DEFAULT_ITEM_TAX_TEMPLATE_FIELD
        )
    except Exception:
        return None
    configured = (configured or "").strip()
    if not configured:
        return None
    try:
        row = frappe.db.get_value(
            "Item Tax Template", configured, ["company", "disabled"], as_dict=True
        )
    except Exception:
        return None
    if not row or int(row.get("disabled") or 0):
        return None
    if company and row.get("company") != company:
        return None
    return configured


@frappe.whitelist()
def get_suppliers(search: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_manager_access()
    filters: Dict[str, Any] = {}
    fields = ["name", "supplier_name", "supplier_group", "supplier_type", "disabled"]
    or_filters: List[Any] = []
    if search:
        like = f"%{search}%"
        # Use simple field names in or_filters to avoid doctype qualification issues
        or_filters = [["name", "like", like], ["supplier_name", "like", like]]
    rows = frappe.get_all(
        "Supplier",
        filters=filters,
        or_filters=or_filters,
        fields=fields,
        limit=limit,
        order_by="modified desc",
    )
    return rows


@frappe.whitelist()
def get_recent_suppliers(limit: int = 20) -> List[Dict[str, Any]]:
    """Return most recently used suppliers inferred from Purchase Invoices."""
    _ensure_manager_access()
    pi_rows = frappe.get_all(
        "Purchase Invoice",
        fields=["supplier", "posting_date"],
        order_by="posting_date desc, creation desc",
        limit=100,
    )
    ordered_suppliers: List[str] = []
    seen = set()
    for r in pi_rows:
        s = r.get("supplier")
        if s and s not in seen:
            ordered_suppliers.append(s)
            seen.add(s)
        if len(ordered_suppliers) >= limit:
            break
    # Fallback: fill with most recently modified suppliers if needed
    if len(ordered_suppliers) < limit:
        need = limit - len(ordered_suppliers)
        more = frappe.get_all(
            "Supplier",
            filters={"name": ["not in", ordered_suppliers] if ordered_suppliers else None},
            fields=["name"],
            limit=need,
            order_by="modified desc",
        )
        for m in more:
            nm = m.get("name")
            if nm and nm not in seen:
                ordered_suppliers.append(nm)
                seen.add(nm)

    if not ordered_suppliers:
        return []

    # Fetch details and preserve order
    details_map = {
        r["name"]: r
        for r in frappe.get_all(
            "Supplier",
            filters={"name": ["in", ordered_suppliers]},
            fields=["name", "supplier_name", "supplier_group", "supplier_type", "disabled"],
            limit=len(ordered_suppliers),
        )
    }
    result: List[Dict[str, Any]] = []
    for s in ordered_suppliers:
        if s in details_map:
            result.append(details_map[s])
    return result


@frappe.whitelist()
def search_items(
    search: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    item_group: Optional[str] = None,
    for_request: int = 0,
) -> List[Dict[str, Any]]:
    """Purchasable items, with the context a buyer needs to decide.

    Each row carries on-hand quantity and the rate last actually paid, so the
    buyer is not choosing a price blind — the single most common complaint about
    a bare item picker.

    ``for_request`` widens the role gate: raising a request needs to search
    items too, and that path is open to all floor staff, not just buyers.
    """
    if int(for_request or 0):
        from jarz_pos.api.purchase_request import _ensure_request_access

        _ensure_request_access()
    else:
        _ensure_manager_access()

    limit = max(1, min(int(limit or 20), 100))
    start = max(0, int(page or 0)) * limit

    filters: Dict[str, Any] = {
        "disabled": 0,
        "is_purchase_item": 1,
        "has_variants": 0,
    }
    if item_group:
        filters["item_group"] = item_group
    or_filters = []
    if search:
        like = f"%{search}%"
        or_filters = [
            ["Item", "name", "like", like],
            ["Item", "item_name", "like", like],
            ["Item", "item_group", "like", like],
        ]
    fields = [
        "name as item_code",
        "item_name",
        "stock_uom",
        "item_group",
    ]
    items = frappe.get_all(
        "Item",
        filters=filters,
        or_filters=or_filters,
        fields=fields,
        limit_page_length=limit,
        limit_start=start,
        order_by="item_name asc",
    )
    if not items:
        return []

    codes = [it["item_code"] for it in items]
    uom_map = _get_item_uoms_bulk(codes)
    price_map = _get_item_prices_bulk(codes)
    on_hand = _on_hand_bulk(codes)
    last_paid = _last_paid_bulk(codes)
    tax_map = _item_tax_templates_bulk(codes)

    for it in items:
        code = it["item_code"]
        it["uoms"] = uom_map.get(code) or [{"uom": it["stock_uom"], "conversion_factor": 1}]
        it["prices"] = price_map.get(code, [])
        it["on_hand_qty"] = on_hand.get(code, 0.0)
        it["last_purchase_rate"] = last_paid.get(code, 0.0)
        # Pre-fills the line's VAT so the buyer only overrides the exceptions:
        # the item's own template, else the configured site default.
        it["item_tax_template"] = tax_map.get(code)
    return items


def _get_item_uoms_bulk(item_codes: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """UOMs for many items in two queries instead of one get_doc per item."""
    if not item_codes:
        return {}
    stock_uoms = {
        r["name"]: r["stock_uom"]
        for r in frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes]},
            fields=["name", "stock_uom"],
            limit_page_length=0,
        )
    }
    out: Dict[str, List[Dict[str, Any]]] = {
        code: [{"uom": stock_uoms.get(code), "conversion_factor": 1}] for code in item_codes
    }
    for row in frappe.get_all(
        "UOM Conversion Detail",
        filters={"parent": ["in", item_codes], "parenttype": "Item"},
        fields=["parent", "uom", "conversion_factor"],
        order_by="parent asc, idx asc",
        limit_page_length=0,
    ):
        parent = row["parent"]
        if row.get("uom") == stock_uoms.get(parent):
            continue
        out.setdefault(parent, []).append({
            "uom": row.get("uom"),
            "conversion_factor": row.get("conversion_factor") or 1,
        })
    return out


def _get_item_prices_bulk(item_codes: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    if not item_codes:
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in frappe.get_all(
        "Item Price",
        filters={"price_list": STANDARD_BUYING, "item_code": ["in", item_codes], "buying": 1},
        fields=["item_code", "uom", "price_list_rate"],
        order_by="item_code asc, uom asc",
        limit_page_length=0,
    ):
        out.setdefault(row["item_code"], []).append({
            "uom": row.get("uom"),
            "rate": float(row.get("price_list_rate") or 0),
        })
    return out


def _item_tax_templates_bulk(
    item_codes: List[str],
    company: Optional[str] = None,
    include_default: bool = True,
) -> Dict[str, Optional[str]]:
    """The Item Tax Template each item should default to.

    Two layers, in this order:

    1. the template the item carries on its own ``taxes`` table — this is what
       makes "only some items have VAT" work without the buyer remembering
       which: the item itself declares its tax treatment;
    2. failing that, the site-wide default from Jarz POS Settings — most
       purchases are taxed at the standard rate, so making every item master
       spell that out would be busywork with a silent under-charge as the cost
       of forgetting.

    Only Item Tax rows with no ``tax_category`` are considered — a categorised
    row is conditional on the party's tax category, which a POS purchase does
    not set, so honouring it here would apply a tax that ERPNext itself would
    not.

    The site default is layered in *here* rather than at each call site so that
    every surface agrees: the item search, the item detail, the requests list
    and the invoice builder all pre-fill the same template.
    """
    if not item_codes:
        return {}
    out: Dict[str, Optional[str]] = {}
    for row in frappe.get_all(
        "Item Tax",
        filters={"parent": ["in", item_codes], "parenttype": "Item"},
        fields=["parent", "item_tax_template", "tax_category"],
        order_by="parent asc, idx asc",
        limit_page_length=0,
    ):
        if row.get("tax_category"):
            continue
        out.setdefault(row["parent"], row.get("item_tax_template"))

    if include_default:
        default = _default_item_tax_template(company)
        if default:
            for code in item_codes:
                # A row that exists but names no template is not a decision to
                # go untaxed — the buyer expresses that per line, by sending an
                # explicit empty ``item_tax_template``.
                if not out.get(code):
                    out[code] = default
    return out


def _resolve_line_tax_template(
    row: Dict[str, Any], inherited: Dict[str, Optional[str]]
) -> str:
    """The VAT template a cart line should carry. Strict precedence:

    1. an explicit ``item_tax_template`` key on the line — **including an
       explicit empty string**, which is how the buyer says "this one is not
       taxed" for an item whose master (or the site default) says otherwise;
    2. the Item master's own template;
    3. the configured site default;
    4. nothing.

    ``inherited`` is the output of :func:`_item_tax_templates_bulk`, which has
    already collapsed layers 2 and 3. Only a *wholly absent* key falls back.
    """
    if "item_tax_template" in row:
        return (row.get("item_tax_template") or "").strip()
    item_code = row.get("item_code") or row.get("item")
    return (inherited.get(item_code) or "").strip()


def _on_hand_bulk(item_codes: List[str]) -> Dict[str, float]:
    if not item_codes:
        return {}
    totals: Dict[str, float] = {}
    for row in frappe.get_all(
        "Bin",
        filters={"item_code": ["in", item_codes]},
        fields=["item_code", "actual_qty"],
        limit_page_length=0,
    ):
        totals[row["item_code"]] = totals.get(row["item_code"], 0.0) + float(row.get("actual_qty") or 0)
    return totals


def _last_paid_bulk(item_codes: List[str]) -> Dict[str, float]:
    """Rate last actually paid, from submitted invoice lines.

    Deliberately not ``Item.last_purchase_rate``: that field is also written by
    Purchase Orders, so it can show a price that was quoted but never paid.
    """
    if not item_codes:
        return {}
    latest: Dict[str, float] = {}
    for row in frappe.get_all(
        "Purchase Invoice Item",
        filters={"item_code": ["in", item_codes], "docstatus": 1},
        fields=["item_code", "rate", "creation"],
        order_by="creation desc",
        limit_page_length=0,
    ):
        latest.setdefault(row["item_code"], float(row.get("rate") or 0))
    return latest


def _get_item_uoms(item_code: str) -> List[Dict[str, Any]]:
    try:
        doc = frappe.get_doc("Item", item_code)
        uoms = []
        stock_uom = doc.get("stock_uom")
        # Always include stock UOM with factor 1
        uoms.append({"uom": stock_uom, "conversion_factor": 1})
        for row in (doc.get("uoms") or []):
            # avoid duplicate of stock uom
            if row.get("uom") == stock_uom:
                continue
            uoms.append({
                "uom": row.get("uom"),
                "conversion_factor": row.get("conversion_factor") or 1,
            })
        return uoms
    except Exception:
        return []


def _get_item_prices(item_code: str) -> List[Dict[str, Any]]:
    rows = frappe.get_all(
        "Item Price",
        filters={
            "price_list": STANDARD_BUYING,
            "item_code": item_code,
            "buying": 1,
        },
        fields=["uom", "price_list_rate"],
        order_by="uom asc",
    )
    # normalize
    return [{"uom": r.get("uom"), "rate": float(r.get("price_list_rate") or 0)} for r in rows]


@frappe.whitelist()
def get_item_details(item_code: str) -> Dict[str, Any]:
    _ensure_manager_access()
    item = frappe.get_doc("Item", item_code)
    return {
        "item_code": item.name,
        "item_name": item.item_name,
        "stock_uom": item.stock_uom,
        "uoms": _get_item_uoms(item.name),
        "prices": _get_item_prices(item.name),
        "item_tax_template": _item_tax_templates_bulk([item.name]).get(item.name),
    }


@frappe.whitelist()
def get_item_tax_templates(company: Optional[str] = None) -> List[Dict[str, Any]]:
    """Item Tax Templates a purchase line can carry, with their rates.

    ``rate`` is the sum of the template's rows, which is what the buyer thinks
    of as "the VAT %" — a template is almost always a single rate, and showing a
    number beats showing an opaque template name in a dropdown.

    ``is_default`` marks the one configured on Jarz POS Settings, so the picker
    can show which rate a line gets when nobody chooses. Additive: the shape of
    the list is otherwise unchanged.
    """
    _ensure_manager_access()
    filters: Dict[str, Any] = {"disabled": 0}
    if company:
        filters["company"] = company
    templates = frappe.get_all(
        "Item Tax Template",
        filters=filters,
        fields=["name", "title", "company"],
        order_by="title asc",
        limit_page_length=0,
    )
    if not templates:
        return []

    rates: Dict[str, List[Dict[str, Any]]] = {}
    for row in frappe.get_all(
        "Item Tax Template Detail",
        filters={"parent": ["in", [t["name"] for t in templates]]},
        fields=["parent", "tax_type", "tax_rate"],
        order_by="parent asc, idx asc",
        limit_page_length=0,
    ):
        rates.setdefault(row["parent"], []).append({
            "account": row.get("tax_type"),
            "rate": float(row.get("tax_rate") or 0),
        })

    default = _default_item_tax_template(company)
    for t in templates:
        rows = rates.get(t["name"], [])
        t["taxes"] = rows
        t["rate"] = sum(r["rate"] for r in rows)
        t["is_default"] = 1 if t["name"] == default else 0
    return templates


@frappe.whitelist()
def get_item_price(item_code: str, uom: Optional[str] = None) -> Dict[str, Any]:
    _ensure_manager_access()
    filters = {
        "price_list": STANDARD_BUYING,
        "item_code": item_code,
        "buying": 1,
    }
    if uom:
        filters["uom"] = uom
    row = frappe.get_all("Item Price", filters=filters, fields=["uom", "price_list_rate"], limit=1)
    if row:
        r = row[0]
        return {"uom": r.get("uom"), "rate": float(r.get("price_list_rate") or 0)}
    # no direct UOM price; try stock uom and return that for reference
    stock = frappe.db.get_value("Item", item_code, "stock_uom")
    row2 = frappe.get_all("Item Price", filters={"price_list": STANDARD_BUYING, "item_code": item_code, "buying": 1, "uom": stock}, fields=["price_list_rate"], limit=1)
    if row2:
        return {"uom": stock, "rate": float(row2[0].get("price_list_rate") or 0)}
    return {"uom": uom or stock, "rate": 0.0}


@frappe.whitelist()
def create_purchase_invoice(
    supplier: str,
    posting_date: Optional[str] = None,
    is_paid: int = 0,
    items: Optional[List[Dict[str, Any]]] = None,
    company: Optional[str] = None,
    payment_option: Optional[str] = None,
    shipping_amount: Optional[float] = None,
    bill_no: Optional[str] = None,
    bill_date: Optional[str] = None,
    taxes_template: Optional[str] = None,
    remarks: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create and submit a Purchase Invoice with ``update_stock=1``.

    ``items``: list of ``{item_code, qty, uom, rate?, warehouse?,
    item_tax_template?, material_request?, material_request_item?}``.  A missing
    rate is fetched from Standard Buying; a missing warehouse is resolved by
    :func:`resolve_purchase_warehouse`.

    ``item_tax_template`` is how a purchase carries VAT on only *some* lines —
    see :func:`_ensure_item_tax_rows` for why a template alone is not enough.
    Omit the key and the line inherits the Item master's template, else the site
    default on Jarz POS Settings; send it *empty* to keep the line untaxed
    (:func:`_resolve_line_tax_template`).
    ``taxes_template`` remains the whole-invoice alternative; the two compose,
    since a template-driven line simply overrides the invoice rate for its own
    accounts.

    When a line carries ``material_request_item`` ERPNext's own status updater
    writes ``received_qty`` back to the Material Request and recomputes
    ``per_received`` — that is what closes a team item request, and it only
    engages because ``update_stock`` is 1.

    ``is_paid`` pays the invoice directly on the document (no separate Payment
    Entry).  ``idempotency_key`` makes a retry safe: the same key returns the
    invoice created the first time instead of buying the goods twice.
    """
    _ensure_manager_access()
    if not supplier:
        frappe.throw(_("Supplier is required"))
    items = _coerce_rows(items)
    if not items:
        frappe.throw(_("At least one item is required"))

    # Idempotency: a double-tap on a slow connection used to create two
    # invoices, double stock and double cash out. The key is stored on the
    # invoice itself so the guard survives a worker restart.
    idempotency_key = (idempotency_key or "").strip()
    if idempotency_key:
        existing = frappe.db.get_value(
            "Purchase Invoice",
            {"custom_jarz_idempotency_key": idempotency_key, "docstatus": ["<", 2]},
            ["name", "status", "outstanding_amount"],
            as_dict=True,
        )
        if existing:
            return {
                "success": True,
                "purchase_invoice": existing["name"],
                "payment_entry": None,
                "status": existing.get("status"),
                "outstanding_amount": existing.get("outstanding_amount"),
                "deduplicated": True,
            }

    # Resolve default company if not provided
    resolved_company = company
    if not resolved_company:
        # Try user default company
        resolved_company = frappe.defaults.get_user_default("company")
    if not resolved_company:
        # Try global default company
        resolved_company = frappe.db.get_single_value("Global Defaults", "default_company")
    if not resolved_company:
        # If only one company exists, use it
        companies = frappe.get_all("Company", fields=["name"], limit=2)
        if len(companies) == 1:
            resolved_company = companies[0]["name"]
    if not resolved_company:
        frappe.throw(_("Default Company not set. Please configure a default Company."))

    _validate_bill_no(supplier, bill_no)

    pi = frappe.new_doc("Purchase Invoice")
    pi.company = resolved_company
    pi.supplier = supplier
    if posting_date:
        pi.posting_date = posting_date
        pi.set_posting_time = 1
    pi.update_stock = 1
    if idempotency_key:
        pi.custom_jarz_idempotency_key = idempotency_key
    if bill_no:
        pi.bill_no = bill_no.strip()
    if bill_date:
        pi.bill_date = bill_date
    if remarks:
        pi.remarks = remarks
    if taxes_template:
        pi.taxes_and_charges = taxes_template

    # Resolved once for every row that left the key out, rather than per row.
    # The app always sends it; this covers other callers of the endpoint.
    # Carries the item master's template, else the configured site default —
    # company-checked, so a default belonging to another company is simply not
    # applied instead of failing the purchase.
    inherited_tax = _item_tax_templates_bulk(
        [
            code
            for row in items
            if "item_tax_template" not in row
            and (code := row.get("item_code") or row.get("item"))
        ],
        company=resolved_company,
    )

    for row in items:
        item_code = row.get("item_code") or row.get("item")
        if not item_code:
            frappe.throw(_("Item code missing in row"))
        uom = row.get("uom")
        qty = float(row.get("qty") or 0)
        if qty <= 0:
            frappe.throw(_("Quantity must be > 0 for {0}").format(item_code))
        rate = row.get("rate")
        # Determine conversion_factor for selected UOM
        conv = 1
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
        if uom and uom != stock_uom:
            cf = frappe.db.get_value("UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor")
            conv = float(cf or 1)
        # Default price if not supplied
        if rate is None:
            rate_info = get_item_price(item_code, uom)
            rate = float(rate_info.get("rate") or 0)

        # An update_stock invoice with no warehouse lets ERPNext guess the
        # destination, which in a multi-branch setup files receipts under the
        # wrong branch. Always resolve one explicitly.
        warehouse = resolve_purchase_warehouse(
            item_code, resolved_company, row.get("warehouse")
        )

        line: Dict[str, Any] = {
            "item_code": item_code,
            "qty": qty,
            "uom": uom or stock_uom,
            "conversion_factor": conv,
            "rate": float(rate),
            "warehouse": warehouse,
        }

        # Per-line VAT: explicit line value (even empty) > item master > site
        # default > nothing. See _resolve_line_tax_template.
        tax_template = _resolve_line_tax_template(row, inherited_tax)
        if tax_template:
            _validate_item_tax_template(tax_template, resolved_company, item_code)
            line["item_tax_template"] = tax_template

        # Carrying these two makes ERPNext's own status updater write
        # received_qty back onto the Material Request and recompute
        # per_received — that is what closes a team item request.
        mr = (row.get("material_request") or "").strip() if row.get("material_request") else None
        mr_item = (row.get("material_request_item") or "").strip() if row.get("material_request_item") else None
        if mr and mr_item:
            _validate_request_link(mr, mr_item, item_code)
            line["material_request"] = mr
            line["material_request_item"] = mr_item

        pi.append("items", line)

    # Must run before the shipping row below: VAT is charged On Net Total, so
    # it is computed from the item lines and not from the freight charge.
    _ensure_item_tax_rows(pi, resolved_company)

    # Shipping as an Actual charge on Freight and Forwarding Charges.
    #
    # This block used to sit inside `try/except Exception: log_error()`, which
    # swallowed its own frappe.throw — so a missing freight account silently
    # dropped the charge and the buyer was told the purchase succeeded with the
    # shipping cost simply gone. Errors propagate now.
    amt = float(shipping_amount or 0)
    if amt > 0:
        account = _get_freight_and_forwarding_account(resolved_company)
        if not account:
            frappe.throw(
                _("Freight and Forwarding Charges account not found for company {0}. Please create or map it.").format(
                    resolved_company
                )
            )
        pi.append("taxes", {
            "category": "Valuation and Total",
            "add_deduct_tax": "Add",
            "charge_type": "Actual",
            "account_head": account,
            "description": "Shipping Expense",
            "tax_amount": amt,
        })

    # Direct payment on the Purchase Invoice itself (no separate Payment Entry).
    #
    # Same fix as above: the old `except Exception` caught the "no account
    # resolved" throw, left is_paid unset, and submitted the invoice as an open
    # payable — after the operator had already handed over the cash.
    if int(is_paid or 0):
        opt_raw = (payment_option or "cash").strip()
        opt_lower = opt_raw.lower()
        mop: Optional[str] = None
        account: Optional[str] = None

        # If payment_option matches a POS Profile name, use that profile's exact-named account
        if opt_raw and frappe.db.exists("POS Profile", opt_raw):
            mop = PAYMENT_MODES.CASH
            account = _get_exact_pos_profile_account(opt_raw, resolved_company) or _get_default_cash_account(resolved_company)
        elif opt_lower == "instapay":
            mop = "InstaPay"
            account = _get_mop_account_account(mop, resolved_company) or _get_default_bank_account(resolved_company)
        elif opt_lower in ("cash", "pos_profile"):
            mop = PAYMENT_MODES.CASH
            # If explicitly 'pos_profile', fall back to any session user's profile mapping; else default cash
            if opt_lower == "pos_profile":
                account = _get_pos_profile_cash_account(resolved_company) or _get_default_cash_account(resolved_company)
            else:
                account = _get_mop_account_account(mop, resolved_company) or _get_default_cash_account(resolved_company)
        else:
            # Unknown option: try as POS Profile, else default to Cash
            mop = PAYMENT_MODES.CASH
            account = _get_exact_pos_profile_account(opt_raw, resolved_company) or _get_default_cash_account(resolved_company)

        if not account:
            frappe.throw(
                _("No account resolved for payment option '{0}'. Configure the Mode of Payment or the POS Profile account.").format(opt_raw)
            )

        pi.is_paid = 1
        pi.mode_of_payment = mop
        # Field name in ERPNext PI is 'cash_bank_account'
        pi.cash_bank_account = account
        # paid_amount is deliberately left alone: grand_total is still 0 on an
        # unsaved doc, and AccountsController.calculate_paid_amount() fills it
        # from outstanding_amount during validate.

    pi.insert(ignore_permissions=False)
    pi.submit()

    # Close the loop for whoever asked for these goods. Runs after submit so the
    # request lines already carry the received_qty ERPNext just wrote.
    linked_request_items = [
        line.material_request_item for line in pi.items if line.get("material_request_item")
    ]
    if linked_request_items:
        from jarz_pos.api.purchase_request import notify_fulfilment

        notify_fulfilment(pi.name, linked_request_items)

    return {
        "success": True,
        "purchase_invoice": pi.name,
        "payment_entry": None,
        "status": pi.status,
        "outstanding_amount": pi.outstanding_amount,
        "deduplicated": False,
    }


def _validate_item_tax_template(template: str, company: str, item_code: str) -> None:
    """Reject a tax template that cannot legally apply to this line."""
    row = frappe.db.get_value(
        "Item Tax Template", template, ["company", "disabled"], as_dict=True
    )
    if not row:
        frappe.throw(_("Item Tax Template {0} does not exist.").format(template))
    if int(row.get("disabled") or 0):
        frappe.throw(_("Item Tax Template {0} is disabled.").format(template))
    if row.get("company") != company:
        frappe.throw(
            _("Item Tax Template {0} belongs to {1}, not {2} (item {3}).").format(
                template, row.get("company"), company, item_code
            )
        )


def _ensure_item_tax_rows(pi: Any, company: str) -> None:
    """Give every account named by a line's Item Tax Template a 0% tax row.

    Without this the whole feature is a silent no-op. ERPNext charges tax by
    walking ``doc.taxes`` and looking each row's ``account_head`` up in the
    line's item tax map (``calculate_taxes``) — a template on a line whose
    account has no row is never consulted, so the invoice submits with the VAT
    quietly missing.

    Rate 0 plus ``set_by_item_tax_template`` is what keeps this from taxing
    *everything*: ``get_item_tax_map`` skips flagged rows when building the base
    rate every line inherits, so an untemplated line stays at 0 while a
    templated one overrides its own accounts to the real rate. That is exactly
    "we buy some items with VAT".

    ERPNext has this same routine (``append_taxes_from_item_tax_template``) but
    it cannot be relied on: it is gated behind an Accounts Settings checkbox,
    and it bails out entirely once ``taxes`` is non-empty — which the shipping
    row, or a chosen invoice-level template, can make it.
    """
    templates = sorted(
        {line.get("item_tax_template") for line in pi.items if line.get("item_tax_template")}
    )
    if not templates:
        return

    existing = {row.account_head for row in (pi.get("taxes") or [])}
    for template in templates:
        for detail in frappe.get_cached_doc("Item Tax Template", template).taxes:
            account = detail.tax_type
            if not account or account in existing:
                continue
            # A cross-company account would be rejected by ERPNext's own
            # validation with a far less obvious message.
            if frappe.get_cached_value("Account", account, "company") != company:
                continue
            pi.append(
                "taxes",
                {
                    "charge_type": "On Net Total",
                    "account_head": account,
                    "rate": 0,
                    "description": account,
                    "set_by_item_tax_template": 1,
                    "category": "Total",
                    "add_deduct_tax": "Add",
                },
            )
            existing.add(account)


def _validate_bill_no(supplier: str, bill_no: Optional[str]) -> None:
    """Enforce the supplier bill number when settings demand it.

    ERPNext already refuses a duplicate ``bill_no`` for the same supplier — but
    only when one is supplied. Without this the guard never fires, so the same
    supplier invoice can be entered twice by two different people.
    """
    bill_no = (bill_no or "").strip()
    if bill_no:
        return
    try:
        required = int(
            frappe.db.get_single_value("Jarz POS Settings", "purchase_require_bill_no") or 0
        )
    except Exception:
        required = 0
    if required:
        frappe.throw(_("Supplier bill number is required for {0}.").format(supplier))


def _validate_request_link(material_request: str, material_request_item: str, item_code: str) -> None:
    """Reject a request link that does not belong to the line it is attached to.

    A mismatched link would make ERPNext credit the wrong request line as
    received, quietly closing a request nobody actually fulfilled.
    """
    row = frappe.db.get_value(
        "Material Request Item",
        material_request_item,
        ["parent", "item_code"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Request line {0} no longer exists.").format(material_request_item))
    if row.get("parent") != material_request:
        frappe.throw(
            _("Request line {0} does not belong to request {1}.").format(
                material_request_item, material_request
            )
        )
    if row.get("item_code") != item_code:
        frappe.throw(
            _("Request line {0} is for item {1}, not {2}.").format(
                material_request_item, row.get("item_code"), item_code
            )
        )
    docstatus = frappe.db.get_value("Material Request", material_request, "docstatus")
    if int(docstatus or 0) != 1:
        frappe.throw(_("Request {0} is not open.").format(material_request))


@frappe.whitelist()
def get_purchase_invoices(
    limit: int = 50,
    page: int = 0,
    supplier: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """Return recent Purchase Invoices for the history tab.

    Filters are all optional and combine with AND. ``search`` matches the
    invoice name or the supplier's bill number.
    """
    _ensure_manager_access()
    limit = max(1, min(int(limit or 50), 200))
    start = max(0, int(page or 0)) * limit

    filters: Dict[str, Any] = {}
    if supplier:
        filters["supplier"] = supplier
    if status:
        filters["status"] = status
    if from_date and to_date:
        filters["posting_date"] = ["between", [from_date, to_date]]
    elif from_date:
        filters["posting_date"] = [">=", from_date]
    elif to_date:
        filters["posting_date"] = ["<=", to_date]

    or_filters: List[Any] = []
    if search:
        like = f"%{search}%"
        or_filters = [["name", "like", like], ["bill_no", "like", like]]

    invoices = frappe.get_all(
        "Purchase Invoice",
        filters=filters,
        or_filters=or_filters,
        fields=[
            "name", "supplier", "supplier_name", "posting_date",
            "grand_total", "status", "docstatus", "creation",
            "is_paid", "outstanding_amount", "bill_no", "bill_date",
            "currency", "net_total", "total_taxes_and_charges",
        ],
        order_by="posting_date desc, creation desc",
        limit_page_length=limit,
        limit_start=start,
    )

    # Count with the same filters — the old unfiltered frappe.db.count() made
    # the client's "load more" arithmetic wrong the moment anything was filtered.
    total = frappe.db.count("Purchase Invoice", filters=filters) if not or_filters else len(
        frappe.get_all(
            "Purchase Invoice",
            filters=filters,
            or_filters=or_filters,
            fields=["name"],
            limit_page_length=0,
        )
    )

    # One query for every line on the page instead of one query per invoice.
    names = [inv["name"] for inv in invoices]
    lines_by_parent: Dict[str, List[Dict[str, Any]]] = {name: [] for name in names}
    if names:
        for row in frappe.get_all(
            "Purchase Invoice Item",
            filters={"parent": ["in", names]},
            fields=[
                "parent", "item_code", "item_name", "qty", "uom", "rate", "amount",
                "warehouse", "item_tax_template",
            ],
            order_by="parent asc, idx asc",
            limit_page_length=0,
        ):
            lines_by_parent.setdefault(row["parent"], []).append(row)

    # The tax rows themselves, not just the total. `total_taxes_and_charges`
    # lumps VAT together with the shipping charge — reporting that figure as
    # "VAT" would overstate it by the freight on every delivered purchase.
    taxes_by_parent: Dict[str, List[Dict[str, Any]]] = {name: [] for name in names}
    if names:
        for row in frappe.get_all(
            "Purchase Taxes and Charges",
            filters={"parent": ["in", names], "parenttype": "Purchase Invoice"},
            fields=["parent", "account_head", "description", "rate", "tax_amount", "charge_type"],
            order_by="parent asc, idx asc",
            limit_page_length=0,
        ):
            taxes_by_parent.setdefault(row["parent"], []).append(row)

    for inv in invoices:
        inv["items"] = lines_by_parent.get(inv["name"], [])
        inv["taxes"] = taxes_by_parent.get(inv["name"], [])

    return {"invoices": invoices, "total": total}


@frappe.whitelist()
def create_supplier(
    supplier_name: str,
    supplier_group: Optional[str] = None,
    phone: Optional[str] = None,
    tax_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a supplier without leaving the app.

    A new vendor turning up mid-purchase used to mean abandoning the cart and
    opening Desk.
    """
    _ensure_manager_access()
    supplier_name = (supplier_name or "").strip()
    if not supplier_name:
        frappe.throw(_("Supplier name is required"))

    existing = frappe.db.get_value("Supplier", {"supplier_name": supplier_name}, "name")
    if existing:
        frappe.throw(_("Supplier {0} already exists.").format(supplier_name))

    group = (supplier_group or "").strip()
    if not group:
        group = frappe.db.get_single_value("Buying Settings", "supplier_group") or frappe.db.get_value(
            "Supplier Group", {"is_group": 0}, "name"
        )
    if not group:
        frappe.throw(_("No Supplier Group is configured. Create one first."))

    doc = frappe.new_doc("Supplier")
    doc.supplier_name = supplier_name
    doc.supplier_group = group
    if tax_id:
        doc.tax_id = tax_id.strip()
    doc.insert()

    if phone:
        try:
            contact = frappe.new_doc("Contact")
            contact.first_name = supplier_name
            contact.append("phone_nos", {"phone": phone.strip(), "is_primary_phone": 1})
            contact.append(
                "links", {"link_doctype": "Supplier", "link_name": doc.name}
            )
            contact.insert()
        except Exception:
            # The supplier itself is created and usable; a contact failure must
            # not lose it. Logged so a broken contact path stays visible.
            frappe.log_error(frappe.get_traceback(), "create_supplier: contact creation failed")

    return {
        "success": True,
        "supplier": {
            "name": doc.name,
            "supplier_name": doc.supplier_name,
            "supplier_group": doc.supplier_group,
            "disabled": 0,
        },
    }


@frappe.whitelist()
def get_supplier_groups(limit: int = 50) -> List[Dict[str, Any]]:
    _ensure_manager_access()
    return frappe.get_all(
        "Supplier Group",
        filters={"is_group": 0},
        fields=["name"],
        order_by="name asc",
        limit_page_length=max(1, min(int(limit or 50), 200)),
    )


@frappe.whitelist()
def get_purchase_taxes_templates(company: Optional[str] = None) -> Dict[str, Any]:
    """Purchase tax templates plus the configured default (e.g. the VAT one)."""
    _ensure_manager_access()
    filters: Dict[str, Any] = {"disabled": 0}
    if company:
        filters["company"] = company
    templates = frappe.get_all(
        "Purchase Taxes and Charges Template",
        filters=filters,
        fields=["name", "title", "company", "is_default"],
        order_by="is_default desc, title asc",
        limit_page_length=0,
    )
    default = frappe.db.get_single_value("Jarz POS Settings", "purchase_default_taxes_template")
    if not default:
        for row in templates:
            if int(row.get("is_default") or 0):
                default = row["name"]
                break
    return {"templates": templates, "default": default}


@frappe.whitelist()
def pay_purchase_invoice(
    purchase_invoice: str,
    payment_option: Optional[str] = None,
    amount: Optional[float] = None,
    posting_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Settle an outstanding (credit) purchase with a Payment Entry.

    The counterpart to buying on supplier terms: `create_purchase_invoice` with
    ``is_paid=0`` leaves a payable, and this is how it gets cleared later.
    """
    _ensure_manager_access()
    if not purchase_invoice:
        frappe.throw(_("Purchase Invoice is required"))

    pi = frappe.get_doc("Purchase Invoice", purchase_invoice)
    if pi.docstatus != 1:
        frappe.throw(_("Only a submitted purchase invoice can be paid."))
    outstanding = float(pi.outstanding_amount or 0)
    if outstanding <= 0:
        frappe.throw(_("{0} has nothing outstanding.").format(purchase_invoice))

    pay_amount = float(amount) if amount is not None else outstanding
    if pay_amount <= 0:
        frappe.throw(_("Payment amount must be greater than zero."))
    if pay_amount - outstanding > 0.005:
        frappe.throw(
            _("Payment of {0} exceeds the outstanding {1}.").format(pay_amount, outstanding)
        )

    account = _resolve_payment_account(payment_option, pi.company)
    mode = _resolve_payment_mode(payment_option, pi.company)

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    # party_amount drives grand_total, paid/received amounts and the reference
    # allocation in one go. Setting those fields by hand instead would leave
    # paid_from_account_currency / account_type inconsistent with the account.
    pe = get_payment_entry(
        "Purchase Invoice",
        purchase_invoice,
        party_amount=pay_amount,
        bank_account=account,
    )
    pe.mode_of_payment = mode
    if posting_date:
        pe.posting_date = posting_date
    pe.flags.ignore_permissions = True
    pe.insert()
    pe.submit()

    pi.reload()
    return {
        "success": True,
        "payment_entry": pe.name,
        "purchase_invoice": pi.name,
        "status": pi.status,
        "outstanding_amount": pi.outstanding_amount,
    }


@frappe.whitelist()
def return_purchase_invoice(
    purchase_invoice: str,
    items: Optional[List[Dict[str, Any]]] = None,
    reason: Optional[str] = None,
    posting_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Send goods back to the supplier — a debit note.

    ``items``: ``[{purchase_invoice_item, qty}]`` where ``qty`` is the positive
    quantity being returned. Omit entirely to return the whole invoice.
    """
    _ensure_manager_access()
    if not purchase_invoice:
        frappe.throw(_("Purchase Invoice is required"))

    source = frappe.get_doc("Purchase Invoice", purchase_invoice)
    if source.docstatus != 1:
        frappe.throw(_("Only a submitted purchase invoice can be returned."))
    if int(source.get("is_return") or 0):
        frappe.throw(_("{0} is itself a return.").format(purchase_invoice))

    rows = _coerce_rows(items)
    # {source line name: positive qty to send back}
    requested: Dict[str, float] = {}
    for row in rows:
        line_name = str(row.get("purchase_invoice_item") or row.get("name") or "").strip()
        qty = float(row.get("qty") or 0)
        if not line_name:
            frappe.throw(_("Return row is missing the invoice line reference."))
        if qty <= 0:
            frappe.throw(_("Return quantity must be greater than zero."))
        requested[line_name] = requested.get(line_name, 0.0) + qty

    from erpnext.controllers.sales_and_purchase_return import make_return_doc

    ret = make_return_doc("Purchase Invoice", purchase_invoice)
    if posting_date:
        ret.posting_date = posting_date
        ret.set_posting_time = 1
    if reason:
        ret.remarks = reason

    if requested:
        kept = []
        for line in ret.items:
            # The source-line reference on a Purchase Invoice return is
            # `purchase_invoice_item` (see get_return_against_item_fields).
            # There is no `pi_detail` column on this doctype.
            source_line = line.get("purchase_invoice_item")
            if source_line not in requested:
                continue

            # make_return_doc has already set qty to the *remaining* returnable
            # amount, net of anything sent back on an earlier debit note, and
            # negative. So this is the authoritative cap — no separate query.
            remaining = abs(float(line.qty or 0))
            want = requested[source_line]
            if want - remaining > 0.005:
                frappe.throw(
                    _("Cannot return {0} of line {1}: only {2} remain un-returned.").format(
                        want, source_line, remaining
                    )
                )
            factor = float(line.conversion_factor or 1)
            line.qty = -want
            line.received_qty = -want
            line.stock_qty = -want * factor
            kept.append(line)

        missing = set(requested) - {line.get("purchase_invoice_item") for line in kept}
        if missing:
            frappe.throw(
                _("These lines are not returnable on {0}: {1}").format(
                    purchase_invoice, ", ".join(sorted(missing))
                )
            )
        ret.items = kept
        for idx, line in enumerate(ret.items, start=1):
            line.idx = idx

    ret.flags.ignore_permissions = True
    ret.insert()
    ret.submit()

    source.reload()
    return {
        "success": True,
        "return_invoice": ret.name,
        "purchase_invoice": source.name,
        "status": source.status,
        "grand_total": ret.grand_total,
    }


def _resolve_payment_account(payment_option: Optional[str], company: str) -> str:
    opt_raw = (payment_option or "cash").strip()
    opt_lower = opt_raw.lower()
    account: Optional[str] = None
    if opt_raw and frappe.db.exists("POS Profile", opt_raw):
        account = _get_exact_pos_profile_account(opt_raw, company) or _get_default_cash_account(company)
    elif opt_lower == "instapay":
        account = _get_mop_account_account("InstaPay", company) or _get_default_bank_account(company)
    else:
        account = _get_mop_account_account(PAYMENT_MODES.CASH, company) or _get_default_cash_account(company)
    if not account:
        frappe.throw(
            _("No account resolved for payment option '{0}'. Configure the Mode of Payment or the POS Profile account.").format(opt_raw)
        )
    return account


def _resolve_payment_mode(payment_option: Optional[str], company: str) -> str:
    opt_lower = (payment_option or "cash").strip().lower()
    return "InstaPay" if opt_lower == "instapay" else PAYMENT_MODES.CASH


def _get_freight_and_forwarding_account(company: str) -> Optional[str]:
    """Resolve the 'Freight and Forwarding Charges' expense account for the company.

    Prefer exact account named "Freight and Forwarding Charges - <Company Abbr>".
    Fallback: any non-group Account in the company with account_name exactly 'Freight and Forwarding Charges'.
    """
    try:
        abbr = frappe.db.get_value("Company", company, "abbr") or ""
        if abbr:
            exact = f"{ACCOUNTS.FREIGHT_AND_FORWARDING} - {abbr}"
            if frappe.db.exists("Account", exact):
                acc = frappe.get_doc("Account", exact)
                if acc.company == company and int(acc.is_group or 0) == 0:
                    return exact
        rows = frappe.get_all(
            "Account",
            filters={"company": company, "is_group": 0, "account_name": ACCOUNTS.FREIGHT_AND_FORWARDING},
            fields=["name"],
            limit=1,
        )
        if rows:
            return rows[0]["name"]
    except Exception:
        frappe.log_error(frappe.get_traceback(), title="_get_freight_and_forwarding_account failed")
    return None


def _get_pos_profile_cash_account(company: str) -> Optional[str]:
    """Return the Account named exactly `<POS Profile> - <Company Abbr>`.

    Fallback: if exact-named Account is missing, try POS Profile's Cash payment method default account.
    """
    try:
        user = frappe.session.user
        profile_names = frappe.get_all("POS Profile User", filters={"user": user}, pluck="parent")
        if not profile_names:
            return None
        profiles = frappe.get_all(
            "POS Profile",
            filters={"name": ["in", profile_names], "disabled": 0, "company": company},
            pluck="name",
        )
        if not profiles:
            return None
        profile = profiles[0]

        # Exact-named Account: "<POS Profile> - <Company Abbr>"
        abbr = frappe.db.get_value("Company", company, "abbr") or ""
        if abbr:
            account_name = f"{profile} - {abbr}"
            if frappe.db.exists("Account", account_name):
                acc = frappe.get_doc("Account", account_name)
                if acc.company == company and int(acc.is_group or 0) == 0:
                    return account_name

        # Fallback to POS Payment Method default Cash account
        rows = frappe.get_all(
            "POS Payment Method",
            filters={"parent": profile, "mode_of_payment": PAYMENT_MODES.CASH},
            fields=["default_account"],
            limit=1,
        )
        if rows and rows[0].get("default_account"):
            return rows[0]["default_account"]
    except Exception:
        frappe.log_error(frappe.get_traceback(), title="_get_pos_profile_cash_account failed")
    return None


def _get_exact_pos_profile_account(profile_name: str, company: str) -> Optional[str]:
    """Resolve the exact-named Account for the given POS Profile within the target company.

    Prefers "<POS Profile> - <Company Abbr>". Falls back to the profile's POS Payment Method default Cash account.
    """
    try:
        if not frappe.db.exists("POS Profile", profile_name):
            return None
        abbr = frappe.db.get_value("Company", company, "abbr") or ""
        if abbr:
            account_name = f"{profile_name} - {abbr}"
            if frappe.db.exists("Account", account_name):
                acc = frappe.get_doc("Account", account_name)
                if acc.company == company and int(acc.is_group or 0) == 0:
                    return account_name
        # Fallback to profile's Cash method default account
        row = frappe.get_all(
            "POS Payment Method",
            filters={"parent": profile_name, "mode_of_payment": PAYMENT_MODES.CASH},
            fields=["default_account"],
            limit=1,
        )
        if row and row[0].get("default_account"):
            return row[0]["default_account"]
    except Exception:
        frappe.log_error(frappe.get_traceback(), title="_get_exact_pos_profile_account failed")
    return None


def _get_mop_account_account(mode_of_payment: str, company: str) -> Optional[str]:
    try:
        rows = frappe.get_all(
            "Mode of Payment Account",
            filters={"parent": mode_of_payment, "company": company},
            fields=["default_account"],
            limit=1,
        )
        if rows and rows[0].get("default_account"):
            return rows[0]["default_account"]
    except Exception:
        frappe.log_error(frappe.get_traceback(), title="_get_mop_account_account failed")
    return None


def _get_default_cash_account(company: str) -> Optional[str]:
    try:
        return frappe.db.get_value("Company", company, "default_cash_account")
    except Exception:
        return None


def _get_default_bank_account(company: str) -> Optional[str]:
    try:
        return frappe.db.get_value("Company", company, "default_bank_account")
    except Exception:
        return None
