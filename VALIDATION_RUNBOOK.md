# Full-stack validation — runbook

One command answers "is the system correct right now", across the POS money
paths, the role gates, the settings and the WooCommerce round-trip.

```bash
cd C:\ERPNext\jarz_pos_mobile\jarz_pos
python scripts/run_full_stack_validation.py
```

Writes `artifacts/full_stack_validation/<run-id>.json` and `.md`. Exit code is 0
only when every suite passed **and** the fixture sweep left the site clean.

## What it runs

| Suite | Proves |
|---|---|
| `settings` | every settings flag changes the behaviour it names; dead settings pinned as dead |
| `roles` | staff / line manager / manager can do exactly what they should, over real HTTP |
| `b2b` | commercial-policy purposes book correctly; the Standard cycle is unchanged |
| `lifecycle` | create → dispatch → settle → cancel, ledger asserted after every step |
| `operations` | cash transfer, expenses, stock moves, inventory count, shift open/close |
| `returns` | post-dispatch returns book correctly and never void the source invoice |
| `money_paths` | payment-method change after dispatch, amendment, address/territory change, InstaPay confirm, batch vs single settlement, cancel guards |
| `woo` | WooCommerce inbound and outbound, operationally and in the ledger |

## Options

```bash
# one or more suites
python scripts/run_full_stack_validation.py --only roles,money_paths

# exercise the real WooCommerce store (see the warning below)
python scripts/run_full_stack_validation.py --woo-mutations

# keep everything it created, for inspection. SKIPS THE SWEEP — purge by hand after.
python scripts/run_full_stack_validation.py --keep-fixtures

# print the commands without running them
python scripts/run_full_stack_validation.py --dry-run
```

Run it directly on the server instead, if you prefer:

```bash
bench --site frontend execute jarz_pos.scripts.full_stack_validation.run
```

## Reading the result

Three states, and the third is the one people get wrong.

* **passed** — the assertion ran and held.
* **failed** — the assertion ran and did not hold.
* **skipped** — the assertion **did not run**, because this site could not
  satisfy its precondition (no second branch, no courier bound, no stock, the
  feature switched off). A skip is *not* a pass. Read the Skipped table before
  trusting a total; a site with no courier can skip every check that would have
  touched Courier Outstanding and still show a green failure count.

The report also carries an **environment fingerprint** — site, app versions, and
every settings value read straight from `tabSingles` so "never written" is
distinguishable from "set to 0". A green report from a site with the feature
under test switched off is worse than no report, which is why the settings are
recorded rather than assumed. Secrets are masked.

## Safety

* **Refuses production, and fails closed.** It does not ask "is this
  production?" and proceed on no — it asks "is this a site I was told to write
  to?", so an unrecognised environment is a stop rather than a green light.
* **Fixtures are prefixed** (`_B2BVALID_`, `_RETVALID_`, `_RETMONEY_`,
  `_ROLETEST_`) and swept afterwards. The sweep is *verified*: leftover fixtures
  count as a failure, because residue is what makes a suite stop being
  repeatable.
* **`--woo-mutations` is off by default and deliberately not tied to
  `--keep-fixtures`.** Staging's WooCommerce store shares an id space with the
  cloned production data, so a fixture can land on an id that already belongs to
  a real record. The harness burns ids past the mapped ceiling for both orders
  *and* customers, and refuses outright if a candidate id is bound to a customer
  with invoices. Turning it on is a decision someone makes on purpose.
* **Nothing runs against production.** Production is verified read-only.

## If a run leaves residue

`--keep-fixtures` skips the sweep by design. Purge by hand:

```bash
bench --site frontend execute jarz_pos.scripts.purge_test_fixtures.run \
  --kwargs '{"dry_run": False}'
```

A Custom Shipping Request will block its invoice — cancel the request first.
The purge deliberately does **not** force-delete shared lookups (Territory,
Customer Group): Frappe's own link check decides whether anything still uses
them, so a "kept: still referenced" line is the sweep working, not failing.

## Before you run it

CI and the deploy share the staging app volume. A run started while CI is
mid-flight reads code shifting underneath it, and the deploy script will refuse
with an integrity error. That refusal is correct — **wait for CI, do not repair
the volume.**

## Adding a suite

Append to `SUITES` in `jarz_pos/scripts/full_stack_validation.py`. The entry
point must accept `cleanup` and return
`{"passed", "failed", "skipped", "checks"}`. Unknown kwargs are checked against
the real signature, so a renamed parameter fails loudly rather than silently
leaving a suite in a mode nobody asked for.

**The one rule.** Never record a skip as a pass. Every harness here once did,
and a site that could not exercise a single money path reported all-green
having proven nothing. If a precondition is missing, call `ctx.skip(name,
reason)` — an assertion that did not run is neither proof nor a defect.
