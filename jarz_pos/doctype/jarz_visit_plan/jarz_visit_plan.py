# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""A day's field route for one B2B rep.

The document is the plan; the child ``stops`` table's **row order is the
visiting order**. Nothing else encodes sequence — no priority column, no
sort field — because two representations of the same order drift, and the one
the rep drags on screen has to be the one that wins.

What this controller owns is the arithmetic that must never disagree with the
route: stop count, per-leg distance, estimated arrival times, day totals. They
are recomputed from the rows on every save, so a plan edited in Desk, through
the API, or by a hand-drag on the phone all land on the same numbers.

What it deliberately does NOT own is the *ordering*. Optimisation is an
explicit act the rep asks for (:func:`jarz_pos.api.visits.optimize_visit_plan`)
— a save that quietly reshuffled the day would overrule a rep who had just
dragged the 11:00 appointment where they wanted it.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import get_time

from jarz_pos.services.route_planner import DEFAULT_VISIT_MINUTES

#: Statuses that mean the stop is no longer part of the drive. A cancelled
#: stop still shows on the plan (the rep wants to know what they dropped) but
#: it must not inflate the distance or push every later arrival time back.
INACTIVE_STATUSES = ("Cancelled",)


class JarzVisitPlan(Document):
    def validate(self):
        self._normalise_stops()
        self._recompute_totals()
        self._recompute_arrival_times()

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------
    def _normalise_stops(self):
        """Drop unusable rows and re-key the sequence.

        A stop without coordinates cannot be routed to, cannot be navigated to,
        and would poison the distance matrix with a (0, 0) point in the Gulf of
        Guinea — which is not a visible error, just a route that is quietly
        3,000 km long. It is refused at the door instead.
        """
        seen = set()
        cleaned = []
        for row in self.get("stops") or []:
            if not row.reference_name:
                continue
            if not _usable_coord(row.latitude) or not _usable_coord(row.longitude):
                frappe.throw(
                    f"Stop '{row.title or row.reference_name}' has no usable coordinates "
                    "and cannot be routed. Set a location on the lead branch first."
                )
            # The same door twice in one day is a mistake every time; the same
            # BRAND twice is legitimate (two branches, two visits).
            #
            # Identity is the POSITION, not the branch label. Chains name every
            # branch after the chain — production carries 7 distinct T-LAB
            # locations all called "T-LAB", and 114 of its 2,645 doors share a
            # name with a different door. Keying on the label silently dropped
            # them from the route. Five decimals is about a metre: separate
            # enough for two branches on one street, coarse enough that two
            # rows describing the same door still collapse.
            key = (
                row.reference_doctype,
                row.reference_name,
                round(float(row.latitude), 5),
                round(float(row.longitude), 5),
            )
            if key in seen:
                continue
            seen.add(key)
            if not row.visit_minutes or row.visit_minutes < 0:
                row.visit_minutes = 0
            cleaned.append(row)

        for index, row in enumerate(cleaned, start=1):
            row.idx = index
        self.stops = cleaned
        self.total_stops = len(cleaned)

    # ------------------------------------------------------------------
    # Totals
    # ------------------------------------------------------------------
    def default_minutes(self) -> int:
        return int(self.default_visit_minutes or DEFAULT_VISIT_MINUTES)

    def _recompute_totals(self):
        """Sum the legs the optimiser stamped on the rows.

        Legs are stamped by the route service, not derived here: the whole
        point of the OSRM path is that a leg can be a road distance rather than
        a formula, and a controller that recomputed them from coordinates would
        throw that away on the next save.
        """
        distance_km = 0.0
        drive_minutes = 0
        service_minutes = 0
        for row in self.get("stops") or []:
            if row.status in INACTIVE_STATUSES:
                continue
            distance_km += float(row.leg_km or 0)
            drive_minutes += int(row.leg_minutes or 0)
            service_minutes += int(row.visit_minutes or 0) or self.default_minutes()

        self.total_distance_km = round(distance_km, 2)
        self.total_drive_minutes = drive_minutes
        self.total_duration_minutes = drive_minutes + service_minutes

    def _recompute_arrival_times(self):
        """Walk the route from the start time, accumulating drive + dwell.

        Guarded end to end: a plan with no start time is perfectly valid (the
        rep leaves when they leave), and it simply gets no arrival estimates
        rather than failing to save.
        """
        start = self.planned_start_time
        if not start:
            for row in self.get("stops") or []:
                row.planned_time = None
            return

        try:
            cursor = _minutes_since_midnight(start)
        except Exception:
            return

        for row in self.get("stops") or []:
            if row.status in INACTIVE_STATUSES:
                row.planned_time = None
                continue
            cursor += int(row.leg_minutes or 0)
            row.planned_time = _as_time_string(cursor)
            cursor += int(row.visit_minutes or 0) or self.default_minutes()


def _usable_coord(value) -> bool:
    """Whether a Float column holds a real location.

    Exact zero is rejected on purpose. It is what an unset Float reads back as,
    and (0, 0) is a point in the Atlantic — a stop there does not fail, it just
    makes the day 5,000 km long. The same rule lives in
    ``visit_planning._coord``; the two must agree, or a stop saves here and is
    silently skipped by the router.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return parsed != 0.0 and -90.0 <= parsed <= 180.0


def _minutes_since_midnight(value) -> int:
    """Frappe hands a Time field back as a ``timedelta``, not a ``time``.

    Learned the hard way elsewhere in this app; ``get_time`` normalises both
    shapes plus the string a client posts.
    """
    parsed = get_time(value)
    return parsed.hour * 60 + parsed.minute


def _as_time_string(total_minutes: int) -> str:
    """``HH:MM:SS``, wrapping past midnight rather than overflowing.

    A day that runs past midnight is a planning problem the rep can see on the
    screen; a ``ValueError`` on hour 25 is a save that fails for no reason they
    can act on.
    """
    total_minutes = int(total_minutes) % (24 * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}:00"
