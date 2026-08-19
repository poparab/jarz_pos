"""Reading Single settings without losing the difference between unset and zero.

``frappe.db.get_single_value`` ends in ``cast_fieldtype(df.fieldtype, val)``,
which for ``Int`` and ``Check`` is ``cint(value)``. A field with **no row at all**
in ``tabSingles`` therefore comes back as ``0`` — indistinguishable from an
operator who deliberately set 0. Every ``if value is None: return default`` guard
written against that helper is dead code, and the declared default never applies.

That has bitten this codebase repeatedly and in the same shape each time:

* the label feature shipped dark on staging — ``auto_consume=False``,
  ``alerts_enabled=False``, ``buffer_days=0`` — on a site where nobody had
  touched a thing, while every endpoint answered 200;
* ``tracking_link_ttl_hours`` reads 0, so ``_is_expired`` returns ``False`` and
  customer tracking links never expire despite a declared 24-hour default;
* ``purchase_request_schedule_days`` reads 0, so ``int(value)`` succeeds, the
  ``default=3`` is unreachable, and every item request is dated today.

The reader below is the one this module exists to share. It was written first
inside ``services/label_stock.py`` (``_single_value``), where it is still in
place and still correct — that copy is deliberately left alone for now rather
than re-pointed here, because it sits on a live inventory-valuation path and the
change would be churn on working code with nothing to gain but tidiness. New
callers use this module; that one is the reason it exists.

Note the asymmetry worth remembering: ``Select`` and ``Data`` cast through
``cstr(None)`` to ``""``, which most fallbacks *do* treat as unset — so those
field types were never affected, and the bug looked narrower than it was.
"""
from __future__ import annotations

from typing import Any, Optional

import frappe


def raw_single_value(doctype: str, fieldname: str) -> Optional[Any]:
    """The stored value for a Single's field, or ``None`` when never written.

    Returns the raw string as MariaDB holds it; callers coerce. ``None`` means
    the field has no row — not that it holds an empty value.
    """
    try:
        rows = frappe.db.sql(
            "select `value` from `tabSingles` where `doctype`=%s and `field`=%s limit 1",
            (doctype, fieldname),
        )
    except Exception:
        return None
    if not rows:
        return None
    value = rows[0][0]
    return None if value is None else value


def single_int(doctype: str, fieldname: str, default: int) -> int:
    """Int setting, with ``default`` applied only when nobody has ever set it.

    An explicit 0 is honoured as 0. That distinction is the whole point: for
    several of these fields zero is a meaningful, deliberately-chosen value
    ("never expire", "no limit"), so silently substituting the default would
    be just as wrong as never applying it.
    """
    raw = raw_single_value(doctype, fieldname)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def single_float(doctype: str, fieldname: str, default: float) -> float:
    """Float/Currency setting, with the same unset-versus-zero distinction."""
    raw = raw_single_value(doctype, fieldname)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def single_flag(doctype: str, fieldname: str, default: bool) -> bool:
    """Check setting, with ``default`` applied only when never written.

    This is where a JSON ``"default": "1"`` goes to die: the default in the
    DocType applies when the Single is first created, so a Check field added by
    a later migration lands with no row and reads ``0`` forever after. Passing
    the intended default here is what makes the declared one real.
    """
    raw = raw_single_value(doctype, fieldname)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return bool(int(float(str(raw).strip())))
    except (TypeError, ValueError):
        return bool(raw)
