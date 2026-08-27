"""Prove that every field this app *declares* actually exists on the site.

``bench migrate`` exiting 0 is not evidence that a new field landed. Two
mechanisms in Frappe drop schema silently, and both have been hit here:

* **Fixtures skip on a matching ``modified``.** The fixture importer compares the
  JSON's ``modified`` timestamp against the stored doc and skips the import when
  they match. A flag-only edit to ``fixtures/custom_field.json`` -- flipping
  ``in_list_view``, widening a ``length``, adding an ``options`` line -- therefore
  does nothing at all unless ``modified`` is also bumped. The deploy reports
  success; the field keeps its old definition.
* **A ``dt`` missing from ``hooks.fixtures``.** The filter in ``hooks.py`` lists
  the doctypes whose Custom Fields are exported *and imported*. A field added to
  the JSON for a ``dt`` absent from that list is never imported on any site, and
  nothing anywhere says so. ``hooks.py`` already carries a comment warning that
  omitting a ``dt`` there makes its fields "silently never migrate".

Both failures look identical from the deploy's side: green migrate, missing
field, and the first symptom is an endpoint reading ``None`` in production days
later. This module closes that gap by asserting the *declared* schema against
the *live* schema after every migrate, and failing the deploy when they differ.

What is checked
---------------
1. Every Custom Field in ``fixtures/custom_field.json`` resolves through
   ``frappe.get_meta(dt).get_field(fieldname)`` -- the same lookup the ORM uses,
   so a pass means the application layer can actually read the field.
2. Its ``dt`` is covered by the ``Custom Field`` fixture filter of *some
   installed app* — the union, not just this app's hook. ``jarz_pos`` declares
   ``Territory.double_shipping_single_trip`` and reads it in
   ``doctype/delivery_trip``, while it is ``jarz_woocommerce_integration`` that
   lists ``Territory`` and actually imports it. Judging coverage from this app
   alone would report a present field as never-imported. Reported as a
   **warning** and never a failure: an uncovered field may still be live (created
   in Desk, or inherited from the site the fixture was exported from), and the
   only thing coverage proves is whether a *fresh* site would get it.
3. Every DocType shipped as JSON under ``jarz_pos/doctype/`` exists, and each of
   its storable fields is present in meta. Layout fieldtypes (breaks, HTML,
   headings) and virtual fields carry no storage and are skipped.
4. For non-Single doctypes, storable fields also have a real column. Meta can be
   satisfied while the ``ALTER TABLE`` never ran; a Single keeps its values as
   rows in ``tabSingles`` and legitimately has no column of its own.

Read-only. Nothing here writes, and it is safe to run on production.

Run::

    bench --site frontend execute jarz_pos.scripts.verify_schema_landed.run

Exits non-zero when anything is missing, which is what makes the deploy stop.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import frappe

MARKER_START = "SCHEMA_LANDED_JSON_START"
MARKER_END = "SCHEMA_LANDED_JSON_END"

APP_NAME = "jarz_pos"

#: Fieldtypes that define layout or presentation and never own a column or a
#: meta-backed value. Checking them would produce noise, not signal.
_NON_STORAGE_FIELDTYPES = {
    "Section Break",
    "Column Break",
    "Tab Break",
    "HTML",
    "Heading",
    "Button",
    "Fold",
    "Image",
}


def _app_path() -> str:
    return frappe.get_app_path(APP_NAME)


def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _app_custom_field_dts(app_name: str) -> Optional[set]:
    """The ``dt`` values whose Custom Fields ``app_name``'s fixtures hook imports.

    Returns ``None`` when the hook does not use the ``[["dt", "in", [...]]]``
    filter shape. An unrecognised shape must not be read as "nothing is
    covered" — that would turn a parsing gap into a false finding.
    """
    try:
        fixtures = frappe.get_hooks("fixtures", app_name=app_name) or []
    except Exception:
        return None

    for entry in fixtures:
        if not isinstance(entry, dict) or entry.get("dt") != "Custom Field":
            continue
        for condition in entry.get("filters") or []:
            if (
                isinstance(condition, (list, tuple))
                and len(condition) == 3
                and condition[0] == "dt"
                and str(condition[1]).lower() == "in"
                and isinstance(condition[2], (list, tuple))
            ):
                return {str(value) for value in condition[2]}
    return None


def _fixtured_custom_field_dts() -> Optional[set]:
    """Every ``dt`` that *any installed app* imports Custom Fields for.

    Deliberately not just this app's hook. ``jarz_woocommerce_integration``
    lists ``Territory`` in its own filter and ships
    ``Territory.double_shipping_single_trip`` in its fixture — a field
    ``jarz_pos`` also declares and reads. Judging coverage from ``jarz_pos``
    alone would report that field as never-imported when another installed app
    imports it on every migrate, i.e. a false finding on a field that is in fact
    present. The union is the only definition of "covered" that matches what the
    site actually does.

    Returns ``None`` when no app's hook could be parsed, which suppresses the
    coverage check rather than reporting everything as uncovered.
    """
    try:
        apps = frappe.get_installed_apps() or []
    except Exception:
        apps = [APP_NAME]

    if APP_NAME not in apps:
        apps = list(apps) + [APP_NAME]

    covered: set = set()
    parsed_any = False
    for app in apps:
        app_dts = _app_custom_field_dts(app)
        if app_dts is None:
            continue
        parsed_any = True
        covered |= app_dts

    return covered if parsed_any else None


def _meta_field(doctype: str, fieldname: str):
    try:
        return frappe.get_meta(doctype).get_field(fieldname)
    except Exception:
        return None


def _doctype_exists(doctype: str) -> bool:
    try:
        return bool(frappe.db.exists("DocType", doctype))
    except Exception:
        return False


def _is_single(doctype: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).issingle)
    except Exception:
        return False


def _has_column(doctype: str, fieldname: str) -> Optional[bool]:
    """``None`` when the answer cannot be established, so it is never asserted."""
    try:
        return bool(frappe.db.has_column(doctype, fieldname))
    except Exception:
        return None


def _is_virtual(field: Dict[str, Any]) -> bool:
    try:
        return bool(frappe.utils.cint(field.get("is_virtual")))
    except Exception:
        return False


def _check_custom_fields(findings: Dict[str, List[Dict[str, str]]]) -> int:
    path = os.path.join(_app_path(), "fixtures", "custom_field.json")
    rows = _load_json(path)
    if not isinstance(rows, list):
        findings["errors"].append(
            {"what": "fixtures/custom_field.json", "why": "unreadable or not a list"}
        )
        return 0

    covered = _fixtured_custom_field_dts()
    checked = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        doctype = row.get("dt")
        fieldname = row.get("fieldname")
        if not doctype or not fieldname:
            continue
        checked += 1

        # Coverage is a WARNING, never a failure, and never short-circuits the
        # meta check below. A field can be uncovered here and still be present —
        # created by hand in Desk, or inherited from a site this fixture was
        # exported from. Only the live meta says whether it is actually there,
        # so that check always runs and it is the one that fails the deploy.
        # What this adds is the reason a *fresh* site would not get the field.
        if covered is not None and doctype not in covered:
            findings["unfixtured"].append(
                {
                    "doctype": doctype,
                    "fieldname": fieldname,
                    "why": (
                        "declared in fixtures/custom_field.json but no installed app "
                        "lists this 'dt' in its Custom Field fixture filter — it will "
                        "not be imported onto a fresh site"
                    ),
                }
            )

        if not _doctype_exists(doctype):
            # A custom field for a doctype this site does not have (an app that
            # is not installed here) is an environment difference, not a failed
            # migrate. Recorded, never asserted.
            findings["skipped"].append(
                {
                    "doctype": doctype,
                    "fieldname": fieldname,
                    "why": "DocType not installed on this site",
                }
            )
            continue

        if _meta_field(doctype, fieldname) is None:
            findings["missing"].append(
                {
                    "doctype": doctype,
                    "fieldname": fieldname,
                    "why": "declared as a Custom Field but absent from the live meta",
                }
            )
            continue

        if str(row.get("fieldtype") or "") in _NON_STORAGE_FIELDTYPES or _is_virtual(row):
            continue
        if _is_single(doctype):
            continue
        if _has_column(doctype, fieldname) is False:
            findings["missing_column"].append(
                {
                    "doctype": doctype,
                    "fieldname": fieldname,
                    "why": "present in meta but the table has no column -- the ALTER never ran",
                }
            )

    return checked


def _check_app_doctypes(findings: Dict[str, List[Dict[str, str]]]) -> int:
    root = os.path.join(_app_path(), "doctype")
    if not os.path.isdir(root):
        return 0

    checked = 0
    for folder in sorted(os.listdir(root)):
        definition = os.path.join(root, folder, f"{folder}.json")
        if not os.path.isfile(definition):
            continue
        payload = _load_json(definition)
        if not isinstance(payload, dict) or payload.get("doctype") != "DocType":
            continue

        doctype = payload.get("name")
        if not doctype:
            continue
        checked += 1

        if not _doctype_exists(doctype):
            findings["missing"].append(
                {
                    "doctype": doctype,
                    "fieldname": "*",
                    "why": "DocType shipped as JSON but absent from the site",
                }
            )
            continue

        single = _is_single(doctype)
        for field in payload.get("fields") or []:
            if not isinstance(field, dict):
                continue
            fieldname = field.get("fieldname")
            if not fieldname:
                continue
            if str(field.get("fieldtype") or "") in _NON_STORAGE_FIELDTYPES:
                continue
            if _is_virtual(field):
                continue

            if _meta_field(doctype, fieldname) is None:
                findings["missing"].append(
                    {
                        "doctype": doctype,
                        "fieldname": fieldname,
                        "why": "declared in the DocType JSON but absent from the live meta",
                    }
                )
                continue

            if single:
                continue
            if _has_column(doctype, fieldname) is False:
                findings["missing_column"].append(
                    {
                        "doctype": doctype,
                        "fieldname": fieldname,
                        "why": "present in meta but the table has no column -- the ALTER never ran",
                    }
                )

    return checked


def run() -> Dict[str, Any]:
    """Assert the declared schema is live. Raises when it is not."""
    findings: Dict[str, List[Dict[str, str]]] = {
        "missing": [],
        "missing_column": [],
        "unfixtured": [],
        "skipped": [],
        "errors": [],
    }

    custom_fields_checked = _check_custom_fields(findings)
    doctypes_checked = _check_app_doctypes(findings)

    # Only ground truth fails the deploy: a field the live meta does not have, a
    # column that was never created, a DocType that is not on the site. Fixture
    # coverage is reported alongside as a warning — it explains why a fresh site
    # would lack a field, but a covered-by-nobody field that is nonetheless
    # present is not a reason to stop a release.
    failure_buckets = ("missing", "missing_column", "errors")
    failures = [item for bucket in failure_buckets for item in findings[bucket]]

    summary: Dict[str, Any] = {
        "app": APP_NAME,
        "site": frappe.local.site,
        "custom_fields_checked": custom_fields_checked,
        "doctypes_checked": doctypes_checked,
        "missing": findings["missing"],
        "missing_column": findings["missing_column"],
        "unfixtured": findings["unfixtured"],
        "skipped_count": len(findings["skipped"]),
        "errors": findings["errors"],
        "ok": not failures,
    }

    print(MARKER_START)
    print(json.dumps(summary, indent=2, default=str))
    print(MARKER_END)

    # Printed whether or not the run passes: a warning that only appears on a
    # failing run is invisible on exactly the deploys that are still fine, which
    # is when there is time to act on it.
    for item in findings["unfixtured"]:
        print(
            f"Schema verification WARNING: {item['doctype']}.{item['fieldname']} -- "
            f"{item['why']}"
        )

    if failures:
        lines = [
            f"Schema verification FAILED: {len(failures)} declared field(s) are not "
            f"live on {frappe.local.site} after migrate.",
            "",
        ]
        for bucket in failure_buckets:
            for item in findings[bucket]:
                if "doctype" in item:
                    lines.append(
                        f"  [{bucket}] {item['doctype']}.{item['fieldname']} -- {item['why']}"
                    )
                else:
                    lines.append(f"  [{bucket}] {item.get('what')} -- {item.get('why')}")
        lines += [
            "",
            "Most likely cause: a fixture edit whose 'modified' timestamp was not "
            "bumped, so the import skipped it. Bump 'modified' in "
            "fixtures/custom_field.json and redeploy, or add the missing 'dt' to the "
            "Custom Field filter in hooks.fixtures.",
        ]
        raise frappe.ValidationError("\n".join(lines))

    print(
        f"Schema verification OK: {custom_fields_checked} custom field(s) and "
        f"{doctypes_checked} DocType(s) are live on {frappe.local.site}."
    )
    return summary
