import frappe
from frappe.utils import flt
from typing import Optional

@frappe.whitelist(allow_guest=False)
def get_pos_profiles():
    """Return list of POS Profile names enabled for the current user.

    In ERPNext a user is linked to a profile via the child table
    `POS Profile User` not a direct field on the parent doc. We therefore
    first fetch all rows of that child table for the session user and then
    collect their parent POS Profile names that are **not disabled**.
    """
    user = frappe.session.user

    linked_rows = frappe.get_all(
        'POS Profile User',
        filters={'user': user},
        pluck='parent',  # returns list[str]
    )

    if not linked_rows:
        return []

    profiles = frappe.get_all(
        'POS Profile',
        filters={
            'name': ('in', linked_rows),
            'disabled': 0,
        },
        pluck='name',
    )
    return profiles

@frappe.whitelist(allow_guest=False)
def get_profile_bundles(profile: str):
    """Return bundles with items available to the given POS profile."""
    
    # For now, just get all available bundles
    # Future: filter by POS profile permissions
    filters = {}

    bundles = frappe.get_all(
        'Jarz Bundle',
        filters=filters,
        fields=['name as id', 'bundle_name as name', 'bundle_price as price', 'free_shipping'],
    )

    for b in bundles:
        bundle_item_groups = frappe.get_all(
            'Jarz Bundle Item Group',
            filters={'parent': b['id']},
            fields=['item_group', 'quantity'],
            order_by='idx'
        )
        
        processed_groups = []
        for group_info in bundle_item_groups:
            items_in_group = frappe.get_all(
                'Item',
                filters={'item_group': group_info['item_group'], 'disabled': 0, 'is_sales_item': 1},
                fields=['name as id', 'item_name as name', 'standard_rate as price'],
            )

            # Get warehouse from POS profile for stock quantities
            # try:
            #     wh = frappe.db.get_value('POS Profile', profile, 'warehouse')
            # except Exception:
            #     wh = None

            # Use selling price list linked to POS profile when available
            try:
                price_list = frappe.db.get_value('POS Profile', profile, 'selling_price_list')
            except Exception:
                price_list = None

            if price_list:
                for item in items_in_group:
                    rate = frappe.db.get_value(
                        'Item Price',
                        {'price_list': price_list, 'item_code': item['id']},
                        'price_list_rate'
                    )
                    if rate is not None:
                        item['price'] = rate

            # attach stock qty per POS profile warehouse if defined (same as main items)
            try:
                wh = frappe.db.get_value('POS Profile', profile, 'warehouse')
                print(f"Bundle items API - Profile: {profile} - Warehouse: {wh}")
            except Exception:
                wh = None
                print(f"Bundle items API - Profile: {profile} - Warehouse: None (error)")

            if wh:
                for item in items_in_group:
                    qty = frappe.db.get_value('Bin', {'warehouse': wh, 'item_code': item['id']}, 'actual_qty') or 0
                    # Debug: Log the stock fetching for comparison
                    print(f"Bundle item {item['name']} (ID: {item['id']}) - Warehouse: {wh} - Stock: {qty}")
                    item['qty'] = qty
                    item['actual_qty'] = qty  # Add both fields for consistency

            processed_groups.append({
                'group_name': group_info['item_group'],
                'quantity': group_info['quantity'],
                'items': items_in_group
            })
            
        b['item_groups'] = processed_groups
        # Normalize flag for clients
        try:
            b['free_shipping'] = 1 if int(b.get('free_shipping') or 0) else 0
        except Exception:
            b['free_shipping'] = 0

    return bundles

@frappe.whitelist(allow_guest=False)
def get_profile_products(profile: str):
    """Return items whose item_group is allowed for the given POS profile."""
    # ERPNext v14+: child DocType exists; earlier/forked instances may not
    try:
        item_groups = frappe.get_all(
            'POS Profile Item Group',
            filters={'parent': profile},
            pluck='item_group',
        )
    except Exception:
        # Fallback: read the item_groups child table directly from the profile doc
        try:
            p_doc = frappe.get_cached_doc('POS Profile', profile)
            item_groups = [row.item_group for row in getattr(p_doc, 'item_groups', [])]
        except Exception:
            item_groups = []

    if not item_groups:
        return []

    items = frappe.get_all(
        'Item',
        filters={'item_group': ('in', item_groups), 'disabled': 0, 'is_sales_item': 1},
        fields=[
            'name as id',
            'item_name as name',
            'standard_rate as price',  # fallback
            'item_group',
        ],
    )

    # Use selling price list linked to POS profile when available
    try:
        price_list = frappe.db.get_value('POS Profile', profile, 'selling_price_list')
    except Exception:
        price_list = None

    if price_list:
        for itm in items:
            rate = frappe.db.get_value('Item Price', {
                'price_list': price_list,
                'item_code': itm['id'],
            }, 'price_list_rate') or 0
            if rate:
                itm['price'] = rate

    # attach stock qty per POS profile warehouse if defined
    try:
        wh = frappe.db.get_value('POS Profile', profile, 'warehouse')
        print(f"Main items API - Profile: {profile} - Warehouse: {wh}")
    except Exception:
        wh = None
        print(f"Main items API - Profile: {profile} - Warehouse: None (error)")

    if wh:
        for itm in items:
            qty = frappe.db.get_value('Bin', {'warehouse': wh, 'item_code': itm['id']}, 'actual_qty') or 0
            # Debug: Log the stock fetching for comparison
            print(f"Main item {itm['name']} (ID: {itm['id']}) - Warehouse: {wh} - Stock: {qty}")
            itm['qty'] = qty

    return items 


@frappe.whitelist(allow_guest=False)
def get_sales_partners(search: Optional[str] = None, limit: int = 10):
    """Return a short, touch-friendly list of Sales Partners.

    Args:
        search: Optional search text to filter by name/partner_name (case-insensitive LIKE)
        limit: Max number of partners to return (default 10)

    Returns: List of { name, partner_name, title }
    """
    # Some ERPNext versions have 'enabled' instead of 'disabled' on Sales Partner
    filters = {}
    try:
        if frappe.db.has_column("Sales Partner", "enabled"):
            filters["enabled"] = 1
        elif frappe.db.has_column("Sales Partner", "disabled"):
            filters["disabled"] = 0
    except Exception:
        # If meta check fails, proceed without status filter
        pass

    # Ensure limit is an int (Frappe may pass query args as strings)
    try:
        limit_i = int(limit) if limit else 10
    except Exception:
        limit_i = 10

    # Apply simple search on name or partner_name
    # Note: LIKE filters use % wildcard in MariaDB
    try:
        if search:
            like = f"%{search}%"
            partners = frappe.get_all(
                "Sales Partner",
                filters=filters,
                or_filters=[
                    ["Sales Partner", "name", "like", like],
                    ["Sales Partner", "partner_name", "like", like],
                ],
                fields=["name", "partner_name"],
                order_by="partner_name asc",
                limit_page_length=limit_i,
            )
        else:
            partners = frappe.get_all(
                "Sales Partner",
                filters=filters,
                fields=["name", "partner_name"],
                order_by="partner_name asc",
                limit_page_length=limit_i,
            )
    except Exception as err:
        frappe.log_error(f"get_sales_partners failed: {err}", "Jarz POS get_sales_partners")
        partners = []

    # Add a unified display title used by the mobile client
    for p in partners:
        p["title"] = p.get("partner_name") or p.get("name")
    return partners


@frappe.whitelist(allow_guest=False)
def get_pos_profile_account_balance(profile: str):
    """Return the cash account balance linked to a POS Profile.

    The project convention stores one cash Drawer Account per POS profile using
    the same name (optionally suffixed with the company abbreviation). This
    helper resolves the account and aggregates posted GL entries to expose the
    live drawer balance for the mobile client header.
    """

    profile_name = (profile or "").strip()
    if not profile_name:
        frappe.throw("POS profile is required")

    profile_doc = frappe.db.get_value("POS Profile", profile_name, ["name", "company"], as_dict=True)
    if not profile_doc:
        frappe.throw(f"POS Profile '{profile_name}' was not found")

    company = profile_doc.get("company")
    company_abbr = frappe.db.get_value("Company", company, "abbr") if company else None

    candidate_accounts = [profile_name]
    if company_abbr and not profile_name.endswith(f" - {company_abbr}"):
        candidate_accounts.append(f"{profile_name} - {company_abbr}")

    account_doc = None
    for candidate in candidate_accounts:
        account_doc = frappe.db.get_value("Account", candidate, ["name", "account_currency"], as_dict=True)
        if account_doc:
            break

    if not account_doc:
        account_doc = frappe.db.get_value(
            "Account",
            {"account_name": profile_name, "company": company} if company else {"account_name": profile_name},
            ["name", "account_currency"],
            as_dict=True,
        )

    if not account_doc:
        frappe.throw(f"Account matching POS profile '{profile_name}' was not found")

    balance_row = frappe.db.sql(
        """
        select
            coalesce(sum(debit), 0) - coalesce(sum(credit), 0) as balance
        from `tabGL Entry`
        where account = %s and docstatus = 1
        """,
        account_doc["name"],
        as_dict=True,
    )

    balance_value = flt(balance_row[0]["balance"]) if balance_row else 0.0

    currency = (
        account_doc.get("account_currency")
        or (company and frappe.db.get_value("Company", company, "default_currency"))
        or frappe.defaults.get_global_default("currency")
    )

    return {
        "profile": profile_name,
        "account": account_doc["name"],
        "balance": balance_value,
        "currency": currency,
    }