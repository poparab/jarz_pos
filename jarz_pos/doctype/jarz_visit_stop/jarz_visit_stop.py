# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""One door on a day's route.

A child row rather than its own document on purpose: a route is edited as a
whole — dragged, re-optimised, trimmed — and a child table gives that
atomically. The row still answers "when was this lead last on a route?"
through a plain query on ``parent``.

Everything a screen needs to draw the stop is COPIED here at the time it is
added: name, branch, coordinates, phone. The catalog importer rewrites the
``Jarz Lead Branch`` rows wholesale on every re-import, so a stop that read its
coordinates through the lead would silently move — or vanish — between the
morning the route was planned and the morning it is driven.
"""

from __future__ import annotations

from frappe.model.document import Document


class JarzVisitStop(Document):
    pass
