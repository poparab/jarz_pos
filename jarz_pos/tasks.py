def run_nightly_rfm_segmentation():
    """Nightly RFM customer segmentation job."""
    from jarz_pos.services.rfm_segmentation import run_segmentation
    run_segmentation()


def run_weekly_velocity_update():
    """Weekly: recalculate sales velocity for all stock items."""
    from jarz_pos.services.demand_forecasting import run_velocity_update
    run_velocity_update()


def run_daily_inventory_digest():
    """Daily at 7am: send inventory alert email."""
    from jarz_pos.services.demand_forecasting import send_daily_digest
    send_daily_digest()


def _get_online_payment_alert_recipients() -> list[str]:
    """Return enabled users who should be alerted about aging unconfirmed online payments."""
    import frappe

    users: set[str] = set()
    try:
        rows = frappe.get_all(
            "Has Role",
            filters={
                "role": ["in", ["JARZ Manager", "POS Manager", "System Manager"]],
                "parenttype": "User",
            },
            pluck="parent",
        )
        for user in rows or []:
            if user and user != "Guest":
                users.add(user)
    except Exception:
        return []

    recipients: list[str] = []
    for user in users:
        try:
            if frappe.db.get_value("User", user, "enabled"):
                recipients.append(user)
        except Exception:
            recipients.append(user)
    return recipients


def _create_online_payment_alert_notifications(row: dict, users: list[str], hours: int) -> None:
    """Create a per-manager Notification Log entry for an aging unconfirmed online order."""
    import frappe

    if not users:
        return

    invoice_name = row.get("name")
    subject = f"Unconfirmed online payment: {invoice_name}"
    message = (
        f"Order {invoice_name} for {row.get('customer_name') or row.get('customer')} "
        f"({row.get('custom_payment_method') or 'online'}) has been Out for Delivery "
        f"awaiting payment confirmation for over {hours} hour(s). "
        f"Amount: {float(row.get('grand_total') or 0):.2f}."
    )
    for user in users:
        try:
            note = frappe.new_doc("Notification Log")
            note.subject = subject
            note.email_content = message
            note.for_user = user
            note.type = "Alert"
            note.document_type = "Sales Invoice"
            note.document_name = invoice_name
            note.insert(ignore_permissions=True)
        except Exception:
            continue


#: Safe fallback when Jarz POS Settings has no (or an invalid) value configured.
DEFAULT_UNCONFIRMED_ONLINE_PAYMENT_ALERT_HOURS = 6


def get_unconfirmed_online_payment_alert_hours() -> int:
    """Single source of truth for the "how long is too long" threshold used to
    escalate an unpaid InstaPay/Mobile Wallet order still sitting Out for Delivery.

    Read from Jarz POS Settings (``instapay_unconfirmed_alert_hours``); the mobile
    reconciliation screen's escalation list must show exactly the same orders this
    hourly job flags, so both call this helper instead of each keeping their own
    copy of the threshold lookup.
    """
    try:
        from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings

        settings = get_jarz_settings()
        raw = getattr(settings, "instapay_unconfirmed_alert_hours", None)
        if raw is not None and int(raw) > 0:
            return int(raw)
    except Exception:
        pass
    return DEFAULT_UNCONFIRMED_ONLINE_PAYMENT_ALERT_HOURS


def reconcile_awaiting_online_payments():
    """Hourly: re-align every ``Awaiting Payment`` order with its own ledger.

    The escalation job below alerts on orders that have been awaiting payment too
    long. It never asked whether they are still awaiting anything -- so an order
    paid through the kanban card's own InstaPay button, which posts the Payment
    Entry without clearing the flag, stayed in the queue for ever AND got
    escalated. Six production orders were in that state on 2026-09-09.

    This runs first, and is the backstop for the whole feature: whatever route
    the money took, whatever route retired the receipt, an hour later the flag
    and the receipts list agree with the ledger again. Per-invoice failures are
    isolated so one bad row cannot stop the sweep, and it never raises out of
    the scheduler.

    ``reconcile_payment_confirmation`` returns a result only when it actually
    changed something, so ``healed`` counts writes, not rows looked at. A steady
    hour is silent and commits nothing -- it used to report every awaiting order
    as reconciled and commit an empty transaction every hour.
    """
    import frappe

    try:
        from jarz_pos.services.delivery_handling import reconcile_payment_confirmation

        rows = frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1, "custom_payment_confirmation_status": "Awaiting Payment"},
            pluck="name",
            limit_page_length=0,
        )
        healed = 0
        for name in rows:
            try:
                if reconcile_payment_confirmation(name):
                    healed += 1
            except Exception:
                frappe.logger().error(
                    f"reconcile_awaiting_online_payments: failed on {name}"
                )
        if healed:
            frappe.db.commit()
            frappe.logger().info(
                f"reconcile_awaiting_online_payments: changed {healed} of "
                f"{len(rows)} awaiting orders"
            )
    except Exception:
        frappe.logger().error(
            "reconcile_awaiting_online_payments failed: " + frappe.get_traceback()
        )


def escalate_unconfirmed_online_payments():
    """Hourly: alert managers about unpaid InstaPay/Mobile Wallet orders that have sat
    Out for Delivery awaiting payment confirmation past the configured threshold.

    Guarded: never raises out of the scheduler.
    """
    import frappe
    from jarz_pos.constants import WS_EVENTS

    try:
        hours = get_unconfirmed_online_payment_alert_hours()

        cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours)

        rows = frappe.get_all(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "custom_payment_confirmation_status": "Awaiting Payment",
                "custom_payment_confirmation_alerted": 0,
                "custom_ofd_unconfirmed_since": ["<=", cutoff],
            },
            fields=[
                "name",
                "customer",
                "customer_name",
                "grand_total",
                "custom_payment_method",
                "custom_ofd_unconfirmed_since",
                "custom_kanban_profile",
                "pos_profile",
            ],
            limit_page_length=0,
        )
        if not rows:
            return

        recipients = _get_online_payment_alert_recipients()

        for row in rows:
            try:
                payload = {
                    "type": "online_payment_unconfirmed_aging",
                    "invoice": row.get("name"),
                    "customer": row.get("customer"),
                    "customer_name": row.get("customer_name"),
                    "amount": float(row.get("grand_total") or 0),
                    "payment_method": row.get("custom_payment_method"),
                    "unconfirmed_since": str(row.get("custom_ofd_unconfirmed_since") or ""),
                    "threshold_hours": hours,
                    "pos_profile": row.get("custom_kanban_profile") or row.get("pos_profile"),
                }
                try:
                    from jarz_pos.utils.realtime import publish_to_branches

                    branch = payload.get("pos_profile")
                    publish_to_branches(
                        WS_EVENTS.INVOICE_STATE_CHANGE,
                        payload,
                        [branch] if branch else [],
                    )
                except Exception:
                    pass

                _create_online_payment_alert_notifications(row, recipients, hours)

                # Idempotency: mark alerted so we do not re-notify every hour
                frappe.db.set_value(
                    "Sales Invoice",
                    row.get("name"),
                    "custom_payment_confirmation_alerted",
                    1,
                    update_modified=False,
                )
            except Exception:
                frappe.logger().warning(
                    f"escalate_unconfirmed_online_payments: failed for {row.get('name')}: "
                    f"{frappe.get_traceback()}"
                )
                continue

        try:
            frappe.db.commit()
        except Exception:
            pass
    except Exception:
        try:
            frappe.log_error(
                frappe.get_traceback(), "escalate_unconfirmed_online_payments failed"
            )
        except Exception:
            pass
