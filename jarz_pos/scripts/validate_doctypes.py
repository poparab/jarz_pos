"""Utility script to verify all DocTypes of jarz_pos are discoverable.

Usage:
    bench --site <site> execute jarz_pos.scripts.validate_doctypes.run
"""

from __future__ import annotations

import frappe


def run():  # pragma: no cover - helper script

    app = "jarz_pos"
    failures = []
    doctypes = []

    # Collect doctypes declared by module scanning of our app
    for dt in frappe.get_all("DocType", filters={"module": ["like", "%jarz%"]}, pluck="name"):
        doctypes.append(dt)

    print(f"Discovered candidate DocTypes (module contains 'jarz'): {len(doctypes)}")
    for name in sorted(doctypes):
        try:
            meta = frappe.get_meta(name)
            assert meta.name == name
        except Exception as exc:  # noqa: BLE001
            failures.append((name, str(exc)))

    if failures:
        print("\nFAILURES:")
        for name, err in failures:
            print(f" - {name}: {err}")
        raise SystemExit(1)

    print("All DocTypes loaded successfully.")
