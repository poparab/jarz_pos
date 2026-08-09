"""Seed ``Jarz POS Settings`` defaults WITHOUT clobbering operator choices.

Why this exists — and why the fixture it replaces was actively harmful.

``Jarz POS Settings`` is a Single, and it used to be listed in ``hooks.fixtures``
as ``{"dt": "Jarz POS Settings"}``. Frappe imports a Single fixture through
``import_doc`` with ``force=True`` and ``delete_old_doc``, i.e. it *rebuilds the
document* from the JSON on disk. Any field absent from that JSON therefore
reverts to its doctype default on **every** ``bench migrate``.

The fixture listed only account names and receipt text. It listed no feature
flags at all. So every flag on this Single silently reverted to 0 on every
backend deploy:

* ``enable_invoice_returns`` — documented in its own description as "the instant
  rollback lever — it needs no deploy and no restart". A deploy turned it off.
* ``enable_courier_delivery_actions``, ``require_delivery_pin_for_ofd``,
  ``enable_customer_tracking`` — same.

That was observed, not theorised: ``enable_courier_delivery_actions`` was set to
1 on staging, two deploys ran, and it read back 0 with nobody having touched it.
An operator turning a flag on in Desk had no way to know a later deploy would
undo it, because nothing failed and nothing logged.

The seeding still has to happen — a fresh site needs these account names — so it
moves here, to ``after_migrate``, with one rule that the fixture could not
express:

    **Only ever fill a field that is currently empty. Never overwrite.**

That keeps a fresh install working while making Desk the source of truth for
anything an operator has deliberately set.

Writes go through ``frappe.db.set_value`` per field rather than ``doc.save()``.
A full save of this Single fails link validation if *any* Link field on it points
at a missing record — one dangling account reference has previously taken down
unrelated seeders that saved this doc. Per-field writes cannot be poisoned that
way.
"""
from __future__ import annotations

from typing import Any, Dict

import frappe

SETTINGS_DOCTYPE = "Jarz POS Settings"

#: Values seeded only when the field is empty. Previously the body of
#: fixtures/jarz_pos_settings.json.
#:
#: Feature flags are deliberately ABSENT: their doctype default is 0, a fresh
#: site should start dark, and an operator's decision must survive a deploy.
SEED_DEFAULTS: Dict[str, Any] = {
    # POS ledger accounts. accounts_setup.ensure_pos_accounts runs earlier in
    # after_migrate and creates these, so the names resolve by the time we get here.
    "cash_over_short_account": "Cash Over Short - J",
    "indirect_expenses_parent": "Indirect Expenses - J",
    "freight_charges_account": "Freight and Forwarding Charges - J",
    "courier_outstanding_account": "Courier Outstanding - J",
    "default_bank_parent_account": "Bank Accounts - J",
    "mobile_wallet_account": "Mobile Wallet - J",
    "default_receivable_account": "Debtors - J",
    "default_payable_account": "Creditors - J",
    # Operational defaults
    "default_stock_uom": "Nos",
    "standard_buying_price_list": "Standard Buying",
    "delivery_employee_group": "Delivery",
    "delivery_supplier_group": "Delivery",
    # Receipt chrome
    "receipt_header_text": "ORDER RECEIPT",
    "receipt_footer_text": "Thank you for Your Order",
    "receipt_phone": "01061332266",
    "receipt_website": "https://www.orderjarz.com",
}


def _dynamic_defaults() -> Dict[str, Any]:
    """Defaults whose value cannot be written down as a constant.

    The default purchase VAT template is named after the company abbreviation
    ERPNext generates, so it is resolved from the site rather than hardcoded —
    and only when it actually exists. ``ensure_purchase_vat_template`` runs
    earlier in ``after_migrate`` and creates it; if it could not (no tax account
    on the chart, no unambiguous company) this returns nothing and the setting
    stays blank, which is the safe state.
    """
    out: Dict[str, Any] = {}
    try:
        from jarz_pos.setup.purchase_setup import default_item_tax_template_name

        template = default_item_tax_template_name()
        if template:
            out["purchase_default_item_tax_template"] = template
    except Exception:
        # A missing default is harmless; an exception here would take the whole
        # seeding pass down with it.
        pass
    return out


def _is_empty(value: Any) -> bool:
    """Empty means None or a blank/whitespace string.

    0 and False are NOT empty. A numeric or check field an operator set to zero
    is a decision, not an absence, and re-seeding it would be exactly the bug
    this module exists to remove.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def ensure_settings_defaults() -> None:
    """Fill only the empty fields on the settings Single. Never raises.

    Registered in ``after_migrate``. It must not raise: an exception here aborts
    the shared migrate that every other app on the bench also depends on.
    """
    try:
        if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
            return

        meta = frappe.get_meta(SETTINGS_DOCTYPE)
        filled = []

        for fieldname, default in {**SEED_DEFAULTS, **_dynamic_defaults()}.items():
            # A field the doctype no longer carries is skipped rather than
            # written blindly, so a removed field cannot break the migrate.
            if not meta.get_field(fieldname):
                continue
            try:
                current = frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
            except Exception:
                continue
            if not _is_empty(current):
                continue

            # A Link pointing at a record that does not exist is worse than a
            # blank: it fails validation on every later full save of this Single.
            options = (meta.get_field(fieldname).get("options") or "").strip()
            if meta.get_field(fieldname).get("fieldtype") == "Link" and options:
                try:
                    if not frappe.db.exists(options, default):
                        continue
                except Exception:
                    continue

            try:
                frappe.db.set_value(
                    SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, fieldname, default,
                    update_modified=False,
                )
                filled.append(fieldname)
            except Exception:
                continue

        if filled:
            frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
            try:
                frappe.logger("jarz_pos").warning(
                    "Jarz POS Settings: seeded empty fields %s", ", ".join(filled)
                )
            except Exception:
                pass
    except Exception:
        try:
            frappe.log_error(
                frappe.get_traceback(), "ensure_settings_defaults failed"
            )
        except Exception:
            pass
