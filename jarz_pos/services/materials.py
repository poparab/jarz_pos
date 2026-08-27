"""Rendering and sharing of B2B sales material (price lists, product photos).

A rep finishes a visit, records the contacts, and now has to put the price list
in the owner's hands. Attaching five files to a WhatsApp chat one at a time is
the manual process this module replaces: the rep picks a *pack*, the backend
mints one unguessable link, and the owner opens a page built for one job --
reading a price list on a phone.

**Why the page serves images and not the PDF.** Mobile browsers treat an inline
PDF as somebody else's problem: Android Chrome usually downloads it, iOS Safari
renders it in a viewer with its own zoom that fights the page, and neither can
be made to feel like the phone's own gallery. So every source -- PDF page or
JPG -- is rasterised here, once, into a ladder of JPEGs, and the viewer gets a
plain ``<img>`` it can transform on the GPU. The original file stays
downloadable for the owner who wants to forward it.

**Why three tiers and not one.**

``thumb``   480px  -- the feed list. Dozens can decode without stalling a scroll.
``screen``  1600px -- what the viewer opens with. Sharp at any phone's fit-width
                      on a 3x display, and small enough to arrive over 3G.
``full``    3200px -- swapped in once the reader pinches. Roughly 2.7x the
                      pixels a 400pt-wide phone can show, which is the whole
                      point: past that, zooming reveals detail rather than blur.

The ladder stops at 3200 on purpose. A decoded JPEG costs width*height*4 bytes
of RAM, so 3200x2400 is ~30MB and a 6400px tier would be ~120MB -- an
out-of-memory kill on the low-end Androids this audience actually carries.
Sharpness past 3200 comes from "Download the original", not from a bigger tier.

**Derivatives are public static files, deliberately.** They live under
``public/files/jarz_materials/<material>/<digest>/`` and are served by nginx
without touching Python. Routing them through a token-checked endpoint would put
a Frappe worker in front of every image request and defeat HTTP caching -- i.e.
it would produce exactly the lag this feature exists to avoid. The path carries
a content digest, so the URLs are unguessable in practice and a re-upload
publishes a new directory instead of poisoning a cache. What IS token-gated is
the *set*: knowing that a price list exists tells you nothing about which lead
was sent what, and the share payload is the only place that mapping lives.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from typing import Any

import frappe
from frappe.utils import get_url

MATERIAL_DOCTYPE = "Jarz Sales Material"
SHARE_DOCTYPE = "Jarz Material Share"
SHARE_ITEM_DOCTYPE = "Jarz Material Share Item"

#: Public route prefix for a share link. Must agree with
#: ``hooks.website_route_rules`` and with the path parsing in ``www/m.html``.
SHARE_ROUTE_PREFIX = "/m"

#: Bytes of entropy per share token -> a 16-character URL-safe string. Shorter
#: than the tracking token (16 bytes) on purpose: this link is read by a human
#: inside a WhatsApp message, and 96 bits is still unguessable.
TOKEN_BYTES = 12

#: (name, long edge in px, JPEG quality). Order matters: the largest is rendered
#: first and every smaller tier is derived from it, so a PDF page is rasterised
#: exactly once.
TIERS: tuple[tuple[str, int, int], ...] = (
    ("full", 3200, 88),
    ("screen", 1600, 84),
    ("thumb", 480, 76),
)

TIER_NAMES = tuple(name for name, _, _ in TIERS)

#: Text renders soft at the quality that suits a photograph, and a price list is
#: nothing but text. Applied to pages that came out of a PDF.
_TEXT_QUALITY_BONUS = 6

#: Hard ceiling on pages taken from one source. A 300-page catalogue would
#: otherwise write 900 JPEGs and stall a worker; the original stays downloadable.
MAX_PAGES = 40

#: Long edge, in pixels, that a PDF page is rasterised at. Equal to the largest
#: tier so nothing is ever upscaled.
PDF_RENDER_LONG_EDGE = 3200

#: pdfium will happily render at any scale and then exhaust the box. A4 at 72dpi
#: is 842pt on the long edge, so 3200/842 ~ 3.8; the cap only bites on sources
#: with unusually small page boxes.
PDF_MAX_SCALE = 8.0

#: Extensions handled as a single-page image. Anything else that is not a PDF is
#: recorded as "download only" rather than failed -- a rep attaching a .docx
#: price list should still get a working link.
IMAGE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
)
PDF_EXTENSIONS = (".pdf",)

#: Directory under ``sites/<site>/public/files`` holding every derivative.
CACHE_DIRNAME = "jarz_materials"

RENDER_PENDING = "Pending"
RENDER_READY = "Ready"
RENDER_FAILED = "Failed"
RENDER_DOWNLOAD_ONLY = "Download Only"


def _logger():
    return frappe.logger("jarz_materials", allow_site=True)


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def source_path(file_url: str | None) -> str:
    """Absolute path on disk of the File behind ``file_url``.

    Resolution goes through the ``File`` DocType first because that is the only
    thing that knows whether the row was later made private (which moves the
    bytes to a different directory). The manual fallback exists because a
    ``file_url`` whose File row was deleted must still render rather than take
    the whole material down.
    """
    url = (file_url or "").strip()
    if not url:
        frappe.throw("This material has no attached file.")

    name = None
    try:
        name = frappe.db.get_value("File", {"file_url": url}, "name")
    except Exception:
        name = None
    if name:
        try:
            path = frappe.get_doc("File", name).get_full_path()
            if path and os.path.exists(path):
                return path
        except Exception:
            _logger().warning(f"File.get_full_path failed for {url}", exc_info=True)

    clean = url.split("?")[0]
    basename = os.path.basename(clean)
    for candidate in (
        frappe.get_site_path("public", "files", basename),
        frappe.get_site_path("private", "files", basename),
    ):
        if os.path.exists(candidate):
            return candidate

    frappe.throw(f"The file behind {url} is missing from disk.")


def content_digest(path: str) -> str:
    """First 12 hex characters of the file's SHA-256.

    Keys the cache directory. Re-uploading a corrected price list under the same
    material therefore writes a NEW directory, and every link minted afterwards
    points at the new pixels while the sweep drops what nothing can reach.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Rasterisation
# ---------------------------------------------------------------------------


def _load_image_pages(path: str) -> list:
    """One page: the image itself, EXIF-rotated and flattened to RGB.

    ``exif_transpose`` is not a nicety. A photo shot in portrait on any phone is
    stored landscape with an orientation tag; resave without honouring the tag
    and every product photo the rep takes arrives sideways.
    """
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            # A transparent PNG composited onto white, because JPEG has no alpha
            # and Pillow would otherwise render the transparent parts black.
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert("RGB")
        return [img.copy()]


def _load_pdf_pages(path: str) -> list:
    """Every page of the PDF, rasterised at :data:`PDF_RENDER_LONG_EDGE`.

    ``pypdfium2`` first (Apache/BSD, self-contained wheels, no system poppler),
    ``pymupdf`` second if the site happens to carry it. Neither being present is
    not fatal -- :func:`build_derivatives` records the material as download-only
    and the share link still works.
    """
    from PIL import Image

    try:
        import pypdfium2  # type: ignore
    except ImportError:
        pypdfium2 = None

    if pypdfium2 is not None:
        pdf = pypdfium2.PdfDocument(path)
        try:
            pages = []
            for index in range(min(len(pdf), MAX_PAGES)):
                page = pdf[index]
                width, height = page.get_size()
                longest = max(width, height) or 1
                scale = min(PDF_RENDER_LONG_EDGE / longest, PDF_MAX_SCALE)
                bitmap = page.render(scale=scale)
                pages.append(bitmap.to_pil().convert("RGB"))
            return pages
        finally:
            pdf.close()

    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("no PDF renderer installed (pypdfium2 or pymupdf)") from exc

    doc = fitz.open(path)
    try:
        pages = []
        for index in range(min(doc.page_count, MAX_PAGES)):
            page = doc.load_page(index)
            rect = page.rect
            longest = max(rect.width, rect.height) or 1
            scale = min(PDF_RENDER_LONG_EDGE / longest, PDF_MAX_SCALE)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            pages.append(
                Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            )
        return pages
    finally:
        doc.close()


def _cache_dir(material: str, digest: str) -> str:
    return frappe.get_site_path("public", "files", CACHE_DIRNAME, material, digest)


def _cache_url(material: str, digest: str, filename: str) -> str:
    return f"/files/{CACHE_DIRNAME}/{material}/{digest}/{filename}"


def _write_tier(image, target_long_edge: int, quality: int, path: str) -> tuple[int, int]:
    """Save one tier and return its real pixel size.

    **Never upscales.** A 900px photo asked for the 3200px tier is written at
    900px and the manifest says so, so the viewer's zoom ceiling stays honest
    instead of promising detail that was interpolated into existence.
    """
    from PIL import Image

    width, height = image.size
    longest = max(width, height) or 1
    if longest > target_long_edge:
        ratio = target_long_edge / longest
        size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
        resized = image.resize(size, Image.LANCZOS)
    else:
        resized = image

    tmp = path + ".tmp"
    resized.save(
        tmp,
        format="JPEG",
        quality=quality,
        # Progressive: the reader sees a whole blurry page immediately instead
        # of a sharp top edge crawling down, which on 3G is the difference
        # between "loading" and "broken".
        progressive=True,
        optimize=True,
        # 4:4:4 for the sharp tiers. Chroma subsampling is invisible on a
        # photograph and very visible on the coloured text of a price list.
        subsampling=0 if quality >= 88 else 2,
    )
    os.replace(tmp, path)
    return resized.size


def build_derivatives(material: str, force: bool = False) -> dict[str, Any]:
    """Rasterise one material into the tier ladder. Returns its manifest.

    Idempotent and cheap to re-call: an unchanged source with a manifest already
    on disk returns immediately, which is what makes it safe to call lazily from
    the share endpoint as a self-heal.
    """
    doc = frappe.get_doc(MATERIAL_DOCTYPE, material)
    path = source_path(doc.attachment)
    digest = content_digest(path)
    directory = _cache_dir(material, digest)
    manifest_path = os.path.join(directory, "manifest.json")

    if (
        not force
        and doc.source_hash == digest
        and doc.render_status in (RENDER_READY, RENDER_DOWNLOAD_ONLY)
        and os.path.exists(manifest_path)
    ):
        try:
            stored = json.loads(doc.render_manifest or "null")
            if stored:
                return stored
        except (ValueError, TypeError):
            pass
        return _read_manifest(manifest_path)

    extension = os.path.splitext(path)[1].lower()
    status = RENDER_READY
    error = None
    pages: list = []
    from_text = False

    try:
        if extension in PDF_EXTENSIONS:
            pages = _load_pdf_pages(path)
            from_text = True
        elif extension in IMAGE_EXTENSIONS:
            pages = _load_image_pages(path)
        else:
            status = RENDER_DOWNLOAD_ONLY
    except Exception as exc:  # noqa: BLE001 - recorded on the doc, never swallowed
        _logger().error(f"build_derivatives({material}) rasterise failed", exc_info=True)
        status = RENDER_FAILED
        error = str(exc)[:1000]

    manifest: dict[str, Any] = {"digest": digest, "pages": [], "count": 0}

    if pages:
        os.makedirs(directory, exist_ok=True)
        try:
            for index, page in enumerate(pages):
                entry: dict[str, Any] = {"i": index, "tiers": {}}
                for tier_name, long_edge, quality in TIERS:
                    filename = f"p{index}-{tier_name}.jpg"
                    size = _write_tier(
                        page,
                        long_edge,
                        min(96, quality + (_TEXT_QUALITY_BONUS if from_text else 0)),
                        os.path.join(directory, filename),
                    )
                    entry["tiers"][tier_name] = {
                        "u": _cache_url(material, digest, filename),
                        "w": size[0],
                        "h": size[1],
                    }
                entry["w"] = entry["tiers"]["full"]["w"]
                entry["h"] = entry["tiers"]["full"]["h"]
                manifest["pages"].append(entry)
                page.close()
            manifest["count"] = len(manifest["pages"])
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
        except Exception as exc:  # noqa: BLE001
            _logger().error(f"build_derivatives({material}) write failed", exc_info=True)
            status = RENDER_FAILED
            error = str(exc)[:1000]
            manifest = {"digest": digest, "pages": [], "count": 0}

    _sweep_stale_digests(material, keep=digest)

    frappe.db.set_value(
        MATERIAL_DOCTYPE,
        material,
        {
            "source_hash": digest,
            "page_count": manifest["count"],
            "render_status": status,
            "render_error": error,
            "render_manifest": json.dumps(manifest),
        },
        update_modified=False,
    )
    return manifest


def _read_manifest(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"pages": [], "count": 0}


def _sweep_stale_digests(material: str, keep: str) -> None:
    """Drop cache directories for superseded versions of this material.

    Deliberately runs on rebuild rather than on a schedule: the moment a new
    digest is written is the moment the old one stops being reachable from any
    freshly minted link, and leaving it costs disk on a box that also holds the
    database.
    """
    root = frappe.get_site_path("public", "files", CACHE_DIRNAME, material)
    if not os.path.isdir(root):
        return
    for entry in os.listdir(root):
        if entry == keep:
            continue
        victim = os.path.join(root, entry)
        if os.path.isdir(victim):
            shutil.rmtree(victim, ignore_errors=True)


def drop_cache(material: str) -> None:
    """Remove every derivative of one material (called when it is deleted)."""
    root = frappe.get_site_path("public", "files", CACHE_DIRNAME, material)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


def enqueue_build(material: str, force: bool = False) -> None:
    """Rasterise in the background, falling back to inline on a queue-less site.

    A 20-page catalogue is seconds of CPU: too long to hold a form submit, and
    nothing for a worker. ``bench execute`` and the test runner have no worker,
    hence the fallback.
    """
    try:
        frappe.enqueue(
            "jarz_pos.services.materials.build_derivatives",
            queue="long",
            timeout=1800,
            material=material,
            force=force,
            enqueue_after_commit=True,
        )
    except Exception:
        _logger().warning(f"enqueue_build({material}) fell back to inline", exc_info=True)
        try:
            build_derivatives(material, force=force)
        except Exception:
            _logger().error(f"inline build_derivatives({material}) failed", exc_info=True)


def manifest_for(material: str, build_if_missing: bool = True) -> dict[str, Any]:
    """The stored manifest, rebuilt on the spot if it is absent or stale.

    The self-heal is what makes a share link safe to send the second after the
    material was uploaded: if the worker has not run yet, the first reader pays
    a few seconds instead of opening an empty page.
    """
    row = frappe.db.get_value(
        MATERIAL_DOCTYPE,
        material,
        ["render_manifest", "render_status", "source_hash"],
        as_dict=True,
    )
    if not row:
        return {"pages": [], "count": 0}

    if row.render_status in (RENDER_READY, RENDER_DOWNLOAD_ONLY) and row.render_manifest:
        manifest = None
        try:
            manifest = json.loads(row.render_manifest)
        except (ValueError, TypeError):
            manifest = None
        if manifest is not None:
            if not manifest.get("count"):
                # Download-only material: nothing to rasterise, nothing stale.
                if row.render_status == RENDER_DOWNLOAD_ONLY:
                    return manifest
            elif os.path.isdir(_cache_dir(material, row.source_hash or "")):
                return manifest

    if not build_if_missing:
        return {"pages": [], "count": 0}
    try:
        return build_derivatives(material, force=True)
    except Exception:
        _logger().error(f"manifest_for({material}) rebuild failed", exc_info=True)
        return {"pages": [], "count": 0}


# ---------------------------------------------------------------------------
# Share tokens and links
# ---------------------------------------------------------------------------


def new_token() -> str:
    """A fresh, collision-checked share token."""
    for _ in range(8):
        token = secrets.token_urlsafe(TOKEN_BYTES)
        if not frappe.db.exists(SHARE_DOCTYPE, {"token": token}):
            return token
    frappe.throw("Could not mint a unique share token.")
    return ""  # unreachable; keeps the return type honest for callers


def share_url(token: str) -> str:
    """Absolute public URL of a share, e.g. ``https://erp.orderjarz.com/m/AbC...``."""
    return f"{get_url().rstrip('/')}{SHARE_ROUTE_PREFIX}/{token}"


def whatsapp_url(msisdn: str, message: str) -> str:
    """``wa.me`` deep link, or a plain ``send?text=`` when there is no number.

    ``wa.me`` is the only officially supported way to open a chat from outside
    WhatsApp, and it can carry text but never a file -- which is precisely why
    this feature sends a link to a page instead of attachments. Without an
    MSISDN the link still opens WhatsApp with the message composed and lets the
    rep pick the chat.
    """
    from urllib.parse import quote

    text = quote(message or "", safe="")
    digits = "".join(ch for ch in (msisdn or "") if ch.isdigit())
    if digits:
        return f"https://wa.me/{digits}?text={text}"
    return f"https://wa.me/?text={text}"


#: Substituted with the contact's first name, or with :data:`NAME_FALLBACK`.
NAME_PLACEHOLDER = "{name}"

#: Substituted with the share URL. The rep may move it anywhere in the message,
#: or delete it -- see :func:`render_message` for what happens then.
LINK_PLACEHOLDER = "{link}"

#: Used when the rep sends to a number with no name against it. Lives here, not
#: in the app, so one Arabic copy deck governs both.
NAME_FALLBACK = "بحضرتك"


def default_message_template() -> str:
    """The Arabic message the rep sends, before the placeholders are filled.

    Arabic-first because the reader is an Egyptian cafe owner, and phrased to do
    one job the bare link cannot: say that it opens in the browser, so it is not
    mistaken for a download they have no space for.

    Returned as a *template* rather than finished text because the app shows it
    in an editable box before sending, and the URL does not exist until the
    share row is inserted. The rep edits around ``{link}``; the server fills it
    in at the last moment, so no amount of editing can produce a message with a
    broken or missing link.
    """
    return "\n".join(
        [
            f"أهلاً {NAME_PLACEHOLDER} 👋",
            "معاك فريق چارز.",
            "ده لينك قائمة الأسعار وصور المنتجات:",
            LINK_PLACEHOLDER,
            "",
            "بيفتح على الموبايل على طول من غير تحميل، وتقدر تكبّر بصباعك تشوف التفاصيل.",
        ]
    )


def render_message(template: str | None, url: str, contact_name: str | None = None) -> str:
    """Fill ``{name}`` and ``{link}`` in the rep's message.

    A template that lost its ``{link}`` -- deleted by accident while editing --
    gets the URL appended rather than sent without it. A message about a price
    list that does not contain the price list is the one failure mode worth
    defending against unconditionally.
    """
    text = (template or "").strip() or default_message_template()
    person = (contact_name or "").strip() or NAME_FALLBACK
    text = text.replace(NAME_PLACEHOLDER, person)
    if LINK_PLACEHOLDER in text:
        return text.replace(LINK_PLACEHOLDER, url)
    if url and url not in text:
        return f"{text}\n{url}"
    return text


# ---------------------------------------------------------------------------
# Who opened it
# ---------------------------------------------------------------------------

VIEW_DOCTYPE = "Jarz Material View"

#: Upper bound on a single reading session, in seconds. The page reports its own
#: visible-time, and a tab left open for a week must not land in the diary as
#: "read the price list for six days".
MAX_SESSION_SECONDS = 2 * 3600


def parse_user_agent(ua: str | None) -> dict[str, str]:
    """Device / OS / browser from a User-Agent string.

    Hand-rolled rather than a dependency, because the question this answers is
    narrow: was the prospect on a phone, and did they read it inside WhatsApp's
    in-app browser or in a real one. It is deliberately coarse — a wrong minor
    version costs nothing here, and a new dependency on every deploy costs more
    than the precision is worth.

    Order matters throughout. Every in-app browser also claims to be Safari or
    Chrome, and Edge/Samsung/Opera all claim to be Chrome, so the specific
    tokens have to be tested before the generic ones.
    """
    text = (ua or "").strip()
    if not text:
        return {"device_type": "", "os": "", "browser": ""}
    low = text.lower()

    if "ipad" in low or ("android" in low and "mobile" not in low):
        device = "Tablet"
    elif any(token in low for token in ("iphone", "ipod", "android", "mobile", "windows phone")):
        device = "Phone"
    else:
        device = "Desktop"

    if "iphone" in low or "ipad" in low or "ipod" in low or "cpu os" in low:
        os_name = "iOS"
    elif "android" in low:
        os_name = "Android"
    elif "windows" in low:
        os_name = "Windows"
    elif "mac os" in low or "macintosh" in low:
        os_name = "macOS"
    elif "cros" in low:
        os_name = "ChromeOS"
    elif "linux" in low:
        os_name = "Linux"
    else:
        os_name = ""

    # In-app browsers first: the whole point is telling "opened it straight from
    # the WhatsApp message" apart from "moved it to a real browser", which is a
    # meaningfully warmer signal for a rep.
    if "wv)" in low or "; wv" in low:
        browser = "In-app browser"
    elif "fban" in low or "fbav" in low:
        browser = "Facebook in-app"
    elif "instagram" in low:
        browser = "Instagram in-app"
    elif "edg/" in low or "edga/" in low or "edgios/" in low:
        browser = "Edge"
    elif "samsungbrowser" in low:
        browser = "Samsung Internet"
    elif "opr/" in low or "opera" in low:
        browser = "Opera"
    elif "firefox" in low or "fxios" in low:
        browser = "Firefox"
    elif "crios" in low or "chrome" in low:
        browser = "Chrome"
    elif "safari" in low:
        browser = "Safari"
    else:
        browser = ""

    return {"device_type": device, "os": os_name, "browser": browser}


def _clean_client_value(value: Any, limit: int = 60) -> str:
    """Client-supplied strings are untrusted; keep them short and printable."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:limit]


def record_view(share, client: dict[str, Any] | None = None, fingerprint: str = "") -> str | None:
    """Insert one :data:`VIEW_DOCTYPE` row. Returns its name, or None.

    Best effort by construction: a customer looking at a price list must never
    see an error because the analytics row could not be written.
    """
    from frappe.utils import now_datetime

    client = client or {}
    try:
        doc = frappe.get_doc(
            {
                "doctype": VIEW_DOCTYPE,
                "share": share.name,
                "reference_name": share.reference_name,
                "viewed_on": now_datetime(),
                "fingerprint": fingerprint or None,
                "screen": _clean_client_value(client.get("screen")),
                "viewport": _clean_client_value(client.get("viewport")),
                "language": _clean_client_value(client.get("language"), 20),
                "timezone": _clean_client_value(client.get("timezone"), 60),
                **parse_user_agent(_request_user_agent()),
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        _logger().error(f"record_view failed for {share.name}", exc_info=True)
        return None


def _request_user_agent() -> str:
    try:
        return frappe.get_request_header("User-Agent") or ""
    except Exception:
        return ""


def update_view_engagement(
    view_name: str,
    share_name: str,
    seconds: Any = None,
    pages_viewed: Any = None,
    max_zoom: Any = None,
    downloaded: Any = None,
) -> bool:
    """Fold a beacon's numbers into one view row. Monotonic, clamped, guarded.

    Only ever raises a value, never lowers it: the page sends cumulative totals
    and beacons can arrive out of order (or twice), so ``max`` is the only
    combination that is stable under both. The row must belong to ``share_name``
    — the token is the credential, and without that check any token holder could
    rewrite any view row by guessing its hash.
    """
    from frappe.utils import cint, flt

    try:
        row = frappe.db.get_value(
            VIEW_DOCTYPE,
            view_name,
            ["name", "share", "seconds", "pages_viewed", "max_zoom", "downloaded"],
            as_dict=True,
        )
    except Exception:
        return False
    if not row or row.share != share_name:
        return False

    values: dict[str, Any] = {}
    if seconds is not None:
        capped = max(0, min(cint(seconds), MAX_SESSION_SECONDS))
        if capped > cint(row.seconds):
            values["seconds"] = capped
    if pages_viewed is not None:
        pages = max(0, min(cint(pages_viewed), MAX_PAGES))
        if pages > cint(row.pages_viewed):
            values["pages_viewed"] = pages
    if max_zoom is not None:
        zoom = max(0.0, min(flt(max_zoom), 20.0))
        if zoom > flt(row.max_zoom):
            values["max_zoom"] = zoom
    if downloaded is not None and cint(downloaded) and not cint(row.downloaded):
        values["downloaded"] = 1

    if not values:
        return True
    try:
        frappe.db.set_value(VIEW_DOCTYPE, view_name, values, update_modified=False)
        return True
    except Exception:
        _logger().error(f"update_view_engagement failed for {view_name}", exc_info=True)
        return False


def latest_view_summary(share_names: list[str]) -> dict[str, dict[str, Any]]:
    """Newest view per share, for the rep's history list. Guarded -> {}."""
    if not share_names:
        return {}
    try:
        rows = frappe.get_all(
            VIEW_DOCTYPE,
            filters={"share": ["in", share_names]},
            fields=[
                "share",
                "viewed_on",
                "device_type",
                "os",
                "browser",
                "seconds",
                "pages_viewed",
                "max_zoom",
                "downloaded",
            ],
            order_by="viewed_on desc",
            limit_page_length=0,
        )
    except Exception:
        _logger().warning("latest_view_summary failed", exc_info=True)
        return {}

    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        # Ordered newest first, so the first row seen for a share is its latest.
        summary.setdefault(row.share, dict(row))
    return summary
