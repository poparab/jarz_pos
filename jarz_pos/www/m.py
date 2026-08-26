"""Controller for the public ``/m/<token>`` sales-material page.

Deliberately almost empty, for the same reason ``www/track.py`` is.

**Nothing per-token is rendered server-side.** Frappe's page cache keys on the
*resolved route*, which for every dynamic ``/m/<token>`` URL is the single
string ``"m"`` (``website_route_rules`` maps the token segment away before
``TemplatePage`` ever sees it). If this controller embedded the token — or the
lead's name, or which price list was sent — into the HTML, one cached render
would be handed to every subsequent visitor: one prospect shown the pack that
was put together for a competitor down the road, produced by infrastructure
rather than by any bug in the query.

Two independent defences, because one is not enough for that failure mode:

1. ``no_cache = 1`` below. ``TemplatePage.set_pymodule_properties`` copies this
   module attribute into the page context, and ``website.utils.cache_html``
   refuses to store a render when it is set.
2. The template never receives the token at all. ``m.html`` reads it from
   ``window.location.pathname`` and fetches
   ``jarz_pos.api.materials.get_public_share`` itself. Even a cached shell is
   therefore correct for whoever loads it.

The page also does **no** server-side validation of the token: a 404 for an
unknown token and a 200 for a real one is an enumeration oracle in HTTP status
form, undoing the care taken in the JSON endpoint. Every ``/m/...`` URL renders
the same shell; the JSON answer is the only authority.
"""

from __future__ import annotations

from typing import Any

from jarz_pos.services import materials as _materials

#: Read by ``TemplatePage.set_pymodule_properties``. Defence #1 above; do not
#: remove.
no_cache = 1

#: A price list sent to one prospect has no business in a search index.
sitemap = 0


def get_context(context: Any) -> Any:
    """Static shell context. Contains no token and no material data."""
    context.no_cache = 1
    context.no_sitemap = 1
    context.share_endpoint = "/api/method/jarz_pos.api.materials.get_public_share"
    context.route_prefix = _materials.SHARE_ROUTE_PREFIX
    context.title = "Jarz"
    return context
