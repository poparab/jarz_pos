"""Controller for the public ``/track/<token>`` page.

Deliberately almost empty, and the emptiness is the design.

**Nothing per-token is rendered server-side.** Frappe's page cache keys on the
*resolved route*, which for every dynamic ``/track/<token>`` URL is the single
string ``"track"`` (``website_route_rules`` maps the token segment away before
``TemplatePage`` ever sees it). So if this controller embedded the token — or the
customer's status, or the courier's position — into the HTML, one cached render
would be handed to every subsequent visitor: a cross-customer leak produced by
infrastructure rather than by any bug in the query.

Two independent defences, because one is not enough for that failure mode:

1. ``no_cache = 1`` below. ``TemplatePage.set_pymodule_properties`` copies this
   module attribute into the page context, and ``website.utils.cache_html``
   refuses to store a render when it is set. This is the documented mechanism.
2. The template never receives the token at all. ``track.html`` reads it from
   ``window.location.pathname`` and fetches
   ``jarz_pos.api.tracking.get_public_status`` itself. Even a cached page is
   therefore correct for whoever loads it, which makes the whole class of bug
   structurally impossible rather than merely switched off.

The page also does **no** server-side validation of the token. That is
intentional: a 404 for an unknown token and a 200 for a real one is an
enumeration oracle in HTTP status form, undoing the care taken in the JSON
endpoint. Every ``/track/...`` URL renders the same shell; the JSON answer is the
only authority, and it says ``not found`` identically for wrong and expired.
"""

from __future__ import annotations

from typing import Any

import frappe

from jarz_pos.services import tracking as _tracking

#: Read by ``TemplatePage.set_pymodule_properties``. See the docstring — this is
#: defence #1 and must not be removed.
no_cache = 1

#: Keep a per-customer link out of sitemaps and off crawlers.
sitemap = 0


def get_context(context: Any) -> Any:
    """Static shell context. Contains no order data and no token."""
    context.no_cache = 1
    context.no_sitemap = 1
    # The client needs to know where to poll and how often; neither is secret,
    # and hard-coding them in the template would let the route and the service
    # constant drift apart.
    context.status_endpoint = "/api/method/jarz_pos.api.tracking.get_public_status"
    context.poll_interval_sec = _tracking.DEFAULT_POLL_INTERVAL_SEC
    context.route_prefix = _tracking.TRACKING_ROUTE_PREFIX
    context.title = "Track your order"
    return context
