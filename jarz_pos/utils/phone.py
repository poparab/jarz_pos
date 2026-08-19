"""Egyptian phone-number canonicalisation for POS customer identity.

The same subscriber reaches this database spelled several ways: ``01111034268``
from the POS keypad and from WooCommerce checkout, ``+201111034268`` from Woo
accounts that stored the country code, and a handful of ``201111034268``.  Every
lookup that compares ``mobile_no`` with a plain ``=`` — the duplicate-customer
guard in :func:`jarz_pos.api.customer.create_customer` among them — treats those
as three different people.

Deliberately local to jarz_pos: the WooCommerce integration carries its own copy
under its own module boundary, and the two apps never import from each other.
"""

from __future__ import annotations


def normalize_phone(value: str | None) -> str:
    """Return the local Egyptian spelling (``0XXXXXXXXXX``) of *value*.

    Non-Egyptian and unrecognised inputs are returned digit-stripped rather than
    guessed at, so nothing is silently mangled.
    """
    if not value:
        return ""
    raw = "".join(ch for ch in str(value) if ch.isdigit() or ch == "+").strip()
    if not raw:
        return ""

    digits = raw.lstrip("+")
    if not digits.isdigit():
        return raw

    if digits.startswith("0020"):
        digits = digits[2:]
    if digits.startswith("20") and len(digits) == 12:
        return "0" + digits[2:]
    if digits.startswith("200") and len(digits) == 13:
        return digits[2:]
    return raw


def phone_variants(value: str | None) -> list[str]:
    """Every stored spelling that means the same number, canonical form first.

    Use this for equality lookups against ``mobile_no`` / ``phone``; historical
    rows were written before canonicalisation existed, so matching only the
    canonical form still misses them.
    """
    canonical = normalize_phone(value)
    if not canonical:
        return []

    variants = [canonical]
    if canonical.isdigit() and canonical.startswith("0") and len(canonical) == 11:
        national = canonical[1:]
        for variant in (f"+20{national}", f"20{national}", f"0020{national}"):
            if variant not in variants:
                variants.append(variant)

    raw = "".join(ch for ch in str(value) if ch.isdigit() or ch == "+").strip()
    if raw and raw not in variants:
        variants.append(raw)
    return variants


def phone_search_terms(value: str | None) -> list[str]:
    """Substrings for a ``LIKE`` search that match any stored spelling.

    A search for ``01111034268`` must still find a row stored as
    ``+201111034268``; the shared tail (``1111034268``) is what both spellings
    have in common, so that is what gets matched.
    """
    canonical = normalize_phone(value)
    raw = str(value or "").strip()
    terms: list[str] = []

    if canonical.isdigit() and canonical.startswith("0") and len(canonical) == 11:
        terms.append(canonical[1:])  # national significant number, shared by every spelling
    for candidate in (canonical, raw):
        if candidate and candidate not in terms:
            terms.append(candidate)
    return terms
