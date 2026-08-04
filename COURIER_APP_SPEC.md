# Courier App — Agreed Scope & Architecture

**Status:** Draft for agreement — not yet started
**Date:** 2026-08-03
**Owner:** @jarz

---

## 1. Goal

A dedicated Android app for Jarz's own delivery couriers, integrated with ERPNext, covering:

- Per-order delivery execution (run sheet, proof of delivery, failure handling)
- COD cash accountability and a courier-visible statement
- Verified delivery coordinates (the "door pin" database)
- Background location tracking during active duty
- Anomaly detection (detour, idle, speeding, delivered-far-from-pin)

Deliberately **out of scope for now**: customer-facing live tracking page, route
optimization, ETAs, WooCommerce status pushback, gamification, iOS.

---

## 2. Architecture decisions (agreed)

| Decision | Choice | Notes |
|---|---|---|
| Courier mobile app | **Separate Flutter app in a NEW repo** | The POS repo root *is* the POS Flutter package root — nesting is not viable |
| Code sharing | **None in P0/P1 — duplicate the plumbing** | Two separate repos cannot share a Flutter `path:` dependency. Revisit later with a `git:`-pinned package |
| Backend | **New Frappe app `jarz_courier`** | Layered on `jarz_pos`, not a peer |
| Dependency direction | `jarz_courier` → `jarz_pos` (one way, never reversed) | `required_apps = ["jarz_pos"]` |
| Platform | Android only | No iOS in scope |
| Background location | `flutter_foreground_task` + `geolocator` | Free stack; no paid plugin |
| Maps — display | **`flutter_map` + OSM tiles** | Already a POS dependency, already used in `features/leads/.../lead_map.dart` |
| Maps — navigation | `url_launcher` deeplink to Google Maps / Waze | Free, already a POS dependency |
| Maps — coordinates | Parsed from Google Maps links / Woo pin | No Geocoding API calls, no Google SDK |
| Distribution | Firebase App Distribution + Shorebird | Own app-id and own tester list — never reuse the POS ones |

### 2.1 The hard guardrail

> **`jarz_courier` never writes GL, never creates a Journal Entry, and never inserts a
> `Courier Transaction` directly. It reads freely and writes *through* `jarz_pos`
> service functions.**

Rationale: the GL audit suite covers `jarz_pos` only. Any money logic that lands in
`jarz_courier` is untested money logic and creates a second source of truth.

### 2.2 Flutter app integration

The two Flutter apps have **no direct coupling** — no deeplinks, no shared storage, no
app-to-app calls. They integrate entirely through the backend: same Frappe site, same
auth, dispatcher actions in POS, courier actions in the courier app, coordinated via
document state and per-branch realtime rooms.

---

## 3. Finding: the Delivery Trip is NOT the unit of work

Investigated 2026-08-03. This changes the foundation and removes the riskiest item
from the critical path.

### 3.1 What the code actually does

**The courier is assigned on the Sales Invoice, not on the trip.**
`Sales Invoice` carries `custom_courier_party_type` and `custom_courier_party`.
Both the trip path (`api/trips.py`) and the individual path
(`services/delivery_handling.py`) write these same two fields.

**`Delivery Trip` is a thin batching wrapper.** Its only real business logic is
`_compute_double_shipping()` in `jarz_pos/doctype/delivery_trip/delivery_trip.py`:

> If **all** invoices in the trip resolve to the same effective territory
> (`custom_sub_territory` or `territory`), and that territory has the
> `double_shipping_single_trip` flag → `is_double_shipping = 1` → shipping expense × 2
> at Out-for-Delivery.

**`send_trip_for_delivery()` is a loop over the per-invoice primitives.** For each
invoice it calls `ensure_delivery_note_for_invoice()`, then either
`mark_courier_outstanding()` (unpaid) or `_create_shipping_expense_to_creditors_je()`
plus a `Courier Transaction` insert (paid). The source comments say as much:
*"same as individual settle-later flow"* and *"Same accounting as
handle_paid_settle_later in settlement_strategies"*.

### 3.2 Consequence

The **Sales Invoice is already the unit of work**, and the per-invoice accounting
primitives already exist and are already covered by the GL audit suite.

Therefore:

- **We do NOT refactor `Delivery Trip` / `Delivery Trip Invoice` into a stop model.**
- **We do NOT touch the trip logic or its accounting at all.**
- The courier's run sheet is a **query**, not a document:
  `Sales Invoice WHERE custom_courier_party = me AND custom_sales_invoice_state = 'Out for Delivery'`
  — this works today with zero schema change.
- Delivery outcome fields go on the **Sales Invoice**, alongside the existing
  `custom_sales_invoice_state` machine.

A persistent work-unit doctype (`Courier Run`) is introduced only in P2, when we need
something a query cannot provide: an anchor for the duty session, the GPS polyline, and
run-level distance.

### 3.3 Open question — double shipping

`Delivery Trip` is left exactly as-is, used only for the double-shipping multiplier and
the `get_double_shipping_impact` panel on the shipping analytics dashboard.

**To verify:** are Delivery Trips still being created in production? If they are not,
then `is_double_shipping` is never computed and the ×2 shipping expense is never
applied — meaning double-shipping territories are being systematically under-expensed.
Check production `Delivery Trip` creation volume before deciding.

Possible follow-up (not in scope yet): auto-create a Delivery Trip when a courier's run
qualifies for double shipping, so the expense applies without manual trip creation.

### 3.4 Hard constraint — never rewrite `address_line2`

Maps links are stored today as `address_line2 = "Location: <url>"` (`api/customer.py:720`).
The backfill must **read** that field and **copy** the coordinates into the new geo fields.
It must never move, clear, or reformat `address_line2`, because that field is:

- part of the WooCommerce address-dedup signature (`customer_sync._address_signature_parts`) —
  changing it forks a duplicate `Address` for every migrated record; and
- part of the WooCommerce outbound-push trigger set (`outbound_sync`) — changing it fans out a
  customer sync **and** an invoice sync to WooCommerce for every migrated address.

The new `custom_*` geo fields are safe by construction: woo's Address outbound hooks are
field-gated on a list that contains no `custom_*` field, so a geo-only save never reaches
WooCommerce. That safety disappears the moment anyone bundles a text edit into the same save.

---

## 4. Modules

Phase tags: `[P0]` foundation · `[P1]` working app · `[P2]` tracking · `[P3]` intelligence

### A. `jarz_pos` — foundation changes

| # | Module | Scope | Phase |
|---|---|---|---|
| A1 | **Invoice delivery outcome fields** | Custom fields on `Sales Invoice`: `custom_delivery_sequence`, `custom_arrived_at`, `custom_delivered_at`, `custom_delivery_latitude`, `custom_delivery_longitude`, `custom_delivery_accuracy_m`, `custom_delivery_attempt_no`, `custom_delivery_failure_reason` | P0 |
| A2 | **Failure state + reasons** | New `Delivery Failure Reason` setup doctype (code, EN/AR label, next action: reschedule/return/cancel). Add `Delivery Failed` to the invoice state machine, or model as OFD + failure reason — **decision pending, see §6** | P0 |
| A3 | **Per-invoice courier transitions** | Service functions `mark_invoice_arrived()`, `mark_invoice_delivered()`, `mark_invoice_failed()` — wrapping the existing primitives, idempotent, safe for offline retry | P0 |
| A4 | **Address geo fields** | Custom fields on `Address`: `custom_latitude`, `custom_longitude`, `custom_geo_source`, `custom_geo_confidence`, `custom_geo_accuracy_m`, `custom_geo_verified_on`. Confidence ladder enforced server-side: `courier_verified > customer_pin > pos_link > territory_centroid` — never downgrade | P0 |
| A5 | **Courier identity resolution** | `Employee.user_id` → courier party; "my invoices" service reusing `assert_courier_matches_pos_profile` | P0 |
| A6 | **OFD pin gate** | Add "address has no coordinates" to `_build_ofd_preview_errors()`, behind a settings flag for soft launch. **Two call sites** — `api/kanban.py` AND `api/trips.py`; patching one leaves the other bypassing the gate | P1 |
| A7 | **Geo resolution core** *(relocated from B5)* | `utils/geo.py` pure parser (short-link redirect resolve, `!3d/!4d` first, `@` fallback, `q=`/`ll=`, Plus Codes); `services/geo_resolution.py` as the sole writer to the Address geo fields with rank-based never-downgrade; `api/geo.py` preview/execute/dry-run; `events/address.py` before_save clamp; backfill script. **Must live here, not in `jarz_courier`** — D1 (the POS paste field) needs the parser and `jarz_pos` cannot import `jarz_courier` | P0 |

*`Delivery Trip`, `Delivery Trip Invoice`, `Courier Transaction`, settlement strategies,
and Delivery Note creation are **untouched**.*

### B. `jarz_courier` — new Frappe app

| # | Module | Features | Phase |
|---|---|---|---|
| B1 | **Identity & Device** | `Courier Device` doctype (party, device id, FCM token, app/OS version, bound_on, is_active); one active device per courier; phone + PIN login; force-unbind from Desk | P1 |
| B2 | **Duty session** | `Courier Duty` doctype (courier, branch, start/end, vehicle, opening float, closing cash, status); start/end duty API; duty-close reconciliation summary | P1 |
| B3 | **Run sheet API** | `get_my_run`, `get_stop_detail` — courier-scoped and branch-scoped queries over Sales Invoice. Stop payload: customer, phone, address text, landmark, coords, amount to collect, item summary, notes. All writes delegate to A3 | P1 |
| B4 | **Proof of Delivery** | `Delivery Proof` doctype (invoice ref, type photo/signature/OTP, file, captured lat/lng/accuracy, timestamp, recipient name, `is_mocked`); upload API tolerant of deferred/retried uploads; per-POS-Profile config of required POD types | P1 |
| B5 | **Consensus hardening** *(reduced scope — parser moved to A7)* | Scheduled job that clusters `Delivery Proof` coordinates per address and calls `jarz_pos.services.geo_resolution` to promote to `courier_verified` at 2–3 deliveries within ~40 m. This is the only part of geo resolution that belongs here, because it reads a `jarz_courier` doctype | P2 |
| B6 | **Courier accounting** | Statement API over `Courier Transaction` (unsettled balance, collected today, fees earned, deductions, settlement history with JE links); `Courier Deposit Declaration` doctype (amount, method cash/InstaPay, reference, photo, status); manager confirm calls the `jarz_pos` settlement service. **Read-only over the ledger — the declaration is the only write** | P1 |
| B7 | **Location tracking** | `Courier Run` doctype (anchors duty + polyline + distance); ping ingest → **Redis only, no ORM writes**; batch ingest for offline flush; last-known key + realtime publish to **per-branch room** (never `user="*"`); polyline persisted on run close; stale-ping watchdog + high-priority FCM wake; mock-location rejection | P2 |
| B8 | **Notifications** | FCM registration + send (assignment, run change, settlement confirmed) | P2 |
| B9 | **Anomaly detection** | Detour ratio, idle time, speeding, delivered-far-from-pin, ping gaps, mock GPS; `Courier Anomaly` doctype + Desk report | P3 |

### C. Flutter courier app

| # | Module | Features | Phase |
|---|---|---|---|
| C1 | **Shell & auth** | Phone + PIN, device binding, session, Arabic-first + RTL, large targets, high-contrast for sunlight | P1 |
| C2 | **Duty** | Start/end duty, opening float; blocked if tracking health fails (P2 onward) | P1 |
| C3 | **Run sheet** | Ordered list of today's stops | P1 |
| C4 | **Stop detail & actions** | Call, WhatsApp deeplink, Navigate (Google Maps deeplink), Arrived / Delivered / Failed + reason | P1 |
| C5 | **POD capture** | Camera + compression, signature, OTP, **foreground GPS capture at delivery**, recipient name | P1 |
| C6 | **Cash** | Per-stop collected confirmation, running total, declare deposit | P1 |
| C7 | **My Account** | Statement, balance, settlement history | P1 |
| C8 | **Offline** | Hive queue for every action, deferred photo upload, retry and conflict handling | P1 |
| C9 | **Background tracking** | `flutter_foreground_task` + `geolocator`; ping buffer; boot receiver + sticky restart; stationary mode | P2 |
| C10 | **Tracking Health** | Self-diagnosis: permissions, battery exemption, OEM autostart, last ping, queue depth — with fix-it deeplinks | P2 |
| C11 | **Push** | FCM | P2 |

**No shared package in P0/P1.** Two separate GitHub repos cannot share a Flutter `path:`
dependency, and the POS repo root *is* its package root, so there is no sibling directory to
put one in. Copy the ~600 lines of plumbing instead — API client, auth/session, Hive sync
queue, connectivity, Sentry — using `core/offline/offline_queue.dart` (note
`maxReplayAttempts = 3` / `markAttemptFailed`, exactly the semantics POD upload needs) and
`core/sync/offline_sync_service.dart` as the reference implementations. Revisit extraction at
P2 with a `git:`-ref-pinned package in its own repo.

**No shared UI, ever** — courier UI is a different product (one-handed, huge targets, readable
in sun, helmet-compatible).

### D. POS app / Desk — dispatcher side

| # | Module | Features | Phase |
|---|---|---|---|
| D1 | **Maps-link field** | Paste, resolve, preview on mini-map (reuse `features/leads/.../lead_map.dart`), show distance from branch, validate. **No `pubspec.yaml` change** — that would force `full_apk` and lose Shorebird patching. **This is a migration, not a greenfield feature:** `api/customer.py:720` already writes `address_line2 = f"Location: {location_link}"`, so Maps links are being captured at POS today and form a ready-made backfill corpus | P0 |
| D2 | **Kanban delivery progress** | Courier column shows "7/12 delivered" | P1 |
| D3 | **Deposit confirmation** | Manager approves courier deposit declarations | P1 |
| D4 | **Courier assignment** | Mostly exists; add optional run sequencing | P1 |
| D5 | **Live fleet map** | Per-branch realtime | P3 |

### E. WooCommerce — minimal

| # | Module | Features | Phase |
|---|---|---|---|
| E1 | **Checkout map pin** | `_jarz_lat` / `_jarz_lng` on the order | P0 |
| E2 | **Passthrough on sync** | Write pin into `Address` geo fields at confidence `customer_pin` | P0 |

Status pushback, custom order statuses, and tracking links wait for the
customer-facing phase. `jarz_woocommerce_integration` must **not** import `jarz_courier`
— contract on standard Sales Invoice fields only.

---

## 5. Phasing

| Phase | Contents | Delivers | Rough effort *(estimate)* |
|---|---|---|---|
| **P0** | A1–A5, **A7**, D1, E1, E2 | Coordinates flowing in, invoice outcome fields ready. Not user-visible. | ~1.5 weeks |
| **P1** | A6, B1–B4, B6, C1–C8, D2–D4 | **Working courier app: run sheet, POD, cash, statement — door pins accumulating.** Zero background tracking. | ~4–6 weeks |
| **P2** | **B5**, B7, B8, C9–C11 | Background tracking, live position, push, consensus pin hardening | ~3–4 weeks |
| **P3** | B9, D5 | Anomaly detection + dispatcher live map | ~2 weeks |

**Key property:** P1 is fully shippable and useful with no background location at all.
Couriers get a real app, the business gets POD and cash accountability, and the door-pin
database starts compounding — all before the hardest engineering problem is touched.

---

## 6. Open decisions

1. **Failed delivery state.** Does a failed stop move the invoice to a new
   `Delivery Failed` state, or stay `Out for Delivery` with a failure reason and attempt
   count? A new state is cleaner for the Kanban but touches the state machine and every
   consumer of it. *Recommendation: stay OFD + reason for P1; add a state only if the
   Kanban needs the column.*

2. **Partial delivery** (customer refuses some items at the door). Real GL consequences.
   *Recommendation: out of scope for P1 — route to the existing return workflow.*

3. **Double shipping** — see §3.3. Verify whether trips are still created in production.

4. **Anomaly precision.** Money-grade (fuel allowances, disciplinary action) needs a
   tighter ping interval and stricter accuracy filtering, costing battery. Directional-
   only is much cheaper. This sets the `distanceFilter` and is hard to retrofit.

---

## 7. Plumbing this creates

- `required_apps = ["jarz_pos"]` in `jarz_courier/hooks.py`; `jarz_pos` must install first.
- **`deploy_backend.ps1` has NO `bench install-app` anywhere** — only `backup`, `migrate`,
  `clear-cache`. `bench migrate` will not create a new app's DocTypes. Worse,
  `Resolve-GitTarget` hard-throws on a missing clone, so naming `jarz_courier` in
  `$deployedApps` before an idempotent bootstrap lands **breaks every backend deploy,
  including POS hotfixes**. Precedent: `jarz_observability` sits in the bench, in zero deploy
  scripts, absent from `apps.txt`, and silently never deploys.
- Also converge `server-config/deploy_remote.ps1` (its staging/prod app lists have already
  diverged) and `verify_deployment.sh` (`CUSTOM_APPS` is hardcoded to two apps, so it reports
  green without ever checking a third).
- **Add `jarz_courier` to the backend CI**, or its logic ships untested while `jarz_pos`
  stays green and we feel falsely safe. Note only 38 of 107 `jarz_pos` test files are in the
  `MODULES` array today — assume no coverage unless the module name is listed.
- **Fix the `backend-tests.yml` concurrency group** — it is per-ref, and the workflow writes
  into the live staging `erp_apps` volume, so two feature branches running CI concurrently
  interleave writes. A live hazard that worsens with every parallel lane.
- **The domain-isolation hook does NOT flag `jarz_courier` → `jarz_pos`.**
  `domain-isolation-check.py` returns `None` for any path outside `jarz_pos/` and
  `jarz_woocommerce_integration/`, so `jarz_courier` is simply unguarded. The real gap is the
  **reverse** — nothing stops `jarz_pos` importing `jarz_courier`. Add deny rules for
  `jarz_pos → jarz_courier` and `jarz_woocommerce_integration → jarz_courier`.
- **Put `jarz_pos_mobile/.github/` under version control first.** `ai-sync.ps1`, the hook
  scripts, all skills and prompts — the declared canonical source for `.claude/` and
  `CLAUDE.md` — currently live in no git repo at all.
- Then update those canonical sources and run `ai-sync.ps1` so CLAUDE.md documents the new
  topology and the directional dependency rule.

---

## 8. First three tasks

1. **A4 + B5** — Address geo fields + the Google Maps link parser
2. **D1** — POS paste-and-validate field, so real coordinates start arriving immediately
3. **A1 + A3** — invoice outcome fields + per-invoice courier transition services

Note that (1) and (2) are pure additions with no risk to existing accounting, and they
start the door-pin database — the only part of this project where waiting has a real cost.
