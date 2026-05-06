# POS Shift Close Staging Issue Plan

## Issue

Staging has two close-shift failure surfaces that need to be treated separately:

1. Desk `POS Closing Entry` can end up in a `document has been modified after you have opened it` conflict after save.
2. Mobile app shift close is failing too, but it does not use the desk form flow, so it is likely surfacing a backend close-process failure instead of the same form-state conflict.

## Confirmed Staging Findings

### 1. Desk and mobile do not use the same client flow

Desk flow:

- ERPNext POS page creates a draft `POS Closing Entry` and routes the user to the form.
- The draft is seeded with:
  - `period_end_date = now`
  - `posting_date = today`
  - `posting_time = now`

Code surfaces:

- `erpnext/public/dist/js/point-of-sale.bundle.*.js`
- `erpnext/accounts/doctype/pos_closing_entry/pos_closing_entry.js`

Mobile flow:

- The app does not save a `POS Closing Entry` form.
- It calls `jarz_pos.api.shift.end_shift` directly and only displays the backend result or exception.

Code surfaces:

- `jarz_pos/api/shift.py`
- `jarz_pos_mobile/jarz_pos/lib/src/features/shift/data/shift_repository.dart`
- `jarz_pos_mobile/jarz_pos/lib/src/features/shift/state/shift_notifier.dart`

### 2. The latest staging desk close attempt was mutated repeatedly before failing

Affected document:

- `POS-CLO-2026-00001`

Observed on staging:

- `docstatus = 0`
- `status = Failed`
- `pos_opening_entry = POS-OPE-2025-00001`
- `posting_date = 2026-05-02`
- `posting_time = 18:28:28.554737`
- `period_end_date = 2026-05-02 18:28:22`
- final `modified = 2026-05-02 18:28:31.043076`

Version history for that same draft shows the user repeatedly saving it, and each save changed:

- `period_end_date`
- `posting_time`

This confirms the draft timestamps were drifting between saves.

### 3. The close process itself failed for a backend inventory reason

The same staging `POS Closing Entry` now carries this saved backend error:

- `4.0 units of Item Blueberry Medium needed in Warehouse Finished Goods - J ... for Stock Entry MAT-STE-jarz-2025-00064 to complete this transaction.`

That means the document did not just hit a form timestamp issue. The actual close process also failed during downstream transaction handling.

### 4. The problematic opening entry is stale data

Affected staging opening entry:

- `POS-OPE-2025-00001`

Current state on staging:

- `status = Open`
- `docstatus = 1`
- `period_start_date = 2025-07-04 04:52:04.859183`

This is an old shift that remained open into May 2026. That stale opening entry likely increases the chance of strange close behavior and should be treated as part of the investigation, not background noise.

### 5. I did not find evidence that mobile close is hitting the same timestamp mismatch

What is confirmed:

- Mobile does not use the desk form or form reload cycle.
- The mobile close endpoint is a direct backend call.
- Staging error logs showed unrelated `TimestampMismatchError` rows for `register_mobile_device`, but not for `jarz_pos.api.shift.end_shift`.

Current conclusion:

- The mobile close error is probably not the same desk `document modified` problem.
- The more likely mobile failure is the backend close-process failure being surfaced to the app as a raw exception.

## Most Likely Root Cause Split

### Desk root cause

The draft `POS Closing Entry` behaves like a moving target:

1. the desk flow seeds current timestamps into the draft
2. the same draft keeps getting newer timestamp values across save attempts
3. the backend then mutates the same document again when close processing fails and writes `status = Failed` plus `error_message`

That combination is enough to produce a stale-form conflict in desk, even though the user experiences it as a date/time-seconds problem.

### Mobile root cause

Mobile likely fails on the backend close logic itself, not on optimistic locking of a loaded draft form. The latest staging evidence points at inventory or stock-posting failure during close submission.

## Files Involved

Backend:

- `apps/erpnext/erpnext/accounts/doctype/pos_closing_entry/pos_closing_entry.js`
- `apps/erpnext/erpnext/accounts/doctype/pos_closing_entry/pos_closing_entry.py`
- `apps/jarz_pos/jarz_pos/api/shift.py`

Mobile app:

- `jarz_pos_mobile/jarz_pos/lib/src/features/shift/data/shift_repository.dart`
- `jarz_pos_mobile/jarz_pos/lib/src/features/shift/state/shift_notifier.dart`
- `jarz_pos_mobile/jarz_pos/lib/src/features/shift/presentation/shift_end_screen.dart`

## Investigation Plan

### Phase 1: Reproduce both paths on staging with a controlled shift

1. Open a fresh staging shift created specifically for this investigation.
2. Create a minimal valid sales flow under that shift.
3. Attempt close from desk.
4. Attempt close from mobile against a separate fresh test shift.
5. Capture exact user-visible error text for both paths.

Goal:

- confirm whether the two failures are truly separate in live use
- avoid relying on the stale 2025 opening entry as the only reproduction case

### Phase 2: Fix the desk timestamp drift problem

1. Audit why draft `period_end_date` is being refreshed repeatedly.
2. Confirm whether `posting_time` is also being rewritten by client behavior on every save.
3. Stop changing draft close timestamps after initial creation unless the user explicitly edits them.
4. Ensure the desk form does not remain stale when close processing updates the same document to `Failed`.

Candidate fix areas:

- do not reset `period_end_date` on every draft form load
- avoid rewriting `posting_time` for an already-created draft
- force a reload or switch the user into a read-only failed state after close processing updates the document

### Phase 3: Fix the backend close-process failure

1. Trace exactly why closing this staging shift requires `MAT-STE-jarz-2025-00064` to succeed.
2. Determine whether close-shift should be blocked earlier with a clearer validation message.
3. Decide whether the underlying issue is:
   - stale invoices included in the close set
   - stock posting tied to consolidation
   - an invalid or incomplete material transfer dependency
4. Make the close failure actionable instead of surfacing only after the draft is already being mutated.

### Phase 4: Verify mobile error handling

1. Confirm the mobile app is receiving the backend close error verbatim.
2. If the backend returns a raw traceback or generic response, normalize it into a clean message.
3. Ensure mobile distinguishes business validation failure from transport or auth failure.

## Validation Plan

### Desk validation

1. Create a new staging shift.
2. Save the closing draft multiple times without changing business inputs.
3. Verify `period_end_date` and `posting_time` do not keep drifting unexpectedly.
4. Submit the close.
5. If backend close fails, verify the user gets a stable failed document state instead of a stale-form conflict.

### Mobile validation

1. Create a new staging shift.
2. Close it through the mobile app.
3. Verify the exact backend success or failure message displayed to the user.
4. Confirm the mobile path does not produce desk-style timestamp mismatch behavior.

### Data validation

1. Review old open shifts on staging, especially `POS-OPE-2025-00001`.
2. Either close, clean, or isolate stale historical test data so it does not keep contaminating close-shift testing.

## Acceptance Criteria

- Desk close no longer triggers `document has been modified after you have opened it` for normal close attempts.
- Draft close timestamps remain stable unless intentionally edited.
- Mobile close either succeeds or returns a clear backend business error.
- Close-process failures are diagnosable from the returned message without needing DB inspection.
- Stale historical shifts are no longer the default reproduction environment for this issue.

## Current Working Conclusion

This does not look like one single staging bug.

- Desk issue: form-state and document mutation timing problem around a draft `POS Closing Entry` whose timestamps keep changing and then gets mutated again when close processing fails.
- Mobile issue: likely backend close failure, with current staging evidence pointing to stock or material-transfer validation during close processing.