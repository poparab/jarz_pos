import frappe
from frappe import _
from frappe.utils import flt
from typing import Optional

from jarz_pos.constants import QUERY_LIMITS, ROLES
from jarz_pos.utils.invoice_utils import sanitize_printable_text


_MANAGER_PRICING_ROLES = {
    ROLES.JARZ_MANAGER,
    "JARZ line manager",
    ROLES.JARZ_LINE_MANAGER,
}


def _has_manager_pricing_access() -> bool:
    roles = {
        str(role or "").strip()
        for role in (frappe.get_roles(frappe.session.user) or [])
        if str(role or "").strip()
    }
    return bool(roles.intersection(_MANAGER_PRICING_ROLES))


def _ensure_manager_pricing_access() -> None:
    if not _has_manager_pricing_access():
        frappe.throw(_("Not permitted: manager pricing access required"), frappe.PermissionError)


def _normalize_price_list_name(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    return cleaned or None


def _resolve_effective_price_list(profile: str, requested_price_list: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    default_price_list = _normalize_price_list_name(
        frappe.db.get_value("POS Profile", profile, "selling_price_list")
    )
    requested = _normalize_price_list_name(requested_price_list)

    if requested and requested != default_price_list:
        _ensure_manager_pricing_access()

    effective_price_list = requested or default_price_list
    if effective_price_list and not frappe.db.exists("Price List", effective_price_list):
        frappe.throw(_("Price List {0} was not found").format(effective_price_list))

    return effective_price_list, default_price_list


def _get_item_price_from_price_list(item_code: str, price_list: Optional[str]) -> Optional[float]:
    if not price_list:
        return None

    rate = frappe.db.get_value(
        "Item Price",
        {"price_list": price_list, "item_code": item_code},
        "price_list_rate",
    )
    if rate is None:
        return None
    return flt(rate)


def _get_valid_sales_item_codes(item_codes):
    """Return item codes that are enabled and allowed for sales."""
    item_codes = [item_code for item_code in item_codes if item_code]
    if not item_codes:
        return set()

    return set(
        frappe.get_all(
            'Item',
            filters={'name': ('in', item_codes), 'disabled': 0, 'is_sales_item': 1},
            pluck='name',
        )
    )

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
        fields=['name'],
    )

    # Attach custom_allow_delivery_partner flag if the custom field exists
    has_dp_field = False
    try:
        has_dp_field = bool(frappe.get_meta('POS Profile').get_field('custom_allow_delivery_partner'))
    except Exception:
        pass

    result = []
    for p in profiles:
        row = p['name']
        if has_dp_field:
            allow_dp = frappe.db.get_value('POS Profile', p['name'], 'custom_allow_delivery_partner')
            result.append({'name': p['name'], 'allow_delivery_partner': bool(allow_dp)})
        else:
            result.append(row)
    return result


@frappe.whitelist(allow_guest=False)
def get_pos_price_lists(profile: str):
    """Return POS-selectable selling price lists for manager pricing flows."""
    from jarz_pos.utils.validation_utils import assert_pos_profile_enabled

    assert_pos_profile_enabled(profile)
    _ensure_manager_pricing_access()

    default_price_list = _normalize_price_list_name(
        frappe.db.get_value("POS Profile", profile, "selling_price_list")
    )
    profile_currency = _normalize_price_list_name(
        frappe.db.get_value("POS Profile", profile, "currency")
    )

    rows = frappe.get_all(
        "Price List",
        filters={"enabled": 1, "selling": 1},
        fields=["name", "currency"],
        limit_page_length=QUERY_LIMITS.DEFAULT_LIST,
        order_by="name asc",
    )

    has_zero_shipping_flag = False
    has_display_label = False
    try:
        has_zero_shipping_flag = bool(frappe.db.has_column("Price List", "custom_jarz_zero_shipping_default"))
    except Exception:
        has_zero_shipping_flag = False
    try:
        has_display_label = bool(frappe.db.has_column("Price List", "custom_jarz_price_override_label"))
    except Exception:
        has_display_label = False

    results = []
    seen = set()
    for row in rows:
        name = _normalize_price_list_name(row.get("name"))
        if not name or name in seen:
            continue
        if profile_currency and row.get("currency") and row.get("currency") != profile_currency:
            continue

        payload = {
            "name": name,
            "currency": row.get("currency") or profile_currency,
            "is_default": name == default_price_list,
            "zero_shipping_default": False,
            "display_label": name,
        }
        if has_zero_shipping_flag:
            payload["zero_shipping_default"] = bool(
                frappe.db.get_value("Price List", name, "custom_jarz_zero_shipping_default")
            )
        if has_display_label:
            payload["display_label"] = (
                frappe.db.get_value("Price List", name, "custom_jarz_price_override_label")
                or name
            )

        results.append(payload)
        seen.add(name)

    if default_price_list and default_price_list not in seen:
        results.insert(
            0,
            {
                "name": default_price_list,
                "currency": profile_currency,
                "is_default": True,
                "zero_shipping_default": False,
                "display_label": default_price_list,
            },
        )

    return results


@frappe.whitelist(allow_guest=False)
def get_commercial_policies(profile: str | None = None):
    """Return enabled commercial policies (order purposes) the current user may apply.

    Manager-gated, mirroring get_pos_price_lists. The Flutter cart uses this to render
    the Order Purpose selector. Policies that require a role the user lacks are omitted.
    """
    _ensure_manager_pricing_access()

    if not frappe.db.exists("DocType", "Jarz Commercial Policy"):
        return []

    profile_company = None
    if profile:
        profile_company = frappe.db.get_value("POS Profile", profile, "company")

    user_roles = set(frappe.get_roles(frappe.session.user) or [])
    rows = frappe.get_all(
        "Jarz Commercial Policy",
        filters={"enabled": 1},
        fields=[
            "name", "policy_name", "order_purpose", "price_list",
            "discount_percentage", "shipping_income_behavior",
            "shipping_expense_behavior", "courier_behavior",
            "require_role", "company", "pos_profile",
        ],
        order_by="priority asc, policy_name asc",
        limit_page_length=QUERY_LIMITS.DEFAULT_LIST,
    )

    results = []
    for row in rows:
        # Skip Standard (inert) and out-of-scope / not-permitted policies.
        if (row.get("order_purpose") or "Standard") == "Standard":
            continue
        if row.get("pos_profile") and profile and row.get("pos_profile") != profile:
            continue
        if row.get("company") and profile_company and row.get("company") != profile_company:
            continue
        require_role = (row.get("require_role") or "").strip()
        if require_role and require_role not in user_roles:
            continue
        results.append({
            "name": row.get("name"),
            "policy_name": row.get("policy_name"),
            "order_purpose": row.get("order_purpose"),
            "price_list": row.get("price_list"),
            "discount_percentage": float(row.get("discount_percentage") or 0),
            "waives_shipping_income": (row.get("shipping_income_behavior") == "Zero"),
            "no_courier": (row.get("courier_behavior") == "No Courier"),
        })
    return results


@frappe.whitelist(allow_guest=False)
def resolve_customer_price_list(customer: str, pos_profile: str | None = None):
    """Resolve the effective selling price list (B2B tier) for a customer.

    Cascade: Customer.default_price_list → Customer Group.default_price_list → None.
    The Flutter cart calls this when a B2B customer is selected under a customer-group
    driven order purpose (one whose policy has no fixed price list) so it can show the
    correct tier prices before checkout. Returns {"price_list": <name|null>}.
    """
    result = {"price_list": None}
    if not customer or not frappe.db.exists("Customer", customer):
        return result
    pl = frappe.db.get_value("Customer", customer, "default_price_list")
    if not pl:
        group = frappe.db.get_value("Customer", customer, "customer_group")
        if group:
            pl = frappe.db.get_value("Customer Group", group, "default_price_list")
    pl = (pl or "").strip() or None
    # Only surface enabled selling price lists.
    if pl and not frappe.db.get_value("Price List", pl, "selling"):
        pl = None
    result["price_list"] = pl
    return result


@frappe.whitelist(allow_guest=False)
def get_profile_bundles(profile: str, price_list: Optional[str] = None):
    """Return bundles with items available to the given POS profile."""
    from jarz_pos.utils.validation_utils import assert_pos_profile_enabled
    assert_pos_profile_enabled(profile)

    effective_price_list, _default_price_list = _resolve_effective_price_list(
        profile,
        requested_price_list=price_list,
    )

    # For now, just get all available bundles
    # Future: filter by POS profile permissions
    filters = {}
    try:
        if frappe.db.has_column('Jarz Bundle', 'disabled'):
            filters['disabled'] = 0
    except Exception:
        pass

    bundles = frappe.get_all(
        'Jarz Bundle',
        filters=filters,
        fields=['name as id', 'bundle_name as name', 'bundle_price as price', 'free_shipping', 'erpnext_item'],
    )

    valid_bundle_item_codes = _get_valid_sales_item_codes(
        [bundle.get('erpnext_item') for bundle in bundles]
    )

    filtered_bundles = []

    for b in bundles:
        if not b.get('erpnext_item') or b['erpnext_item'] not in valid_bundle_item_codes:
            continue

        bundle_item_groups = frappe.get_all(
            'Jarz Bundle Item Group',
            filters={'parent': b['id']},
            fields=['name', 'idx', 'item_group', 'quantity'],
            order_by='idx'
        )

        processed_groups = []
        bundle_has_empty_required_group = False
        for group_info in bundle_item_groups:
            items_in_group = frappe.get_all(
                'Item',
                filters={'item_group': group_info['item_group'], 'disabled': 0, 'is_sales_item': 1},
                fields=['name as id', 'item_name as name', 'standard_rate as price', 'allow_negative_stock'],
            )

            if not items_in_group:
                bundle_has_empty_required_group = True
                break

            # Get warehouse from POS profile for stock quantities
            # try:
            #     wh = frappe.db.get_value('POS Profile', profile, 'warehouse')
            # except Exception:
            #     wh = None

            if effective_price_list:
                for item in items_in_group:
                    rate = _get_item_price_from_price_list(item['id'], effective_price_list)
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

            for item in items_in_group:
                item['allow_negative_stock'] = bool(int(item.get('allow_negative_stock') or 0))

            processed_groups.append({
                'group_name': group_info['item_group'],
                'group_key': group_info.get('name') or f"{group_info['item_group']}::{group_info.get('idx') or (len(processed_groups) + 1)}",
                'group_index': group_info.get('idx') or (len(processed_groups) + 1),
                'quantity': group_info['quantity'],
                'items': items_in_group
            })

        if bundle_has_empty_required_group:
            continue

        if effective_price_list:
            bundle_rate = _get_item_price_from_price_list(b['erpnext_item'], effective_price_list)
            if bundle_rate is not None:
                b['price'] = bundle_rate

        b['item_groups'] = processed_groups
        b['parent_item_code'] = b.get('erpnext_item')
        b['price_list'] = effective_price_list
        # Normalize flag for clients
        try:
            b['free_shipping'] = 1 if int(b.get('free_shipping') or 0) else 0
        except Exception:
            b['free_shipping'] = 0

        filtered_bundles.append(b)

    return filtered_bundles

@frappe.whitelist(allow_guest=False)
def get_profile_products(profile: str, price_list: Optional[str] = None):
    """Return items whose item_group is allowed for the given POS profile."""
    effective_price_list, _default_price_list = _resolve_effective_price_list(
        profile,
        requested_price_list=price_list,
    )

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
            'allow_negative_stock',
        ],
    )

    if effective_price_list:
        for itm in items:
            rate = _get_item_price_from_price_list(itm['id'], effective_price_list)
            if rate is not None:
                itm['price'] = rate
            itm['price_list'] = effective_price_list

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

    for itm in items:
        itm['allow_negative_stock'] = bool(int(itm.get('allow_negative_stock') or 0))

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

@frappe.whitelist(allow_guest=False)
def is_pos_profile_open(pos_profile: str):
    """
    Check if the given POS Profile is currently open based on its timetable.
    Returns True if within opening hours, False otherwise.
    """
    from datetime import datetime, timedelta
    
    # Get the POS Profile Timetable
    timetable_doc = frappe.get_value(
        'POS Profile Timetable',
        {'pos_profile': pos_profile},
        ['name'],
        as_dict=True
    )
    
    if not timetable_doc:
        # If no timetable is configured, assume the profile is always open
        return {'is_open': True, 'message': 'No timetable configured'}
    
    # Get current datetime
    now = datetime.now()
    current_day = now.strftime('%A')  # Monday, Tuesday, etc.
    current_time = now.time()
    
    # Get the timetable entries for this POS Profile
    day_timings = frappe.get_all(
        'POS Profile Day Timing',
        filters={
            'parent': timetable_doc['name'],
            'day': current_day
        },
        fields=['opening_time', 'closing_time', 'same_day']
    )
    
    if not day_timings:
        # No schedule for today, branch is closed
        return {'is_open': False, 'message': f'Branch is closed on {current_day}'}
    
    # Check if current time falls within any of the day's time slots
    for timing in day_timings:
        opening_time = timing['opening_time']
        closing_time = timing['closing_time']
        same_day = timing.get('same_day', 'Same Day')
        
        # Convert to datetime.time objects if needed
        if isinstance(opening_time, str):
            opening_time = datetime.strptime(opening_time, '%H:%M:%S').time()
        elif isinstance(opening_time, timedelta):
            total_secs = int(opening_time.total_seconds())
            hours, remainder = divmod(total_secs, 3600)
            minutes, seconds = divmod(remainder, 60)
            opening_time = datetime.strptime(f'{hours:02d}:{minutes:02d}:{seconds:02d}', '%H:%M:%S').time()
        if isinstance(closing_time, str):
            closing_time = datetime.strptime(closing_time, '%H:%M:%S').time()
        elif isinstance(closing_time, timedelta):
            total_secs = int(closing_time.total_seconds())
            hours, remainder = divmod(total_secs, 3600)
            minutes, seconds = divmod(remainder, 60)
            closing_time = datetime.strptime(f'{hours:02d}:{minutes:02d}:{seconds:02d}', '%H:%M:%S').time()
        
        # Handle "Next Day" scenario (e.g., closing time is after midnight)
        if same_day == 'Next Day':
            # If closing time is "next day", we need to check if:
            # 1. Current time is after opening time today, OR
            # 2. Current time is before closing time (which represents early morning of next day)
            if current_time >= opening_time or current_time < closing_time:
                return {'is_open': True, 'message': 'Branch is open'}
        else:
            # Same day - normal case
            if opening_time <= current_time <= closing_time:
                return {'is_open': True, 'message': 'Branch is open'}
    
    return {'is_open': False, 'message': f'Branch is closed at this time'}


# ---------------------------------------------------------------------------
# Receipt configuration
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def get_receipt_config():
    """Return receipt branding/config from Jarz POS Settings.

    Mobile app calls this once on startup (or after profile change) to
    populate receipt templates without hard-coding text in the APK.
    """
    defaults = {
        "header": "ORDER RECEIPT",
        "footer": "Thank you for Your Order",
        "phone": "01061332266",
        "website": "www.orderjarz.com",
        "logo": "",
    }
    try:
        from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings
        s = get_jarz_settings()
        return {
            "header": sanitize_printable_text(s.receipt_header_text) or defaults["header"],
            "footer": sanitize_printable_text(s.receipt_footer_text) or defaults["footer"],
            "phone": sanitize_printable_text(s.receipt_phone) or defaults["phone"],
            "website": sanitize_printable_text(s.receipt_website) or defaults["website"],
            "logo": (s.receipt_logo or "").strip() or defaults["logo"],
        }
    except Exception:
        return defaults


@frappe.whitelist(allow_guest=False)
def get_territory_pos_profile(customer: str):
    """Return the POS Profile mapped to the given customer's territory.

    Response shape::

        {
            "customer": "<customer name>",
            "territory": "<territory name or null>",
            "territory_pos_profile": "<pos profile name or null>"
        }
    """
    from jarz_pos.utils.invoice_utils import resolve_territory_pos_profile

    customer = (customer or "").strip()
    territory = frappe.db.get_value("Customer", customer, "territory") if customer else None
    territory_profile = resolve_territory_pos_profile(customer) if customer else None
    return {
        "customer": customer,
        "territory": territory or None,
        "territory_pos_profile": territory_profile,
    }