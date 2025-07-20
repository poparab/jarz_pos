import frappe

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
    """
    Return all enabled Jarz Bundles with their selectable items.
    
    For each bundle, this will return the item groups and the quantity
    of items to be selected from each group, along with the actual items
    available within those groups.
    """
    meta = frappe.get_meta('Jarz Bundle')
    has_disabled = any(df.fieldname == 'disabled' for df in meta.fields)
    filters = {'disabled': 0} if has_disabled else {}

    bundles = frappe.get_all(
        'Jarz Bundle',
        filters=filters,
        fields=['name as id', 'bundle_name as name', 'bundle_price as price'],
    )

    try:
        price_list = frappe.db.get_value('POS Profile', profile, 'selling_price_list')
    except Exception:
        price_list = None

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

            if price_list:
                for item in items_in_group:
                    rate = frappe.db.get_value(
                        'Item Price',
                        {'price_list': price_list, 'item_code': item['id']},
                        'price_list_rate'
                    )
                    if rate is not None:
                        item['price'] = rate

            processed_groups.append({
                'group_name': group_info['item_group'],
                'quantity': group_info['quantity'],
                'items': items_in_group
            })
            
        b['item_groups'] = processed_groups

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
    except Exception:
        wh = None

    if wh:
        for itm in items:
            qty = frappe.db.get_value('Bin', {'warehouse': wh, 'item_code': itm['id']}, 'actual_qty') or 0
            itm['qty'] = qty

    return items 