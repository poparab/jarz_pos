"""Sales-material sharing API: the rep's send sheet, and the customer's page.

Two audiences in one module, separated by exactly one line of code each — the
gate that is the first statement of every endpoint below.

**Staff side** (``_ensure_b2b_access``): list the library, mint a share, read
what has already been sent to a lead, force a re-render. Same gate as
``api/crm.py`` and ``api/journey.py``, never a bespoke one.

**Customer side** (``get_public_share``, ``allow_guest=True``): one endpoint,
reachable by anybody on the internet, authorised solely by an unguessable token
and rate limited twice. It follows ``api/tracking.py`` line for line, including
the two rules that matter most there:

1. **Unknown, expired and disabled all answer identically.** A distinguishable
   "expired" confirms the token was once real, which is the only thing an
   enumeration attempt actually wants to learn.
2. **The response is an allow-list, not a filtered document.** The payload is
   built key by key from the share; nothing is spread in from a doc, so a field
   added to ``Jarz Material Share`` later cannot leak into a public response by
   default. Note what is deliberately absent: the lead's name and ID, the rep's
   identity, the phone number, the view counters, and every other share.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import frappe
from frappe.utils import cint, now_datetime

from jarz_pos.api.crm import _ensure_b2b_access
from jarz_pos.services import materials as materials_service
from jarz_pos.utils.phone import whatsapp_msisdn

try:  # pragma: no cover - import shape depends on the Frappe build
    from frappe.rate_limiter import rate_limit as _frappe_rate_limit
except Exception:  # pragma: no cover
    _frappe_rate_limit = None  # type: ignore[assignment]

MATERIAL_DOCTYPE = materials_service.MATERIAL_DOCTYPE
SHARE_DOCTYPE = materials_service.SHARE_DOCTYPE

REFERENCE_DOCTYPES = ("Lead", "Opportunity", "Customer")

#: Per (IP, token) budget at the transport edge. A reader opens the page, maybe
#: refreshes, maybe forwards it to a partner: 20/min is generous for that and
#: still bounds a scrape.
RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SEC = 60

#: How long one viewer's opening is remembered before it counts again. Without
#: it, a reader who scrolls back to the tab three times reads as three separate
#: openings and the rep chases a prospect who looked once.
VIEW_THROTTLE_SEC = 30 * 60

_VIEW_KEY_PREFIX = "jarz_pos:mshare:seen:"

#: Fields read for the rep's picker. An allow-list here too, because this
#: response goes to a phone over mobile data and ``render_manifest`` is
#: kilobytes of JSON nobody on that screen needs.
_MATERIAL_FIELDS = (
    "name",
    "title",
    "title_ar",
    "material_type",
    "attachment",
    "is_default",
    "sort_order",
    "page_count",
    "render_status",
)


def _logger():
    return frappe.logger("jarz_materials", allow_site=True)


def _rate_limited(fn: Callable) -> Callable:
    """Apply Frappe's rate limiter when this build has one.

    Keyed on ``token``, which is why the public endpoint's parameter is named
    exactly that: the decorator reads ``frappe.form_dict[key]``, so renaming the
    parameter silently degrades this to an IP-only limit.
    """
    if _frappe_rate_limit is None:  # pragma: no cover - depends on Frappe build
        return fn
    return _frappe_rate_limit(
        key="token",
        limit=RATE_LIMIT_MAX_REQUESTS,
        seconds=RATE_LIMIT_WINDOW_SEC,
        ip_based=True,
    )(fn)


def _ensure_public_share_permission() -> None:
    """Gate for the guest endpoint. First statement, like every other module.

    There is deliberately no ``frappe.has_permission`` call, and that absence is
    a decision rather than an omission. ``frappe.session.user`` here is
    ``Guest``; the credential is the token.

    What it does enforce is the one thing a permissionless per-token endpoint
    genuinely needs: the response must never be cached. Without it a reverse
    proxy can store one customer's payload against a URL and hand it to the
    next caller — a cross-customer leak manufactured by infrastructure.
    """
    try:
        frappe.local.no_cache = 1
    except Exception:
        pass
    try:
        frappe.local.response_headers["Cache-Control"] = "no-store, private, max-age=0"
    except Exception:
        pass


def not_found() -> dict[str, Any]:
    """The single failure envelope. See rule 1 in the module docstring."""
    return {"ok": False, "error": "not found"}


def _as_list(value: Any) -> list[str]:
    """Accept a JSON array, a comma-joined string, or a real list.

    Dio form-encodes a Dart ``List`` into repeated keys that Frappe flattens to
    the LAST value only, so the app sends a JSON string — but ``bench execute``
    and the tests pass a real list, and a hand-built curl passes CSV.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError:
                items = [part for part in text.split(",")]
        else:
            items = [part for part in text.split(",")]
    else:
        items = [value]
    return [str(item).strip() for item in items if str(item or "").strip()]


def _ensure_reference(reference_doctype: str, reference_name: str) -> None:
    if reference_doctype not in REFERENCE_DOCTYPES:
        frappe.throw("Reference Type must be one of: " + ", ".join(REFERENCE_DOCTYPES))
    if not reference_name or not frappe.db.exists(reference_doctype, reference_name):
        frappe.throw(f"{reference_doctype} '{reference_name}' not found.")


def _display_title(row: Any) -> str:
    """English title, falling back to the Arabic one.

    The customer-facing page is English (see ``www/m.html``), so the English
    title leads; ``title_ar`` remains the fallback for a material that only has
    one, rather than showing the reader a bare record id.
    """
    if not hasattr(row, "get"):
        return ""
    english = (row.get("title") or "").strip()
    if english:
        return english
    return (row.get("title_ar") or row.get("name") or "").strip()


# ---------------------------------------------------------------------------
# Staff endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_sales_materials(include_disabled: Any = 0) -> dict[str, Any]:
    """The library the rep picks from, plus the message template.

    Bundled into one call on purpose: the send sheet needs both the moment it
    opens, and a rep standing in a cafe on a weak signal should pay one
    round trip, not two.

    Returns ``{"materials": [...], "message_template": str,
    "name_placeholder": str, "link_placeholder": str, "name_fallback": str}``.
    The placeholders are returned rather than hard-coded in the app so the
    substitution contract has exactly one definition.
    """
    _ensure_b2b_access()

    filters: dict[str, Any] = {}
    if not cint(include_disabled):
        filters["enabled"] = 1

    try:
        rows = frappe.get_all(
            MATERIAL_DOCTYPE,
            filters=filters,
            fields=list(_MATERIAL_FIELDS),
            order_by="sort_order asc, title asc",
            limit_page_length=0,
        )
    except Exception:
        # Code ships before ``bench migrate`` runs. An empty library is a send
        # sheet that says "nothing to send yet"; an exception is a crash on a
        # screen the rep just opened.
        _logger().error("get_sales_materials failed", exc_info=True)
        rows = []

    materials = [
        {
            "name": row.name,
            "title": row.title,
            "title_ar": row.title_ar,
            "display_title": _display_title(row),
            "material_type": row.material_type,
            "download_url": row.attachment,
            "is_default": bool(row.is_default),
            "page_count": cint(row.page_count),
            "ready": row.render_status
            in (materials_service.RENDER_READY, materials_service.RENDER_DOWNLOAD_ONLY),
        }
        for row in rows
    ]

    return {
        "materials": materials,
        "message_template": materials_service.default_message_template(),
        "name_placeholder": materials_service.NAME_PLACEHOLDER,
        "link_placeholder": materials_service.LINK_PLACEHOLDER,
        "name_fallback": materials_service.NAME_FALLBACK,
    }


@frappe.whitelist()
def create_material_share(
    reference_name: str,
    materials: Any = None,
    reference_doctype: str = "Lead",
    contact_name: str | None = None,
    contact_phone: str | None = None,
    message: str | None = None,
    channel: str = "WhatsApp",
    expires_hours: Any = None,
    log_note: Any = 1,
) -> dict[str, Any]:
    """Mint one share link and the WhatsApp deep link that carries it.

    ``message`` is the rep's edited template and may contain ``{name}`` and
    ``{link}``; both are substituted here, after the row exists and therefore
    after the URL does. See :func:`materials.render_message` for the case where
    the rep deleted the placeholder.

    Returns ``{"name", "token", "url", "whatsapp_url", "message", "msisdn",
    "pending_render": [...]}``. ``pending_render`` is not an error: the images
    are still being built and the customer's page will self-heal, but the app
    can warn a rep who is about to send a link to a 12MB catalogue that was
    uploaded ten seconds ago.
    """
    _ensure_b2b_access()
    _ensure_reference(reference_doctype, reference_name)

    names = _as_list(materials)
    if not names:
        frappe.throw("Pick at least one material to send.")

    rows = frappe.get_all(
        MATERIAL_DOCTYPE,
        filters={"name": ["in", names], "enabled": 1},
        fields=["name", "title", "title_ar", "material_type", "render_status"],
        limit_page_length=0,
    )
    found = {row.name: row for row in rows}
    missing = [name for name in names if name not in found]
    if missing:
        frappe.throw("These materials are unavailable: " + ", ".join(missing))

    doc = frappe.get_doc(
        {
            "doctype": SHARE_DOCTYPE,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "contact_name": (contact_name or "").strip() or None,
            "contact_phone": (contact_phone or "").strip() or None,
            "channel": (channel or "WhatsApp").strip() or "WhatsApp",
            # Ordered by the rep's selection, not by the query, so the price
            # list stays first if that is how they picked it.
            "items": [
                {
                    "material": name,
                    "title": found[name].title,
                    "material_type": found[name].material_type,
                }
                for name in names
            ],
        }
    )
    if cint(expires_hours) > 0:
        from frappe.utils import add_to_date

        doc.expires_on = add_to_date(now_datetime(), hours=cint(expires_hours))

    doc.insert(ignore_permissions=True)

    url = materials_service.share_url(doc.token)
    rendered = materials_service.render_message(message, url, doc.contact_name)
    frappe.db.set_value(SHARE_DOCTYPE, doc.name, "message", rendered, update_modified=False)
    doc.message = rendered

    pending = [
        name
        for name in names
        if found[name].render_status
        not in (materials_service.RENDER_READY, materials_service.RENDER_DOWNLOAD_ONLY)
    ]
    for name in pending:
        materials_service.enqueue_build(name)

    if cint(log_note):
        _log_send(doc, [found[name].title for name in names])

    msisdn = whatsapp_msisdn(doc.contact_phone)
    return {
        "name": doc.name,
        "token": doc.token,
        "url": url,
        "whatsapp_url": materials_service.whatsapp_url(msisdn, rendered),
        "message": rendered,
        "msisdn": msisdn,
        "pending_render": pending,
    }


def _log_send(share, titles: list[str]) -> None:
    """Write the rep's diary entry for the send. Best effort, always.

    A journey note that cannot be written must not lose the share: the link is
    already minted and the rep is about to press send in WhatsApp.
    """
    try:
        listed = "\n".join(f"- {title}" for title in titles)
        who = f" to {share.contact_name}" if share.contact_name else ""
        frappe.get_doc(
            {
                "doctype": "Jarz Journey Note",
                "reference_doctype": share.reference_doctype,
                "reference_name": share.reference_name,
                "entry_type": "WhatsApp",
                "contact_person": share.contact_name,
                "contact_phone": share.contact_phone,
                "note": f"Sent sales material{who} on WhatsApp.\n{listed}",
            }
        ).insert(ignore_permissions=True)
    except Exception:
        _logger().error(f"send journey note failed for {share.name}", exc_info=True)


@frappe.whitelist()
def get_material_shares(
    reference_name: str, reference_doctype: str = "Lead", limit: Any = 20
) -> dict[str, Any]:
    """Everything already sent to one record, newest first.

    Exists so a rep does not mint a fifth link to the same owner: the sheet
    shows "sent 3 days ago, opened twice" with the old URL ready to resend.
    """
    _ensure_b2b_access()
    _ensure_reference(reference_doctype, reference_name)

    try:
        rows = frappe.get_all(
            SHARE_DOCTYPE,
            filters={
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
            },
            fields=[
                "name",
                "token",
                "contact_name",
                "contact_phone",
                "channel",
                "sent_by",
                "sent_on",
                "view_count",
                "first_viewed_on",
                "last_viewed_on",
                "message",
            ],
            order_by="creation desc",
            limit_page_length=cint(limit) or 20,
        )
    except Exception:
        _logger().error("get_material_shares failed", exc_info=True)
        return {"shares": [], "count": 0}

    tokens = [row.token for row in rows]
    titles: dict[str, list[str]] = {}
    if tokens:
        try:
            for item in frappe.get_all(
                materials_service.SHARE_ITEM_DOCTYPE,
                filters={"parent": ["in", [row.name for row in rows]]},
                fields=["parent", "title", "material", "idx"],
                order_by="idx asc",
                limit_page_length=0,
            ):
                titles.setdefault(item.parent, []).append(item.title or item.material)
        except Exception:
            _logger().warning("share item titles failed", exc_info=True)

    # What the reader's device and attention looked like on their latest visit.
    # Guarded: a site that has not migrated the view DocType yet still returns a
    # perfectly good history, just without this.
    views = materials_service.latest_view_summary([row.name for row in rows])

    shares = []
    for row in rows:
        view = views.get(row.name) or {}
        shares.append(
            {
                "name": row.name,
                "url": materials_service.share_url(row.token),
                "contact_name": row.contact_name,
                "contact_phone": row.contact_phone,
                "channel": row.channel,
                "sent_by": row.sent_by,
                "sent_on": row.sent_on,
                "view_count": cint(row.view_count),
                "first_viewed_on": row.first_viewed_on,
                "last_viewed_on": row.last_viewed_on,
                "message": row.message,
                "titles": titles.get(row.name, []),
                "device_type": view.get("device_type") or "",
                "os": view.get("os") or "",
                "browser": view.get("browser") or "",
                "seconds": cint(view.get("seconds")),
                "pages_viewed": cint(view.get("pages_viewed")),
                "max_zoom": float(view.get("max_zoom") or 0),
                "downloaded": bool(cint(view.get("downloaded"))),
            }
        )
    return {"shares": shares, "count": len(shares)}


@frappe.whitelist()
def rebuild_material(name: str) -> dict[str, Any]:
    """Force a re-render of one material. Manager-only.

    The escape hatch for a render that failed on a corrupt upload or on a site
    that gained a PDF renderer after the fact.
    """
    frappe.has_permission(MATERIAL_DOCTYPE, ptype="write", throw=True)
    if not frappe.db.exists(MATERIAL_DOCTYPE, name):
        frappe.throw(f"Material '{name}' not found.")
    manifest = materials_service.build_derivatives(name, force=True)
    return {
        "ok": True,
        "pages": manifest.get("count", 0),
        "status": frappe.db.get_value(MATERIAL_DOCTYPE, name, "render_status"),
    }


# ---------------------------------------------------------------------------
# Customer endpoint
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
@_rate_limited
def get_public_share(
    token: str | None = None,
    screen: str | None = None,
    viewport: str | None = None,
    language: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Everything the customer's page renders, addressed by its share token.

    The parameter name ``token`` is load-bearing twice over: Frappe binds form
    keys to parameter names and silently discards anything the signature does
    not declare, and :func:`_rate_limited` keys its bucket off the same name.

    ``screen`` / ``viewport`` / ``language`` / ``timezone`` are the four things
    only the browser knows and none of which require a permission prompt. They
    ride along on this request rather than a second one, because a reader on
    mobile data should pay one round trip, not two. Everything else about the
    device is read server-side from the User-Agent.
    """
    _ensure_public_share_permission()
    try:
        return _resolve_public_share(
            token,
            client={
                "screen": screen,
                "viewport": viewport,
                "language": language,
                "timezone": timezone,
            },
        )
    except frappe.PermissionError:
        raise
    except Exception:
        # The traceback goes to the Error Log; the caller gets the same opaque
        # answer as a wrong token. Echoing str(exc) to a guest would leak
        # doctype names, field names and occasionally SQL.
        frappe.log_error(frappe.get_traceback(), "get_public_share failed")
        return not_found()


def _resolve_public_share(
    token: str | None, client: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Token -> payload. Every refusal collapses to :func:`not_found`."""
    cleaned = (token or "").strip()
    if not cleaned or len(cleaned) > 64:
        return not_found()

    try:
        name = frappe.db.get_value(SHARE_DOCTYPE, {"token": cleaned}, "name")
    except Exception:
        # Pre-migrate: the DocType does not exist yet. Same answer as a wrong
        # token, so the deploy window is not an information channel either.
        return not_found()
    if not name:
        return not_found()

    share = frappe.get_doc(SHARE_DOCTYPE, name)
    if share.is_expired():
        return not_found()

    items = []
    for row in share.items or []:
        material = frappe.db.get_value(
            MATERIAL_DOCTYPE,
            row.material,
            ["name", "title", "title_ar", "material_type", "attachment", "enabled"],
            as_dict=True,
        )
        if not material or not material.enabled:
            # A material pulled from the library disappears from links already
            # sent, rather than 404-ing the whole page.
            continue
        manifest = materials_service.manifest_for(material.name)
        pages = [
            {
                "thumb": page["tiers"]["thumb"]["u"],
                "screen": page["tiers"]["screen"]["u"],
                "full": page["tiers"]["full"]["u"],
                "w": page["tiers"]["full"]["w"],
                "h": page["tiers"]["full"]["h"],
            }
            for page in manifest.get("pages", [])
            if page.get("tiers", {}).get("full")
        ]
        if not pages and not material.attachment:
            continue
        # A material with no pages but a file -- a .docx price list, or a PDF on
        # a site with no renderer -- still ships as a download-only card. The
        # rep's send is otherwise silently short by one item.
        items.append(
            {
                "title": _display_title(material),
                "type": material.material_type,
                "download_url": material.attachment,
                "pages": pages,
            }
        )

    if not items:
        return not_found()

    view_name = _record_view(share, client or {})

    return {
        "ok": True,
        "greeting_name": (share.contact_name or "").strip() or None,
        "items": items,
        # Handed back so the page's end-of-session beacon can update the row it
        # created rather than guessing at one. Useless on its own: the beacon
        # also has to present the token, and the row must belong to it.
        "view": view_name,
    }


def _record_view(share, client: dict[str, Any]) -> str | None:
    """Count this opening, unless the same viewer already counted recently.

    Returns the ``Jarz Material View`` row this session belongs to, so the
    page's end-of-session beacon updates that row rather than opening a new one.

    The throttle key now HOLDS the view name instead of a bare "1". That is what
    makes a refresh behave correctly: the reader is not counted twice, and their
    reading time still lands on the row the first load created. Without it, a
    reader who reloads would either fork a second row or lose the beacon.

    Best effort in both directions: a Redis outage must neither double-count nor
    blank the customer's page, so a cache failure falls through to counting (an
    over-count is a worse rep signal than a miss, but a crash is worse than
    both).
    """
    fingerprint = _viewer_fingerprint()
    cache = None
    key = None
    try:
        cache = frappe.cache()
        key = cache.make_key(f"{_VIEW_KEY_PREFIX}{share.token}:{fingerprint}")
        existing = cache.get_value(key)
        if existing:
            # Same reader, same window: reuse the row, count nothing new.
            return str(existing)
    except Exception:
        _logger().warning("view throttle unavailable", exc_info=True)
        cache = None

    view_name = materials_service.record_view(share, client, fingerprint)

    if cache is not None and key is not None:
        try:
            cache.set_value(key, view_name or "1", expires_in_sec=VIEW_THROTTLE_SEC)
        except Exception:
            pass

    try:
        share.record_view()
        frappe.db.commit()
    except Exception:
        _logger().error(f"record_view failed for {share.name}", exc_info=True)

    return view_name


@frappe.whitelist(allow_guest=True)
@_rate_limited
def record_material_engagement(
    token: str | None = None,
    view: str | None = None,
    seconds: Any = None,
    pages: Any = None,
    zoom: Any = None,
    downloaded: Any = None,
) -> dict[str, Any]:
    """End-of-session beacon from the customer's page. Guest-callable.

    Answers the question a rep actually has after "did they open it?" — did they
    *read* it. Seconds visible, how many pages they reached, how far they zoomed
    (anything above 1 means they were reading detail rather than glancing), and
    whether they took the file away.

    Everything here is observable without asking the reader for any permission:
    no geolocation, no cookies beyond the session, no fingerprinting library.

    Authorised by the same token as the page, and the view row must belong to
    that token — see :func:`materials.update_view_engagement`. Values only ever
    move up, so a replayed or duplicated beacon is harmless.

    Always returns ``{"ok": True}``. A beacon is fire-and-forget by nature
    (``navigator.sendBeacon`` discards the response), so reporting a failure
    would be shouting into a void; genuine errors go to the log instead.
    """
    _ensure_public_share_permission()
    try:
        cleaned = (token or "").strip()
        view_name = (view or "").strip()
        if not cleaned or not view_name or len(cleaned) > 64 or len(view_name) > 64:
            return {"ok": True}
        share_name = frappe.db.get_value(SHARE_DOCTYPE, {"token": cleaned}, "name")
        if not share_name:
            return {"ok": True}
        materials_service.update_view_engagement(
            view_name,
            share_name,
            seconds=seconds,
            pages_viewed=pages,
            max_zoom=zoom,
            downloaded=downloaded,
        )
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "record_material_engagement failed")
    return {"ok": True}


def _viewer_fingerprint() -> str:
    """A coarse, non-identifying key for the view throttle.

    A hash of the client IP and user agent, truncated. Deliberately hashed and
    never stored on the document: the feature needs "is this the same tab as a
    minute ago", which does not require keeping the customer's IP address in
    the database.
    """
    import hashlib

    parts = []
    try:
        parts.append(frappe.local.request_ip or "")
    except Exception:
        pass
    try:
        parts.append(frappe.get_request_header("User-Agent") or "")
    except Exception:
        pass
    raw = "|".join(parts) or "anonymous"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]
