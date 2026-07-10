"""B2B Price List management API for Jarz POS (role-gated).

Whitelisted endpoints powering the Flutter Pricing screen: browse price lists and
their category (item-group) prices + per-item overrides, see which customers each
list serves, and (for managers) edit category prices, per-item overrides and the
customer<->list assignment.

Design / data model (frozen contract v2):
  - Category price  = ONE ``Jarz Price List Category Rate`` row per (price_list,
    item_group). This is the source of truth the order resolver
    (``services/invoice_creation.py::_resolve_item_rate``) falls back to when an item
    has no per-item row. It is NOT an Item Price row — this ERPNext build makes
    ``Item Price.item_code`` / ``uom`` mandatory with no ``item_group`` field, so
    category rates need their own app-owned table.
  - Per-item override = a per-item generic ``Item Price`` row (``item_code`` set, no
    ``customer``). Takes precedence over the category row in the resolver.
  - Customer<->list assignment = native ``Customer.default_price_list``. When empty the
    customer inherits its ``Customer Group.default_price_list`` (a customer's own default
    always wins over the group default).

Access:
  - WRITES  -> FULL managers only (``_ensure_full_manager_pricing_access``: manager-pricing
    access AND B2B access). This excludes the JARZ line manager (no B2B) and B2B reps
    (no pricing); net editors are JARZ Manager + System Manager/Administrator. Raises
    ``frappe.PermissionError`` otherwise.
  - READS   -> managers (incl. line managers) OR B2B Sales Reps (read-only). Combines the
    manager-pricing gate with ``api.crm._can_access_b2b`` so reps/line-mgrs browse but never edit.

All writes are idempotent and ``frappe.db.exists`` / get-or-update guarded, mirroring the
upsert pattern in ``scripts/seed_example_b2b_prices.py::_upsert_item_price``. Customer-scoped
Item Price rows are NEVER surfaced in these generic reads/writes.
"""

import frappe

from jarz_pos.api.pos import (
    _has_manager_pricing_access,
)
from jarz_pos.api.crm import _can_access_b2b


_DEFAULT_CURRENCY = "EGP"

# App-owned category-rate table (one row per price_list + item_group).
_CATEGORY_RATE_DOCTYPE = "Jarz Price List Category Rate"


# ---------------------------------------------------------------------------
# Guards / small helpers
# ---------------------------------------------------------------------------
def _ensure_pricing_read_access():
    """Raise unless the caller is a manager or a B2B Sales Rep (read-only)."""
    if _has_manager_pricing_access():
        return
    if _can_access_b2b():
        return
    frappe.throw(
        "Not permitted: pricing read access requires a manager or B2B Sales Rep role.",
        frappe.PermissionError,
    )


def _ensure_full_manager_pricing_access():
    """Raise unless the caller is a FULL manager (may EDIT prices).

    Editing prices is a B2B/commercial function, so it requires BOTH manager
    pricing access AND B2B access. This deliberately EXCLUDES the JARZ line
    manager (who has pricing access but is walled off from B2B) and B2B Sales
    Reps (who have B2B access but no pricing rights — read-only). The net
    editor is the JARZ Manager (Administrator qualifies too, since it implicitly
    holds every role).
    """
    if _has_manager_pricing_access() and _can_access_b2b():
        return
    frappe.throw(
        "Not permitted: editing prices requires a full manager role "
        "(JARZ Manager). Line managers and B2B reps have read-only pricing access.",
        frappe.PermissionError,
    )


def _doctype_exists(name):
    try:
        return bool(frappe.db.exists("DocType", name))
    except Exception:
        return False


def _has_field(doctype, fieldname):
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


def _price_list_currency(price_list):
    """Currency of a Price List, falling back to the default currency."""
    try:
        cur = frappe.db.get_value("Price List", price_list, "currency")
        return (cur or "").strip() or _DEFAULT_CURRENCY
    except Exception:
        return _DEFAULT_CURRENCY


def _default_selling_price_list():
    """The site default selling price list (Selling Settings), or None."""
    try:
        return (
            frappe.db.get_single_value("Selling Settings", "selling_price_list") or None
        )
    except Exception:
        return None


def _num(value):
    """Coerce a DB numeric to float, or None when unset."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Category / override / customer resolution helpers (generic — no customer rows)
# ---------------------------------------------------------------------------
def _pricing_categories():
    """Item Groups that contain at least one enabled, sellable item.

    Returns ``[{"item_group", "item_count"}]`` sorted by item_group. These are the
    rows the pricing UI renders as editable category prices.
    """
    try:
        rows = frappe.get_all(
            "Item",
            filters={"disabled": 0, "is_sales_item": 1},
            fields=["item_group", "count(name) as item_count"],
            group_by="item_group",
            order_by="item_group asc",
        )
    except Exception:
        rows = []
    out = []
    for r in rows:
        group = r.get("item_group")
        if not group:
            continue
        out.append({"item_group": group, "item_count": int(r.get("item_count") or 0)})
    return out


def _category_rate(price_list, item_group):
    """Rate of the ``Jarz Price List Category Rate`` row for (price_list, item_group)."""
    try:
        return _num(
            frappe.db.get_value(
                _CATEGORY_RATE_DOCTYPE,
                {"price_list": price_list, "item_group": item_group},
                "rate",
            )
        )
    except Exception:
        return None


def _find_category_price(price_list, item_group):
    """Name of the existing ``Jarz Price List Category Rate`` row, or None."""
    try:
        rows = frappe.get_all(
            _CATEGORY_RATE_DOCTYPE,
            filters={"price_list": price_list, "item_group": item_group},
            pluck="name",
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _find_item_override(price_list, item_code):
    """Name of the existing generic per-item Item Price row, or None."""
    try:
        rows = frappe.get_all(
            "Item Price",
            filters={
                "item_code": item_code,
                "price_list": price_list,
                "customer": ["in", [None, ""]],
            },
            pluck="name",
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _category_rows_for_list(price_list):
    """All pricing categories annotated with this list's rate (num|null)."""
    rows = []
    for cat in _pricing_categories():
        rows.append(
            {
                "item_group": cat["item_group"],
                "rate": _category_rate(price_list, cat["item_group"]),
                "item_count": cat["item_count"],
            }
        )
    return rows


def _item_overrides_for_list(price_list):
    """Per-item generic overrides in a price list (item_code set, no customer)."""
    try:
        rows = frappe.get_all(
            "Item Price",
            filters={
                "price_list": price_list,
                "customer": ["in", [None, ""]],
                "item_code": ["is", "set"],
            },
            fields=["item_code", "item_name", "item_group", "price_list_rate"],
            order_by="item_group asc, item_code asc",
        )
    except Exception:
        rows = []
    out = []
    for r in rows:
        code = r.get("item_code")
        if not code:
            continue
        out.append(
            {
                "item_code": code,
                "item_name": r.get("item_name")
                or frappe.db.get_value("Item", code, "item_name")
                or code,
                "item_group": r.get("item_group")
                or frappe.db.get_value("Item", code, "item_group"),
                "rate": _num(r.get("price_list_rate")) or 0.0,
            }
        )
    return out


def _customers_for_price_list(price_list):
    """Customers served by a price list: direct assignment UNION group-derived.

    - direct: ``Customer.default_price_list == price_list``.
    - group : the customer has NO own default list AND its Customer Group's
      ``default_price_list == price_list`` (own default always wins, so these never
      overlap the direct set).

    Returns ``[{"customer","customer_name","assignment","customer_group"}]``.
    """
    out = []
    seen = set()

    # 1. Direct assignments.
    try:
        direct = frappe.get_all(
            "Customer",
            filters={"default_price_list": price_list},
            fields=["name", "customer_name", "customer_group"],
            order_by="customer_name asc",
        )
    except Exception:
        direct = []
    for c in direct:
        out.append(
            {
                "customer": c.get("name"),
                "customer_name": c.get("customer_name") or c.get("name"),
                "assignment": "direct",
                "customer_group": c.get("customer_group"),
            }
        )
        seen.add(c.get("name"))

    # 2. Group-derived: customers with no own default in a group defaulting to this list.
    try:
        groups = frappe.get_all(
            "Customer Group",
            filters={"default_price_list": price_list},
            pluck="name",
        )
    except Exception:
        groups = []
    if groups:
        try:
            derived = frappe.get_all(
                "Customer",
                filters={
                    "customer_group": ["in", groups],
                    "default_price_list": ["in", [None, ""]],
                },
                fields=["name", "customer_name", "customer_group"],
                order_by="customer_name asc",
            )
        except Exception:
            derived = []
        for c in derived:
            if c.get("name") in seen:
                continue
            out.append(
                {
                    "customer": c.get("name"),
                    "customer_name": c.get("customer_name") or c.get("name"),
                    "assignment": "group",
                    "customer_group": c.get("customer_group"),
                }
            )
            seen.add(c.get("name"))

    return out


def _customer_effective_list(customer):
    """Resolve (effective_price_list, assignment) for a customer.

    Cascade mirrors ``api.pos.resolve_customer_price_list``: own default wins, else the
    customer group's default, else none. assignment is "direct" | "group" | "none".
    """
    own = frappe.db.get_value("Customer", customer, "default_price_list")
    own = (own or "").strip() or None
    if own:
        return own, "direct"
    group = frappe.db.get_value("Customer", customer, "customer_group")
    if group:
        gpl = frappe.db.get_value("Customer Group", group, "default_price_list")
        gpl = (gpl or "").strip() or None
        if gpl:
            return gpl, "group"
    return None, "none"


# ---------------------------------------------------------------------------
# Reads (managers + B2B reps)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_price_lists():
    """All selling price lists with customer counts + category price rows.

    Shape:
        {"price_lists": [
            {"name","currency","enabled":bool,"is_default":bool,"customer_count":int,
             "categories":[{"item_group","rate":num|null,"item_count":int}]}
        ]}
    """
    _ensure_pricing_read_access()

    default_list = _default_selling_price_list()
    try:
        lists = frappe.get_all(
            "Price List",
            filters={"selling": 1},
            fields=["name", "currency", "enabled"],
            order_by="name asc",
        )
    except Exception:
        lists = []

    out = []
    for pl in lists:
        name = pl.get("name")
        out.append(
            {
                "name": name,
                "currency": pl.get("currency") or _DEFAULT_CURRENCY,
                "enabled": bool(pl.get("enabled")),
                "is_default": name == default_list,
                "customer_count": len(_customers_for_price_list(name)),
                "categories": _category_rows_for_list(name),
            }
        )
    return {"price_lists": out}


@frappe.whitelist()
def get_price_list_detail(price_list):
    """Full detail of one price list: categories, per-item overrides, customers.

    Shape:
        {"name","currency","enabled","is_default",
         "categories":[{"item_group","rate":num|null,"item_count":int}],
         "item_overrides":[{"item_code","item_name","item_group","rate":num}],
         "customers":[{"customer","customer_name","assignment","customer_group"}]}
    """
    _ensure_pricing_read_access()

    price_list = (price_list or "").strip()
    if not price_list or not frappe.db.exists("Price List", price_list):
        frappe.throw(f"Price List '{price_list}' not found.")

    row = frappe.db.get_value(
        "Price List", price_list, ["currency", "enabled"], as_dict=True
    ) or {}

    return {
        "name": price_list,
        "currency": row.get("currency") or _DEFAULT_CURRENCY,
        "enabled": bool(row.get("enabled")),
        "is_default": price_list == _default_selling_price_list(),
        "categories": _category_rows_for_list(price_list),
        "item_overrides": _item_overrides_for_list(price_list),
        "customers": _customers_for_price_list(price_list),
    }


@frappe.whitelist()
def get_customer_pricing(customer):
    """Reverse ("double entry") view: what pricing does a customer actually get.

    Shape:
        {"customer","customer_name","customer_group",
         "effective_price_list":str|null,"assignment":"direct"|"group"|"none",
         "prices":[{"item_group","item_code":str|null,"item_name":str|null,
                    "rate":num,"source":"override"|"category"|"none"}]}
    """
    _ensure_pricing_read_access()

    customer = (customer or "").strip()
    if not customer or not frappe.db.exists("Customer", customer):
        frappe.throw(f"Customer '{customer}' not found.")

    info = frappe.db.get_value(
        "Customer", customer, ["customer_name", "customer_group"], as_dict=True
    ) or {}
    effective, assignment = _customer_effective_list(customer)

    prices = []
    if effective:
        # One row per pricing category: the category rate for the effective list, or a
        # "none" row (rate 0) when that category has no configured price yet.
        for cat in _pricing_categories():
            rate = _category_rate(effective, cat["item_group"])
            prices.append(
                {
                    "item_group": cat["item_group"],
                    "item_code": None,
                    "item_name": None,
                    "rate": rate if rate is not None else 0.0,
                    "source": "category" if rate is not None else "none",
                }
            )
        # Plus every per-item override in the effective list.
        for ov in _item_overrides_for_list(effective):
            prices.append(
                {
                    "item_group": ov["item_group"],
                    "item_code": ov["item_code"],
                    "item_name": ov["item_name"],
                    "rate": ov["rate"],
                    "source": "override",
                }
            )

    return {
        "customer": customer,
        "customer_name": info.get("customer_name") or customer,
        "customer_group": info.get("customer_group"),
        "effective_price_list": effective,
        "assignment": assignment,
        "prices": prices,
    }


@frappe.whitelist()
def list_pricing_categories():
    """Item Groups that contain enabled sellable items (category rows).

    Shape: {"categories":[{"item_group","item_count":int}]}
    """
    _ensure_pricing_read_access()
    return {"categories": _pricing_categories()}


@frappe.whitelist()
def search_b2b_customers(query=""):
    """Search Company customers (type=Company) for the pricing screen.

    Shape: {"customers":[{"customer","customer_name","customer_group",
                          "default_price_list":str|null}]}
    """
    _ensure_pricing_read_access()

    filters = {}
    if _has_field("Customer", "customer_type"):
        filters["customer_type"] = "Company"

    or_filters = None
    q = (query or "").strip()
    if q:
        or_filters = {
            "name": ["like", f"%{q}%"],
            "customer_name": ["like", f"%{q}%"],
        }

    try:
        rows = frappe.get_all(
            "Customer",
            filters=filters,
            or_filters=or_filters,
            fields=["name", "customer_name", "customer_group", "default_price_list"],
            order_by="customer_name asc",
            limit_page_length=50,
        )
    except Exception:
        rows = []

    return {
        "customers": [
            {
                "customer": r.get("name"),
                "customer_name": r.get("customer_name") or r.get("name"),
                "customer_group": r.get("customer_group"),
                "default_price_list": (r.get("default_price_list") or "").strip()
                or None,
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Writes (managers only)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def create_price_list(price_list_name, currency=_DEFAULT_CURRENCY):
    """Create a selling price list (idempotent). Returns {"name"}.

    selling=1, buying=0, enabled=1. If the list already exists it is returned as-is
    (never overwritten), mirroring the get-or-create seeding pattern.
    """
    _ensure_full_manager_pricing_access()

    price_list_name = (price_list_name or "").strip()
    if not price_list_name:
        frappe.throw("price_list_name is required.")
    currency = (currency or "").strip() or _DEFAULT_CURRENCY

    if frappe.db.exists("Price List", price_list_name):
        return {"name": price_list_name}

    doc = frappe.get_doc(
        {
            "doctype": "Price List",
            "price_list_name": price_list_name,
            "selling": 1,
            "buying": 0,
            "enabled": 1,
            "currency": currency,
        }
    )
    doc.insert(ignore_permissions=True)
    return {"name": doc.name}


@frappe.whitelist()
def set_category_price(price_list, item_group, rate):
    """Upsert the category rate for (price_list, item_group).

    Stored as a single ``Jarz Price List Category Rate`` row (NOT an Item Price — no
    per-item fan-out). A ``rate`` of null/empty DELETES the category row (clears the
    category price). Returns {"ok": True}.
    """
    _ensure_full_manager_pricing_access()

    price_list = (price_list or "").strip()
    item_group = (item_group or "").strip()
    if not price_list or not frappe.db.exists("Price List", price_list):
        frappe.throw(f"Price List '{price_list}' not found.")
    if not item_group or not frappe.db.exists("Item Group", item_group):
        frappe.throw(f"Item Group '{item_group}' not found.")

    existing = _find_category_price(price_list, item_group)

    if rate in (None, ""):
        if existing:
            frappe.delete_doc(_CATEGORY_RATE_DOCTYPE, existing, ignore_permissions=True)
        return {"ok": True}

    rate = float(rate)
    if existing:
        frappe.db.set_value(
            _CATEGORY_RATE_DOCTYPE, existing, "rate", rate, update_modified=True
        )
        return {"ok": True}

    doc = frappe.get_doc(
        {
            "doctype": _CATEGORY_RATE_DOCTYPE,
            "price_list": price_list,
            "item_group": item_group,
            "rate": rate,
            "currency": _price_list_currency(price_list),
        }
    )
    doc.insert(ignore_permissions=True)
    return {"ok": True}


@frappe.whitelist()
def set_item_override(price_list, item_code, rate=None):
    """Upsert (or delete) a per-item generic Item Price override.

    A ``rate`` of null/empty DELETES the override (item reverts to its category rate).
    Otherwise the single generic per-item row (no customer) is created/updated.
    Returns {"ok": True}.
    """
    _ensure_full_manager_pricing_access()

    price_list = (price_list or "").strip()
    item_code = (item_code or "").strip()
    if not price_list or not frappe.db.exists("Price List", price_list):
        frappe.throw(f"Price List '{price_list}' not found.")
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(f"Item '{item_code}' not found.")

    existing = _find_item_override(price_list, item_code)

    if rate in (None, ""):
        if existing:
            frappe.delete_doc("Item Price", existing, ignore_permissions=True)
        return {"ok": True}

    rate = float(rate)
    if existing:
        frappe.db.set_value(
            "Item Price", existing, "price_list_rate", rate, update_modified=True
        )
        return {"ok": True}

    doc = frappe.get_doc(
        {
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": price_list,
            "price_list_rate": rate,
            "selling": 1,
            "currency": _price_list_currency(price_list),
        }
    )
    doc.insert(ignore_permissions=True)
    return {"ok": True}


@frappe.whitelist()
def assign_customer_to_price_list(customer, price_list=None):
    """Set (or clear) ``Customer.default_price_list``.

    A null/empty ``price_list`` clears the assignment, reverting the customer to its
    Customer Group's default price list. Returns {"ok": True}.
    """
    _ensure_full_manager_pricing_access()

    customer = (customer or "").strip()
    if not customer or not frappe.db.exists("Customer", customer):
        frappe.throw(f"Customer '{customer}' not found.")

    price_list = (price_list or "").strip() or None
    if price_list and not frappe.db.exists("Price List", price_list):
        frappe.throw(f"Price List '{price_list}' not found.")

    frappe.db.set_value(
        "Customer", customer, "default_price_list", price_list, update_modified=True
    )
    return {"ok": True}
