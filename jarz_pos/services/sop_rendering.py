"""Work-instruction rendering for the Production Board SOPs.

A Standard Operating Procedure is written **once**, against one BOM batch, and
then read on a phone next to a mixer while somebody runs *three* batches.  The
numbers on the screen therefore have to be the numbers for the batch actually
being made, not the numbers the author typed.  That is what this module does:
it substitutes ``{{item:CODE}}`` tokens with live, scaled quantities and scales
each step's duration.

Testability contract: **this module never imports frappe**.  Every function is
pure over plain dicts and numbers, so its tests need no patching whatsoever.
Anything that touches the database lives in ``api/sop.py`` behind a resolver.

Token grammar
-------------
``{{item:PIST-SPR}}``        -> ``"1.830 Kg Pistachio spread"``  (qty x batches)
``{{item:PIST-SPR|qty}}``    -> ``"1.830"``
``{{item:PIST-SPR|name}}``   -> ``"Pistachio spread"``
``{{item:PIST-SPR|uom}}``    -> ``"Kg"``

Whitespace inside the braces is tolerated (``{{ item : X | qty }}``), because
the instruction is authored in a Text Editor by somebody who is thinking about
pastry, not about parsers.

An **unknown** code is left verbatim *and* reported in the ``unresolved`` list.
Silently deleting it would be the worst possible failure mode here: a step that
reads "add   of sugar" is a food-safety problem, whereas a step that still
reads ``{{item:SUGR}}`` is visibly broken and gets fixed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ── Vocabulary shared with the DocType Select options ───────────────────
# Kept here rather than in constants.py so the pure module stays importable
# on its own, and because these strings are the DocType's options verbatim.

SCALING_FIXED = "Fixed"
SCALING_PER_BATCH = "Per Batch"
SCALING_PER_UNIT = "Per Unit"
SCALING_MODES = (SCALING_FIXED, SCALING_PER_BATCH, SCALING_PER_UNIT)

CAPTURE_NONE = "None"
CAPTURE_NUMBER = "Number"
CAPTURE_PHOTO = "Photo"
CAPTURE_TEMPERATURE = "Temperature"
CAPTURE_TYPES = (CAPTURE_NONE, CAPTURE_NUMBER, CAPTURE_PHOTO, CAPTURE_TEMPERATURE)
NUMERIC_CAPTURE_TYPES = (CAPTURE_NUMBER, CAPTURE_TEMPERATURE)

# ``Work Order.jarz_sop_version`` stores "<sop name>#<version>" so that a batch
# run last month still resolves to the instructions that were on the screen at
# the time.  The separator is deliberately a character that cannot appear in a
# naming-series name.
VERSION_STAMP_SEPARATOR = "#"

DEFAULT_DECIMALS = 3

# Payload is captured whole and split afterwards so that an *invalid* variant
# (``{{item:X|weight}}``) is still recognised as a token and reported, instead
# of quietly failing to match and looking like ordinary prose.
_TOKEN_RE = re.compile(r"\{\{\s*item\s*:\s*([^{}]+?)\s*\}\}", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

_VARIANT_FULL = ""
_VARIANTS = frozenset({_VARIANT_FULL, "qty", "name", "uom"})

# A Text Editor is free to emit non-breaking spaces and HTML-escaped braces.
# Both are invisible to the author and fatal to a naive matcher, so they are
# folded back to their plain equivalents before anything else happens.
_MARKUP_REPLACEMENTS = (
    ("&nbsp;", " "),
    ("&#160;", " "),
    ("&#xa0;", " "),
    ("&#xA0;", " "),
    ("\xa0", " "),
    ("&#123;", "{"),
    ("&#x7b;", "{"),
    ("&#x7B;", "{"),
    ("&lbrace;", "{"),
    ("&#125;", "}"),
    ("&#x7d;", "}"),
    ("&#x7D;", "}"),
    ("&rbrace;", "}"),
)


# ── Small pure helpers ──────────────────────────────────────────────────


def to_float(value: Any, default: float = 0.0) -> float:
    """Coerce to float without turning a legitimate ``0`` into the default.

    ``float(value or default)`` is the obvious version and it is wrong: zero
    batches must stay zero, not silently become one.
    """
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def normalise_markup(text: Any) -> str:
    """Fold Text-Editor artefacts back to plain characters."""
    out = "" if text is None else str(text)
    for needle, replacement in _MARKUP_REPLACEMENTS:
        if needle in out:
            out = out.replace(needle, replacement)
    return out


def format_version_stamp(sop_name: Any, version: Any) -> str:
    """Build the ``SOP-0001#3`` value stamped onto a Work Order."""
    name = str(sop_name or "").strip()
    if not name:
        return ""
    try:
        number = int(version or 1)
    except (TypeError, ValueError):
        number = 1
    return f"{name}{VERSION_STAMP_SEPARATOR}{number}"


def parse_version_stamp(stamp: Any) -> Tuple[Optional[str], Optional[int]]:
    """Split a stamp back into ``(sop_name, version)``.

    Tolerant by design — a malformed or empty stamp yields ``(None, None)`` so
    the caller falls back to the active SOP rather than blowing up.
    """
    raw = str(stamp or "").strip()
    if not raw:
        return None, None

    name, _, version_part = raw.partition(VERSION_STAMP_SEPARATOR)
    name = name.strip()
    if not name:
        return None, None

    try:
        version = int(version_part.strip())
    except (TypeError, ValueError):
        version = None
    return name, version


def _remember(bucket: List[str], value: str) -> None:
    """Append preserving first-seen order, without duplicates."""
    if value and value not in bucket:
        bucket.append(value)


def _lowered_index(*maps: Mapping[str, Any]) -> Dict[str, str]:
    """Case-insensitive fallback index of every known component code.

    Item codes are typed by hand into the instruction; matching only on an
    exact case would fail the author for a reason they cannot see.
    """
    index: Dict[str, str] = {}
    for mapping in maps:
        for code in (mapping or {}):
            key = str(code).strip().lower()
            if key and key not in index:
                index[key] = code
    return index


def _split_payload(payload: str) -> Tuple[str, str, bool]:
    """``"X|qty"`` -> ``("X", "qty", True)``; an unknown variant is invalid."""
    parts = str(payload).split("|")
    code = _TAG_RE.sub("", parts[0]).strip()

    if len(parts) == 1:
        return code, _VARIANT_FULL, True
    if len(parts) > 2:
        return code, _VARIANT_FULL, False

    variant = _TAG_RE.sub("", parts[1]).strip().lower()
    if variant not in _VARIANTS or variant == _VARIANT_FULL:
        # A trailing pipe with nothing after it is a typo, not the full form.
        return code, _VARIANT_FULL, False
    return code, variant, True


def _lookup_code(
    code: str,
    component_qty_map: Mapping[str, Any],
    name_map: Mapping[str, Any],
    uom_map: Mapping[str, Any],
    lowered: Mapping[str, str],
) -> Optional[str]:
    if code in component_qty_map or code in name_map or code in uom_map:
        return code
    return lowered.get(code.strip().lower())


# ── Public API ──────────────────────────────────────────────────────────


def render_instruction(
    text: Any,
    *,
    component_qty_map: Mapping[str, Any],
    uom_map: Mapping[str, Any],
    name_map: Mapping[str, Any],
    batches: Any,
    decimals: int = DEFAULT_DECIMALS,
) -> Tuple[str, List[str]]:
    """Substitute every ``{{item:...}}`` token; return ``(text, unresolved)``.

    ``component_qty_map`` holds the requirement for **one** BOM batch; every
    quantity rendered is that figure multiplied by ``batches``.

    Unknown codes and unknown variants come back untouched *and* listed in
    ``unresolved`` — see the module docstring for why they are never dropped.
    """
    source = normalise_markup(text)
    if not source or "{{" not in source:
        return source, []

    component_qty_map = component_qty_map or {}
    uom_map = uom_map or {}
    name_map = name_map or {}

    batch_count = to_float(batches, 1.0)
    try:
        places = max(0, int(decimals))
    except (TypeError, ValueError):
        places = DEFAULT_DECIMALS

    lowered = _lowered_index(component_qty_map, name_map, uom_map)
    unresolved: List[str] = []

    def _replace(match: "re.Match[str]") -> str:
        payload = match.group(1)
        code, variant, valid = _split_payload(payload)

        if not valid or not code:
            _remember(unresolved, str(payload).strip())
            return match.group(0)

        resolved = _lookup_code(code, component_qty_map, name_map, uom_map, lowered)
        if resolved is None:
            _remember(unresolved, code)
            return match.group(0)

        qty = to_float(component_qty_map.get(resolved), 0.0) * batch_count
        qty_text = f"{qty:.{places}f}"
        name_text = str(name_map.get(resolved) or resolved).strip()
        uom_text = str(uom_map.get(resolved) or "").strip()

        if variant == "qty":
            return qty_text
        if variant == "name":
            return name_text
        if variant == "uom":
            return uom_text
        return " ".join(part for part in (qty_text, uom_text, name_text) if part)

    return _TOKEN_RE.sub(_replace, source), unresolved


def scale_duration(duration_mins: Any, scaling_mode: Any, batches: Any, units: Any) -> float:
    """Scale one step's duration for the run actually being made.

    ``Fixed`` covers the steps that do not care how much is in the bowl —
    preheating an oven takes as long for one batch as for four.  ``Per Batch``
    and ``Per Unit`` cover the ones that do.  An unrecognised mode is treated
    as ``Fixed``: under-stating a duration is a scheduling annoyance, whereas
    multiplying by a mode nobody meant is a wrong number on a wall board.
    """
    duration = to_float(duration_mins, 0.0)
    mode = str(scaling_mode or SCALING_FIXED).strip()

    if mode == SCALING_PER_BATCH:
        return duration * to_float(batches, 0.0)
    if mode == SCALING_PER_UNIT:
        return duration * to_float(units, 0.0)
    return duration


def _step_payload(
    raw: Mapping[str, Any],
    position: int,
    *,
    batches: float,
    units: float,
    component_qty_map: Mapping[str, Any],
    uom_map: Mapping[str, Any],
    name_map: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    render_kwargs = {
        "component_qty_map": component_qty_map,
        "uom_map": uom_map,
        "name_map": name_map,
        "batches": batches,
    }

    # Titles are rendered too: "Weigh {{item:PIST-SPR|qty}} of spread" is a
    # perfectly natural thing to write in the one-line summary.
    title, title_unresolved = render_instruction(raw.get("title") or "", **render_kwargs)
    instruction, body_unresolved = render_instruction(raw.get("instruction") or "", **render_kwargs)

    duration = round(
        scale_duration(raw.get("duration_mins"), raw.get("scaling_mode"), batches, units),
        3,
    )

    step_no = raw.get("step_no")
    try:
        step_no = int(step_no)
    except (TypeError, ValueError):
        step_no = position

    capture_type = str(raw.get("capture_type") or CAPTURE_NONE).strip() or CAPTURE_NONE

    payload = {
        "step_no": step_no,
        "title": title,
        "instruction_html": instruction,
        "image_url": raw.get("image") or None,
        "duration_mins": duration,
        "scaling_mode": str(raw.get("scaling_mode") or SCALING_FIXED).strip() or SCALING_FIXED,
        "requires_confirmation": bool(to_float(raw.get("requires_confirmation"), 0.0)),
        "capture_type": capture_type,
        "capture_label": raw.get("capture_label") or None,
        "capture_min": to_float(raw.get("capture_min"), 0.0),
        "capture_max": to_float(raw.get("capture_max"), 0.0),
    }
    return payload, title_unresolved + body_unresolved


def render_sop(
    sop_dict: Mapping[str, Any],
    *,
    batches: Any,
    units: Any,
    component_qty_map: Mapping[str, Any],
    uom_map: Mapping[str, Any],
    name_map: Mapping[str, Any],
) -> Dict[str, Any]:
    """Render every step of an SOP for a specific run size.

    Returns ``{batches, units, steps, total_duration_mins, unresolved_tokens}``.
    ``instruction_text`` is deliberately **not** produced here — stripping HTML
    is ``frappe.utils``' job and this module stays frappe-free; ``api/sop.py``
    adds it on the way out.
    """
    batch_count = to_float(batches, 1.0)
    unit_count = to_float(units, 0.0)

    steps: List[Dict[str, Any]] = []
    unresolved: List[str] = []

    raw_steps: Sequence[Mapping[str, Any]] = (sop_dict or {}).get("steps") or []
    for position, raw in enumerate(raw_steps, start=1):
        payload, step_unresolved = _step_payload(
            raw or {},
            position,
            batches=batch_count,
            units=unit_count,
            component_qty_map=component_qty_map or {},
            uom_map=uom_map or {},
            name_map=name_map or {},
        )
        steps.append(payload)
        for token in step_unresolved:
            _remember(unresolved, token)

    return {
        "batches": batch_count,
        "units": unit_count,
        "steps": steps,
        "total_duration_mins": round(sum(s["duration_mins"] for s in steps), 3),
        "unresolved_tokens": unresolved,
    }
