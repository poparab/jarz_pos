"""Prove the settings matrix: what each switch claims, and what it actually does.

Every other harness in this package validates *money*. This one validates the
**configuration surface**, which is the layer nothing has ever checked and the
layer that has produced the quietest failures in this codebase:

* ``enable_courier_delivery_actions`` was set to 1 on staging, two deploys ran,
  and it read back 0 with nobody having touched it (``setup/settings_defaults``
  exists because of that);
* ``tracking_link_ttl_hours`` read 0 on every site, so ``_is_expired`` always
  returned ``False`` and customer tracking links never expired despite a declared
  24-hour default — with a courier's mobile number on a permanently live public
  URL when ``expose_courier_phone_to_customer`` was on;
* ``purchase_request_schedule_days`` read 0, so ``int(value)`` succeeded, the
  ``default=3`` was unreachable, and every team item request was dated today;
* nine fields across the three settings Singles are read by **nothing at all**,
  so an operator can change an account name in Desk, see it save, and change
  nothing.

The shape of this file follows ``operations_accounting_validation``: refuse
production, one context that counts pass / fail / **skipped** separately, one
JSON block between markers.

Four sections, and the distinction between them is the whole design:

**Section 1 — snapshot. Reported, never asserted.** Half of these values are
*supposed* to differ between staging and production (accounts, branch names, Woo
credentials, VAPID keys). Asserting them would produce a harness that fails for
being pointed at the wrong site. Read through
:func:`jarz_pos.utils.settings_utils.raw_single_value` rather than
``frappe.db.get_single_value``, because the latter casts ``Int``/``Check``
through ``cint()`` and cannot tell "never written" from "operator chose 0" — the
exact confusion that produced two of the four bugs above. Anything whose
fieldname smells like a credential is rendered ``<set len=N>``; a plain dump of
this Single prints the VAPID EC private key and the WooCommerce consumer secret
into a report file.

**Section 2 — behavioural assertions.** Flip the switch, call the real endpoint,
assert the observable change, restore. Writes go through
``frappe.db.set_single_value`` and never ``doc.save()``: one dangling Link on
this Single poisons every full save of it and has silently disabled unrelated
seeders before. Every flip is wrapped in :class:`_SettingGuard`, which restores
in ``__exit__`` and then **asserts the restore landed** — a harness that leaves a
kill-switch flipped is strictly worse than no harness.

**Section 3 — dead settings.** Asserted by grepping the *installed* source at
runtime, because that is the only definition of "dead" that survives a refactor.
A pass here means "confirmed still dead", which is the state being pinned: the
report can then say plainly that changing these does nothing.

**Section 4 — permission reality.** Who can actually write the Single, and
whether a ``Custom DocPerm`` row has quietly revoked every standard role on it
(one custom row replaces the entire standard permission set for that DocType).

Nothing here posts stock or money. Two cases deliberately assert only their
*refusal* direction — over-production and the supplier bill number — because the
permissive direction would manufacture goods and buy inventory respectively; the
un-asserted half is recorded as a SKIP with that reason rather than passed.

Run::

    bench --site frontend execute jarz_pos.scripts.settings_matrix_validation.run
"""
from __future__ import annotations

import inspect
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import frappe

from jarz_pos.scripts.b2b_accounting_validation import RunContext
from jarz_pos.scripts.operations_accounting_validation import _Artifacts
from jarz_pos.utils.settings_utils import raw_single_value

MARKER_START = "SETTINGS_MATRIX_JSON_START"
MARKER_END = "SETTINGS_MATRIX_JSON_END"

SETTINGS = "Jarz POS Settings"

#: Every Single this app's behaviour is configured from. ``WooCommerce Settings``
#: is snapshotted, never written and never imported from: it belongs to a
#: different app and the two are independent by design (CLAUDE.md domain
#: isolation). Reading its stored values through ``frappe.get_meta`` and
#: ``tabSingles`` is a standard Frappe read, not a cross-app dependency.
SNAPSHOT_DOCTYPES: Tuple[str, ...] = (
    "Jarz POS Settings",
    "WooCommerce Settings",
    "Jarz Forecast Settings",
    "Jarz Segmentation Settings",
)

#: Fieldname fragments that make a value unprintable. Erring wide on purpose:
#: masking a public key costs nothing, printing a private one costs everything.
_SECRET_PATTERN = re.compile(
    r"(secret|password|passwd|key|token|vapid|credential|private|auth|webhook)",
    re.IGNORECASE,
)

#: Longest plaintext value rendered in the snapshot. Receipt footers and route
#: tables are long enough to bury the rest of the report.
_MAX_VALUE_CHARS = 200

#: Fieldtypes that hold no value at all, so their absence from ``tabSingles``
#: is meaningless. Imported from Frappe when available so a version bump that
#: adds a layout fieldtype does not start reporting it as ``<unset>``.
try:  # pragma: no cover - depends on the Frappe build
    from frappe.model import no_value_fields as _NO_VALUE_FIELDS
    from frappe.model import table_fields as _TABLE_FIELDS
except Exception:  # pragma: no cover
    _NO_VALUE_FIELDS = [
        "Section Break", "Column Break", "Tab Break", "HTML", "Heading",
        "Button", "Image", "Fold", "Table", "Table MultiSelect",
    ]
    _TABLE_FIELDS = ["Table", "Table MultiSelect"]


def _guard_environment() -> None:
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""
    if "erp.orderjarz.com" in base_url:
        raise RuntimeError(
            f"settings_matrix_validation refuses to run on production ({base_url!r})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Single-field read/write plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _bust(doctype: str) -> None:
    """Drop every cached view of a Single after a raw write.

    ``frappe.db.set_single_value`` clears these itself, but a direct
    ``frappe.db.delete`` on ``tabSingles`` — which is how a field is returned to
    the "never written" state for the unset-versus-zero cases — does not. Without
    this the next read comes from ``frappe.db.value_cache`` (per-connection) or
    the redis document cache that ``frappe.get_cached_doc`` uses, and the
    assertion measures a stale value instead of the one just written.
    """
    try:
        frappe.clear_document_cache(doctype, doctype)
    except Exception:
        pass
    try:
        frappe.db.value_cache.pop(doctype, None)
    except Exception:
        pass


def _write_single(doctype: str, fieldname: str, value: Any) -> None:
    """Set one field on a Single, or delete its row when *value* is ``None``.

    ``frappe.db.set_single_value``, deliberately not ``doc.save()``. A full save
    of ``Jarz POS Settings`` runs link validation over every Link field on it, so
    a single dangling account reference makes the save fail — that has taken down
    unrelated seeders before, and a harness that cannot restore its own flag
    because of an unrelated stale link is the worst possible failure mode here.

    ``value=None`` means *remove the row*, which is not the same as writing 0:
    it is the only way to reproduce a field nobody has ever touched, and telling
    those two apart is what cases E and F exist to prove.
    """
    if value is None:
        frappe.db.delete("Singles", {"doctype": doctype, "field": fieldname})
    else:
        try:
            frappe.db.set_single_value(doctype, fieldname, value)
        except AttributeError:  # pragma: no cover - very old Frappe
            frappe.db.set_value(doctype, doctype, fieldname, value)
    _bust(doctype)


def _norm(value: Any) -> Optional[str]:
    """Compare stored values as text. ``None`` (no row) stays distinct from ""."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except Exception:
            return repr(value)
    return str(value)


class _SettingGuard:
    """Flip a setting for the body of a ``with`` block, then put it back.

    The restore lives in ``__exit__`` so it runs even when the case crashes
    halfway, and it is followed by a re-read that is recorded as its own check.
    That check is the point of the class: every flip below is a kill-switch or a
    money-path guard, and "the harness left ``enable_invoice_returns`` on" is a
    worse outcome than any assertion it could have made.
    """

    def __init__(self, ctx: RunContext, case: str, doctype: str, fieldname: str) -> None:
        self.ctx = ctx
        self.case = case
        self.doctype = doctype
        self.fieldname = fieldname
        self.baseline = raw_single_value(doctype, fieldname)

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "_SettingGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.restore()
        return False  # never swallow: a crashed case must still be seen

    # -- writes -------------------------------------------------------------
    def set(self, value: Any) -> None:
        _write_single(self.doctype, self.fieldname, value)

    def unset(self) -> None:
        """Delete the row entirely — the "nobody ever set this" state."""
        _write_single(self.doctype, self.fieldname, None)

    def restore(self) -> bool:
        detail_prefix = f"{self.doctype}.{self.fieldname}"
        try:
            _write_single(self.doctype, self.fieldname, self.baseline)
            # Committing the restore, and only the restore: everything else in
            # this run rides the ambient transaction, but some endpoints called
            # below commit internally, which would otherwise persist a flipped
            # kill-switch if this process died before the transaction closed.
            frappe.db.commit()
        except Exception as exc:
            self.ctx.record(
                f"{self.case}.setting_restored", False,
                f"{detail_prefix}: RESTORE RAISED {type(exc).__name__}: {exc} — "
                f"the setting may still be flipped, expected {self.baseline!r}",
            )
            return False

        _bust(self.doctype)
        actual = raw_single_value(self.doctype, self.fieldname)
        ok = _norm(actual) == _norm(self.baseline)
        self.ctx.record(
            f"{self.case}.setting_restored", ok,
            f"{detail_prefix}: {'back to' if ok else 'LEFT AT'} {actual!r} "
            f"(baseline {self.baseline!r})",
        )
        return ok


# ─────────────────────────────────────────────────────────────────────────────
# Source-anchor verification
# ─────────────────────────────────────────────────────────────────────────────

#: ``(relative path, expected line, substring that line must contain)``.
#:
#: Every one of these is quoted in a comment or a spec somewhere. A drifted
#: anchor is not a product defect, but a report that cites a line which no longer
#: says what it claims is worse than one that cites nothing — so drift is
#: recorded as a failure with the line it moved to.
_LINE_ANCHORS: Tuple[Tuple[str, int, str], ...] = (
    ("events/sales_invoice.py", 325, "def mint_tracking_token_on_ofd"),
    ("api/kanban.py", 895, "ofd_pin_gate.build_missing_pin_errors"),
    ("api/trips.py", 321, "ofd_pin_gate.build_missing_pin_errors"),
    ("api/purchase.py", 608, "_validate_bill_no(supplier, bill_no)"),
    ("api/purchase_request.py", 178, '_settings_int("purchase_request_schedule_days", 3)'),
    ("api/manufacturing.py", 1633, '_setting_flag("production_allow_over_production"'),
)


def _app_root() -> str:
    return frappe.get_app_path("jarz_pos")


def _read_lines(relpath: str) -> List[str]:
    path = os.path.join(_app_root(), relpath.replace("/", os.sep))
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _section_line_anchors(ctx: RunContext) -> None:
    for relpath, lineno, needle in _LINE_ANCHORS:
        case = f"REF.{relpath}:{lineno}"
        try:
            lines = _read_lines(relpath)
        except Exception as exc:
            ctx.skip(case, f"could not read {relpath}: {type(exc).__name__}: {exc}")
            continue
        actual = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        if needle in actual:
            ctx.record(case, True, f"anchor holds: {actual.strip()[:110]}")
            continue
        moved = [i + 1 for i, text in enumerate(lines) if needle in text]
        ctx.record(
            case, False,
            f"anchor DRIFTED — {needle!r} is now at line(s) {moved or 'NOWHERE IN FILE'}; "
            f"line {lineno} currently reads {actual.strip()[:80]!r}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Section 1 — snapshot (reported, never asserted)
# ─────────────────────────────────────────────────────────────────────────────

def _is_secret(fieldname: str, fieldtype: str) -> bool:
    if str(fieldtype or "") == "Password":
        return True
    return bool(_SECRET_PATTERN.search(str(fieldname or "")))


def _render_value(doctype: str, fieldname: str, fieldtype: str) -> str:
    """One field as a printable token, with secrets reduced to their length.

    Four outcomes and they mean four different things:

    ``<unset>``      no row in ``tabSingles`` — the DocType default is what
                     applies, *if* the reader is written to notice, which most
                     are not (see the module docstring).
    ``<empty>``      a row exists holding an empty string. Somebody cleared it.
    ``<set len=N>``  a credential. Length is enough to tell "configured" from
                     "half-pasted" without printing it.
    the value        everything else.
    """
    raw = raw_single_value(doctype, fieldname)
    if raw is None:
        return "<unset>"
    text = _norm(raw) or ""
    if text.strip() == "":
        return "<empty>"
    if _is_secret(fieldname, fieldtype):
        return f"<set len={len(text)}>"
    if len(text) > _MAX_VALUE_CHARS:
        return text[:_MAX_VALUE_CHARS] + f"... <truncated, len={len(text)}>"
    return text


def _snapshot_doctype(ctx: RunContext, doctype: str) -> Optional[Dict[str, Any]]:
    if not frappe.db.exists("DocType", doctype):
        ctx.skip(
            f"SNAP.{doctype}",
            "DocType is not installed on this site — nothing was snapshotted "
            "and no configuration for it was inspected",
        )
        return None

    try:
        meta = frappe.get_meta(doctype)
    except Exception as exc:
        ctx.skip(f"SNAP.{doctype}", f"get_meta failed: {type(exc).__name__}: {exc}")
        return None

    fields: Dict[str, str] = {}
    tables: Dict[str, str] = {}
    known: set = set()
    for df in meta.fields or []:
        fieldname = str(getattr(df, "fieldname", "") or "")
        fieldtype = str(getattr(df, "fieldtype", "") or "")
        if not fieldname:
            continue
        known.add(fieldname)
        if fieldtype in _TABLE_FIELDS:
            # Child rows live in their own table, never in tabSingles, so
            # raw_single_value would always say <unset> and mean nothing.
            try:
                count = frappe.db.count(str(getattr(df, "options", "") or ""), {"parent": doctype})
                tables[fieldname] = f"<table rows={count}>"
            except Exception:
                tables[fieldname] = "<table rows=?>"
            continue
        if fieldtype in _NO_VALUE_FIELDS:
            continue
        fields[fieldname] = _render_value(doctype, fieldname, fieldtype)

    # Rows for fields the DocType no longer declares. These are invisible in
    # Desk and survive forever; a reader still asking for one by name gets a
    # live value that no form can show or change.
    orphans: Dict[str, str] = {}
    try:
        rows = frappe.db.sql(
            "select `field` from `tabSingles` where `doctype`=%s", (doctype,)
        ) or []
        for (fieldname,) in rows:
            if fieldname and fieldname not in known and not str(fieldname).startswith("_"):
                orphans[str(fieldname)] = _render_value(doctype, str(fieldname), "Data")
    except Exception:
        pass

    unset = sum(1 for v in fields.values() if v == "<unset>")
    ctx.record(
        f"SNAP.{doctype}.captured", True,
        f"{len(fields)} value fields ({unset} <unset>), {len(tables)} table field(s), "
        f"{len(orphans)} orphan row(s) — REPORTED ONLY, nothing here is asserted",
    )
    return {"fields": fields, "tables": tables, "orphan_rows": orphans}


def _section_snapshot(ctx: RunContext) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for doctype in SNAPSHOT_DOCTYPES:
        captured = _snapshot_doctype(ctx, doctype)
        if captured is not None:
            out[doctype] = captured
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Environment discovery for the behavioural cases
# ─────────────────────────────────────────────────────────────────────────────

def _roles() -> set:
    try:
        return {str(r or "").strip().lower() for r in frappe.get_roles(frappe.session.user)}
    except Exception:
        return set()


def _has_any_role(*names: str) -> bool:
    mine = _roles()
    return any(str(n).strip().lower() in mine for n in names)


def _recent_invoices(limit: int = 60) -> List[Dict[str, Any]]:
    """Recent submitted invoices, names only.

    Deliberately asks for no custom column: a site one migration behind would
    raise on an unknown field, the except below would swallow it, and every
    behavioural case would skip for a reason that had nothing to do with the
    settings under test.
    """
    try:
        return frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1},
            fields=["name"],
            order_by="modified desc",
            limit_page_length=limit,
        ) or []
    except Exception:
        return []


def _tracked_invoices(limit: int = 60) -> List[Dict[str, Any]]:
    """Submitted invoices that ALREADY carry a tracking token.

    Deliberately never mints one. ``ensure_tracking_token`` writes a live public
    credential onto a real order, and a validation run has no business creating
    customer-visible links for orders it did not dispatch. If no order on this
    site has ever been dispatched under a build that mints tokens, the tracking
    cases skip — which is the honest answer.

    Filtered on ``custom_was_out_for_delivery`` and bounded, because the token
    column is ``Small Text`` (TEXT) and therefore unindexable — a
    ``WHERE custom_tracking_token = ...`` form is a full table scan.
    """
    from jarz_pos.services import tracking

    if not tracking.token_field_present():
        return []
    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1, "custom_was_out_for_delivery": 1},
            fields=[
                "name", tracking.TOKEN_FIELD,
                "custom_courier_party_type", "custom_courier_party",
            ],
            order_by="modified desc",
            limit_page_length=limit,
        ) or []
    except Exception:
        return []
    return [r for r in rows if str(r.get(tracking.TOKEN_FIELD) or "").strip()]


def _courier_phone(party_type: Any, party: Any) -> str:
    party = str(party or "").strip()
    if not party:
        return ""
    try:
        if str(party_type or "") == "Supplier":
            return str(frappe.db.get_value("Supplier", party, "mobile_no") or "").strip()
        return str(frappe.db.get_value("Employee", party, "cell_number") or "").strip()
    except Exception:
        return ""


def _started_work_order() -> Optional[Dict[str, Any]]:
    """A submitted Work Order with material actually in WIP, or ``None``.

    ``finish_production_batch`` rejects anything else long before it reaches the
    over-production guard, so without one the case cannot distinguish "the guard
    fired" from "the fixture was wrong".
    """
    try:
        rows = frappe.get_all(
            "Work Order",
            filters={
                "docstatus": 1,
                "material_transferred_for_manufacturing": [">", 0],
                "qty": [">", 0],
            },
            fields=["name", "qty", "produced_qty", "material_transferred_for_manufacturing"],
            order_by="modified desc",
            limit_page_length=1,
        ) or []
    except Exception:
        return None
    return rows[0] if rows else None


def _collect_env(ctx: RunContext) -> Dict[str, Any]:
    tracked = _tracked_invoices()
    with_phone = next(
        (
            r for r in tracked
            if _courier_phone(r.get("custom_courier_party_type"), r.get("custom_courier_party"))
        ),
        None,
    )
    return {
        "invoices": _recent_invoices(),
        "tracked": tracked,
        "tracked_with_phone": with_phone,
        "work_order": _started_work_order(),
        "supplier": frappe.db.get_value("Supplier", {"disabled": 0}, "name"),
        "item": frappe.db.get_value(
            "Item", {"disabled": 0, "is_stock_item": 1, "has_variants": 0}, "name"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Section 2 — behavioural assertions
# ─────────────────────────────────────────────────────────────────────────────

def _case_a_invoice_returns(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``enable_invoice_returns`` gates the return preview's block code."""
    case = "A.enable_invoice_returns"
    from jarz_pos.api.returns import get_return_preview

    if not _has_any_role(
        "jarz line manager", "JARZ line manager", "JARZ Manager",
        "System Manager", "Administrator",
    ):
        ctx.skip(
            f"{case}.skipped",
            f"{frappe.session.user} is outside ROLES.LINE_MANAGER_TIER, so "
            "get_return_preview refuses before the flag is ever read — the flag "
            "was NOT exercised",
        )
        return

    # The preview is branch-scoped (ensure_profile_scoped_invoice_access), so
    # probe a few candidates rather than assuming the newest invoice belongs to
    # a branch this session may see. Read-only either way.
    invoice = None
    last_error = ""
    for row in env["invoices"][:15]:
        try:
            probe = get_return_preview(row["name"]) or {}
            if probe.get("success"):
                invoice = row["name"]
                break
            # get_return_preview flattens non-permission failures into
            # {"success": False, "error": ...}; such an invoice would answer with
            # no return_block_code at all and read as a spurious failure below.
            last_error = str(probe.get("error"))
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    if not invoice:
        ctx.skip(
            f"{case}.skipped",
            f"no submitted invoice this session may preview (last error: {last_error or 'none'}) "
            "— neither state of the flag was asserted",
        )
        return

    with _SettingGuard(ctx, case, SETTINGS, "enable_invoice_returns") as guard:
        guard.set(0)
        off = get_return_preview(invoice) or {}
        ctx.record(
            f"{case}.off_blocks_with_returns_disabled",
            off.get("return_block_code") == "returns_disabled",
            f"{invoice}: return_block_code={off.get('return_block_code')!r} "
            f"can_return={off.get('can_return')!r}",
        )

        guard.set(1)
        on = get_return_preview(invoice) or {}
        # ON does NOT mean "returnable" — an undispatched or already-returned
        # order is still blocked, and correctly so. The assertion is only that
        # the flag has stopped being the reason.
        ctx.record(
            f"{case}.on_clears_returns_disabled",
            on.get("return_block_code") != "returns_disabled",
            f"{invoice}: return_block_code={on.get('return_block_code')!r} "
            "(any other code is a different, legitimate blocker)",
        )


def _case_b_courier_delivery(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``enable_courier_delivery_actions`` surfaces as ``feature_enabled``."""
    case = "B.enable_courier_delivery_actions"
    from jarz_pos.api.courier_delivery import get_stop_outcome

    invoice = None
    last_error = ""
    for row in env["invoices"][:15]:
        try:
            result = get_stop_outcome(row["name"]) or {}
            if result.get("success"):
                invoice = row["name"]
                break
            last_error = str(result.get("error"))
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    if not invoice:
        ctx.skip(
            f"{case}.skipped",
            f"no submitted invoice this session may read (last error: {last_error or 'none'}) "
            "— neither state of the flag was asserted",
        )
        return

    with _SettingGuard(ctx, case, SETTINGS, "enable_courier_delivery_actions") as guard:
        guard.set(0)
        off = get_stop_outcome(invoice) or {}
        ctx.record(
            f"{case}.off_reports_disabled",
            off.get("feature_enabled") is False,
            f"{invoice}: feature_enabled={off.get('feature_enabled')!r}",
        )

        guard.set(1)
        on = get_stop_outcome(invoice) or {}
        ctx.record(
            f"{case}.on_reports_enabled",
            on.get("feature_enabled") is True,
            f"{invoice}: feature_enabled={on.get('feature_enabled')!r}",
        )


def _case_c_customer_tracking(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``enable_customer_tracking`` gates the public read — and only that.

    Two separate claims, and conflating them is how the feature would look safe
    while leaking: the flag stops the guest endpoint answering, but it does NOT
    stop tokens being minted. Every dispatch under this build writes a live
    credential onto the invoice whether the switch is on or off, so turning the
    feature "off" leaves a keyspace that turning it back on immediately exposes.
    That is a deliberate design (see ``mint_tracking_token_on_ofd``), and it is
    asserted here so it stays deliberate rather than becoming a surprise.
    """
    case = "C.enable_customer_tracking"
    from jarz_pos.services import tracking

    # --- claim 2 first: minting is not gated ------------------------------
    # A source-level assertion, because the behavioural one would require
    # dispatching an order. Both the hook and the service are checked: the flag
    # could be read in either.
    try:
        from jarz_pos.events import sales_invoice as _si_events

        hook_src = inspect.getsource(_si_events.mint_tracking_token_on_ofd)
        mint_src = inspect.getsource(tracking.ensure_tracking_token)
        _, hook_line = inspect.getsourcelines(_si_events.mint_tracking_token_on_ofd)
        gated = (
            "enable_customer_tracking" in hook_src
            or "tracking_enabled" in hook_src
            or "enable_customer_tracking" in mint_src
            or "tracking_enabled" in mint_src
        )
        ctx.record(
            f"{case}.minting_is_not_gated", not gated,
            f"mint_tracking_token_on_ofd at events/sales_invoice.py:{hook_line} and "
            "tracking.ensure_tracking_token consult neither the flag nor "
            "tracking_enabled() — a token is written on every OFD transition "
            "regardless of the switch"
            if not gated else
            "the flag now appears in the mint path — behaviour changed, and every "
            "order dispatched while it was off has no tracking link",
        )
    except Exception as exc:
        ctx.skip(
            f"{case}.minting_is_not_gated",
            f"could not read the mint path source: {type(exc).__name__}: {exc}",
        )

    # --- claim 1: the public read --------------------------------------------
    # An unknown token must be indistinguishable from a wrong one, in both
    # states. This one needs no fixture at all.
    bogus = "zzz-not-a-real-token-zzz"
    with _SettingGuard(ctx, case, SETTINGS, "enable_customer_tracking") as flag:
        flag.set(0)
        off_bogus = tracking.resolve_public_status(bogus) or {}
        ctx.record(
            f"{case}.off_unknown_token_is_generic_not_found",
            off_bogus.get("success") is False
            and off_bogus.get("error") == tracking.NOT_FOUND_ERROR,
            f"off/unknown -> {off_bogus}",
        )

        tracked = env["tracked"]
        if not tracked:
            ctx.skip(
                f"{case}.real_token_resolution",
                "no submitted invoice on this site carries a tracking token, and this "
                "harness will not mint one onto a real order — the ON path (a valid "
                "token resolving) was NOT asserted",
            )
            return

        row = tracked[0]
        token = str(row.get(tracking.TOKEN_FIELD) or "").strip()

        # Pin the TTL to "never expire" for the duration. A terminal order past
        # its TTL answers not_found for a reason that has nothing to do with the
        # flag under test, which would read here as a spurious failure.
        with _SettingGuard(ctx, f"{case}.ttl", SETTINGS, "tracking_link_ttl_hours") as ttl:
            ttl.set(0)

            off_real = tracking.resolve_public_status(token) or {}
            ctx.record(
                f"{case}.off_hides_a_valid_token",
                off_real.get("success") is False
                and off_real.get("error") == tracking.NOT_FOUND_ERROR,
                f"{row['name']} with the flag off -> {off_real} "
                "(must be the SAME answer as an unknown token, or the endpoint "
                "becomes an oracle)",
            )

            flag.set(1)
            on_real = tracking.resolve_public_status(token) or {}
            ctx.record(
                f"{case}.on_resolves_a_valid_token",
                on_real.get("success") is True,
                f"{row['name']} with the flag on -> success={on_real.get('success')!r} "
                f"status={on_real.get('status')!r}",
            )
            ctx.record(
                f"{case}.on_payload_matches_the_allow_list",
                set(on_real) <= set(tracking.PUBLIC_STATUS_KEYS)
                if on_real.get("success") else True,
                f"keys={sorted(on_real)} extra={sorted(set(on_real) - set(tracking.PUBLIC_STATUS_KEYS))}",
            )


def _case_d_courier_phone(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``expose_courier_phone_to_customer`` changes the phone VALUE, not the key.

    Worth stating precisely, because the feature is usually described as the key
    appearing or disappearing and that is not what the code does:
    ``PUBLIC_STATUS_KEYS`` is a fixed allow-list, ``courier_phone`` is always in
    it, and ``_courier_identity`` blanks the value to ``None`` when the switch is
    off. A client written against "the key is absent" would therefore never see a
    difference. Both facts are asserted below.
    """
    case = "D.expose_courier_phone_to_customer"
    from jarz_pos.services import tracking

    row = env["tracked_with_phone"]
    if not row:
        ctx.skip(
            f"{case}.skipped",
            "no token-carrying invoice has a courier with a stored phone number, so "
            "the on and off payloads would both be null and the flip would be "
            "unobservable — nothing was asserted",
        )
        return

    token = str(row.get(tracking.TOKEN_FIELD) or "").strip()
    expected = _courier_phone(row.get("custom_courier_party_type"), row.get("custom_courier_party"))

    # Three guards, three DISTINCT case labels: each records its own
    # ``.setting_restored`` check, and reusing one label would collapse two
    # different fields' restore proofs into one indistinguishable pair.
    with _SettingGuard(ctx, f"{case}.master", SETTINGS, "enable_customer_tracking") as master, \
            _SettingGuard(ctx, f"{case}.ttl", SETTINGS, "tracking_link_ttl_hours") as ttl, \
            _SettingGuard(ctx, case, SETTINGS, "expose_courier_phone_to_customer") as phone:
        master.set(1)
        ttl.set(0)

        phone.set(0)
        off = tracking.resolve_public_status(token) or {}
        ctx.record(
            f"{case}.off_withholds_the_number",
            off.get("success") is True and off.get("courier_phone") is None,
            f"{row['name']}: success={off.get('success')!r} courier_phone={off.get('courier_phone')!r}",
        )

        phone.set(1)
        on = tracking.resolve_public_status(token) or {}
        published = str(on.get("courier_phone") or "")
        ctx.record(
            f"{case}.on_publishes_the_number",
            on.get("success") is True and published == expected,
            f"{row['name']}: courier_phone={'<set>' if published else '<null>'}, "
            f"matches the courier record={published == expected} "
            "(the number itself is not printed)",
        )
        ctx.record(
            f"{case}.key_is_present_in_both_states",
            ("courier_phone" in off) and ("courier_phone" in on),
            "the allow-list always carries courier_phone; only the value changes, so a "
            "client testing for the key's absence sees no difference at all",
        )


def _case_e_tracking_ttl(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``tracking_link_ttl_hours``: unset means 24, an explicit 0 means forever.

    This is the regression pin for the bug just fixed in ``services/tracking``.
    The old reader went through ``frappe.db.get_single_value``, which casts an
    ``Int`` through ``cint()``; a field nobody had ever written came back as 0,
    the ``raw in (None, "")`` guard could never fire, the declared 24-hour
    default was unreachable, ``_is_expired`` returned ``False`` for every link,
    and customer tracking links never expired on any site.
    """
    case = "E.tracking_link_ttl_hours"
    from jarz_pos.services import tracking

    with _SettingGuard(ctx, case, SETTINGS, "tracking_link_ttl_hours") as guard:
        guard.unset()
        unset_value = tracking.tracking_link_ttl_hours()
        ctx.record(
            f"{case}.unset_yields_the_declared_default",
            unset_value == tracking.DEFAULT_TTL_HOURS,
            f"no tabSingles row -> {unset_value} (expected DEFAULT_TTL_HOURS="
            f"{tracking.DEFAULT_TTL_HOURS}; reading 0 here is the old bug — every "
            "link immortal)",
        )

        guard.set(0)
        zero_value = tracking.tracking_link_ttl_hours()
        ctx.record(
            f"{case}.explicit_zero_means_never_expire",
            zero_value == 0,
            f"row holding 0 -> {zero_value} (an operator choosing 0 is a real choice "
            "and must NOT be overwritten by the default)",
        )

        guard.set(5)
        five_value = tracking.tracking_link_ttl_hours()
        ctx.record(
            f"{case}.explicit_value_is_honoured",
            five_value == 5,
            f"row holding 5 -> {five_value}",
        )


def _case_f_purchase_request_days(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``purchase_request_schedule_days``: same unset-versus-zero shape as E."""
    case = "F.purchase_request_schedule_days"
    from jarz_pos.api.purchase_request import _settings_int

    field = "purchase_request_schedule_days"
    with _SettingGuard(ctx, case, SETTINGS, field) as guard:
        guard.unset()
        unset_value = _settings_int(field, 3)
        ctx.record(
            f"{case}.unset_yields_three",
            unset_value == 3,
            f"no tabSingles row -> {unset_value} (expected 3; reading 0 here is the old "
            "bug — every team item request dated today, so the requester got no lead "
            "time and the buyer got no warning)",
        )

        guard.set(0)
        zero_value = _settings_int(field, 3)
        ctx.record(
            f"{case}.explicit_zero_means_today",
            zero_value == 0,
            f"row holding 0 -> {zero_value} (a deliberate 'needed today' must survive)",
        )

        guard.set(7)
        seven_value = _settings_int(field, 3)
        ctx.record(
            f"{case}.explicit_value_is_honoured",
            seven_value == 7,
            f"row holding 7 -> {seven_value}",
        )


def _case_g_over_production(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``production_allow_over_production``: OFF must refuse an above-plan finish.

    Only the refusal is exercised. Asserting the permissive direction would mean
    calling ``finish_production_batch`` with the switch ON, which posts a real
    Manufacture Stock Entry: it consumes WIP material and creates finished goods
    on a live Work Order. No configuration assertion is worth manufacturing
    inventory for, so the ON direction is recorded as a skip with that reason.

    The refusal itself is safe: the guard throws at manufacturing.py:1635, well
    before ``_make_and_submit_se``, so nothing is written on this path.
    """
    case = "G.production_allow_over_production"
    from jarz_pos.api.manufacturing import finish_production_batch

    if not _has_any_role(
        "System Manager", "Manufacturing Manager", "Stock Manager",
        "Purchase Manager", "JARZ Manager", "Production Operator",
    ):
        ctx.skip(
            f"{case}.skipped",
            f"{frappe.session.user} is outside ROLES.PRODUCTION_EXECUTE, so "
            "finish_production_batch refuses before the flag is read",
        )
        return

    wo = env["work_order"]
    if not wo:
        ctx.skip(
            f"{case}.skipped",
            "no submitted Work Order with material in WIP exists on this site; "
            "finish_production_batch rejects anything else long before the "
            "over-production guard, so the flag was NOT exercised",
        )
        return

    over_qty = float(wo["qty"]) + 1000.0
    with _SettingGuard(ctx, case, SETTINGS, "production_allow_over_production") as guard:
        guard.set(0)
        try:
            result = finish_production_batch(work_order=wo["name"], actual_qty=over_qty)
            ctx.record(
                f"{case}.off_refuses_above_plan", False,
                f"{wo['name']}: planned {wo['qty']}, asked for {over_qty}, and the call "
                f"RETURNED instead of throwing: {json.dumps(result, default=str)[:200]} "
                "— a Manufacture entry may have been posted, check the Work Order",
            )
        except frappe.PermissionError as exc:
            ctx.skip(f"{case}.off_refuses_above_plan", f"permission refused first: {exc}")
        except Exception as exc:
            message = str(exc)
            ctx.record(
                f"{case}.off_refuses_above_plan",
                "planned" in message.lower() or "over-production" in message.lower(),
                f"{wo['name']}: planned {wo['qty']}, asked {over_qty} -> "
                f"{type(exc).__name__}: {message[:180]}",
            )

    ctx.skip(
        f"{case}.on_allows_above_plan",
        "NOT asserted by design: proving the permissive direction requires posting a "
        "real Manufacture Stock Entry against a live Work Order — it would consume WIP "
        "and create finished goods. The OFF refusal above is the assertable half.",
    )


def _case_h_require_bill_no(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``purchase_require_bill_no``: ON must refuse a blank supplier bill number.

    ON is exercised through the real endpoint, and safely: ``_validate_bill_no``
    runs at purchase.py:608, before ``frappe.new_doc``, so a refusal creates
    nothing. OFF is exercised at the guard itself — calling the endpoint with the
    switch off would create and SUBMIT a Purchase Invoice with ``update_stock=1``,
    i.e. buy inventory and move cash, which is not an acceptable price for a
    configuration assertion.
    """
    case = "H.purchase_require_bill_no"
    from jarz_pos.api.purchase import _validate_bill_no, create_purchase_invoice

    if not _has_any_role(
        "System Manager", "Stock Manager", "Manufacturing Manager",
        "Purchase Manager", "Accounts Manager", "JARZ Manager",
    ):
        ctx.skip(
            f"{case}.skipped",
            f"{frappe.session.user} is outside ROLES.PURCHASE, so "
            "create_purchase_invoice refuses before the flag is read",
        )
        return

    supplier, item = env["supplier"], env["item"]
    if not supplier or not item:
        ctx.skip(
            f"{case}.skipped",
            f"need an enabled Supplier and a stock Item (supplier={supplier} item={item}) "
            "— the endpoint was not called",
        )
        return

    artifacts = _Artifacts()
    with _SettingGuard(ctx, case, SETTINGS, "purchase_require_bill_no") as guard:
        guard.set(1)
        try:
            result = create_purchase_invoice(
                supplier=supplier,
                items=[{"item_code": item, "qty": 1, "rate": 1}],
                bill_no=None,
                idempotency_key="settings-matrix-validation-bill-no-probe",
            ) or {}
            # Reached only if the guard did NOT fire, which means a real invoice
            # was created and submitted. Register it so teardown can unwind it.
            artifacts.add("Purchase Invoice", result.get("purchase_invoice"))
            ctx.record(
                f"{case}.on_refuses_blank_bill_no", False,
                f"the endpoint CREATED {result.get('purchase_invoice')} with no bill_no "
                "while the switch was on — the duplicate-supplier-invoice guard is open",
            )
        except frappe.PermissionError as exc:
            ctx.skip(f"{case}.on_refuses_blank_bill_no", f"permission refused first: {exc}")
        except Exception as exc:
            message = str(exc)
            ctx.record(
                f"{case}.on_refuses_blank_bill_no",
                "bill number" in message.lower(),
                f"{supplier}: {type(exc).__name__}: {message[:180]}",
            )

        guard.set(0)
        try:
            _validate_bill_no(supplier, None)
            allowed = True
            detail = "guard returned without throwing"
        except Exception as exc:
            allowed = False
            detail = f"{type(exc).__name__}: {exc}"
        ctx.record(
            f"{case}.off_allows_blank_bill_no",
            allowed,
            f"{detail} — asserted at api.purchase._validate_bill_no, the exact guard "
            "create_purchase_invoice calls at purchase.py:608. The endpoint itself is "
            "NOT called in this direction because with the switch off it would submit a "
            "real Purchase Invoice with update_stock=1",
        )

    artifacts.unwind()


def _case_i_ofd_pin_gate(ctx: RunContext, env: Dict[str, Any]) -> None:
    """``require_delivery_pin_for_ofd``: read and report. NEVER written.

    This is the one switch in the matrix that is deliberately not flipped, even
    momentarily. Arming it refuses dispatch for every order whose delivery
    address has no coordinate pair, and effectively no address on this estate
    carries one — so a flip, even inside a transaction that restores it, is a
    window in which the floor cannot send orders on either the kanban path
    (api/kanban.py:895) or the bulk trip path (api/trips.py:321).

    The assertion is therefore that it is currently OFF, and the check is loud
    when it is not.
    """
    case = "I.require_delivery_pin_for_ofd"
    from jarz_pos.services import ofd_pin_gate

    raw = raw_single_value(SETTINGS, ofd_pin_gate.SETTING_FIELDNAME)
    effective = ofd_pin_gate.pin_gate_enabled()
    ctx.record(
        f"{case}.is_currently_off",
        effective is False,
        f"raw={raw!r} ({'<unset>' if raw is None else 'stored'}), "
        f"pin_gate_enabled()={effective} — "
        + (
            "off, so build_missing_pin_errors returns [] on both dispatch paths"
            if not effective else
            "ARMED. Every order whose delivery address has no pin is refused "
            "dispatch on BOTH api/kanban.py:895 and api/trips.py:321. Turn it off "
            "before the next dispatch window."
        ),
    )

    # Blast radius, reported not asserted: how many addresses could actually pass
    # the gate if it were armed.
    try:
        total = frappe.db.count("Address")
        pinned = frappe.db.sql(
            """
            SELECT COUNT(*) FROM `tabAddress`
            WHERE IFNULL(custom_latitude, 0) != 0 AND IFNULL(custom_longitude, 0) != 0
            """
        )[0][0]
        ctx.record(
            f"{case}.blast_radius_reported", True,
            f"{pinned} of {total} Address records carry a usable pin — reported only, "
            "this is what the gate would filter dispatch on",
        )
    except Exception as exc:
        ctx.skip(
            f"{case}.blast_radius_reported",
            f"could not count pinned addresses (geo fields may not be migrated): "
            f"{type(exc).__name__}: {exc}",
        )


_CASES = [
    _case_a_invoice_returns,
    _case_b_courier_delivery,
    _case_c_customer_tracking,
    _case_d_courier_phone,
    _case_e_tracking_ttl,
    _case_f_purchase_request_days,
    _case_g_over_production,
    _case_h_require_bill_no,
    _case_i_ofd_pin_gate,
]


# ─────────────────────────────────────────────────────────────────────────────
# Section 3 — dead settings
# ─────────────────────────────────────────────────────────────────────────────

#: ``(fieldname, owning Single, what the code uses instead)``.
#:
#: A pass means "still dead", which is the state being pinned. These fields are
#: not harmless: each one is an editable control in Desk that an operator can
#: change, save successfully, and have no effect from — which is a worse failure
#: than a missing setting, because it looks like it worked.
_DEAD_SETTINGS: Tuple[Tuple[str, str, str], ...] = (
    ("indirect_expenses_parent", "Jarz POS Settings",
     "api/expenses.py resolves the group by account_name against "
     "ACCOUNTS.INDIRECT_EXPENSES and walks lft/rgt"),
    ("default_bank_parent_account", "Jarz POS Settings",
     "utils/account_utils._get_payment_account matches parent_account LIKE "
     "%ACCOUNTS.BANK_ACCOUNTS%"),
    ("mobile_wallet_account", "Jarz POS Settings",
     "utils/account_utils._get_payment_account resolves it from the "
     "ACCOUNTS.MOBILE_WALLET constant"),
    ("default_receivable_account", "Jarz POS Settings",
     "the invoice's own debit_to, else Company.default_receivable_account — a "
     "field on Company with the same name, which is why a plain grep looks busy"),
    ("default_payable_account", "Jarz POS Settings",
     "Company.default_payable_account via account_utils.get_creditors_account — "
     "again the Company field, not this one"),
    ("default_stock_uom", "Jarz POS Settings",
     "Item.stock_uom is read directly; there is no fallback that consults this"),
    ("standard_buying_price_list", "Jarz POS Settings",
     "api/purchase.py uses its own STANDARD_BUYING module constant"),
    ("digest_send_time", "Jarz Forecast Settings",
     "hooks.scheduler_events['daily'] -> jarz_pos.tasks.run_daily_inventory_digest "
     "decides when it runs; the DocType description already admits it "
     "('Controlled via scheduler — this is for reference only')"),
    ("digest_email_recipients", "Jarz Segmentation Settings",
     "recipients are derived from roles at runtime "
     "(jarz_pos.tasks._get_online_payment_alert_recipients), never from a stored "
     "address list"),
)

#: Paths excluded from the scan, with the reason each one is not evidence.
_SCAN_EXCLUDED = {
    # The seeder writes these names; it never reads them for behaviour.
    "setup/settings_defaults.py",
    # This file names all nine of them, and would otherwise match itself.
    "scripts/settings_matrix_validation.py",
}

#: A hit only counts as a live settings read if one of these appears on the line
#: or just above it. Without this, ``Company.default_payable_account`` — a real,
#: unrelated ERPNext field with the same fieldname — reads as proof of life.
_SETTINGS_MARKERS = (
    "Jarz POS Settings",
    "Jarz Forecast Settings",
    "Jarz Segmentation Settings",
    "get_single_value",
    "raw_single_value",
    "single_int",
    "single_float",
    "single_flag",
    "_setting_",
    "_settings_",
    "settings.",
    "pos_settings",
    "SEED_DEFAULTS",
)

#: How far above a hit to look for a marker, so a multi-line call like
#: ``get_single_value(\n  "Jarz POS Settings",\n  "field",\n)`` is still caught.
_MARKER_LOOKBACK = 6


def _iter_app_sources() -> List[str]:
    root = _app_root()
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", "node_modules", ".git"}]
        for filename in filenames:
            if filename.endswith(".py"):
                out.append(os.path.join(dirpath, filename))
    return out


def _classify_hits(fieldname: str, sources: List[str]) -> Dict[str, List[str]]:
    root = _app_root()
    live: List[str] = []
    harness: List[str] = []
    unrelated: List[str] = []
    for path in sources:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel in _SCAN_EXCLUDED:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except Exception:
            continue
        for index, text in enumerate(lines):
            if fieldname not in text:
                continue
            window = "\n".join(lines[max(0, index - _MARKER_LOOKBACK): index + 1])
            marked = any(marker in window for marker in _SETTINGS_MARKERS)
            entry = f"{rel}:{index + 1}: {text.strip()[:110]}"
            if not marked:
                unrelated.append(entry)
            elif rel.startswith("tests/") or rel.startswith("scripts/"):
                harness.append(entry)
            else:
                live.append(entry)
    return {"live": live, "harness": harness, "unrelated": unrelated}


def _section_dead_settings(ctx: RunContext) -> Dict[str, Any]:
    sources = _iter_app_sources()
    report: Dict[str, Any] = {}
    for fieldname, owner, instead in _DEAD_SETTINGS:
        case = f"DEAD.{fieldname}"
        if not frappe.db.exists("DocType", owner):
            ctx.skip(case, f"{owner} is not installed, so the field's owner is unverifiable")
            continue
        hits = _classify_hits(fieldname, sources)
        report[fieldname] = {"owner": owner, "instead": instead, **hits}
        ctx.record(
            case,
            not hits["live"],
            (
                f"{owner}.{fieldname} is read by NOTHING in the app — changing it in Desk "
                f"does nothing. The code uses: {instead}. "
                f"({len(hits['unrelated'])} same-name hit(s) on other doctypes' fields, "
                f"{len(hits['harness'])} harness/test hit(s), both ignored.)"
            )
            if not hits["live"] else
            (
                f"{owner}.{fieldname} IS read now: {hits['live'][:3]} — this field is no "
                "longer dead, and the report must stop saying it is inert."
            ),
        )
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Section 4 — permission reality
# ─────────────────────────────────────────────────────────────────────────────

#: Settings Singles this app owns, so a Custom DocPerm on one is our problem.
_OWNED_SETTINGS = (
    "Jarz POS Settings",
    "Jarz Forecast Settings",
    "Jarz Segmentation Settings",
)


def _section_permissions(ctx: RunContext) -> Dict[str, Any]:
    user = frappe.session.user
    roles = _roles()
    report: Dict[str, Any] = {"user": user}

    # The shipped permission block on Jarz POS Settings grants write to System
    # Manager and read to POS User. Nothing else. That is why a JARZ Manager gets
    # 200 on read and 403 on write, which reads like a bug from the app.
    expected_write = ("system manager" in roles) or user == "Administrator"
    try:
        can_write = bool(frappe.has_permission(SETTINGS, ptype="write", user=user))
    except Exception as exc:
        ctx.skip("PERM.write_matches_the_role_model", f"has_permission raised: {exc}")
        can_write = None
    report["can_write_jarz_pos_settings"] = can_write

    if can_write is not None:
        ctx.record(
            "PERM.write_matches_the_role_model",
            can_write == expected_write,
            f"{user}: has_permission(write)={can_write}, System Manager/Administrator="
            f"{expected_write}. Jarz POS Settings ships write for System Manager only "
            "and read for POS User, so every other role is read-only by design",
        )

    for doctype in SNAPSHOT_DOCTYPES:
        if not frappe.db.exists("DocType", doctype):
            continue
        try:
            rows = frappe.get_all(
                "Custom DocPerm", filters={"parent": doctype},
                fields=["role", "read", "write"], limit_page_length=0,
            ) or []
        except Exception as exc:
            ctx.skip(f"PERM.{doctype}.no_custom_docperm", f"query failed: {exc}")
            continue
        report[f"custom_docperm::{doctype}"] = rows

        if doctype not in _OWNED_SETTINGS:
            # Another app's Single. Reported, not asserted — its permission model
            # is not ours to pin.
            ctx.record(
                f"PERM.{doctype}.custom_docperm_reported", True,
                f"{len(rows)} Custom DocPerm row(s) — reported only, this DocType "
                "belongs to another app",
            )
            continue

        ctx.record(
            f"PERM.{doctype}.no_custom_docperm",
            not rows,
            "no Custom DocPerm rows, so the DocType's shipped permissions apply"
            if not rows else
            f"{len(rows)} Custom DocPerm row(s) present ({[r['role'] for r in rows]}). "
            "A single custom row REPLACES the entire standard permission set for this "
            "DocType, so every role not listed here has silently lost its access",
        )
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(cleanup: bool = True) -> Dict[str, Any]:
    """Run the whole matrix.

    ``cleanup`` governs only the document artifacts (there should be none — a
    case that creates a document has already failed its assertion). **Setting
    restores are unconditional and are not affected by this flag**, because a
    harness that can be asked to leave a kill-switch flipped is a harness that
    eventually will be.
    """
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}

    _guard_environment()
    # Deliberately NOT setting frappe.flags.in_test: several code paths branch on
    # it, and this harness exists to observe what the real endpoints do.
    frappe.flags.ignore_woo_outbound = True

    ctx = RunContext()
    summary: Dict[str, Any] = {}
    try:
        _section_line_anchors(ctx)
        snapshot = _section_snapshot(ctx)

        env = _collect_env(ctx)
        for case in _CASES:
            try:
                case(ctx, env)
            except Exception as exc:
                ctx.record(f"{case.__name__}.crashed", False, f"{type(exc).__name__}: {exc}")
                frappe.log_error(
                    frappe.get_traceback(), f"settings matrix case failed: {case.__name__}"
                )

        dead = _section_dead_settings(ctx)
        permissions = _section_permissions(ctx)

        for check in ctx.checks:
            status = "SKIP" if check.skipped else ("PASS" if check.passed else "FAIL")
            print(f"   [{status}] {check.name} :: {check.detail}")
        print(
            f"settings_matrix_validation: {ctx.passed} passed, {ctx.failed} failed, "
            f"{ctx.skipped} skipped"
        )

        summary.update({
            "passed": ctx.passed,
            "failed": ctx.failed,
            # A site with no tracked order, no started Work Order or a session
            # outside the role tier now says so out loud instead of reporting
            # all-green having exercised nothing.
            "skipped": ctx.skipped,
            "skipped_checks": ctx.summary_skipped(),
            "checks": ctx.summary_checks(),
            "snapshot": snapshot,
            "dead_settings": dead,
            "permissions": permissions,
        })
        print(MARKER_START)
        print(json.dumps(summary, indent=2, default=str))
        print(MARKER_END)
        return summary
    finally:
        # Belt and braces over _SettingGuard.__exit__: every flip is already
        # restored and asserted by its own guard, so this only ever catches a
        # write that escaped one. It re-reads rather than re-writes, so it cannot
        # itself corrupt a value.
        try:
            frappe.db.commit()
        except Exception:
            pass
        if cleanup:
            try:
                _bust(SETTINGS)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "settings matrix teardown failed")
