"""Purchasing setup — warehouse routing defaults and the default purchase VAT.

Idempotent, create-only. Safe to run on every ``bench migrate`` via the
``after_migrate`` hook: nothing already configured is ever overwritten, and a
missing warehouse is skipped rather than throwing.

Why this is code and not a hand-edit on the server: the routing table decides
where purchased stock physically lands. Setting it by hand on staging and again
on production is exactly how the two drift apart, and a mis-set warehouse is
invisible until someone counts stock. Seeding it from git means both
environments derive the same configuration from the same commit.

The warehouse names below are the Jarz chart of accounts as of 2026-08-03. They
are *defaults*: an operator who edits Jarz POS Settings afterwards keeps their
change, because every write here is guarded on the field being empty.
"""

from typing import Optional

import frappe

LOGGER_NAME = "purchase_setup"

SETTINGS_DOCTYPE = "Jarz POS Settings"

#: The site's standard purchase VAT. Egypt's headline rate is 14%, and almost
#: everything Jarz buys carries it — so it is the *default*, applied to a line
#: whose Item master declares no tax treatment of its own. Per-item and per-line
#: overrides still win; see jarz_pos.api.purchase._resolve_line_tax_template.
DEFAULT_VAT_TEMPLATE_TITLE = "Egypt VAT 14%"
DEFAULT_VAT_RATE = 14.0

#: Where purchases land when nothing more specific applies. Raw materials are
#: the bulk of what gets bought, which is why this is the fallback.
DEFAULT_PURCHASE_WAREHOUSE = "Raw Material - J"

#: Item Group -> receiving warehouse. Resolution walks *up* the Item Group tree,
#: so a route on "Raw Material" also covers every group nested beneath it.
WAREHOUSE_ROUTES = (
    ("Raw Material", "Raw Material - J"),
    ("Consumable", "Consumables - J"),
)


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _warehouse_is_usable(warehouse: str) -> bool:
    """True when the warehouse exists and stock can actually post into it."""
    row = frappe.db.get_value(
        "Warehouse", warehouse, ["is_group", "disabled"], as_dict=True
    )
    if not row:
        return False
    return not int(row.get("is_group") or 0) and not int(row.get("disabled") or 0)


def _resolve_company() -> Optional[str]:
    """The company these defaults belong to, or None if it is ambiguous.

    Same resolution order as ``create_purchase_invoice``: the global default,
    else the single company if there is only one. Guessing on a multi-company
    site would seed a template against the wrong ledger.
    """
    try:
        company = frappe.db.get_single_value("Global Defaults", "default_company")
        if company:
            return company
        rows = frappe.get_all("Company", fields=["name"], limit=2)
        if len(rows) == 1:
            return rows[0]["name"]
    except Exception:
        frappe.log_error(frappe.get_traceback(), "purchase_setup: company resolution failed")
    return None


def _resolve_tax_account(company: str) -> Optional[str]:
    """An existing postable tax account for the company, or None.

    Deliberately resolved, never created. Which account VAT lands in is a real
    accounting decision belonging to whoever owns the chart of accounts; an app
    inventing one would put tax in a ledger nobody reconciles. If the company
    has no tax account at all we no-op and log, and the operator can point the
    setting at a template of their own.
    """
    try:
        rows = frappe.get_all(
            "Account",
            filters={
                "company": company,
                "account_type": "Tax",
                "is_group": 0,
                "disabled": 0,
            },
            fields=["name", "account_name"],
            order_by="name asc",
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "purchase_setup: tax account lookup failed")
        return None
    if not rows:
        return None
    # A VAT-named account beats a generic one when the chart has several;
    # otherwise first alphabetically, so the choice is at least deterministic.
    for row in rows:
        if "vat" in (row.get("account_name") or "").lower():
            return row["name"]
    return rows[0]["name"]


def default_item_tax_template_name(company: Optional[str] = None) -> Optional[str]:
    """Name of the seeded default VAT template if it exists on this site.

    Read-only. Used by ``settings_defaults`` to decide whether the Jarz POS
    Settings link can safely be pointed at it — a Link on that Single naming a
    missing record breaks every later full save of the doc.
    """
    company = company or _resolve_company()
    if not company:
        return None
    try:
        return frappe.db.get_value(
            "Item Tax Template",
            {"title": DEFAULT_VAT_TEMPLATE_TITLE, "company": company, "disabled": 0},
            "name",
        )
    except Exception:
        return None


def ensure_purchase_vat_template():
    """Create the default 14% purchase VAT template. Create-only, never raises.

    Idempotent by (title, company), not by the generated name, so it stays
    correct whatever ERPNext does with autonaming. An operator who edits the
    rate afterwards keeps their edit, and a template they deliberately disabled
    is not resurrected.
    """
    log = {"set": [], "existing": [], "skipped": []}
    try:
        company = _resolve_company()
        if not company:
            log["skipped"].append("no default company resolved")
        else:
            existing = frappe.db.get_value(
                "Item Tax Template",
                {"title": DEFAULT_VAT_TEMPLATE_TITLE, "company": company},
                "name",
            )
            if existing:
                log["existing"].append(f"item tax template {existing}")
            else:
                account = _resolve_tax_account(company)
                if not account:
                    log["skipped"].append(
                        f"no non-group Tax account for {company}; VAT template not created"
                    )
                else:
                    doc = frappe.new_doc("Item Tax Template")
                    doc.title = DEFAULT_VAT_TEMPLATE_TITLE
                    doc.company = company
                    doc.append("taxes", {"tax_type": account, "tax_rate": DEFAULT_VAT_RATE})
                    doc.flags.ignore_permissions = True
                    doc.insert()
                    log["set"].append(
                        f"item tax template {doc.name} = {DEFAULT_VAT_RATE}% on {account}"
                    )
    except Exception:
        # Never abort the shared bench migrate for a default.
        frappe.log_error(frappe.get_traceback(), "purchase_setup: VAT template seed failed")

    _logger().warning(
        "purchase_setup(vat): set=%s existing=%s skipped=%s"
        % (log["set"], log["existing"], log["skipped"])
    )
    return log


def ensure_purchase_setup():
    """Seed purchasing defaults. Never raises — logs and returns a summary."""
    log = {"set": [], "existing": [], "skipped": []}
    try:
        settings = frappe.get_single(SETTINGS_DOCTYPE)
    except Exception:
        # Runs before the Single exists on a brand-new site; the next migrate
        # picks it up.
        frappe.log_error(frappe.get_traceback(), "purchase_setup: settings unavailable")
        return log

    dirty = False

    # ── Default warehouse ────────────────────────────────────────────────
    current_default = (getattr(settings, "default_purchase_warehouse", None) or "").strip()
    if current_default:
        log["existing"].append(f"default_purchase_warehouse={current_default}")
    elif not frappe.db.exists("Warehouse", DEFAULT_PURCHASE_WAREHOUSE):
        log["skipped"].append(f"missing warehouse: {DEFAULT_PURCHASE_WAREHOUSE}")
    elif not _warehouse_is_usable(DEFAULT_PURCHASE_WAREHOUSE):
        log["skipped"].append(f"unusable warehouse: {DEFAULT_PURCHASE_WAREHOUSE}")
    else:
        settings.default_purchase_warehouse = DEFAULT_PURCHASE_WAREHOUSE
        dirty = True
        log["set"].append(f"default_purchase_warehouse={DEFAULT_PURCHASE_WAREHOUSE}")

    # ── Item Group routes ────────────────────────────────────────────────
    existing_groups = {
        (row.item_group or "").strip()
        for row in (getattr(settings, "purchase_warehouse_routes", None) or [])
    }
    for item_group, warehouse in WAREHOUSE_ROUTES:
        if item_group in existing_groups:
            log["existing"].append(f"route {item_group}")
            continue
        if not frappe.db.exists("Item Group", item_group):
            log["skipped"].append(f"missing item group: {item_group}")
            continue
        if not frappe.db.exists("Warehouse", warehouse):
            log["skipped"].append(f"missing warehouse: {warehouse}")
            continue
        if not _warehouse_is_usable(warehouse):
            log["skipped"].append(f"unusable warehouse: {warehouse}")
            continue
        settings.append(
            "purchase_warehouse_routes",
            {"item_group": item_group, "warehouse": warehouse},
        )
        dirty = True
        log["set"].append(f"route {item_group} -> {warehouse}")

    if dirty:
        try:
            settings.flags.ignore_permissions = True
            # Saving a Single revalidates *every* link on it, not just the two
            # fields written here. Production's Jarz POS Settings carries a
            # dangling cash_over_short_account (an account that exists on
            # staging and not on prod), so a plain save raised
            # LinkValidationError and this seeder silently did nothing there
            # while succeeding on staging — the exact environment drift it was
            # written to prevent. Every warehouse this function writes is
            # existence- and usability-checked above, so skipping link
            # validation here loses nothing and stops one unrelated stale field
            # from blocking purchasing setup.
            settings.flags.ignore_links = True
            settings.save()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "purchase_setup: save failed")
            return log

    # .info() is invisible at the default server log level, so the summary that
    # matters on a real migrate goes out at warning.
    _logger().warning(
        "purchase_setup: set=%s existing=%s skipped=%s"
        % (log["set"], log["existing"], log["skipped"])
    )
    return log
