"""Seed Meta CAPI Settings once: off, in test mode, every order approved by hand."""

import frappe

DOCTYPE = "Meta CAPI Settings"
DEFAULTS = {
    "enabled": 0,
    "test_mode": 1,
    "start_date": "2026-10-19",
    "approval_mode": "Every order",
    "hold_above": 2500,
    "dataset_id": "1296894931563065",
    "api_version": "v23.0",
    "excluded_customer_groups": "B2B\nEmployee\nCommercial\nDistributor\nGovernment\nNon Profit\nSample",
}


def _stored(field):
    return frappe.db.sql(
        "SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s", (DOCTYPE, field)
    )


def execute():
    if not frappe.db.exists("DocType", DOCTYPE):
        return
    for field, value in DEFAULTS.items():
        if not _stored(field):
            frappe.db.set_single_value(DOCTYPE, field, value)
