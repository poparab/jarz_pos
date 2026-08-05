# Courier App — Frozen Contracts (W0)

**Status:** NORMATIVE. Frozen 2026-08-05.
**Companion to:** `COURIER_APP_SPEC.md` (scope) and the parallel build plan (execution).

Every lane codes against this document, not against another lane's code. Renaming anything here
after `bench migrate` has run on staging costs a patch file, not an edit — so changes require
re-freezing and notifying every lane.

---

## 1. DO NOT EDIT — the Sales Invoice state options

Settled 2026-08-05. The "Returned Kanban column" work landed, so the committed value on `main` is
now **seven** options and `test_state_options_frozen.py` asserts exactly this string:

```
Recieved\nIn Progress\nReady\nOut for Delivery\nDelivered\nCancelled\nReturned
```

Stored on `Sales Invoice.custom_sales_invoice_state` as a Select.

**The misspelling "Recieved" is live production data.** It is the stored value in every historical
row, Kanban columns are derived from these options at runtime, and 274 references across 44 files
read them. Correcting the spelling is a data migration, not a typo fix. **This project does not
touch it.**

**No new state is added by this project.** A failed delivery stays `Out for Delivery` and is
expressed through `custom_delivery_failure_reason` + `custom_delivery_attempt_no`.

Guard: `jarz_pos/tests/test_state_options_frozen.py` asserts the exact 7-option string.
The string `custom_sales_invoice_state` must not appear in any diff to
`fixtures/custom_field.json` for the duration of this project.

---

## 2. Sales Invoice — delivery outcome fields (A1)

Anchor chain begins after the existing `custom_courier_party`. Each field's `insert_after` is the
one above it, so the block stays contiguous.

| # | fieldname | fieldtype | insert_after | notes |
|---|---|---|---|---|
| 1 | `custom_delivery_sequence` | Int | `custom_courier_party` | Stop order within the courier's run; 0 = unsequenced |
| 2 | `custom_arrived_at` | Datetime | `custom_delivery_sequence` | |
| 3 | `custom_delivered_at` | Datetime | `custom_arrived_at` | |
| 4 | `custom_delivery_latitude` | Float (precision 6) | `custom_delivered_at` | Where the courier stood at POD |
| 5 | `custom_delivery_longitude` | Float (precision 6) | `custom_delivery_latitude` | |
| 6 | `custom_delivery_accuracy_m` | Float (precision 2) | `custom_delivery_longitude` | GPS accuracy radius in metres |
| 7 | `custom_delivery_attempt_no` | Int, default `0` | `custom_delivery_accuracy_m` | Incremented on every failure |
| 8 | `custom_delivery_failure_reason` | **Small Text** (see below) | `custom_delivery_attempt_no` | Holds a `Delivery Failure Reason` name. Cleared on successful delivery |

### Why field 8 is Small Text and not a Link

Amended 2026-08-05 after the staging migrate failed on this column alone.

`tabSales Invoice` carries **247 columns and is at MariaDB's hard 65,535-byte row
limit**. A Link is `varchar(140)` — roughly 560 inline bytes at utf8mb4 — and the ALTER is
rejected outright:

```
(1118) Row size too large. The maximum row size for the used table type, not counting
BLOBs, is 65535 ... You have to change some columns to TEXT or BLOBs
```

Fields 1–7 are Int, Datetime and Float — small and fixed-width — so they migrated cleanly. This
was the only varchar and the only failure. `Small Text` maps to a TEXT column, which stores an
~20-byte pointer inline.

`options` must stay **empty**. A leftover `"Delivery Failure Reason"` there would make Frappe
treat a TEXT column as a link target and render a broken Desk control.

Referential integrity is not lost: `services/courier_delivery._resolve_failure_reason` resolves the
stored value against the DocType and rejects anything missing or inactive. Only Desk click-through
is given up.

> **This constrains every app, not just this feature.** No new varchar field can be added to
> Sales Invoice by `jarz_pos`, the Woo app, or anything else until existing columns are converted
> to TEXT to reclaim row budget. That is scheduled work against a live table, not a deploy-time fix.

**Every one of these MUST carry `"allow_on_submit": 1`.** They are written on submitted invoices;
without it, Frappe rejects the write and `update_submitted_sales_invoice_fields` filters the field
out and returns `False` **with no error** — a courier's "Delivered" tap would vanish silently.

**Every one of these MUST carry `"no_copy": 1`.** A delivery outcome must not carry over to an
amended invoice.

Fixture entries additionally require `"module": "jarz pos"` and
`"name": "Sales Invoice-<fieldname>"`. A `name` that does not match exactly will be **deleted** by
`cleanup.remove_colliding_custom_fields_for_fixtures` on the next migrate.

---

## 3. Address — geo fields (A4)

Anchor chain begins after the core Frappe field `gps_location` (a free-text `Data` field, last in
the Address layout; we do not use it — it is unstructured and not numerically queryable).

| # | fieldname | fieldtype | insert_after | notes |
|---|---|---|---|---|
| 1 | `custom_latitude` | Float (precision 6) | `gps_location` | |
| 2 | `custom_longitude` | Float (precision 6) | `custom_latitude` | |
| 3 | `custom_geo_source` | Select (see §4) | `custom_longitude` | The label |
| 4 | `custom_geo_confidence` | Int, read-only | `custom_geo_source` | The rank — derived, never written independently |
| 5 | `custom_geo_accuracy_m` | Float (precision 2) | `custom_geo_confidence` | |
| 6 | `custom_geo_verified_on` | Datetime, read-only | `custom_geo_accuracy_m` | Set when source reaches `courier_verified` |

Fixture entries require `"module": "jarz pos"` and `"name": "Address-<fieldname>"`.

### Authorised writers — there are exactly two

Amended 2026-08-05. The original "sole writer" rule was unimplementable: the Woo app cannot import
`jarz_pos` (domain isolation), and it writes with `frappe.db.set_value`, which bypasses the ORM and
therefore bypasses the `before_save` ladder clamp in `jarz_pos/events/address.py`.

| Writer | May write | Rank ceiling |
|---|---|---|
| `jarz_pos/services/geo_resolution.py` | all six fields, any source | up to `manual_override` (50) |
| `jarz_woocommerce_integration/services/geo_passthrough.py` | all six fields | **`customer_pin` (30) only** |

Nothing else writes them — not the Desk form, not a patch, not the POS API directly.

Both writers implement the ladder **independently from §4 of this document**, which is the shared
source of truth. Duplicating a five-entry constant table across a domain boundary is the correct
trade here; importing across it is not. **Each app must carry a test asserting its own
`CONFIDENCE_RANK` matches §4 exactly** — that is what catches drift.

`custom_geo_confidence` is always written in the same call as `custom_geo_source`; a test in each
app asserts the two never disagree.

### Accuracy must never outlive its pin

`custom_geo_accuracy_m` describes the point currently stored. Any write that changes the
coordinates MUST also write the new accuracy — or explicitly NULL it when the incoming source
carries none (Woo pins do not). Leaving the previous value behind produces a stale radius
describing a point that is no longer there, which then silently corrupts any distance or
consensus calculation that trusts it.

### Never rewrite `address_line2`

Maps links live in `address_line2` today as `"Location: <url>"` (`api/customer.py:720`). The
backfill **reads** it and **copies** coordinates out. It must never move, clear, or reformat it —
that field is part of the Woo address-dedup signature (forks duplicate Addresses) and the Woo
outbound-push trigger set (fans out a customer + invoice sync per record).

The `custom_*` fields above are safe by construction: Woo's Address outbound hooks are field-gated
on a list containing no `custom_*` field, so a geo-only save never reaches WooCommerce. **That
safety disappears the moment a text edit is bundled into the same save.**

---

## 4. Confidence ladder — integer ranks

```python
CONFIDENCE_RANK = {
    "territory_centroid": 10,
    "pos_link":           20,
    "customer_pin":       30,
    "courier_verified":   40,
    "manual_override":    50,
}
```

`custom_geo_source` Select options (leading blank is intentional — Frappe convention for optional):

```
\nterritory_centroid\npos_link\ncustomer_pin\ncourier_verified\nmanual_override
```

**Ranks, never string comparison.** Alphabetical ordering puts `courier_verified` below
`customer_pin` and `pos_link` above `manual_override` — the "never downgrade" rule would silently
invert.

**The rule:** a write is accepted only when `incoming_rank >= current_rank`. Equal ranks are
accepted (a fresher pin of the same class wins). Lower ranks are silently ignored, not an error —
a routine Woo re-sync must not fail because the address already has a better pin.

`manual_override` sits highest deliberately: an authorised human with context must be able to fix a
bad pin and have it stick against consensus. It requires a manager role and is logged.

---

## 5. Service signatures (A3)

All three live in `jarz_pos/services/courier_delivery.py`. All return the
`{"success": bool, ...}` envelope and never raise to the caller except `frappe.PermissionError`.

```python
def mark_invoice_arrived(
    invoice_id: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    request_id: str | None = None,
) -> dict: ...

def mark_invoice_delivered(
    invoice_id: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    collected_amount: float | None = None,
    recipient_name: str | None = None,
    is_mocked: bool = False,
    request_id: str | None = None,
) -> dict: ...

def mark_invoice_failed(
    invoice_id: str,
    *,
    failure_reason: str,          # Delivery Failure Reason.name — required
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy_m: float | None = None,
    notes: str | None = None,
    request_id: str | None = None,
) -> dict: ...
```

### Mandatory invariants

1. **Meta assertion first.** Each function asserts its target fields exist via
   `frappe.get_meta("Sales Invoice").get_field(...)` and `frappe.throw`s if not. Without this a
   pre-migration deployment loses writes silently.
2. **All writes go through `delivery_handling.update_submitted_sales_invoice_fields`.** Never
   `frappe.db.set_value` on a submitted invoice.
3. **Dual idempotency.** A deterministic composite token `f"{invoice_id}::delivered"` (immune to
   device reinstall) **plus** the caller-supplied `request_id` replay guard (handles offline
   retry of the same tap). Both are required; `request_id` alone dies when a courier reinstalls.
4. **Failure never changes the state string.** It sets `custom_delivery_failure_reason` and
   increments `custom_delivery_attempt_no`. A test asserts the state field is untouched.
5. **Access gate, in order:** `frappe.has_permission("Sales Invoice", ptype="write", throw=True)`
   → `ensure_profile_scoped_invoice_access(...)` → `ensure_open_shift_for_invoice(...)`.
6. **Feature flag read in the service, not the API** — mirroring
   `services/invoice_return.returns_enabled()`.
7. **Realtime via `utils/realtime.publish_invoice_event` only.** Never `frappe.publish_realtime`
   directly — a bare call broadcasts site-wide.

---

## 6. Delivery Failure Reason — seed set

Doctype `Delivery Failure Reason`, seeded create-only by `setup/courier_setup.py` on
`after_migrate`. Fields: `code` (Data, unique), `label_en`, `label_ar`, `next_action`
(Select: `Reschedule\nReturn\nCancel`), `is_active` (Check, default 1).

| code | label_en | label_ar | next_action |
|---|---|---|---|
| `CUSTOMER_UNREACHABLE` | Customer unreachable | العميل لا يرد | Reschedule |
| `CUSTOMER_REFUSED` | Customer refused the order | العميل رفض الطلب | Return |
| `WRONG_ADDRESS` | Wrong or incomplete address | العنوان خطأ أو ناقص | Reschedule |
| `POSTPONED_BY_CUSTOMER` | Customer asked to postpone | العميل طلب التأجيل | Reschedule |
| `AREA_INACCESSIBLE` | Area inaccessible | المنطقة يصعب الوصول إليها | Reschedule |
| `PAYMENT_UNAVAILABLE` | Customer could not pay | العميل لا يستطيع الدفع | Return |

Seeder must be create-only (`frappe.db.exists` before insert) and must swallow exceptions — it runs
inside the shared `bench migrate` that `jarz_pos` also depends on.

---

## 7. WebSocket events

Appended to `jarz_pos/constants.py` `WS_EVENTS`, then **that file is frozen** for this project.

```python
COURIER_STOP_ARRIVED     = "jarz_pos_courier_stop_arrived"
COURIER_STOP_DELIVERED   = "jarz_pos_courier_stop_delivered"
COURIER_STOP_FAILED      = "jarz_pos_courier_stop_failed"
COURIER_DUTY_CHANGED     = "jarz_pos_courier_duty_changed"
COURIER_DEPOSIT_DECLARED = "jarz_pos_courier_deposit_declared"
ADDRESS_PIN_UPDATED      = "jarz_pos_address_pin_updated"
```

Mirrored in `jarz_pos_mobile/jarz_pos/lib/src/core/constants/ws_events.dart`.

**The invariant is Python ⊆ Dart for events Flutter listens to — not set equality.** The two sets
already differ today (`TRIP_CREATED`/`TRIP_OFD`/`TRIP_COMPLETED` and the `CUSTOM_SHIPPING_*` family
have no Dart counterpart; `newPosInvoice`/`posProfileUpdate`/`itemStockUpdate`/`roomPosUpdates` have
no Python counterpart). Do not write an equality test — it fails on day one.

---

## 8. API conventions

Template: `jarz_pos/api/returns.py`. Every endpoint module follows it exactly.

- `from __future__ import annotations`, full type hints, module docstring explaining the *why*.
- A module-private `_ensure_<feature>_permission()` as the **first statement** of every endpoint.
- `@frappe.whitelist(allow_guest=False)` explicit on every endpoint.
- Return `{"success": True, ...}` / `{"success": False, "error": str}`.
- `except frappe.PermissionError: raise` **before** the generic handler, so permission failures
  surface as real 403s rather than being flattened into a success envelope.
- Feature trio where applicable: **preview** (read-only) / **execute** (write) / **dry-run**
  (resolve and balance, write nothing) — the dry-run exists so staging harnesses can sweep
  production clones.
- Business logic lives in `services/`; the API layer stays thin transport.

---

## 9. `jarz_courier` boundary

- `required_apps = ["jarz_pos"]`. One-way: `jarz_courier` may import `jarz_pos`; `jarz_pos` and
  `jarz_woocommerce_integration` must never import `jarz_courier`.
- **Never writes GL, never creates a Journal Entry, never inserts a `Courier Transaction`.**
  Reads freely; writes through `jarz_pos` service functions. Enforced by
  `jarz_courier/tests/test_no_gl_writes.py`, which AST-greps the app and must be listed in the
  `MODULES` array of its CI workflow.
- **Declares zero Custom Fields on any doctype `jarz_pos` touches.** `jarz_pos`'s collision cleanup
  deletes fields whose `name` differs; whichever app migrates second would win and silently drop
  the other's field. All shared schema is owned by lane P1, exclusively.
- Every migrate hook swallows exceptions — a raising seeder aborts the shared `bench migrate`.

---

## 10a. Geo API wire contract (added 2026-08-05)

This was missing from the original freeze and both sides had to guess. **Frappe silently drops form
keys that are not in a whitelisted method's signature**, so a parameter-name mismatch does not
raise — it manifests as "nothing ever resolves". Pin it here; both sides conform to this, not to
each other.

### `jarz_pos.api.geo.preview_maps_link`

Request — the parameter is named **`link`**:

```json
{ "link": "<pasted text: long URL, maps.app.goo.gl short link, plus code, or bare lat,lng>" }
```

Response:

```json
{
  "success": true,
  "latitude": 30.0444,
  "longitude": 31.2357,
  "precision": "pin" | "viewport" | "query" | "plus_code",
  "distance_from_branch_m": 4213.7,
  "error": null
}
```

- `precision` distinguishes a real pin (`!3d/!4d`) from a mere viewport centre (`@`), which can be
  hundreds of metres off. The client may warn on `viewport`.
- `distance_from_branch_m` is **optional**. When absent the client accepts the point — it applies
  its own 150 km sanity ceiling as a backstop only. If the backend later returns its own
  plausibility verdict, that verdict wins over the client ceiling.
- On failure: `{"success": false, "error": "<english reason>"}`. The client shows a localised
  generic message and logs the reason; server strings are English-only and would break the Arabic
  UI if surfaced verbatim.

### Address save payload

`create_customer` and `save_customer_shipping_address` accept these optional keys:

| key | type |
|---|---|
| `location_link` | string, the raw pasted text |
| `latitude` | float |
| `longitude` | float |
| `geo_source` | one of §4's source labels |

Same silent-drop property: until the fields are migrated, sending them is a harmless no-op.

---

## 10. Release-classification trap for the POS Flutter app

`classify_mobile_release.ps1` excludes `lib/src/core/*`, `lib/src/data/*`, `lib/src/domain/*`,
`lib/src/services/*` and `lib/main.dart` from Shorebird patch candidates. Anything under those
paths is a mobile runtime path that is *not* patchable, so it falls through to **`full_apk`** —
a full APK build plus Firebase App Distribution to the POS tester list.

`lib/src/core/constants/ws_events.dart` is in that set. A push to `main` touching only that file
ships a full APK to POS staff for a constants-only change.

**Rule for the courier lanes:** never push a `lib/src/core/*` change on its own. Batch it with the
feature that actually consumes it, so one release carries something real.
