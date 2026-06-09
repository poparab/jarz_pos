def run_nightly_rfm_segmentation():
    """Nightly RFM customer segmentation job."""
    from jarz_pos.services.rfm_segmentation import run_segmentation
    run_segmentation()
