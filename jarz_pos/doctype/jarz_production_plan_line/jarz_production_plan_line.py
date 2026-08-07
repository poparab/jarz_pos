"""Jarz Production Plan Line — one flavour's row on a day's plan.

A child table of ``Jarz Production Plan``.  Every figure on the row except
``planned_qty`` and ``actual_qty`` is derived by the parent from the flavour's
BOM, so there is deliberately no logic here — the maths lives in
``services.daily_production_plan`` and is applied on the parent's save.

The controller still has to exist: Frappe imports a controller module for every
DocType it syncs, child tables included, and a missing one aborts ``bench
migrate`` for the whole app.
"""

from __future__ import annotations

from frappe.model.document import Document


class JarzProductionPlanLine(Document):
	pass
