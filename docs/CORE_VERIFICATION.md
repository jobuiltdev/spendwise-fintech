# SpendWise Fintech Core Verification

## Purpose

This guide explains how to prove that the locked money core still behaves
correctly, end to end.

The money core is not one component. It is a financial account and wallet, a
double-entry ledger, funds holds, a transaction lifecycle, transfers, provider
execution against an external rail, status recovery when that rail does not
answer, and reconciliation against the rail's own records. Each of those has its
own test suite proving its own rules in isolation.

That is not sufficient. A system can satisfy every component boundary and still
be wrong where the parts meet — money reserved but never released, an outcome
posted twice, a timeout quietly read as a failure. The six scenarios described
here follow one customer's money all the way through and assert what actually
happened to the balance.

This is engineering documentation. It describes how to run and interpret
verification that already exists; it defines no behaviour of its own.

---

## What this verifies

One chain, in order:

```
customer / financial account / wallet
    └── ledger funding                      (real double-entry posting)
        └── transfer preparation            (intent + transaction + hold)
            └── funds hold                  (reservation, not a movement)
                └── provider execution      (one submission to the rail)
                    ├── success             → posted, hold released
                    ├── known failure       → nothing posted, hold released
                    └── UNKNOWN             → nothing posted, hold stands
                        └── status recovery (ask the rail what happened)
                            └── reconciliation (compare with the rail's records)
```

Every step is exercised through its real public service. Nothing in the
verification writes a status, a hold, or a journal directly.

---

## Core invariants

These are the properties the six scenarios exist to protect.

**Wallet truth is derived, never stored.** A wallet's figures come from the
ledger and its holds. There is no balance column anywhere; nothing can drift out
of step with the rows it came from.

**A successful transfer posts exactly once.** One accepted transfer produces one
success journal, one released reservation, and one provider submission.

**A known failure posts nothing.** A rail that definitively refuses leaves the
ledger exactly where it was, and returns the reservation to the customer as
spendable funds.

**UNKNOWN keeps funds reserved.** A submission whose answer never arrived may
already have moved money. The reservation stands so the customer cannot spend it
twice.

**UNKNOWN never causes resubmission.** There is no blind retry anywhere in the
core. Ambiguity is resolved by asking, never by sending again.

**Recovery discovers the outcome; it does not decide it.** A later, independent
status lookup provides evidence. That evidence transitions the transaction.

**Reconciliation observes; it never repairs.** Comparing our records with a
provider's export produces findings. It changes no financial row, in any
outcome.

For the reasoning behind each of these, see
[`architecture/SPENDWISE_FINTECH_ARCHITECTURE.md`](architecture/SPENDWISE_FINTECH_ARCHITECTURE.md).
This guide does not restate it.

---

## Prerequisites

- The repository `spendwise-fintech`, on branch `fintech-foundation`.
- The backend Python environment, installed as described in
  [`TESTING.md`](TESTING.md) → *Backend · Setup*.
- For PostgreSQL runs, the database container from `TESTING.md` →
  *Running against PostgreSQL*.

No `.env` file is needed to run tests; `pytest.ini` supplies defaults. See
`TESTING.md` for the full explanation — the canonical setup commands live there
and are not duplicated here.

---

## Quick verification

From `backend/`, on SQLite:

```bash
venv/Scripts/python -m pytest moneycore/tests/test_core_scenarios.py
```

On PostgreSQL (same form as `TESTING.md`, narrowed to this file):

```bash
DATABASE_URL=postgres://spendwise:spendwise@localhost:55433/spendwise \
  venv/Scripts/python -m pytest moneycore/tests/test_core_scenarios.py
```

At the current commit both report:

```
16 passed
```

The count will change as verification grows. What must not change is that this
file is green on both engines.

---

## Understanding the balance numbers

Three figures, one relationship:

| Figure | Meaning |
|---|---|
| **posted** | What the ledger says the wallet holds. Sum of posted journal entries. |
| **held** | What is reserved by effectively-active holds and therefore not spendable. |
| **available** | `posted − held`. What the customer can actually commit right now. |

```
available = posted − effective active holds
```

All three are derived at read time. There is no persisted `wallet.balance`, and
nothing in the core assigns one.

Through a 7 000 transfer from an opening 10 000:

| Stage | posted | held | available |
|---|---|---|---|
| opening | 10 000 | 0 | 10 000 |
| prepared | 10 000 | 7 000 | 3 000 |
| succeeded | 3 000 | 0 | 3 000 |
| failed | 10 000 | 0 | 10 000 |

Note what preparation does **not** do: `posted` is unchanged, because reserving
funds is not moving them. The money leaves the ledger only when the rail
confirms it left.

A hold past its expiry stops reserving funds immediately, whether or not
anything has written `EXPIRED` — so `held` is correct without a cleanup job.

---

## How to interpret UNKNOWN

**UNKNOWN does not mean failed.** It means SpendWise sent a request and cannot
yet prove what the provider did with it. The money may already have gone.

Reading that as a failure is the most expensive mistake available: it releases
the reservation, letting the customer spend funds that have left the account.
Reading it as a reason to retry is the second: it sends the payment twice.

So while a transaction is UNKNOWN:

- **No retry.** The core never resubmits, on any path.
- **Funds stay reserved.** The hold remains active.
- **No success journal.** Nothing is posted, because nothing is proven.
- **Recovery queries independent evidence.** A status lookup asks the rail what
  became of the request already made. It sends nothing and moves nothing, which
  is why it is safe to repeat.
- **Definitive evidence transitions the transaction.** Only when the rail states
  an outcome does the transaction become SUCCEEDED or FAILED.

UNKNOWN is terminal for an *attempt* — that interaction is over and told us
nothing conclusive — but not for a *transaction*, which is still waiting for the
truth.

---

## How to interpret reconciliation

Reconciliation compares provider records against internal state over a time
window. Each comparison produces one item with an overall status:

| Status | Meaning |
|---|---|
| `matched` | The provider's record agrees with internal state. |
| `discrepancy` | They disagree. One or more mismatch flags say how. |
| `provider_only` | A provider record with no matching internal transfer. |
| `internal_only` | An internal transfer with no provider record in the window. |

**All four are findings.** None of them triggers financial repair, automatically
or otherwise.

`internal_only` in particular is not a failure: an export delay, a window edge,
and "it never arrived" are indistinguishable from a reconciliation run's
position, and concluding failure on that guess would release a customer's
reservation. `provider_only` creates nothing — no transaction, no transfer, and
no attachment to whichever internal transfer looks closest.

Acting on a finding is a human decision, taken with the evidence in front of
them.

---

## The six scenarios

Each section below states the flow, the expected state at each point, the
invariant it protects, and how to run it alone.

---

### Scenario 1 — Successful transfer

**Purpose.** Prove that one accepted transfer produces exactly one financial
success posting, and that the reservation ends because the money moved.

**Starting state.** posted 10 000 · held 0 · available 10 000.

**Action sequence.**

1. `prepare_transfer(wallet, destination, 7_000, idempotency_key=...)`
2. `execute_transfer(transfer, provider=<simulator SUCCESS>, counterpart_account=...)`

**Intermediate state**, after preparation:

posted 10 000 · held 7 000 · available 3 000 · hold `ACTIVE` · no new journal.

**Final state.**

| | |
|---|---|
| transaction | `SUCCEEDED` |
| provider attempts | 1, status `SUCCEEDED` |
| provider submissions | 1 |
| hold | released, exactly once |
| success journals | exactly 1 |
| wallet ledger effect | −7 000 |
| posted / held / available | 3 000 · 0 · 3 000 |

**Invariant.** One accepted transfer produces one financial success posting.

**Automated test.**
`test_successful_transfer_posts_once_and_releases_reserved_funds`

```bash
venv/Scripts/python -m pytest \
  moneycore/tests/test_core_scenarios.py::TestSuccessfulTransfer::test_successful_transfer_posts_once_and_releases_reserved_funds
```

**If it fails.** Suspect a regression in exact-once posting or in hold release —
either a second journal for one transfer, or a reservation left standing after
settlement. Investigate before proceeding.

---

### Scenario 2 — Known provider failure

**Purpose.** Prove that a definite refusal restores spendable funds without
inventing a success.

**Starting state.** posted 10 000 · held 0 · available 10 000.

**Action sequence.**

1. `prepare_transfer(...)` for 7 000
2. `execute_transfer(transfer, provider=<simulator KNOWN_FAILURE>, ...)`

**Intermediate state**, after preparation: posted 10 000 · held 7 000 ·
available 3 000.

**Final state.**

| | |
|---|---|
| transaction | `FAILED` |
| provider attempts | 1, status `FAILED` |
| provider submissions | **1** |
| hold | released |
| success journals | 0 — journal count unchanged from funding |
| wallet ledger effect | unchanged |
| posted / held / available | 10 000 · 0 · 10 000 |

**On the submission count.** `KNOWN_FAILURE` is a rejection of a request that
*was* sent, so one submission is the correct count for this path. It is
deliberately distinct from `UNREACHABLE_BEFORE_SUBMISSION`, where the adapter
can prove nothing left. Both are definitive failures; only the second means
nothing was transmitted.

**Invariant.** Known failure restores spendability without inventing success.

**Automated test.**
`test_known_failure_restores_available_funds_without_posting`

```bash
venv/Scripts/python -m pytest \
  moneycore/tests/test_core_scenarios.py::TestKnownProviderFailure::test_known_failure_restores_available_funds_without_posting
```

**If it fails.** A failure path may be stranding customer funds behind a hold
that is never released, or posting a journal for a payment that did not happen.
Investigate before proceeding.

---

### Scenario 3 — Ambiguous transfer, recovered as success

This is the scenario the architecture is shaped around, and the most detailed
section here.

**Purpose.** Prove that a submission whose answer never came back is resolved by
*asking*, and never by sending the payment again.

**Starting state.** posted 10 000 · held 0 · available 10 000.

**Action sequence.**

1. `prepare_transfer(...)` for 7 000
2. `execute_transfer(transfer, provider=<simulator AMBIGUOUS_AFTER_SUBMISSION>, ...)`
3. `recover_provider_attempt(attempt, provider=<simulator status SUCCESS>, ...)`

**Intermediate state**, immediately after the ambiguous execution:

| | |
|---|---|
| transaction | `UNKNOWN` |
| provider attempt | 1, status `UNKNOWN` |
| provider submissions | **1** |
| posted / held / available | 10 000 · 7 000 · 3 000 |
| hold | `ACTIVE` — still reserving |
| success journals | 0 |

Both halves of that matter. The standing reservation stops a double-spend on
money that may already have gone. The absent journal stops us claiming a payment
we cannot prove.

**Final state**, after recovery returns a definitive success:

| | |
|---|---|
| transaction | `SUCCEEDED` |
| provider attempt | **still `UNKNOWN`** |
| recovery evidence | 1 row, outcome `SUCCEEDED` |
| hold | released, exactly once |
| success journals | exactly 1 |
| posted / held / available | 3 000 · 0 · 3 000 |
| provider submissions | **still 1** |
| status lookups | 1 |

**Why the attempt stays UNKNOWN.** The attempt records what was known *at
execution time*: the request went out and no answer came back. That was true
then and is still true now. The recovery evidence row records what was learned
*later*, as a separate observation with its own timestamp.

Overwriting the attempt would destroy the fact that the system once could not
tell — which is precisely the fact an investigator needs when asking how long an
outcome was in doubt, or how often this rail leaves requests unanswered. History
is appended to, never rewritten.

**Invariant.** Ambiguity is resolved by observation, never by repeating the
transfer.

**Automated test.**
`test_ambiguous_transfer_recovers_successfully_without_resubmission`

```bash
venv/Scripts/python -m pytest \
  moneycore/tests/test_core_scenarios.py::TestAmbiguousTransferRecoveringSuccessfully::test_ambiguous_transfer_recovers_successfully_without_resubmission
```

**If it fails.** Check the submission count first. A count above 1 means the
core resubmitted an ambiguous payment, which is a duplicate-payment defect.
Otherwise suspect the ambiguity or recovery invariants: a released hold while
UNKNOWN, a journal posted before evidence arrived, or an attempt rewritten by a
later observation. Investigate before proceeding.

---

### Scenario 4 — Ambiguous transfer, recovered as failure

**Purpose.** Prove the same ambiguity can safely resolve the *other* way, still
without a second submission.

**Starting state.** posted 10 000 · held 0 · available 10 000.

**Action sequence.** As scenario 3, but recovery returns a definitive failure.

**Intermediate state.** Identical to scenario 3: `UNKNOWN`, held 7 000,
available 3 000, no journal, one submission.

**Final state.**

| | |
|---|---|
| transaction | `FAILED` |
| provider attempt | **still `UNKNOWN`** |
| recovery evidence | 1 row, outcome `FAILED` |
| hold | released, exactly once |
| success journals | 0 |
| posted / held / available | 10 000 · 0 · 10 000 |
| provider submissions | **still 1** |

Nothing was ever posted, so nothing needs reversing.

**Invariant.** Ambiguity can resolve to failure without duplicate payment.

**Automated test.**
`test_ambiguous_transfer_can_resolve_failed_without_resubmission`

```bash
venv/Scripts/python -m pytest \
  moneycore/tests/test_core_scenarios.py::TestAmbiguousTransferRecoveringAsFailed::test_ambiguous_transfer_can_resolve_failed_without_resubmission
```

**If it fails.** As scenario 3 — check the submission count first, then the
recovery path. Investigate before proceeding.

---

### Scenario 5 — Recovered success reconciles as matched

**Purpose.** Prove the three subsystems tell one story: an ambiguous submission,
an answer recovered later, and a provider export that confirms it.

**Action sequence.**

1. `prepare_transfer(...)` for 7 000
2. `execute_transfer(...)` with `AMBIGUOUS_AFTER_SUBMISSION` → `UNKNOWN`
3. `recover_provider_attempt(...)` with status success → `SUCCEEDED`
4. `reconcile_transfers(provider=..., window=...)` against a provider record
   with the same identity, amount, currency and outcome

**Expected reconciliation result.**

| | |
|---|---|
| run status | `COMPLETED` |
| items | exactly 1 |
| overall status | `MATCHED` |
| discrepancy flags | none |
| summary `matched` | 1 |
| summary `discrepancies` | 0 |

**Financial state during reconciliation: unchanged.** Transaction status,
journal linkage, hold status, journal count, entry count, all three balances and
the provider attempt count are captured before the run and asserted identical
after it.

**Invariant.** Provider evidence and internal financial truth can be compared
independently after recovery, without the comparison changing either.

**Automated test.** `test_recovered_success_reconciles_as_matched`

```bash
venv/Scripts/python -m pytest \
  moneycore/tests/test_core_scenarios.py::TestRecoveredSuccessReconciles::test_recovered_success_reconciles_as_matched
```

**If it fails.** Recovered financial state and provider evidence are not lining
up — either correlation by reference is broken, or a recovered success is not
being represented the way reconciliation reads it. Investigate before
proceeding.

---

### Scenario 6 — Reconciliation amount mismatch

**Purpose.** Prove that reconciliation surfaces a disagreement in full detail
and changes nothing.

**Setup.** A settled successful transfer of **7 000**, built through the real
services. The provider record uses the same valid identity but reports
**7 100**.

**Expected reconciliation result.**

| | |
|---|---|
| run status | `COMPLETED` |
| overall status | `DISCREPANCY` |
| `amount_mismatch` | `true` |
| discrepancy codes | `('amount_mismatch',)` |
| provider amount | 7 100 |
| internal amount | 7 000 |
| summary `matched` | 0 |
| summary `discrepancies` | 1 |
| summary `amount_mismatches` | 1 |

**Identical before and after the run:**

- transaction status
- journal linkage, journal count, and the full list of journal entries
- hold status
- posted, held, available
- wallet ledger effect
- provider attempt count and status

And explicitly: no reversal journal exists, no correcting transaction was
created, and no second journal was posted.

**Why.** A system that quietly rewrote its own books to agree with someone
else's file would have no books worth keeping — and the file is at least as
likely to be the thing that is wrong. The disagreement is recorded as a finding;
what to do about it is a human decision.

**Invariant.** Reconciliation detects disagreement. It does not repair money.

**Automated test.**
`test_reconciliation_amount_mismatch_does_not_mutate_financial_truth`

```bash
venv/Scripts/python -m pytest \
  moneycore/tests/test_core_scenarios.py::TestReconciliationMismatch::test_reconciliation_amount_mismatch_does_not_mutate_financial_truth
```

**If it fails.** Either reconciliation is mutating financial truth — the
before/after comparison is where that shows — or it is failing to surface a
disagreement it should have flagged. Both are serious. Investigate before
proceeding.

---

## Running one scenario

The canonical form is the full node id — file path, class, test:

```bash
venv/Scripts/python -m pytest \
  moneycore/tests/test_core_scenarios.py::TestSuccessfulTransfer::test_successful_transfer_posts_once_and_releases_reserved_funds
```

`-k` is convenient when you remember part of a name but not its class:

```bash
venv/Scripts/python -m pytest moneycore/tests/test_core_scenarios.py -k ambiguous
```

Prefer the node id when recording a result, since `-k` can match more than you
intended.

---

## Running all six — why the count says 16

`test_core_scenarios.py` currently reports **16 passed**, not 6.

The six named tests below are the business-flow anchors — one per canonical
scenario:

| # | Test |
|---|---|
| 1 | `test_successful_transfer_posts_once_and_releases_reserved_funds` |
| 2 | `test_known_failure_restores_available_funds_without_posting` |
| 3 | `test_ambiguous_transfer_recovers_successfully_without_resubmission` |
| 4 | `test_ambiguous_transfer_can_resolve_failed_without_resubmission` |
| 5 | `test_recovered_success_reconciles_as_matched` |
| 6 | `test_reconciliation_amount_mismatch_does_not_mutate_financial_truth` |

The remaining ten are narrow companions to those narratives:

- four repeat-safety proofs — re-executing a settled or failed transfer is
  refused before reaching the rail, recovery is safe to repeat, and a mismatch
  observed twice does not converge;
- six proofs that the scenarios go *through* the core rather than around it —
  no direct assignment of financial state, no direct row creation, no outcome
  service called behind the provider's back, all four public entry points
  genuinely exercised, no promotional vocabulary in the package, and the opening
  10 000 coming from a real balanced posting.

Run the six alone with:

```bash
venv/Scripts/python -m pytest moneycore/tests/test_core_scenarios.py \
  -k "posts_once or restores_available_funds or recovers_successfully or resolve_failed or reconciles_as_matched or does_not_mutate"
```

---

## What a failure means

| Failing scenario | What it points at |
|---|---|
| 1 | A possible exact-once regression in ledger posting or hold release. |
| 2 | A failure path that may strand customer funds, or post a success that did not happen. |
| 3 or 4 | The ambiguity, resubmission or recovery invariants may be broken. Check the submission count first. |
| 5 | Recovered financial state and provider evidence no longer reconcile. |
| 6 | Reconciliation may be mutating financial truth, or failing to surface a disagreement. |

A failure here means **investigate before proceeding**. This guide deliberately
offers no repair steps: every one of these scenarios guards an invariant where
guessing at a fix is worse than stopping.

---

## Full regression check

The scenario file is a fast confidence check, not a substitute for the suite.
For the full backend run on both engines, see [`TESTING.md`](TESTING.md).

Counts recorded at commit `10ebf03`, for orientation only:

| Run | Result |
|---|---|
| SQLite, full backend suite | 2325 passed, 174 skipped, 1 xfailed |
| PostgreSQL, `moneycore` | 2224 passed |
| PostgreSQL, remainder | 275 passed, 1 xfailed |

**These are the verification counts at that commit, not expected values.** They
will grow. Do not encode them as CI thresholds — a test count is not an
invariant, and pinning one turns every new test into a failure.

---

## Not covered by this guide

This guide verifies the current money core and nothing beyond it. It makes no
claim about:

- authentication or transaction security beyond the core's present assumptions
- customer identity verification or risk assessment
- operational controls, dashboards or manual intervention tooling
- spending intelligence and its integration with the ledger
- migration of existing expense data
- integration with any real payment rail
- production hardening, deployment or certification

Two limits worth stating plainly:

**The provider is a deterministic simulator.** It is provider-neutral, performs
no I/O, sleeps never, and uses no randomness, so a scenario reproduces exactly.
That is what makes it useful for verification — and it is also why passing
proves the *core's* handling is correct, not that any particular rail behaves
this way.

**This is not rail certification.** Nothing here asserts anything about a real
provider's availability, latency, error semantics or service levels. Those are
production questions, and they remain open.
