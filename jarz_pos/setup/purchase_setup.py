"""Purchasing setup — warehouse routing defaults.

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

import frappe

LOGGER_NAME = "purchase_setup"

SETTINGS_DOCTYPE = "Jarz POS Settings"

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
