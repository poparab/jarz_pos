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
``{{item:PIST-SPR|grams}}``  -> ``"1830 g"``     (qty x batches, in grams)
``{{item:COFFEE|grams|x3}}`` -> ``"240 g"``      (grams x 3)
``{{item:COFFEE|qty|x0.3}}`` -> ``"0.024"``      (qty x 0.3, stock UOM)
``{{item:COFFEE|grams|each}}`` -> ``"8 g"``      (for ONE finished unit)
``{{item:MIX+SUGAR|grams}}``   -> ``"970 g"``    (both lines, summed)

``grams`` is for the bench, which weighs in grams while the BOM is in Kg: it
converts ``Kg``/``kg``/``Kilogram`` (x1000) and ``Gram``/``Gm``/``g`` (x1), rounds
to one decimal and drops a trailing ``.0`` ("80 g", "53.3 g").  Any other stock
UOM cannot be converted honestly, so the token is left verbatim and reported
rather than rendered as a wrong number.

``xN`` is an optional segment after the variant, a positive decimal multiplier,
allowed only after ``qty`` or ``grams``.  It exists for derived figures that follow a
fixed ratio to a BOM line - liquid coffee is 3 x the grinds, so the recipe
cannot list it as a component but can still say ``{{item:Coffee beans|grams|x3}}``
and have it follow the BOM and the run size.  On the full form, ``name`` and
``uom`` it makes no sense, so it is invalid (verbatim + reported), as is a zero,
negative or non-numeric ``N``.

``each`` is the other optional modifier (same rules, either order with ``xN``,
each at most once).  It renders the figure for **one** finished unit -
``qty_map[code] / units_per_batch`` - whatever the run size.  The kitchen makes
every Tiramisu size in one go: the bowl steps quote the whole run, and the
"fill each jar" step quotes the portion per jar, which is the number the
person at the scale actually needs.  Without a known ``units_per_batch`` the
token is reported rather than guessed.

``A+B`` in the code position sums two or more components (the tiramisu cream
is the cheesecake mix **plus** the sweet coffee folded into it).  A code that
really contains ``+`` still resolves as itself first; the sum is used only
when the whole string is unknown and every part is known.  ``qty``, ``uom`` and
the full form need one shared UOM; ``grams`` converts each part on its own.

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
_VARIANTS = frozenset({_VARIANT_FULL, "qty", "name", "uom", "grams"})
# Only a numeric rendering can be multiplied; "x3" after a name is meaningless.
_MULTIPLIABLE_VARIANTS = frozenset({"qty", "grams"})
_MULTIPLIER_RE = re.compile(r"^x(\d+(?:\.\d+)?|\.\d+)$", re.IGNORECASE)
_EACH = "each"
_SUM_SEPARATOR = "+"

# Stock UOM -> grams per unit, matched case-insensitively.  Deliberately small:
# a UOM that is not a unit of mass (Nos, Litre, Box) must NOT be guessed at.
_GRAMS_PER_UOM = {
    "kg": 1000.0,
    "kgs": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "g": 1.0,
    "gm": 1.0,
    "gram": 1.0,
    "grams": 1.0,
}

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


def _parse_multiplier(segment: str) -> Optional[float]:
    """``"x3"`` -> ``3.0``; ``None`` for anything else or a non-positive value."""
    match = _MULTIPLIER_RE.match(segment.strip())
    if not match:
        return None
    value = float(match.group(1))
    return value if value > 0 else None


def _split_payload(payload: str) -> Tuple[str, str, float, bool, bool]:
    """``"X|grams|x3|each"`` -> ``("X", "grams", 3.0, True, True)``.

    Returns ``(code, variant, multiplier, each, valid)``.  After the variant
    come at most two modifiers, ``xN`` and ``each``, in either order and each
    at most once, and only on a ``qty``/``grams`` token.  Anything else is
    invalid.
    """
    parts = str(payload).split("|")
    code = _TAG_RE.sub("", parts[0]).strip()
    invalid = (code, _VARIANT_FULL, 1.0, False, False)

    if len(parts) == 1:
        return code, _VARIANT_FULL, 1.0, False, True
    if len(parts) > 4:
        return invalid

    variant = _TAG_RE.sub("", parts[1]).strip().lower()
    if variant not in _VARIANTS or variant == _VARIANT_FULL:
        # A trailing pipe with nothing after it is a typo, not the full form.
        return invalid

    multiplier: Optional[float] = None
    each = False
    for raw in parts[2:]:
        if variant not in _MULTIPLIABLE_VARIANTS:
            return invalid
        segment = _TAG_RE.sub("", raw).strip()
        if segment.lower() == _EACH:
            if each:
                return invalid
            each = True
            continue
        parsed = _parse_multiplier(segment)
        if parsed is None or multiplier is not None:
            return invalid
        multiplier = parsed
    return code, variant, (multiplier if multiplier is not None else 1.0), each, True


def uses_each(text: Any) -> bool:
    """True when any valid token in ``text`` carries the ``each`` modifier."""
    for match in _TOKEN_RE.finditer(normalise_markup(text)):
        _code, _variant, _multiplier, each, valid = _split_payload(match.group(1))
        if valid and each:
            return True
    return False


def to_grams(qty: float, uom: Any) -> Optional[float]:
    """``(0.08, "Kg")`` -> ``80.0``; ``None`` when the UOM is not a mass."""
    factor = _GRAMS_PER_UOM.get(str(uom or "").strip().lower())
    if factor is None:
        return None
    return qty * factor


def format_grams(qty: float, uom: Any) -> Optional[str]:
    """``(0.08, "Kg")`` -> ``"80 g"``; ``None`` when the UOM is not a mass.

    One decimal, trailing ``.0`` dropped: the scales on the bench read to a
    tenth of a gram and "53.3 g" is usable where "53.333 g" is noise.
    """
    grams = to_grams(qty, uom)
    if grams is None:
        return None
    return _grams_text(grams)


def _grams_text(grams: float) -> str:
    text = f"{grams:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    if text == "-0":
        text = "0"
    return f"{text} g"


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


def _resolve_parts(
    code: str,
    component_qty_map: Mapping[str, Any],
    name_map: Mapping[str, Any],
    uom_map: Mapping[str, Any],
    lowered: Mapping[str, str],
) -> Optional[List[str]]:
    """The component code(s) a token names, or ``None`` if any is unknown."""
    whole = _lookup_code(code, component_qty_map, name_map, uom_map, lowered)
    if whole is not None:
        return [whole]
    if _SUM_SEPARATOR not in code:
        return None
    resolved: List[str] = []
    for part in code.split(_SUM_SEPARATOR):
        part = part.strip()
        found = _lookup_code(part, component_qty_map, name_map, uom_map, lowered) if part else None
        if found is None:
            return None
        resolved.append(found)
    return resolved


def format_quantity(qty: Any, uom: Any) -> str:
    """Bench-readable amount: grams for a mass, else a trimmed count and UOM.

    ``(0.898, "Kg")`` -> ``"898 g"``; ``(28.0, "Nos")`` -> ``"28 Nos"``.
    """
    value = to_float(qty, 0.0)
    grams = format_grams(value, uom)
    if grams is not None:
        return grams
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return f"{text} {str(uom or '').strip()}".strip()


# ── Public API ──────────────────────────────────────────────────────────


def render_instruction(
    text: Any,
    *,
    component_qty_map: Mapping[str, Any],
    uom_map: Mapping[str, Any],
    name_map: Mapping[str, Any],
    batches: Any,
    decimals: int = DEFAULT_DECIMALS,
    units_per_batch: Any = None,
) -> Tuple[str, List[str]]:
    """Substitute every ``{{item:...}}`` token; return ``(text, unresolved)``.

    ``component_qty_map`` holds the requirement for **one** BOM batch; every
    quantity rendered is that figure multiplied by ``batches`` - except on an
    ``each`` token, which divides it by ``units_per_batch`` instead.

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
    per_batch_units = to_float(units_per_batch, 0.0)
    try:
        places = max(0, int(decimals))
    except (TypeError, ValueError):
        places = DEFAULT_DECIMALS

    lowered = _lowered_index(component_qty_map, name_map, uom_map)
    unresolved: List[str] = []

    def _replace(match: "re.Match[str]") -> str:
        payload = match.group(1)
        code, variant, multiplier, each, valid = _split_payload(payload)

        if not valid or not code:
            _remember(unresolved, str(payload).strip())
            return match.group(0)

        parts = _resolve_parts(code, component_qty_map, name_map, uom_map, lowered)
        if parts is None:
            _remember(unresolved, code)
            return match.group(0)

        if each:
            if per_batch_units <= 0:
                # "Per jar" without a known yield would be a made-up number.
                _remember(unresolved, str(payload).strip())
                return match.group(0)
            scale = multiplier / per_batch_units
        else:
            scale = batch_count * multiplier

        quantities = [to_float(component_qty_map.get(p), 0.0) * scale for p in parts]
        uoms = [str(uom_map.get(p) or "").strip() for p in parts]
        name_text = " + ".join(str(name_map.get(p) or p).strip() for p in parts)

        if variant == "grams":
            grams = [to_grams(q, u) for q, u in zip(quantities, uoms)]
            if any(g is None for g in grams):
                # Not a unit of mass: refuse rather than print a wrong number.
                _remember(unresolved, str(payload).strip())
                return match.group(0)
            return _grams_text(sum(grams))
        if variant == "name":
            return name_text

        # qty, uom and the full form quote ONE unit, so a sum must share it.
        if len({u.lower() for u in uoms}) > 1:
            _remember(unresolved, str(payload).strip())
            return match.group(0)
        qty_text = f"{sum(quantities):.{places}f}"
        uom_text = uoms[0]

        if variant == "qty":
            return qty_text
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
    units_per_batch: Any = None,
) -> Tuple[Dict[str, Any], List[str]]:
    render_kwargs = {
        "component_qty_map": component_qty_map,
        "uom_map": uom_map,
        "name_map": name_map,
        "batches": batches,
        "units_per_batch": units_per_batch,
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
    units_per_batch: Any = None,
) -> Dict[str, Any]:
    """Render every step of an SOP for a specific run size.

    ``units_per_batch`` (the BOM yield) feeds ``each`` tokens; left unset it is
    ``units / batches``, which is exactly the yield whenever both are known.

    Returns ``{batches, units, steps, total_duration_mins, unresolved_tokens}``.
    ``instruction_text`` is deliberately **not** produced here — stripping HTML
    is ``frappe.utils``' job and this module stays frappe-free; ``api/sop.py``
    adds it on the way out.
    """
    batch_count = to_float(batches, 1.0)
    unit_count = to_float(units, 0.0)
    per_batch_units = to_float(units_per_batch, 0.0)
    if per_batch_units <= 0 and batch_count > 0 and unit_count > 0:
        per_batch_units = unit_count / batch_count

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
            units_per_batch=per_batch_units or None,
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
