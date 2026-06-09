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
