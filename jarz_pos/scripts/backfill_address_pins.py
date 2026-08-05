"""Backfill Address door pins from the Maps links already stored in ``address_line2``.

Run with::

    bench --site frontend execute jarz_pos.scripts.backfill_address_pins.run --kwargs "{'dry_run': True}"
    bench --site frontend execute jarz_pos.scripts.backfill_address_pins.run

``api/customer.py`` has written ``address_line2 = "Location: <url>"`` since the
POS shipped, so every address created through the POS with a pasted Google Maps
link is already a coordinate waiting to be extracted. That is the entire reason
the door-pin database does not start empty — this script converts the corpus.

**The single hard rule: this script READS ``address_line2`` and never writes it.**
That field is

* part of the WooCommerce address-dedup signature
  (``customer_sync._address_signature_parts``) — changing it forks a duplicate
  ``Address`` for every record touched; and
* part of the WooCommerce outbound-push trigger set (``outbound_sync``) —
  changing it fans a customer sync **and** an invoice sync out to WooCommerce
  per record.

Every write goes through ``services.geo_resolution``, which only ever writes the
six ``custom_*`` geo fields, via ``frappe.db.set_value`` with
``update_modified=False``. No Address document is saved, so no Address hook
fires and nothing reaches WooCommerce.

Pins land at ``pos_link`` confidence (rank 20). The ladder does the rest: an
address a courier has already verified (rank 40) or a customer pinned at
checkout (rank 30) is left alone, silently. Re-running the script is therefore
safe and idempotent — a second run simply re-accepts the same ``pos_link`` pin.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe

from jarz_pos.services import geo_resolution
from jarz_pos.utils import geo

#: How many Addresses to load per page. Kept modest so a 100k-row site does not
#: build one enormous result set in memory.
PAGE_SIZE = 500


def _summary() -> Dict[str, Any]:
    return {
        "scanned": 0,
        "with_link": 0,
        "parsed": 0,
        "short_links": 0,
        "unparseable": 0,
        "written": 0,
        "skipped_better_pin": 0,
        "errors": 0,
        "by_precision": {},
    }


def run(
    dry_run: bool = False,
    limit: Optional[int] = None,
    resolve_short_links: bool = False,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Copy coordinates out of ``address_line2`` into the Address geo fields.

    Args:
        dry_run: Parse and decide, but write nothing. Prints the same summary.
        limit: Stop after this many Addresses carrying a link (for a smoke run).
        resolve_short_links: Queue background expansion for ``maps.app.goo.gl``
            links. Off by default — one HTTP round-trip per address is a real
            outbound load and should be a deliberate second pass.
        verbose: Print per-address lines as well as the summary.

    Returns:
        A summary dict. Never raises: a single bad row is counted and skipped.
    """
    stats = _summary()

    if not geo_resolution.geo_fields_present():
        print(
            "ABORT: Address is missing the geo custom fields. Run `bench migrate` first."
        )
        return {**stats, "aborted": True}

    start = 0
    stop = False
    while not stop:
        try:
            rows: List[Dict[str, Any]] = (
                frappe.get_all(
                    "Address",
                    # address_line2 is SELECTed. It is never in an update.
                    fields=[
                        "name",
                        "address_line2",
                        "custom_geo_source",
                        "custom_latitude",
                        "custom_longitude",
                    ],
                    filters={"address_line2": ["like", "%http%"]},
                    order_by="name asc",
                    limit_start=start,
                    limit_page_length=PAGE_SIZE,
                )
                or []
            )
        except Exception as exc:
            print(f"ERROR: could not page Addresses at offset {start}: {exc}")
            frappe.log_error(frappe.get_traceback(), "backfill_address_pins: paging failed")
            break

        if not rows:
            break
        start += len(rows)

        for row in rows:
            stats["scanned"] += 1
            name = row.get("name")
            link = geo.extract_location_link(row.get("address_line2"))
            if not link:
                continue
            stats["with_link"] += 1

            if limit and stats["with_link"] > limit:
                stop = True
                break

            parsed = geo.parse_maps_link(link)
            if not parsed:
                if geo.is_short_maps_link(link):
                    stats["short_links"] += 1
                    if resolve_short_links and not dry_run:
                        geo_resolution.enqueue_short_link_resolution(
                            name, link, source=geo.SOURCE_POS_LINK
                        )
                    if verbose:
                        print(f"  SHORT  {name}: {link[:70]}")
                else:
                    stats["unparseable"] += 1
                    if verbose:
                        print(f"  NOPARSE {name}: {link[:70]}")
                continue

            latitude, longitude, precision = parsed
            stats["parsed"] += 1
            stats["by_precision"][precision] = stats["by_precision"].get(precision, 0) + 1

            try:
                if dry_run:
                    decision = geo_resolution.evaluate_pin_write(
                        name,
                        latitude=latitude,
                        longitude=longitude,
                        source=geo.SOURCE_POS_LINK,
                        accuracy_m=geo.accuracy_for_precision(precision),
                    )
                else:
                    decision = geo_resolution.set_address_pin(
                        name,
                        latitude=latitude,
                        longitude=longitude,
                        source=geo.SOURCE_POS_LINK,
                        accuracy_m=geo.accuracy_for_precision(precision),
                    )
            except Exception as exc:
                stats["errors"] += 1
                frappe.log_error(
                    frappe.get_traceback(), f"backfill_address_pins: {name} failed"
                )
                print(f"  ERROR  {name}: {exc}")
                continue

            if decision.get("accepted"):
                stats["written"] += 1
                if verbose:
                    prefix = "  [dry] " if dry_run else "  WRITE "
                    print(
                        f"{prefix}{name}: {latitude:.6f},{longitude:.6f} ({precision})"
                    )
            else:
                stats["skipped_better_pin"] += 1
                if verbose:
                    print(
                        f"  KEEP   {name}: existing "
                        f"'{decision.get('current_source') or 'none'}' "
                        f"outranks pos_link"
                    )

    if not dry_run:
        try:
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "backfill_address_pins: commit failed")

    print("\n[Address pin backfill] Summary:")
    print(f"  Addresses scanned      : {stats['scanned']}")
    print(f"  Carrying a link        : {stats['with_link']}")
    print(f"  Coordinates parsed     : {stats['parsed']}")
    for precision, count in sorted(stats["by_precision"].items()):
        print(f"    - {precision:<12}: {count}")
    print(f"  Short links (deferred) : {stats['short_links']}")
    print(f"  Links with no coords   : {stats['unparseable']}")
    print(f"  Pins written           : {stats['written']}")
    print(f"  Kept (better pin)      : {stats['skipped_better_pin']}")
    print(f"  Errors                 : {stats['errors']}")
    if dry_run:
        print("  [DRY RUN — nothing was written]")

    return stats
