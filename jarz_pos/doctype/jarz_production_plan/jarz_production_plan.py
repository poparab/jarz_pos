"""Jarz Production Plan — the morning jar target and the evening count.

One document per production day.  In the morning somebody enters how many jars
of each flavour they intend to fill; the document works out how much cheesecake
mix that is and how to split it across the mixer.  In the evening the same
document takes the count of what actually came out.

Everything derived is recomputed on every save from ``lines`` — no field here is
authored by hand except ``planned_qty``, ``actual_qty``, ``actual_batches_run``
and the notes.  Storing the derived figures rather than computing them on read
is deliberate: a plan is a record of what the floor was told to do that day, and
it must not silently change when a BOM is edited next month.

The maths lives in ``services.daily_production_plan`` so it stays testable
without a database.
"""

from __future__ import annotations

from typing import Any, Dict, List

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, now_datetime

from jarz_pos.services import daily_production_plan as planning

STATUS_DRAFT = "Draft"
STATUS_PLANNED = "Planned"
STATUS_CLOSED = "Closed"

OPEN_STATUSES = (STATUS_DRAFT, STATUS_PLANNED)


class JarzProductionPlan(Document):
    def validate(self):
        self._validate_no_duplicate_open_plan()
        self._validate_lines()
        self._refresh_mix_context()
        self._compute_mix_requirement()
        self._compute_actuals()
        self._stamp_closure()

    # ── validation ────────────────────────────────────────────────────────

    def _validate_no_duplicate_open_plan(self) -> None:
        """One open plan per day per company.

        Two people each starting a plan for the same morning is the failure
        this prevents — the floor would then be working from whichever one they
        happened to open.  Closed plans are exempt so the history of a day can
        be re-planned after the fact.
        """
        if self.status == STATUS_CLOSED:
            return

        duplicate = frappe.db.exists(
            "Jarz Production Plan",
            {
                "plan_date": self.plan_date,
                "company": self.company,
                "status": ["in", OPEN_STATUSES],
                "name": ["!=", self.name or ""],
            },
        )
        if duplicate:
            frappe.throw(
                _("An open production plan already exists for {0} on {1}: {2}").format(
                    self.company, self.plan_date, duplicate
                )
            )

    def _validate_lines(self) -> None:
        seen: Dict[str, int] = {}
        for row in self.lines or []:
            if not row.item_code:
                continue
            if row.item_code in seen:
                frappe.throw(
                    _("Item {0} appears twice, on rows {1} and {2}.").format(
                        row.item_code, seen[row.item_code], row.idx
                    )
                )
            seen[row.item_code] = row.idx

            if cint(row.planned_qty) < 0:
                frappe.throw(_("Row {0}: planned qty cannot be negative.").format(row.idx))
            # actual_qty is intentionally left alone when blank — blank means
            # "not counted yet" and is not the same as zero.
            if row.actual_qty is not None and row.actual_qty != "" and cint(row.actual_qty) < 0:
                frappe.throw(_("Row {0}: actual qty cannot be negative.").format(row.idx))

    # ── derivation ────────────────────────────────────────────────────────

    def _refresh_mix_context(self) -> None:
        """Pull the batch definition from the mix item's default BOM."""
        self.mix_item = planning._resolve_mix_item()
        batch_qty, uom, bom_name = planning._resolve_mix_batch_qty(self.mix_item)
        self.mix_batch_qty = flt(batch_qty, 3)
        self.mix_uom = uom

        if not bom_name:
            # Not fatal: the jar plan is still worth recording.  But the batch
            # split is meaningless without a batch size, so say why rather than
            # rendering a confident zero.
            self.overproduction_note = _(
                "{0} has no submitted default BOM, so batch sizing is unavailable."
            ).format(self.mix_item)

    def _compute_mix_requirement(self) -> None:
        per_unit_map = self._resolve_mix_per_unit()

        demand_lines: List[Dict[str, Any]] = []
        for row in self.lines or []:
            per_unit = flt(per_unit_map.get(row.item_code, 0))
            row.mix_qty_per_unit = per_unit
            row.mix_qty = flt(cint(row.planned_qty) * per_unit, 3)
            row.jars_per_batch = flt(
                planning.jars_per_batch(
                    batch_qty=flt(self.mix_batch_qty), mix_qty_per_unit=per_unit
                )
                or 0,
                1,
            )
            demand_lines.append(
                {
                    "item_code": row.item_code,
                    "planned_qty": cint(row.planned_qty),
                    "mix_qty_per_unit": per_unit,
                }
            )

        demand = planning.aggregate_mix_demand(demand_lines)
        self.total_mix_qty = flt(demand["total_mix_qty"], 3)

        required = planning.batches_needed(
            total_mix_qty=self.total_mix_qty, batch_qty=flt(self.mix_batch_qty)
        )
        split = planning.plan_mixer_runs(
            required,
            run_quality=planning._resolve_run_quality(),
            waste_weight=planning._resolve_waste_weight(),
        )

        self.required_batches = flt(split["required_batches"], 3)
        self.planned_batches = flt(split["planned_batches"], 2)
        self.run_count = cint(split["run_count"])
        self.overproduction_batches = flt(split["overproduction_batches"], 3)
        self.mixer_runs = " + ".join(_format_run(r) for r in split["runs"]) or "—"

        self._describe_overproduction(split)

    def _describe_overproduction(self, split: Dict[str, Any]) -> None:
        """Say what the rounding costs in jars, not just in batches.

        "0.48 batches spare" means nothing on the floor; "about 58 medium jars
        of mix left over" is a number somebody can decide what to do with.
        """
        if split.get("capped"):
            return  # note already set by _refresh_mix_context, or a config problem

        spare = flt(split["overproduction_batches"])
        if spare <= 0.001:
            self.overproduction_note = ""
            return

        reference = self._largest_line_yield()
        if reference:
            item_code, per_batch = reference
            self.overproduction_note = _(
                "Rounding to whole mixer runs makes {0} batches of spare mix — "
                "roughly {1} extra {2}."
            ).format(flt(spare, 2), cint(spare * per_batch), item_code)
        else:
            self.overproduction_note = _(
                "Rounding to whole mixer runs makes {0} batches of spare mix."
            ).format(flt(spare, 2))

    def _largest_line_yield(self):
        """The biggest planned mix-using line, to express spare mix in its units."""
        best = None
        for row in self.lines or []:
            if flt(row.jars_per_batch) <= 0 or cint(row.planned_qty) <= 0:
                continue
            if best is None or cint(row.planned_qty) > best[2]:
                best = (row.item_code, flt(row.jars_per_batch), cint(row.planned_qty))
        return (best[0], best[1]) if best else None

    def _resolve_mix_per_unit(self) -> Dict[str, float]:
        """Mix consumed per finished unit, keyed by item code.

        Read from the one-level BOM rather than the explosion, which is why the
        mix has to be a sub-assembly line.  A jar whose BOM still carries the
        mix flattened into cheese and cream reads as zero — reported by
        ``api.daily_plan.check_bom_readiness`` rather than guessed at here.
        """
        rows = planning._resolve_planable_rows(self.company, self.mix_item)
        mapping = {r["item_code"]: flt(r.get("mix_qty_per_unit") or 0) for r in rows}

        for row in self.lines or []:
            if row.item_code in mapping and not row.default_bom:
                match = next((r for r in rows if r["item_code"] == row.item_code), None)
                if match:
                    row.default_bom = match.get("default_bom")

        return mapping

    def _compute_actuals(self) -> None:
        summary = planning.summarise_actuals(
            [
                {
                    "item_code": row.item_code,
                    "planned_qty": cint(row.planned_qty),
                    "actual_qty": row.actual_qty,
                }
                for row in self.lines or []
            ]
        )

        by_item = {r["item_code"]: r for r in summary["lines"]}
        for row in self.lines or []:
            computed = by_item.get(row.item_code) or {}
            variance = computed.get("variance_qty")
            row.variance_qty = cint(variance) if variance is not None else 0

        self.total_planned_units = cint(summary["planned_total"])
        self.total_actual_units = cint(summary["actual_total"])

        # Default the batches actually run to the plan, so a day that went
        # exactly as planned needs no extra data entry.
        if self.actual_batches_run in (None, "", 0) and self.status == STATUS_CLOSED:
            self.actual_batches_run = flt(self.planned_batches)

        realised = planning.realised_yield(
            actual_mix_batches=flt(self.actual_batches_run),
            actual_units=summary["actual_total"],
        )
        self.realised_units_per_batch = flt(realised or 0, 1)

    def _stamp_closure(self) -> None:
        if self.status == STATUS_CLOSED:
            if not self.closed_on:
                self.closed_on = now_datetime()
                self.closed_by = frappe.session.user
        else:
            self.closed_on = None
            self.closed_by = None


def _format_run(value: float) -> str:
    """``2.0`` reads as ``2``; ``1.5`` stays ``1.5``."""
    value = flt(value)
    return str(int(value)) if value == int(value) else f"{value:g}"
