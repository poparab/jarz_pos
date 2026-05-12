"""FCM token backfill — disable stale tokens that have accumulated since PROD-POS-002 fix.

Run with:
    bench --site frontend execute jarz_pos.scripts.backfill_fcm_tokens.run

What this does:
1. Finds all enabled Jarz Mobile Device rows.
2. Sends a silent data-only "ping" push to each unique token.
3. Uses _is_invalid_token_error() to classify failures.
4. Disables every row whose token returned an invalid-token error.
5. Prints a summary report (kept / disabled / unexpected errors).

IMPORTANT:
- Run on staging first, verify the summary, then run on production.
- This script does NOT delete any rows — it only sets enabled=0.
- Run after the Phase B code change (notifications.py hardening) is deployed.

Safety:
- All DB writes go through frappe.db.set_value (same path as _disable_token).
- A dry_run=True flag prints what would be disabled without touching the DB.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import frappe


def run(dry_run: bool = False) -> Dict[str, Any]:
    """Backfill: probe every enabled device token and disable stale ones.

    Args:
        dry_run: If True, print the plan without writing to the DB.

    Returns:
        Summary dict with keys: kept, disabled, unexpected_errors, total.
    """
    from jarz_pos.api.notifications import _initialize_firebase_app, _is_invalid_token_error

    frappe.init(site=frappe.local.site if hasattr(frappe, "local") and frappe.local else None)

    # --- Phase 1: Firebase must be initialized before we can send probes ---
    if not _initialize_firebase_app():
        frappe.log_error("FCM backfill aborted: Firebase could not be initialized.", "FCM Backfill")
        print("ERROR: Firebase could not be initialized. Check site_config.json and Error Log.")
        return {"kept": 0, "disabled": 0, "unexpected_errors": 0, "total": 0, "aborted": True}

    from firebase_admin import messaging

    # --- Phase 2: Fetch all enabled device rows ---
    rows = frappe.get_all(
        "Jarz Mobile Device",
        filters={"enabled": 1},
        fields=["name", "token", "user"],
        order_by="name asc",
    )
    print(f"[FCM Backfill] Found {len(rows)} enabled device rows.")

    if not rows:
        print("[FCM Backfill] Nothing to do.")
        return {"kept": 0, "disabled": 0, "unexpected_errors": 0, "total": 0}

    # Deduplicate tokens while tracking which rows share each token.
    token_to_rows: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        token = (row.get("token") or "").strip()
        if not token:
            continue
        token_to_rows.setdefault(token, []).append(row)

    print(f"[FCM Backfill] {len(token_to_rows)} unique tokens to probe.")

    kept = 0
    disabled = 0
    disabled_tokens = 0
    unexpected_errors = 0

    for token, device_rows in token_to_rows.items():
        # Build a silent data-only probe message (no notification; won't wake the app)
        probe = messaging.Message(
            data={"type": "fcm_probe", "source": "backfill"},
            token=token,
        )

        try:
            messaging.send(probe)
            # Token is valid
            kept += 1
            print(f"  OK  {token[:40]}... ({len(device_rows)} row(s))")

        except Exception as exc:
            if _is_invalid_token_error(exc):
                # Stale token — disable all rows sharing it
                disabled += len(device_rows)
                disabled_tokens += 1
                row_names = [r["name"] for r in device_rows]
                print(f"  DISABLE  {token[:40]}... — {str(exc)[:80]} ({row_names})")

                if not dry_run:
                    for row in device_rows:
                        try:
                            frappe.db.set_value(
                                "Jarz Mobile Device",
                                row["name"],
                                "enabled",
                                0,
                                update_modified=False,
                            )
                        except Exception as db_exc:
                            frappe.log_error(
                                f"Backfill: failed to disable {row['name']}: {db_exc}",
                                "FCM Backfill",
                            )
                            print(f"    DB ERROR disabling {row['name']}: {db_exc}")
                else:
                    print(f"    [dry_run] would disable {len(row_names)} row(s)")
            else:
                # Unexpected failure — log and continue
                unexpected_errors += 1
                frappe.log_error(
                    f"FCM backfill unexpected error for token {token[:40]}...: {exc}",
                    "FCM Backfill",
                )
                print(f"  ERROR  {token[:40]}... — {str(exc)[:80]}")

    if not dry_run:
        frappe.db.commit()

    total = kept + disabled + unexpected_errors
    summary = {
        "kept": kept,
        "disabled": disabled,
        "disabled_tokens": disabled_tokens,
        "unexpected_errors": unexpected_errors,
        "total": total,
        "dry_run": dry_run,
    }

    print("\n[FCM Backfill] Summary:")
    print(f"  Tokens probed : {len(token_to_rows)}")
    print(f"  Kept valid    : {kept}")
    print(f"  Disabled stale: {disabled} rows ({disabled_tokens} tokens)")
    print(f"  Unexpected err: {unexpected_errors}")
    if dry_run:
        print("  [DRY RUN — no DB changes made]")

    return summary
