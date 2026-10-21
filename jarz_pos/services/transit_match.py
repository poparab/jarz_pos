"""Transit matching: recommend which open send supplied each pool receipt. Pure, no DB.

A send ("I sent out", a transit-backed Dispatch) put stock in the company's transit
warehouse without naming where it went. A pool receipt ("I received", unsourced or
naming a sender that has an open send) says what arrived without proving who sent
it. A manager approves the match; this module only proposes it, with reasons the
app localizes.

:func:`recommend` takes plain dicts and returns plain dicts, so it is unit-tested
without a site. Its rules, in order (each send's ``remaining`` is consumed as
allocations are made, so a send is never over-allocated across receipts):

0. A receipt that names its sender draws only from that sender's sends, and a
   send never supplies its own source warehouse.
1. **Exact unique pairs** (global, per item): a send whose remaining equals a
   receipt's qty, when neither has another eligible partner with that same qty.
2. **Whole shipment** (receipts oldest first, receipts with two or more lines
   still open): the send that covers every open line, best by (most exact lines,
   already supplies this receipt, sent before the receipt, smallest time gap,
   highest affinity, name).
3. **Per line**: candidate sends ranked by score (+3 exact qty, +2 already
   supplies another line of this receipt, +1 sent before the receipt, +affinity),
   tiebreak smaller time gap then name; a short best send is topped up from the
   next ones (``split``).
4. **Leftovers**: no eligible send ever had the item -> one direct suggestion
   (``dispatch=None``) from the named sender, the usual supplier or the default
   source, confidence ``low``; eligible sends existed but are used up ->
   ``unresolved_qty``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

QTY_DP = 3
EPS = 5e-4

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

R_EXACT = "exact_qty"
R_ONLY_SENDER = "only_sender"
R_SAME_SHIPMENT = "same_shipment"
R_SENT_BEFORE = "sent_before"
R_SENT_AFTER = "sent_after"
R_USUAL_SUPPLIER = "usual_supplier"
R_NAMED = "named_by_receiver"
R_SPLIT = "split"
R_NO_SEND = "no_send_declared"

#: An affinity at or above this share earns the ``usual_supplier`` reason.
USUAL_SHARE = 0.5


def _r(value: Any) -> float:
	try:
		qty = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	if not math.isfinite(qty):
		return 0.0
	return round(qty, QTY_DP)


def _when(value: Any) -> datetime | None:
	"""A datetime from a datetime or an ISO-ish string; ``None`` when unknown."""
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.replace(tzinfo=None)
	text = str(value).strip().replace("T", " ")
	try:
		return datetime.fromisoformat(text).replace(tzinfo=None)
	except ValueError:
		return None


def _sent_before(send: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool | None:
	"""True/False when both times are known, else ``None``."""
	s, r = _when(send.get("declared_at")), _when(receipt.get("declared_at"))
	if s is None or r is None:
		return None
	return s <= r


def _gap(send: Mapping[str, Any], receipt: Mapping[str, Any]) -> float:
	s, r = _when(send.get("declared_at")), _when(receipt.get("declared_at"))
	if s is None or r is None:
		return float("inf")
	return abs((r - s).total_seconds())


def _eligible(send: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
	"""Rule 0: a named sender restricts the receipt; a send never supplies its own source."""
	source = send.get("source")
	if not source or source == receipt.get("target"):
		return False
	named = receipt.get("named_source")
	return not named or named == source


class _State:
	def __init__(
		self,
		sends: Sequence[Mapping[str, Any]],
		receipts: Sequence[Mapping[str, Any]],
		affinity: Mapping[tuple[str, str], float],
	):
		self.sends = sorted(
			(s for s in sends or [] if s.get("name")),
			key=lambda s: (_when(s.get("declared_at")) or datetime.max, str(s.get("name"))),
		)
		self.receipts = sorted(
			(r for r in receipts or [] if r.get("name")),
			key=lambda r: (_when(r.get("declared_at")) or datetime.max, str(r.get("name"))),
		)
		self.send_by_name = {s["name"]: s for s in self.sends}
		self.affinity = {k: float(v or 0) for k, v in (affinity or {}).items()}
		self.remaining: dict[str, dict[str, float]] = {
			s["name"]: {code: _r(q) for code, q in (s.get("lines") or {}).items() if _r(q) > EPS}
			for s in self.sends
		}
		self.left: dict[str, dict[str, float]] = {
			r["name"]: {code: _r(q) for code, q in (r.get("lines") or {}).items() if _r(q) > EPS}
			for r in self.receipts
		}
		# Who had each item open before anything was allocated (``only_sender``, rule 4).
		self.initial_holders: dict[str, list[str]] = {}
		for s in self.sends:
			for code in self.remaining[s["name"]]:
				self.initial_holders.setdefault(code, []).append(s["name"])
		# receipt -> item -> [suggestion]
		self.out: dict[str, dict[str, list[dict[str, Any]]]] = {r["name"]: {} for r in self.receipts}
		# receipt -> sends already supplying it
		self.suppliers: dict[str, set[str]] = {r["name"]: set() for r in self.receipts}

	def aff(self, source: str | None, target: str | None) -> float:
		return float(self.affinity.get((source, target), 0.0) or 0.0)

	def eligible_holders(self, receipt: Mapping[str, Any], code: str, initial: bool = False) -> list[str]:
		names = self.initial_holders.get(code, []) if initial else [
			s["name"] for s in self.sends if self.remaining[s["name"]].get(code, 0.0) > EPS
		]
		return [n for n in names if _eligible(self.send_by_name[n], receipt)]

	def allocate(
		self,
		receipt: Mapping[str, Any],
		code: str,
		send_name: str,
		qty: float,
		confidence: str,
		reasons: list[dict[str, Any]],
	) -> None:
		qty = _r(qty)
		if qty <= EPS:
			return
		rname = receipt["name"]
		self.remaining[send_name][code] = _r(self.remaining[send_name].get(code, 0.0) - qty)
		self.left[rname][code] = _r(self.left[rname].get(code, 0.0) - qty)
		self.suppliers[rname].add(send_name)
		self.out[rname].setdefault(code, []).append(
			{
				"dispatch": send_name,
				"source_warehouse": self.send_by_name[send_name].get("source"),
				"qty": qty,
				"confidence": confidence,
				"reasons": reasons,
			}
		)

	def common_reasons(self, send_name: str, receipt: Mapping[str, Any], code: str) -> list[dict[str, Any]]:
		"""Reasons that hold for any pick: order, usual supplier, named, only sender."""
		send = self.send_by_name[send_name]
		reasons: list[dict[str, Any]] = []
		if len(self.initial_holders.get(code, [])) == 1:
			reasons.append({"code": R_ONLY_SENDER})
		before = _sent_before(send, receipt)
		if before is True:
			reasons.append({"code": R_SENT_BEFORE})
		elif before is False:
			reasons.append({"code": R_SENT_AFTER})
		share = self.aff(send.get("source"), receipt.get("target"))
		if share >= USUAL_SHARE:
			reasons.append({"code": R_USUAL_SUPPLIER, "share": round(share, 3)})
		if receipt.get("named_source"):
			reasons.append({"code": R_NAMED})
		return reasons


def _rule_exact_pairs(st: _State) -> None:
	"""Rule 1: per item, a send and a receipt whose quantities match and match nobody else."""
	changed = True
	while changed:
		changed = False
		codes = sorted({c for left in st.left.values() for c in left})
		for code in codes:
			for receipt in st.receipts:
				want = st.left[receipt["name"]].get(code, 0.0)
				if want <= EPS:
					continue
				exact = [
					n
					for n in st.eligible_holders(receipt, code)
					if abs(st.remaining[n].get(code, 0.0) - want) <= EPS
				]
				if len(exact) != 1:
					continue
				send_name = exact[0]
				send = st.send_by_name[send_name]
				partners = [
					r
					for r in st.receipts
					if abs(st.left[r["name"]].get(code, 0.0) - want) <= EPS
					and st.left[r["name"]].get(code, 0.0) > EPS
					and _eligible(send, r)
				]
				if len(partners) != 1:
					continue
				reasons = [{"code": R_EXACT, "qty": want}] + st.common_reasons(send_name, receipt, code)
				st.allocate(receipt, code, send_name, want, CONF_HIGH, reasons)
				changed = True


def _rule_whole_shipment(st: _State) -> None:
	"""Rule 2: one send covering every still-open line of a multi-line receipt."""
	for receipt in st.receipts:
		rname = receipt["name"]
		open_lines = {c: q for c, q in st.left[rname].items() if q > EPS}
		if len(open_lines) < 2:
			continue
		covers = []
		for send in st.sends:
			name = send["name"]
			if not _eligible(send, receipt):
				continue
			rem = st.remaining[name]
			if all(rem.get(c, 0.0) >= q - EPS for c, q in open_lines.items()):
				exact = sum(1 for c, q in open_lines.items() if abs(rem.get(c, 0.0) - q) <= EPS)
				before = _sent_before(send, receipt) is True
				key = (
					-exact,
					0 if name in st.suppliers[rname] else 1,
					0 if before else 1,
					_gap(send, receipt),
					-st.aff(send.get("source"), receipt.get("target")),
					str(name),
				)
				covers.append((key, name, exact, before))
		if not covers:
			continue
		covers.sort(key=lambda c: c[0])
		best_key, best, best_exact, best_before = covers[0]
		if len(covers) == 1:
			confidence = CONF_HIGH
		else:
			_k2, _n2, second_exact, second_before = covers[1]
			ahead = best_exact > second_exact or (best_exact == second_exact and best_before and not second_before)
			confidence = CONF_MEDIUM if ahead else CONF_LOW
		for code, qty in sorted(open_lines.items()):
			reasons: list[dict[str, Any]] = [{"code": R_SAME_SHIPMENT}]
			if abs(st.remaining[best].get(code, 0.0) - qty) <= EPS:
				reasons.append({"code": R_EXACT, "qty": qty})
			reasons += st.common_reasons(best, receipt, code)
			st.allocate(receipt, code, best, qty, confidence, reasons)


def _line_score(st: _State, send_name: str, receipt: Mapping[str, Any], code: str, want: float) -> float:
	send = st.send_by_name[send_name]
	score = 0.0
	if abs(st.remaining[send_name].get(code, 0.0) - want) <= EPS:
		score += 3
	if send_name in st.suppliers[receipt["name"]]:
		score += 2
	if _sent_before(send, receipt) is True:
		score += 1
	score += st.aff(send.get("source"), receipt.get("target"))
	return score


def _rule_per_line(st: _State) -> None:
	"""Rule 3: rank the candidate sends per line; split across the next ones when short."""
	for receipt in st.receipts:
		rname = receipt["name"]
		for code in sorted(st.left[rname]):
			want = st.left[rname].get(code, 0.0)
			if want <= EPS:
				continue
			candidates = st.eligible_holders(receipt, code)
			if not candidates:
				continue
			ranked = sorted(
				candidates,
				key=lambda n: (-_line_score(st, n, receipt, code, want), _gap(st.send_by_name[n], receipt), str(n)),
			)
			scores = [_line_score(st, n, receipt, code, want) for n in ranked]
			picks: list[tuple[str, float, bool]] = []
			need = want
			for name in ranked:
				if need <= EPS:
					break
				take = min(st.remaining[name].get(code, 0.0), need)
				if take <= EPS:
					continue
				exact = abs(st.remaining[name].get(code, 0.0) - want) <= EPS
				picks.append((name, _r(take), exact))
				need = _r(need - take)
			split = len(picks) > 1
			if split:
				confidence = CONF_LOW
			elif len(ranked) == 1:
				confidence = CONF_HIGH
			elif scores[0] - scores[1] >= 1 - 1e-9:
				confidence = CONF_MEDIUM
			else:
				confidence = CONF_LOW
			for name, take, exact in picks:
				reasons: list[dict[str, Any]] = []
				if exact:
					reasons.append({"code": R_EXACT, "qty": want})
				if name in st.suppliers[rname]:
					reasons.append({"code": R_SAME_SHIPMENT})
				reasons += st.common_reasons(name, receipt, code)
				if split:
					reasons.append({"code": R_SPLIT})
				st.allocate(receipt, code, name, take, confidence, reasons)


def _direct_source(
	st: _State, receipt: Mapping[str, Any], default_source: str | None
) -> tuple[str | None, list[dict[str, Any]]]:
	"""Rule 4 sender guess: named sender, else the highest-affinity side, else *default_source*."""
	target = receipt.get("target")
	named = receipt.get("named_source")
	if named and named != target:
		return named, [{"code": R_NAMED}]
	best: tuple[float, str] | None = None
	for (source, tgt), share in st.affinity.items():
		if tgt != target or not source or source == target or share <= 0:
			continue
		if best is None or (share, str(source)) > (best[0], best[1]):
			best = (share, source)
	if best is not None:
		return best[1], [{"code": R_USUAL_SUPPLIER, "share": round(best[0], 3)}]
	if default_source and default_source != target:
		return default_source, []
	return None, []


def recommend(
	sends: Sequence[Mapping[str, Any]],
	receipts: Sequence[Mapping[str, Any]],
	affinity: Mapping[tuple[str, str], float] | None = None,
	default_source: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
	"""``{receipt_name: [line]}``, one line per receipt item, in the receipt's item order.

	*sends*: ``[{name, source, declared_at, lines: {item_code: remaining}}]`` (open sends).
	*receipts*: ``[{name, target, declared_at, named_source|None, lines: {item_code: qty}}]``.
	*affinity*: ``{(source, target): share 0..1}``. *default_source*: the direct-guess
	fallback when there is no history (the Factory).

	A line is ``{"item_code", "qty", "suggestions": [{"dispatch": name|None,
	"source_warehouse", "qty", "confidence", "reasons": [{"code", ...}]}],
	"unresolved_qty"}``.
	"""
	st = _State(sends, receipts, affinity or {})
	_rule_exact_pairs(st)
	_rule_whole_shipment(st)
	_rule_per_line(st)

	result: dict[str, list[dict[str, Any]]] = {}
	for receipt in st.receipts:
		rname = receipt["name"]
		lines_out: list[dict[str, Any]] = []
		for code, raw in (receipt.get("lines") or {}).items():
			qty = _r(raw)
			suggestions = list(st.out[rname].get(code, []))
			left = _r(st.left[rname].get(code, 0.0))
			unresolved = 0.0
			if left > EPS:
				if not st.eligible_holders(receipt, code, initial=True):
					source, reasons = _direct_source(st, receipt, default_source)
					if source:
						suggestions.append(
							{
								"dispatch": None,
								"source_warehouse": source,
								"qty": left,
								"confidence": CONF_LOW,
								"reasons": [{"code": R_NO_SEND}] + reasons,
							}
						)
					else:
						unresolved = left
				else:
					unresolved = left
			lines_out.append(
				{
					"item_code": code,
					"qty": qty,
					"suggestions": suggestions,
					"unresolved_qty": _r(unresolved),
				}
			)
		result[rname] = lines_out
	return result


def affinity_from_totals(rows: Iterable[tuple[str, str, float]]) -> dict[tuple[str, str], float]:
	"""``{(source, target): share}`` from ``(source, target, qty)`` totals. Pure.

	Each target's shares add up to 1 across the sources that sent to it.
	"""
	totals: dict[tuple[str, str], float] = {}
	per_target: dict[str, float] = {}
	for source, target, qty in rows or []:
		q = float(qty or 0)
		if not source or not target or source == target or q <= 0:
			continue
		totals[(source, target)] = totals.get((source, target), 0.0) + q
		per_target[target] = per_target.get(target, 0.0) + q
	return {
		key: round(q / per_target[key[1]], 4) for key, q in totals.items() if per_target.get(key[1], 0) > 0
	}
