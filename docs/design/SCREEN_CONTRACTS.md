# SpendWise Fintech — Screen Contracts

**Status:** Foundation. The **template** is a LOCKED UX REQUIREMENT. Individual
contracts are unwritten.
**Decision source:** the approved D0–D6 decision series.
**Implementation state:** None. M0 has not begun.
**Authority:** Below `UX_RULES.md` and `SPENDWISE_UI_UX_SPEC.md`.

---

## 1. What a screen contract is

A screen contract is the **definition of done for one screen**: what it is for,
where its data comes from, every state it can be in, what it must never do, and
what is still unanswered.

**Rules — LOCKED:**

1. **No screen is built without a completed contract.**
2. Every state row in §2 is answered explicitly. **"N/A" is a valid answer; blank
   is not.**
3. The contract records **CURRENT vs TARGET** and its
   **KEEP / ADAPT / REPLACE / REMOVE / ADD** disposition. **REPLACE requires
   concrete justification.**
4. A **KEEP** screen gets a short contract confirming its behaviour is unchanged —
   **not** a redesign brief.
5. Anything provider-, compliance-, or investor-dependent is written **DEFERRED**,
   never filled with a plausible placeholder. **No KYC limits, no provider names,
   no SLAs, no production thresholds.**
6. A contract states **whether the screen is a root destination or a focused
   flow**, because that determines whether root bottom navigation appears.
7. A contract describes **what must be true**, not how it is stored. Persistence
   shape, token names, and component names are implementation decisions and are
   **OPEN** unless separately approved.
8. The contract governs the mockup, never the reverse.

---

## 2. The screen-contract template — LOCKED

Copy verbatim for each screen. Do not remove sections.

```markdown
## <Screen name>

**Route:** <expo-router path, or "TARGET — not yet routed">
**Placement:** Root destination (Home | Activity | Plan | You) OR Focused flow
**Root bottom navigation:** Shown (root destinations only) | Not shown (focused flow)
**Status:** CURRENT | TARGET
**Disposition:** KEEP | ADAPT | REPLACE | REMOVE | ADD
**Justification (REPLACE only):** <concrete, named limitation>
**Predecessor:** <existing route this evolves from, or "none">

### Purpose
One sentence. If it takes two, the screen is doing two jobs.

### Entry points
Where the customer arrives from, and in what state.

### Exits
Every way out (including back/close for focused flows), and what each does to
unsaved input.

### Record types shown
Manual expense | Financial transaction | Both | Neither
If "Both": how they stay clearly distinguishable (label and form, not colour
alone).
If a financial transaction appears: what is IMMUTABLE FINANCIAL FACT and what is
EDITABLE SPENDWISE INTERPRETATION.

### Data contract
- Authoritative source: <backend endpoint / ledger / customer input>
- Queries: <TanStack Query keys>
- Authoritative fields: <fields the backend owns; never client-calculated —
  balances, fees, totals, limits, capabilities, verification status>
- Freshness policy: <how long a value stays fresh; how cached data is marked>
- Refetch triggers: <focus, pull-to-refresh, interval, push>

### Money on this screen
- Amounts shown and their currency (banking V1 is NGN; tracker may be
  multi-currency)
- Confirmation that no authoritative amount, fee, or total is client-calculated
- Confirmation that unlike currencies are never aggregated
- Balance terminology used: Total balance | Available balance | Being confirmed
  (no invented terms)
- Where combined manual + financial spending figures appear, how the semantics
  are made explicit

### States
Every row required. "N/A" is an answer; blank is not.

| State | Treatment |
|---|---|
| Loading | |
| Empty | |
| Offline | |
| Stale / cached (must not appear current) | |
| Degraded service | |
| Session expired | |
| Restricted capability (canSend / canReceive / …) | |
| Security cooldown | |
| Verification state (processing / manual review / mismatch / unable to verify / provider unavailable) | |
| Transaction Confirming | |
| Unknown financial value (never a fake ₦0) | |
| Error — recoverable | |
| Error — unrecoverable | |

### Transfer states shown
If transfer states appear:
- Which appear here (normal: Processing → Successful; ambiguous: Processing →
  Confirming → Successful OR Failed)
- Confirmation that Pending / Awaiting Confirmation is NOT routinely shown
- Confirmation that backend UNKNOWN is never surfaced
- Confirmation that Confirming has no Retry, no fabricated provider stages, does
  not resubmit, and survives backgrounding
- Confirmation that timeout is not treated as failure
- Confirmation that failure is shown only when authoritative

### Actions
| Action | Precondition | Confirmation | Idempotency key? | Duplicate-tap guard? | Unavailable offline? |
|---|---|---|---|---|---|

Money-moving actions follow Recipient → Amount → Review → PIN. Review shows
authoritative recipient name, bank, masked account, amount, fee, total.
Insufficient balance and known limit violations are caught at Amount where
authoritative information is available.
No cancellation semantics unless the backend and product flow explicitly support
them.

### Accessibility
- Touch targets
- Screen reader semantics and semantic grouping
- Dynamic text where practical
- Contrast; no colour-only statuses
- Reduced motion
- Logical focus; keyboard handling (preserve existing good behaviour)
- Accessible monetary announcements

### Copy
Key strings, especially state and error copy. Errors say what happened, whether
money moved, and what to do next. No invented SLAs, support numbers, or security
claims.

### Non-goals
What this screen deliberately does not do.

### Deferred / Not in V1
Provider-, compliance-, or investor-dependent items (DEFERRED). Out-of-current-
scope capability recorded as "not in V1", never as a permanent exclusion.

### Open questions
Numbered, with what each blocks.
```

---

## 3. Contract register

**None are written yet.** Dispositions follow `SPENDWISE_UI_UX_SPEC.md` §2.1.

### 3.1 Root destinations

| Screen | Tab | Disposition | Predecessor |
|---|---|---|---|
| Home | Home | **ADAPT** | `(tabs)/index` |
| Activity | Activity | **ADAPT** | `(tabs)/expenses/index` |
| Plan | Plan | **ADD** (shell over KEEP content) | — |
| You | You | **ADAPT** | `(tabs)/profile/index` |

### 3.2 Focused money flows — ADD

All are focused flows: **no root bottom navigation**.

| Screen | Notes |
|---|---|
| Send — recipient | |
| Send — amount | Catches insufficient balance and known limit violations pre-PIN |
| Send — review | Authoritative recipient, bank, masked account, amount, fee, total |
| Send — PIN | Duplicate-tap guard; idempotency |
| Send — processing / result | Processing normal and brief |
| Send — confirming | No Retry; no fabricated stages; survives backgrounding; no resubmit |
| Receive | Account name, account number, bank/provider name, Copy, Share. No QR, no Request Money in V1 |
| Transaction detail (financial) | Immutable facts vs editable interpretation |
| Financial account activation | Pre-account customers see an activation path |

### 3.3 Focused security, verification, and support flows

| Screen | Disposition | Notes |
|---|---|---|
| Login (phone-required) | **ADAPT** | Existing auth preserved where compatible |
| Register / onboarding (phone + email) | **ADAPT** | |
| OTP / device verification | **ADD** | |
| Transaction PIN — set / change / recover | **ADD** | Triggers cooldown |
| Security cooldown notice | **ADD** | Capability-driven; do not assume receiving is disabled |
| Verification / KYC | **ADD** | Tiers 0–3; requirements and limits **DEFERRED** |
| Support / dispute case | **ADD** | Transaction-contextual; case, status, timeline, related transaction |

### 3.4 Existing screens being rehomed — KEEP or ADAPT

Contracts here are **confirmations**, not redesign briefs.

| Screen | Current route | Disposition | Destination |
|---|---|---|---|
| Expense detail | `(tabs)/expenses/[id]` | KEEP | Activity |
| Expense create / edit | `(tabs)/expenses/new`, `edit/[id]` | KEEP | Focused flow from Activity/Home |
| Budget list / detail | `(tabs)/budgets/*` | KEEP | Plan |
| Budget create / edit | `(tabs)/budgets/new`, `edit/[id]` | KEEP | Focused flow |
| Recurring (dashboard, list, detail, create, edit) | `(tabs)/profile/recurring/*` | KEEP / ADAPT | Plan — planning language only |
| Analytics overview, trends, categories | `(tabs)/analytics/*` | KEEP | Plan / Spending |
| Analytics — anomalies | `(tabs)/analytics/anomalies` | KEEP | Feeds contextual insights |
| Analytics — health | `(tabs)/analytics/health` | **REMOVE numeric score from customer UX** | Qualitative experience replaces it; legacy stored fields retained where migration needs them |
| Reports (index, new, history, detail, scheduled) | `(tabs)/profile/reports/*` | KEEP, de-emphasised | Secondary access; placement **OPEN** |
| Categories, incl. curated colours | `(tabs)/profile/categories/*` | KEEP | You |
| Profile / settings / preferences / edit profile / change password / about | `(tabs)/profile/*` | KEEP / ADAPT | You |
| Groups (list, detail, members, balances, expenses, new, join) | `groups/*` | KEEP | Focused stack; Home → More → Groups |

### 3.5 Shell

| Item | Current | Disposition |
|---|---|---|
| Root tab bar | `components/app-tabs.tsx` (NativeTabs, 5 triggers) | **ADAPT** → four root destinations, subtle glass. Replacement only on a concrete documented limitation (**OPEN**). |

---

## 4. Worked example — format demonstration only

Illustrates the template's shape. **The screen is TARGET and not authorised for
implementation.** Deferred content stays deferred.

```markdown
## Send — confirming

**Route:** TARGET — not yet routed
**Placement:** Focused flow
**Root bottom navigation:** Not shown
**Status:** TARGET
**Disposition:** ADD
**Predecessor:** none

### Purpose
Tell the customer truthfully that SpendWise is confirming their transfer with the
receiving bank, until the backend resolves it.

### Entry points
From Send — processing when the backend outcome is UNKNOWN. Re-entered from Home
in-flight context, Activity, or a push notification after app close.

### Exits
Close returns to Home or Activity. The transfer continues regardless; leaving the
screen changes nothing about it.

### Record types shown
Financial transaction only. No editing affordance.

### Data contract
- Authoritative source: backend transfer state. UNKNOWN is a backend state and is
  never surfaced; this screen is its customer-facing representation.
- Authoritative fields: amount, fee, total, recipient details, state — none
  client-calculated.
- Freshness policy: resolves from authoritative backend state; the client never
  infers resolution from elapsed time.
- Refetch triggers: focus, push, backend-driven update.

### Money on this screen
Amount, fee, total, all authoritative. Currency NGN (banking V1). Reserved funds
may be described as "Being confirmed". No invented balance terminology.

### States
| State | Treatment |
|---|---|
| Loading | Skeleton of the confirming layout |
| Empty | N/A — always has a subject |
| Offline | Last-known state, marked cached, with timestamp; no actions |
| Stale / cached | Marked cached; never presented as current |
| Degraded service | Distinct treatment — NOT presented as Confirming |
| Session expired | Route to auth, return here after |
| Restricted capability | N/A — the transfer is already submitted |
| Security cooldown | N/A for this screen |
| Verification state | N/A |
| Transaction Confirming | The screen's primary state |
| Unknown financial value | Explicit unknown treatment; never a fake ₦0 |
| Error — recoverable | Fetching state failed: show last-known, retry the fetch
  (never the transfer) |
| Error — unrecoverable | Explain; exit to Activity where the transfer remains |

### Transfer states shown
Confirming only, resolving to Successful or Failed.
- Pending / Awaiting Confirmation is NOT shown.
- Backend UNKNOWN is never surfaced.
- No Retry action. No fabricated deterministic provider stages. Does not
  resubmit. Survives app close and backgrounding.
- Timeout is not treated as failure. Failure appears only when authoritative.

### Actions
| Action | Precondition | Confirmation | Idempotency key? | Duplicate-tap guard? | Unavailable offline? |
|---|---|---|---|---|---|
| Close | always | none | n/a | n/a | no |
| View transaction | always | none | n/a | n/a | no |

No Retry. No cancellation.

### Accessibility
State announced, not only animated. No colour-only status. Full accessible
monetary announcement. Adequate targets and contrast in light and dark. Reduced
motion respected — no animation implying imminent success.

### Copy
Explains that SpendWise is confirming with the receiving bank or provider. No
promised timing unless the backend supplies one. No fabricated stages.

### Non-goals
No retry. No cancellation. No resubmission. No provider status vocabulary.

### Deferred / Not in V1
Settlement-time expectations (DEFERRED — backend/provider). Provider attribution
(DEFERRED).

### Open questions
1. Push-notification copy on resolution. Blocks: M7.
2. Whether Home surfaces an in-flight confirming affordance. Blocks: M5.
```

---

## 5. Review checklist

- [ ] Purpose is one sentence
- [ ] CURRENT vs TARGET stated
- [ ] Disposition stated; REPLACE carries concrete justification
- [ ] Root destination vs focused flow stated; bottom-nav presence correct
- [ ] Record types stated; manual expenses clearly distinguishable
- [ ] Immutable financial facts separated from editable SpendWise interpretation
- [ ] Every state row answered — no blanks
- [ ] Unknown treatment defined and is never a fake ₦0
- [ ] Cached data marked; never presented as current
- [ ] Degraded service distinct from transaction Confirming
- [ ] Offline money operations unavailable and never queued
- [ ] Transfer states match the locked normal and ambiguous flows
- [ ] Pending / Awaiting Confirmation not routinely shown
- [ ] Confirming: no Retry, no fabricated stages, no resubmit, survives backgrounding
- [ ] Timeout not treated as failure; failure only when authoritative
- [ ] Insufficient balance / limit violations caught at Amount where possible
- [ ] Review shows authoritative recipient, bank, masked account, amount, fee, total
- [ ] No client-calculated authoritative fees, balances, or totals
- [ ] Idempotency and duplicate-tap guards on money-moving actions
- [ ] No cancellation semantics
- [ ] Balance terminology limited to Total / Available / Being confirmed
- [ ] Unlike currencies never aggregated
- [ ] Combined spending figures have explicit semantics
- [ ] No hardcoded limits, tiers, provider names, SLAs, or security claims
- [ ] Capability-driven restriction copy; no generic "account frozen"
- [ ] Accessibility section complete for light and dark
- [ ] Deferred marked deferred; not-in-V1 marked as scope, not prohibition
- [ ] Non-goals stated
- [ ] Open questions numbered with what they block
