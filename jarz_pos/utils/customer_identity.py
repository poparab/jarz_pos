"""Pure helpers for spotting a customer the POS already has.

Production, 2026-11-22: Woo order 22309 was booked on a NEW customer named "..."
(phone 01007023214) although the same person already existed as "مصطفي حسين"
(phone 01018689687, 5 orders) at essentially the same address:

	existing: حدايق الزيتون شارع العزيز بالله عمارة شركة الباسم الدور الرابع شقة الباب الحديد
	new:      محافظه القاهره حدايق الزيتون شارع العزيز بالله عمارة شركة الباسم الدور الرابع شقة الباب الحديد

The primary-phone hard block in ``create_customer`` cannot catch that (different
number). These functions let the POS *warn* on a matching address and refuse a
name with no letters in it. Everything here is pure (no frappe, no database) so
it can be unit-tested anywhere.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# ── normalisation ───────────────────────────────────────────────────────

# Tashkeel (harakat, superscript alef, Quranic marks) and tatweel.
_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC_FOLD = str.maketrans(
	{
		"أ": "ا",
		"إ": "ا",
		"آ": "ا",
		"ٱ": "ا",
		"ى": "ي",
		"ی": "ي",
		"ة": "ه",
		"ؤ": "و",
		"ئ": "ي",
		"ک": "ك",
	}
)
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Anything that is not a letter/digit (``\w`` minus underscore) becomes a space.
_PUNCT = re.compile(r"[^\w]+|_+", re.UNICODE)


def normalize_text(text: Any) -> str:
	"""Fold *text* for comparison. Pure, never raises.

	Arabic-Indic digits -> ASCII, tashkeel/tatweel stripped, ``أإآ``->``ا``,
	``ى``->``ي``, ``ة``->``ه``, ``ؤ``->``و``, ``ئ``->``ي``, punctuation dropped,
	whitespace collapsed, casefolded.
	"""
	clean = unicodedata.normalize("NFKC", str(text or "")).translate(_ARABIC_DIGITS)
	clean = _TASHKEEL.sub("", clean).translate(_ARABIC_FOLD).casefold()
	return " ".join(_PUNCT.sub(" ", clean).split())


# ── names ───────────────────────────────────────────────────────────────


def _is_name_letter(ch: str) -> bool:
	if ch == "ـ":  # tatweel is category Lm but is not a letter of a name
		return False
	if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
		return True
	if not ch.isalpha():
		return False
	code = ord(ch)
	return (
		0x0600 <= code <= 0x06FF  # Arabic
		or 0x0750 <= code <= 0x077F  # Arabic Supplement
		or 0x08A0 <= code <= 0x08FF  # Arabic Extended-A
		or 0xFB50 <= code <= 0xFDFF  # Arabic Presentation Forms-A
		or 0xFE70 <= code <= 0xFEFF  # Arabic Presentation Forms-B
		or 0x00C0 <= code <= 0x024F  # Latin-1 Supplement / Extended (é, ü ...)
	)


def name_has_letters(name: Any) -> bool:
	"""True when *name* holds at least one Arabic or Latin letter.

	``"..."``, ``"-"``, ``"123"``, ``"؟؟"`` and ``"ـــ"`` (tatweel) are not names.
	"""
	return any(_is_name_letter(ch) for ch in str(name or ""))


def is_invalid_customer_name(name: Any) -> bool:
	"""True for a supplied (non-blank) name that has no Arabic or Latin letter.

	A blank name is *not* flagged here: callers that require a name already
	reject a blank one, and a duplicate check called with partial input must not
	report the empty field as invalid.
	"""
	text = str(name or "").strip()
	return bool(text) and not name_has_letters(text)


def names_match(a: Any, b: Any) -> bool:
	"""Normalised exact match of two customer names (both must be non-empty)."""
	na, nb = normalize_text(a), normalize_text(b)
	return bool(na) and na == nb


# ── addresses ───────────────────────────────────────────────────────────

MIN_ADDRESS_CHARS = 10
"""Addresses shorter than this (normalised) are ignored - e.g. a Sales Partner
name ("Talabat") typed into the address line."""

SUBSTRING_MIN_CHARS = 15
SUBSTRING_MIN_DISTINCT_TOKENS = 3
TOKEN_OVERLAP_THRESHOLD = 0.8
TOKEN_OVERLAP_MIN_TOKENS = 4
# The shared distinctive tokens must also cover this share of the LONGER
# address. Without it a vague "street + district" address matched every
# detailed address on that street: on production the 22309 check flagged
# "٣ شارع العزيز بالله حدائق الزيتون" and "الزيتون شارع العزيز بالله" (other
# customers, same street) as the same place.
LONGER_COVERAGE_MIN = 0.6
LIKE_TOKENS = 3
LIKE_MIN_TOKEN_CHARS = 3

# Words every Cairo address carries. They are kept in the text (the substring
# rule compares whole text) but do not count as evidence in the token-overlap
# rule, otherwise "شارع X عمارة 5 الدور 2 شقة 3" matches "شارع Y عمارة 5 الدور 2 شقة 3".
# Written in normalised form (see normalize_text).
ADDRESS_STOPWORDS = frozenset(
	{
		# street / building / unit words
		"شارع",
		"الشارع",
		"ش",
		"عماره",
		"العماره",
		"برج",
		"البرج",
		"الدور",
		"دور",
		"شقه",
		"الشقه",
		"رقم",
		"مدخل",
		"المدخل",
		"بلوك",
		"مربع",
		"فيلا",
		"الفيلا",
		"ميدان",
		"طريق",
		"منطقه",
		"المنطقه",
		"حي",
		"الحي",
		"مدينه",
		"تقسيم",
		"ناصيه",
		"عقار",
		"مبني",
		"المبني",
		"الارضي",
		"ارضي",
		"الاول",
		"الثاني",
		"الثالث",
		"الرابع",
		"الخامس",
		"السادس",
		"السابع",
		"الثامن",
		"التاسع",
		"العاشر",
		"الاخير",
		"يمين",
		"شمال",
		"الاسانسير",
		# directions / prepositions
		"بجوار",
		"جنب",
		"امام",
		"خلف",
		"بعد",
		"قبل",
		"عند",
		"من",
		"في",
		"علي",
		"الي",
		"و",
		"اول",
		"اخر",
		# governorate / country
		"محافظه",
		"القاهره",
		"قاهره",
		"الجيزه",
		"جيزه",
		"مصر",
		# English
		"street",
		"st",
		"building",
		"bldg",
		"floor",
		"flat",
		"apartment",
		"apt",
		"no",
		"cairo",
		"giza",
		"egypt",
	}
)

# Letters whose spelling varies in typed Arabic (and the folded forms). Each is
# replaced by the LIKE single-character wildcard so "حدائق" still finds "حدايق".
_AMBIGUOUS_LETTERS = frozenset("اأإآيىهةوؤئ")


def address_text(line1: Any, line2: Any = None) -> str:
	"""address_line1 + address_line2 as one string (line2 optional)."""
	return " ".join(part for part in (str(line1 or "").strip(), str(line2 or "").strip()) if part)


def distinctive_tokens(normalized: str) -> list[str]:
	"""Tokens of a normalised address that carry evidence: stopwords and
	single-letter tokens dropped, digits kept (building / flat numbers)."""
	out: list[str] = []
	for token in normalized.split():
		if token in ADDRESS_STOPWORDS:
			continue
		if len(token) < 2 and not token.isdigit():
			continue
		out.append(token)
	return out


def address_match(a: Any, b: Any) -> bool:
	"""True when two free-text addresses very likely describe the same place.

	* Either side shorter than ``MIN_ADDRESS_CHARS`` normalised chars: no match.
	* The shared distinctive tokens cover less than ``LONGER_COVERAGE_MIN`` of the
	  longer side's: no match (a vague street-only address is not the same place).
	* Substring: the shorter normalised text is at least ``SUBSTRING_MIN_CHARS``
	  long, carries at least ``SUBSTRING_MIN_DISTINCT_TOKENS`` distinctive tokens,
	  and appears in the longer one on word boundaries.
	* Token overlap: |A ∩ B| / min(|A|, |B|) >= ``TOKEN_OVERLAP_THRESHOLD`` over the
	  distinctive token sets, with the smaller set at least
	  ``TOKEN_OVERLAP_MIN_TOKENS`` tokens.
	"""
	na, nb = normalize_text(a), normalize_text(b)
	if len(na) < MIN_ADDRESS_CHARS or len(nb) < MIN_ADDRESS_CHARS:
		return False

	ta, tb = set(distinctive_tokens(na)), set(distinctive_tokens(nb))
	shared = len(ta & tb)
	largest = max(len(ta), len(tb))
	if not largest or shared / largest < LONGER_COVERAGE_MIN:
		return False

	shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
	if (
		len(shorter) >= SUBSTRING_MIN_CHARS
		and len(distinctive_tokens(shorter)) >= SUBSTRING_MIN_DISTINCT_TOKENS
		and f" {shorter} " in f" {longer} "
	):
		return True

	smallest = min(len(ta), len(tb))
	if smallest < TOKEN_OVERLAP_MIN_TOKENS:
		return False
	return shared / smallest >= TOKEN_OVERLAP_THRESHOLD


def _like_pattern(token: str) -> str:
	return "".join("_" if ch in _AMBIGUOUS_LETTERS else ch for ch in token)


def address_like_patterns(address: Any, limit: int = LIKE_TOKENS) -> list[str]:
	"""SQL ``LIKE`` patterns (``%..%``) that preselect candidate addresses.

	Uses the longest distinctive, non-numeric tokens of *address* (ranked by the
	number of letters that are NOT spelling-ambiguous, then by length), with each
	ambiguous letter turned into the ``_`` single-character wildcard. Returns []
	when the address is too short to be worth matching.
	"""
	normalized = normalize_text(address)
	if len(normalized) < MIN_ADDRESS_CHARS:
		return []
	candidates = []
	for token in dict.fromkeys(distinctive_tokens(normalized)):
		if token.isdigit() or len(token) < LIKE_MIN_TOKEN_CHARS:
			continue
		firm = sum(1 for ch in token if ch not in _AMBIGUOUS_LETTERS)
		if firm < 2:
			continue
		candidates.append((firm, len(token), token))
	candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
	return [f"%{_like_pattern(token)}%" for _firm, _len, token in candidates[:limit]]


# ── ranking ─────────────────────────────────────────────────────────────

REASON_PHONE = "phone"
REASON_SECONDARY_PHONE = "secondary_phone"
REASON_ADDRESS = "address"
REASON_NAME = "name"

REASON_WEIGHT = {
	REASON_PHONE: 100,
	REASON_SECONDARY_PHONE: 80,
	REASON_ADDRESS: 60,
	REASON_NAME: 10,
}
STANDALONE_REASONS = (REASON_PHONE, REASON_SECONDARY_PHONE, REASON_ADDRESS)


def order_reasons(reasons: Any) -> list[str]:
	"""Distinct reasons, strongest first."""
	unique = [r for r in dict.fromkeys(reasons or []) if r in REASON_WEIGHT]
	return sorted(unique, key=lambda r: REASON_WEIGHT[r], reverse=True)


def match_score(reasons: Any) -> int:
	return sum(REASON_WEIGHT.get(r, 0) for r in set(reasons or []))


def is_reportable(reasons: Any) -> bool:
	"""A name alone is never a match - common names are too noisy."""
	return any(r in STANDALONE_REASONS for r in (reasons or []))
