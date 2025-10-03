import frappe
from frappe import _
from typing import List, Dict, Any, Optional


STANDARD_BUYING = "Standard Buying"


def _ensure_manager_access():
    roles = set(frappe.get_roles())
    allowed_roles = {
        "System Manager",
        "Purchase Manager",
        "Accounts Manager",
        "Stock Manager",
    }
    if not roles.intersection(allowed_roles):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


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
def search_items(search: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_manager_access()
    filters = {
        "disabled": 0,
        "is_purchase_item": 1,
        "has_variants": 0,
    }
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
    items = frappe.get_all("Item", filters=filters, or_filters=or_filters, fields=fields, limit=limit, order_by="modified desc")
    # Attach price (Standard Buying) and UOMs
    for it in items:
        it["uoms"] = _get_item_uoms(it["item_code"])
        it["prices"] = _get_item_prices(it["item_code"])  # list by UOM
    return items


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
    }


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
) -> Dict[str, Any]:
    """
    Create and submit a Purchase Invoice with update_stock=1.
    items: list of {item_code, qty, uom, rate(optional)}. If rate missing, fetch from Standard Buying.
    If is_paid is truthy, create and submit a Payment Entry to pay the invoice fully.
    """
    _ensure_manager_access()
    if not supplier:
        frappe.throw(_("Supplier is required"))
    if not items:
        frappe.throw(_("At least one item is required"))

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

    pi = frappe.new_doc("Purchase Invoice")
    pi.company = resolved_company
    pi.supplier = supplier
    if posting_date:
        pi.posting_date = posting_date
        pi.set_posting_time = 1
    pi.update_stock = 1

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
        # Append row
        pi.append("items", {
            "item_code": item_code,
            "qty": qty,
            "uom": uom or stock_uom,
            "conversion_factor": conv,
            "rate": float(rate),
        })

    # Add shipping as an Actual charge on Freight and Forwarding Charges (Valuation and Total)
    try:
        amt = float(shipping_amount or 0)
        if amt > 0:
            account = _get_freight_and_forwarding_account(resolved_company)
            if not account:
                frappe.throw(_("Freight and Forwarding Charges account not found for company {0}. Please create or map it.").format(resolved_company))
            pi.append("taxes", {
                "category": "Valuation and Total",
                "add_deduct_tax": "Add",
                "charge_type": "Actual",
                "account_head": account,
                "description": "Shipping Expense",
                "tax_amount": amt,
            })
    except Exception:
        frappe.log_error(frappe.get_traceback(), title="create_purchase_invoice: add shipping failed")

    # Handle direct payment on the Purchase Invoice itself (no separate Payment Entry)
    if int(is_paid or 0):
        try:
            mop: Optional[str] = None
            account: Optional[str] = None
            opt_raw = (payment_option or "cash").strip()
            opt_lower = opt_raw.lower()

            # If payment_option matches a POS Profile name, use that profile's exact-named account
            if opt_raw and frappe.db.exists("POS Profile", opt_raw):
                mop = "Cash"
                account = _get_exact_pos_profile_account(opt_raw, resolved_company) or _get_default_cash_account(resolved_company)
            elif opt_lower == "instapay":
                mop = "InstaPay"
                account = _get_mop_account_account(mop, resolved_company) or _get_default_bank_account(resolved_company)
            elif opt_lower in ("cash", "pos_profile"):
                mop = "Cash"
                # If explicitly 'pos_profile', fall back to any session user's profile mapping; else default cash
                if opt_lower == "pos_profile":
                    account = _get_pos_profile_cash_account(resolved_company) or _get_default_cash_account(resolved_company)
                else:
                    account = _get_mop_account_account(mop, resolved_company) or _get_default_cash_account(resolved_company)
            else:
                # Unknown option: try as POS Profile, else default to Cash
                mop = "Cash"
                account = _get_exact_pos_profile_account(opt_raw, resolved_company) or _get_default_cash_account(resolved_company)

            if not account:
                frappe.throw(_(f"No account resolved for payment option '{opt_raw}'. Configure Mode of Payment or Profile account."))

            # Set payment fields on PI before submit
            pi.is_paid = 1
            pi.mode_of_payment = mop
            # Field name in ERPNext PI is 'cash_bank_account'
            pi.cash_bank_account = account
            try:
                pi.paid_amount = pi.grand_total
            except Exception:
                pass
        except Exception:
            frappe.log_error(frappe.get_traceback(), title="create_purchase_invoice: set is_paid fields failed")

    pi.insert(ignore_permissions=False)
    pi.submit()

    return {
        "success": True,
        "purchase_invoice": pi.name,
        "payment_entry": None,
        "status": pi.status,
        "outstanding_amount": pi.outstanding_amount,
    }


def _get_freight_and_forwarding_account(company: str) -> Optional[str]:
    """Resolve the 'Freight and Forwarding Charges' expense account for the company.

    Prefer exact account named "Freight and Forwarding Charges - <Company Abbr>".
    Fallback: any non-group Account in the company with account_name exactly 'Freight and Forwarding Charges'.
    """
    try:
        abbr = frappe.db.get_value("Company", company, "abbr") or ""
        if abbr:
            exact = f"Freight and Forwarding Charges - {abbr}"
            if frappe.db.exists("Account", exact):
                acc = frappe.get_doc("Account", exact)
                if acc.company == company and int(acc.is_group or 0) == 0:
                    return exact
        rows = frappe.get_all(
            "Account",
            filters={"company": company, "is_group": 0, "account_name": "Freight and Forwarding Charges"},
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
            filters={"parent": profile, "mode_of_payment": "Cash"},
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
            filters={"parent": profile_name, "mode_of_payment": "Cash"},
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
