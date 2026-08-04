"""Keep fully-returned orders parked in the board's ``Returned`` column.

Two jobs, both idempotent:

1. **Backfill.** Orders returned before the ``Returned`` column existed stayed
   wherever the return found them — usually "Out for Delivery". Since
   ``api.kanban.update_invoice_state`` now refuses to move a fully-returned
   order, leaving them there would freeze them in a live column forever:
   strictly worse than the bug this feature set out to fix.

2. **Self-heal.** ``services.invoice_return._stamp_return_state``'s board write is
   best-effort — it must never roll back an otherwise-good return, so it
   swallows and logs its own failures. That leaves the same stuck state
   (``custom_return_status = "Fully Returned"`` + a live column). Running this
   on every migrate reconciles those instead of stranding them.

Deliberately an ``after_migrate`` hook rather than a ``patches.txt`` entry.
``post_model_sync`` patches run in ``SiteMigration.run_schema_updates()``,
which is *before* ``sync_fixtures()`` in ``post_schema_updates()`` — the
``Returned`` Select option would not exist yet, the run would no-op, and
``patch_handler`` would still record it as executed and never retry. It also
means this is re-runnable, which is what makes job 2 work at all.

Written straight to the database on purpose. These are submitted documents and
the value is cosmetic board state — a ``save()`` would fire the
``on_update_after_submit`` hooks (realtime publishes, delivery follow-ups)
during ``bench migrate``, which is neither wanted nor safe there.
"""

from __future__ import annotations

import frappe

RETURNED_BOARD_STATE = "Returned"
STATE_FIELD_ALIASES = (
    "custom_sales_invoice_state",
    "sales_invoice_state",
    "custom_state",
    "state",
)


def ensure_returned_board_state() -> None:
    try:
        meta = frappe.get_meta("Sales Invoice")
    except Exception:
        return

    if not meta.get_field("custom_return_status"):
        # Return workflow was never installed on this site.
        return

    fields = [f for f in STATE_FIELD_ALIASES if meta.get_field(f)]
    if not fields:
        return

    # Writing a value the Select does not allow would trip _validate_selects on
    # the next full save of each document. If the fixture has not landed yet,
    # do nothing and let the next migrate pick it up.
    state_field = meta.get_field(fields[0])
    options = [o.strip() for o in (state_field.options or "").split("\n") if o.strip()]
    if RETURNED_BOARD_STATE not in options:
        frappe.log_error(
            message=(
                f"'{RETURNED_BOARD_STATE}' is not an option on {fields[0]} "
                f"(found: {options}). Skipping — this reconciler retries on the "
                "next migrate. If it persists, another app's fixture is "
                "overwriting the Custom Field."
            ),
            title="Returned board state skipped",
        )
        return

    stuck = frappe.get_all(
        "Sales Invoice",
        filters={
            "docstatus": 1,
            "is_return": 0,
            "custom_return_status": "Fully Returned",
            fields[0]: ["!=", RETURNED_BOARD_STATE],
        },
        pluck="name",
    )
    if not stuck:
        return

    for name in stuck:
        try:
            frappe.db.set_value(
                "Sales Invoice",
                name,
                {field: RETURNED_BOARD_STATE for field in fields},
                update_modified=False,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Returned board state failed for {name}",
            )

    frappe.db.commit()

    # log_error is the only level that survives the servers' ERROR-level logger
    # (see memory: frappe.logger().info() is silent off a dev box), and a data
    # reconciliation is worth an audit trail.
    frappe.log_error(
        message=(
            f"Moved {len(stuck)} fully-returned invoice(s) to "
            f"'{RETURNED_BOARD_STATE}': {', '.join(stuck[:50])}"
            + (" ..." if len(stuck) > 50 else "")
        ),
        title="Returned board state reconciled",
    )
