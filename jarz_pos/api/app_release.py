"""Mobile release floor: what build the app must be on, and the gate that enforces it.

The POS app ships as an APK from ``/pos/download/`` and Firebase App
Distribution, never Play Store, so there is no store-managed "update required"
flow to lean on. This module is that flow: :func:`get_app_requirement` tells a
client whether its build is still allowed to run, and :func:`before_request`
refuses API traffic from builds that are not, so a client that skips the check
still cannot write.

The floor lives in Jarz POS Settings as ``mobile_minimum_android_build``. It is
an Int on a Single, so an unset value reads back as 0 - which is exactly the
disabled state. A site that has never touched the setting is ungated, and no
patch or migration can accidentally lock a device out.

Build numbers are the revision count CI stamps into every APK
(``tool/release_metadata.dart``), so they increase monotonically and compare as
plain integers.

**Raising the floor is unforgiving.** Only ever raise it to a build that already
contains the update gate; devices older than that have no code to render the
blocking screen, so they would see nothing but failing requests.
"""

from __future__ import annotations

from typing import Any, Optional

import frappe

# Only Android is distributed as a file the user installs by hand. Web is
# whatever the server just served, and iOS goes through the courier web build,
# so neither can be - or needs to be - gated on a build number.
GATED_PLATFORM = "android"

DEFAULT_DOWNLOAD_PATH = "/pos/download/"

# Headers the app stamps on every request.
BUILD_HEADER = "X-Jarz-Build"
PLATFORM_HEADER = "X-Jarz-Platform"

# Refused requests answer 426 Upgrade Required: distinct from 401 (which would
# make the app log the user out) and from 403 (which reads as a permission bug).
UPGRADE_REQUIRED_STATUS = 426


class AppUpgradeRequired(Exception):
    """Raised to refuse a request from a build below the floor.

    Carries its own ``http_status_code`` because that attribute - not
    ``frappe.local.response["http_status_code"]`` - is what decides the status
    Frappe actually sends: ``frappe.app.handle_exception`` reads
    ``getattr(e, "http_status_code", 500)``. Raising a plain
    ``frappe.ValidationError`` here would answer 417, which the client has no
    reason to read as "upgrade".

    The response dict is still populated alongside it, because
    ``frappe.utils.response.report_error`` serialises ``frappe.local.response``
    into the body - that is how the download URL reaches the client.
    """

    http_status_code = UPGRADE_REQUIRED_STATUS

# Endpoints a blocked client must still reach, or it cannot show the user what
# is wrong or get them to the fix.
_GATE_EXEMPT_METHODS = {
    "jarz_pos.api.app_release.get_app_requirement",
    "jarz_pos.api.health.ping",
    "logout",
    "frappe.auth.get_logged_user",
}


def _as_build_number(value: Any) -> Optional[int]:
    """Coerce a build number to a positive int, or None if it is not one.

    Everything arriving here is untrusted: a header, a form field, or a Single
    value that may be None. ``int("12.0")`` and ``int(None)`` both raise, so
    both are funnelled into None rather than allowed to surface as a 500.
    """
    if value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _settings_int(fieldname: str) -> int:
    """Read an Int off the settings Single, treating anything unusable as 0.

    0 is the disabled sentinel for both build fields, so failing to 0 fails
    open. That is deliberate: a settings read that breaks must never brick
    every device in the field.
    """
    try:
        raw = frappe.db.get_single_value("Jarz POS Settings", fieldname)
    except Exception:
        return 0
    return _as_build_number(raw) or 0


def _settings_str(fieldname: str) -> str:
    try:
        return (frappe.db.get_single_value("Jarz POS Settings", fieldname) or "").strip()
    except Exception:
        return ""


def _download_url() -> str:
    """Where the update button sends the user.

    Falls back to ``<site>/pos/download/`` - the page publish_apk.ps1 keeps
    pointed at the newest APK - so the gate works on a site that never filled
    the setting in.
    """
    configured = _settings_str("mobile_apk_download_url")
    if configured:
        return configured
    try:
        base = (frappe.utils.get_url() or "").rstrip("/")
    except Exception:
        base = ""
    return base + DEFAULT_DOWNLOAD_PATH


def _normalize_platform(platform: Any) -> str:
    return str(platform or "").strip().lower()


def resolve_requirement(platform: Any, build_number: Any) -> "dict[str, Any]":
    """Pure decision function: given a client's platform and build, what now?

    Split out from the endpoint so both the whitelisted method and the request
    gate answer from one place, and so the decision is testable without a
    request context.
    """
    normalized_platform = _normalize_platform(platform)
    current_build = _as_build_number(build_number)

    minimum_build = _settings_int("mobile_minimum_android_build")
    latest_build = _settings_int("mobile_latest_android_build")

    gated = normalized_platform == GATED_PLATFORM

    # A client that does not report its build cannot be judged. Left as
    # update_required=False on purpose: builds predating this feature send no
    # build number, and blocking them would strand the very devices that have
    # no code to render the prompt.
    update_required = bool(
        gated and minimum_build and current_build and current_build < minimum_build
    )
    update_available = bool(
        gated and latest_build and current_build and current_build < latest_build
    )

    return {
        "ok": True,
        "platform": normalized_platform,
        "gated": gated,
        "current_build": current_build,
        "minimum_build": minimum_build,
        "latest_build": latest_build,
        "update_required": update_required,
        "update_available": update_available,
        "download_url": _download_url(),
        "message": _settings_str("mobile_force_update_message"),
    }


@frappe.whitelist(allow_guest=True)
def get_app_requirement(
    platform: Optional[str] = None,
    build_number: Optional[str] = None,
) -> "dict[str, Any]":
    """Tell the caller whether its build may still run.

    ``allow_guest`` because the check runs before login: a stale build must be
    stopped at the splash screen, not after it has authenticated and started
    writing. Nothing returned is site-specific enough to be worth protecting -
    it is a version floor and a public download URL.
    """
    try:
        return resolve_requirement(platform, build_number)
    except Exception:
        # Fail open, loudly. A broken version check must never be the reason
        # the POS will not open.
        frappe.log_error(frappe.get_traceback(), "get_app_requirement failed")
        return {
            "ok": False,
            "platform": _normalize_platform(platform),
            "gated": False,
            "current_build": _as_build_number(build_number),
            "minimum_build": 0,
            "latest_build": 0,
            "update_required": False,
            "update_available": False,
            "download_url": "",
            "message": "",
        }


def _requested_method() -> str:
    """The dotted method path of the current request, or '' if there is none."""
    try:
        form_dict = getattr(frappe.local, "form_dict", None) or {}
        method = form_dict.get("cmd") or ""
        if method:
            return str(method)
        path = str(getattr(frappe.local.request, "path", "") or "")
        marker = "/api/method/"
        if marker in path:
            return path.split(marker, 1)[1].strip("/")
        return ""
    except Exception:
        return ""


def _refuse_stale_build(build_number: int, minimum_build: int) -> None:
    """Answer 426 with everything the client needs to self-heal.

    Called outside the guarding try/except in :func:`before_request` so the
    refusal itself is never mistaken for a bug and swallowed.
    """
    message = (
        _settings_str("mobile_force_update_message")
        or "This app version ({current}) is out of date. Install build {minimum} or newer to continue.".format(
            current=build_number, minimum=minimum_build
        )
    )
    frappe.local.response["http_status_code"] = UPGRADE_REQUIRED_STATUS
    frappe.local.response["update_required"] = True
    frappe.local.response["minimum_build"] = minimum_build
    frappe.local.response["current_build"] = build_number
    frappe.local.response["download_url"] = _download_url()
    frappe.local.response["message"] = message
    raise AppUpgradeRequired(message)


def before_request() -> None:
    """Refuse API traffic from a build below the floor.

    The in-app screen is the primary gate; this is the backstop for a client
    that never ran the check - killed mid-launch, resumed from a stale isolate,
    or offline when it started. Without it, "force update" is only a request.

    The decision is wrapped whole in a try/except that swallows everything:
    this runs ahead of *every* request on the site, including Desk, Woo
    webhooks and the deploy verifier, and a bug here would take the site down
    rather than merely fail to block an old phone.
    """
    try:
        request = getattr(frappe.local, "request", None)
        if request is None:
            return

        headers = getattr(request, "headers", None)
        if headers is None:
            return

        build_number = _as_build_number(headers.get(BUILD_HEADER))
        if build_number is None:
            # No header: not our app, or a build old enough to predate it.
            # Either way there is nothing to compare and nobody to tell.
            return

        if _normalize_platform(headers.get(PLATFORM_HEADER)) != GATED_PLATFORM:
            return

        minimum_build = _settings_int("mobile_minimum_android_build")
        if not minimum_build or build_number >= minimum_build:
            return

        if _requested_method() in _GATE_EXEMPT_METHODS:
            return
    except Exception:
        # Any failure deciding => let the request through.
        return

    _refuse_stale_build(build_number, minimum_build)
