# SpendWise Fintech — UI/UX Specification

**Status:** Foundation. Authoritative written specification.
**Decision source:** the approved D0–D6 decision series.
**Implementation state:** None. M0 has not begun.
**Authority:** Governs every visual reference. Where a mockup disagrees with this
text, **the mockup is wrong** (`MOCKUP_CORRECTIONS.md`).

---

## 0. Reading rules

| Marker | Meaning |
|---|---|
| **CURRENT** | Exists in this repository today. |
| **TARGET** | Being built. Not yet implemented. |
| **LOCKED** | Closed decision. |
| **DEFERRED** | Awaiting investor, provider, or compliance input. |
| **OPEN** | An implementation decision not owned by this document. |
| **IMPLEMENTATION PROPOSAL** | Suggested mechanism. Not approved. |

**Do not invent product scope.** If a capability is not in this document, it does
not exist yet. **"Not in V1" is recorded as out of current scope, never as a
permanent exclusion.**

---

## 1. Product direction — LOCKED

SpendWise evolves from a personal-finance / expense-tracking product into an
**integrated banking-enabled personal-finance platform**.

It should feel **premium, calm, intelligent, confident, trustworthy, and
financially precise**.

It must feel like **one product**:

```
money → activity → spending understanding → planning → insights
```

**The existing personal-finance product is the intelligence layer on top of the
new money core.** The tracker is not legacy to be tolerated — it is the reason the
money core is worth building.

### 1.1 What exists today — CURRENT

A tracking product: manual expenses, categories, budgets, recurring, groups,
analytics, reports. No money moves through SpendWise.

### 1.2 What is added — TARGET

A money core: financial account, ledger-backed balance, transfers, and receiving.
Banking / money-core **V1 is NGN**.

---

## 2. Preservation principle — LOCKED

| Disposition | Meaning |
|---|---|
| **KEEP** | Survives as-is. |
| **ADAPT** | Sound; extends to meet the money bar. |
| **REPLACE** | New implementation required — **needs concrete justification**. |
| **REMOVE** | Does not belong. Requires a recorded reason. |
| **ADD** | Genuinely new. |

**Prefer KEEP and ADAPT.** This is not a greenfield rewrite.

### 2.1 Surface dispositions

| Surface | Today | Disposition | Destination |
|---|---|---|---|
| Dashboard | `(tabs)/index` | **ADAPT** | **Home** — gains balance, Send/Receive/More, insight |
| Expense list | `(tabs)/expenses/index` | **ADAPT** | **Activity** — becomes unified financial history |
| Expense detail / create / edit | `(tabs)/expenses/*` | **KEEP** | Reached from Activity and Home |
| Budgets | `(tabs)/budgets/*` | **KEEP** | **Plan** |
| Recurring | `(tabs)/profile/recurring/*` | **KEEP / ADAPT** | **Plan** — planning language only (§9) |
| Analytics — trends, categories, anomalies | `(tabs)/analytics/*` | **KEEP** | **Plan / Spending** — useful infrastructure preserved |
| Financial health — numeric customer score | `(tabs)/analytics/health` | **REMOVE from customer UX** | Replaced by qualitative experience (§10) |
| Reports | `(tabs)/profile/reports/*` | **KEEP, de-emphasised** | No root tab; secondary access (§12) |
| Categories, incl. curated colours | `(tabs)/profile/categories/*` | **KEEP** | **You** |
| Profile, settings, preferences, password, about | `(tabs)/profile/settings/*` | **KEEP / ADAPT** | **You** |
| Groups | `groups/*` | **KEEP** | Focused stack; **Home → More → Groups** (§11) |
| Auth | `(auth)/*` | **ADAPT** | Phone-required login, OTP, PIN (§13) |
| Root tab bar (NativeTabs, 5 triggers) | `components/app-tabs.tsx` | **ADAPT** | Four root destinations (§3) |
| Dark mode | theme provider | **KEEP** | Light-first, not light-only |
| Home balance, Send, Receive | — | **ADD** | Home |
| Transaction detail (financial) | — | **ADD** | Activity |
| Verification / KYC | — | **ADD** | You — content **DEFERRED** |
| Support / disputes | — | **ADD** | Transaction-contextual (§15) |

---

## 3. Navigation — LOCKED

Authenticated root navigation is exactly:

```
Home        Activity        Plan        You
```

### 3.1 Bottom navigation

**Bottom navigation exists ONLY on those four root destinations.**

**Focused flows do not show root bottom navigation.** They use a **back/close
hierarchy** instead. Focused flows include:

auth · onboarding · verification/KYC · Send · recipient selection · amount ·
review · PIN · processing/result · transaction detail · budget creation ·
security recovery · disputes · group flows

### 3.2 Tab bar character

The root tab bar **preserves the subtle SpendWise glass / translucent character**.

**The existing NativeTabs implementation is ADAPTED** if it can reliably achieve
the target on the pinned Expo version. **It is not replaced merely because the API
is marked unstable** — replacement requires a concrete, documented limitation
(**OPEN**).

### 3.3 What does not get a tab

- **Insights are not a fifth root tab.** A deeper Insights destination may be
  reachable from Home.
- **Reports do not require a root tab** (§12).
- **Groups are not a root tab** (§11).

---

## 4. Home — LOCKED

Home is the primary visual anchor for the product's hierarchy and density.

### 4.1 Priority order

1. **customer / context**
2. **balance when financially activated**
3. **Send / Receive / More**
4. **recent activity**
5. **spending summary**
6. **one contextual SpendWise insight**

**Recent Activity appears ABOVE spending summary.**

### 4.2 Balance presentation

- **Balance is typography-led.**
- **Do not use a giant gradient wallet card.**
- Terminology is exactly **Total balance**, **Available balance**, and **Being
  confirmed**. No invented terms.
- **Never show a fake ₦0** while financial data is unknown or loading.

### 4.3 Pre-financial-account state

A customer without a financial account **must not see a fake active bank
account**. They see an **activation path**.

### 4.4 More

**More** is the entry to secondary destinations, including **Groups** (§11) and
potentially Reports (§12).

---

## 5. Send — LOCKED

### 5.1 Normal flow

```
Recipient → Amount → Review → PIN → Processing → Successful
```

**Processing is normal and brief.** The ordinary path ends in success — **do not
routinely show Pending or Awaiting Confirmation**.

### 5.2 Amount

**Insufficient balance and known limit violations are caught here — before PIN and
before provider submission** — wherever authoritative information is available.
Surfacing an insufficient-balance failure after submission, when it was catchable
at Amount, is a defect.

### 5.3 Review

Displays **authoritative**: recipient name, bank, masked account, amount, fee,
total.

**Fees come from an authoritative quote. The frontend never calculates
authoritative fees.**

### 5.4 PIN

Transaction PIN follows Review. Money movement is never one tap from Amount.

**Duplicate taps are prevented client-side and server-side. Idempotency is
required.**

### 5.5 Ambiguous flow

```
Processing → Confirming → Successful OR Failed
```

**`Confirming` is the customer-facing representation of backend UNKNOWN. UNKNOWN
is never shown to the customer. A timeout does NOT equal failure.**

`Confirming`:

- **is exceptional**, not the normal path
- **may be longer-lived**
- has **NO Retry action**
- **explains that SpendWise is confirming with the receiving bank / provider**
- **must not contain fabricated deterministic provider stages**
- **survives app close and backgrounding**
- **resolves from authoritative backend state**
- **must not resubmit the transfer**

Reserved funds may be shown as **"Being confirmed"**.

### 5.6 Failure

**Known failure is shown only when failure is authoritative.**

### 5.7 Cancellation

**No transfer cancellation semantics are approved.** Cancellation does not appear
in any Send screen unless the backend and product flow explicitly support it.

### 5.8 Offline

Money-moving operations are unavailable offline and **are never queued for later
automatic submission**.

---

## 6. Receive — LOCKED (V1 scope)

V1 Receive shows:

- **account name**
- **account number**
- **bank / provider display name**
- **Copy account number**
- **Share details**

**Not in V1** — recorded as out of current scope, **not** permanent exclusions:

- **QR code**
- **Request Money protocol**
- **Payment handles / payment links** (unless deliberately designed later)

---

## 7. Activity — LOCKED

**Activity replaces the old Expenses root-tab concept.** It becomes **unified
financial history enhanced by SpendWise interpretation**.

### 7.1 Supports

- search
- filters
- money in
- money out
- categories / status where supported
- pagination

### 7.2 Visual weight

**Successful ordinary transactions should not be visually overwhelmed by green
success badges.** Exceptional states are the prominent ones.

### 7.3 Record types

**Manual expenses must remain clearly distinguishable** from financial
transactions — by label and form, not colour alone.

Combining the two for **spending intelligence is legitimate where the semantics
are explicit**. What is never blended is their **financial identity or ledger
truth**.

### 7.4 Transaction detail

**Separates immutable financial facts from editable SpendWise interpretation.**

| Immutable financial facts | Editable SpendWise interpretation |
|---|---|
| Amount, counterparty, bank, reference, timestamps, status | Category, note, include-in-spending |

A financial transaction **cannot be deleted by the customer**. A manual expense
**remains editable and deletable**.

Transaction detail is a **focused flow** — no root bottom navigation (§3.1).

---

## 8. Plan — LOCKED

Plan contains:

- **Budgets**
- **Recurring**
- **future Goals**, when deliberately introduced (not V1)

### 8.1 Budgets

**Advisory / planning only. Budgets NEVER block banking transactions.** Banking
limits and budgeting are **separate systems**.

Existing budget implementation is **KEEP** unless a specific conflict appears.

Budget creation is a **focused flow** — no root bottom navigation.

---

## 9. Recurring — LOCKED

**Recurring V1 is planning and intelligence only. It is NOT automatic payment or
autopay.**

Allowed concepts: expected amount · cadence · next expected date · detected
recurring behaviour · *Mark as recurring* · *Not recurring* · *Expected Sep 14* ·
*Usually occurs around…*

Existing expense-generation functionality **may remain where it creates expense
records rather than financial payments**.

**Do not imply** *Pay automatically*, *Payment method*, *Pause payment*, or *Next
debit*. Avoid *Subscription automation* where it could imply automated bank
debiting.

---

## 10. Spending, analytics, and financial health — LOCKED

### 10.1 Spending stays focused

- period total
- category breakdown
- trend / comparison
- interpretation

**Avoid analytics-dashboard proliferation.** Existing useful trend and category
infrastructure is **preserved**.

### 10.2 Financial health

**Do NOT expose an invented numeric Financial Health score**, and do not expose
invented numeric sub-scores (debt, save, stability, or similar).

**Use a qualitative, explainable experience instead** — for example: *Good*,
*You're doing well this month*, *Areas to watch*, *Strengths*, *Recommendations*.
Existing useful qualitative narrative may be retained.

The old numeric customer-facing score is **REMOVED from the customer UX**. Stored
legacy fields are **not** deleted where migration compatibility requires them.

### 10.3 Anomaly detection

**KEEP** where explainable. It is **not** retired alongside the numeric health
score, and it may feed contextual SpendWise Insights.

### 10.4 Insights

Insights answer:

1. **What happened?**
2. **Why does it matter?**
3. **What can the customer do?**

**Rules-first intelligence is authoritative.** Optional LLM wording may be layered
on later. **LLMs must never invent** balances, transaction facts, risk facts,
account status, or financial truth.

Insights are **contextual throughout the app**. A deeper Insights destination may
be reachable from Home. **Insights are not a fifth root tab.**

---

## 11. Groups — LOCKED

**Groups / shared expenses are KEEP.** They are **not removed and not structurally
rewritten** as part of the root IA migration.

Preferred future discovery: **Home → More → Groups**.

Group flows remain **focused / root-stack flows without bottom navigation**.

---

## 12. Reports — LOCKED

**Reports are KEEP but de-emphasised.** The existing implementation is **not
deleted**.

They **do not require a root tab**. Likely future placement is secondary access
from **Activity / More** or another appropriate contextual destination. **Exact
placement may be refined during implementation** (**OPEN**).

---

## 13. Auth and onboarding — TARGET

- **Phone required for login.**
- **Phone AND email collected during initial onboarding.**
- **OTP / device verification.**
- **Transaction PIN.**
- Existing secure auth/session infrastructure is **preserved where compatible**;
  existing username/password and Google auth are **ADAPTED**, not casually
  rewritten.
- **Customer-facing V1 does not require speculative generic TOTP or passkey
  settings.** (Not in V1 — not a permanent exclusion.)

Auth and onboarding are **focused flows** — no root bottom navigation.

The existing good keyboard behaviour in the auth screens is **KEEP**.

---

## 14. Security cooldown, capabilities, and verification — LOCKED

### 14.1 Cooldown

A **24-hour heightened-security cooldown** follows a new device, password
change/recovery, PIN change/recovery, or other recovery/security-sensitive
changes.

**Exact capability restrictions are configurable and DEFERRED.**

**Do not assume receiving money is disabled.**

### 14.2 Capability-driven restrictions

The UI expresses restrictions as specific capabilities — `canSend`, `canReceive`,
`canChangeSecuritySettings`, `canUpgradeVerification` — and explains the specific
restriction. **Never a generic "account frozen".**

### 14.3 Verification (KYC)

Conceptual tiers 0–3 exist: Tier 0 pre-KYC / non-financial; Tier 1 NIN baseline;
Tier 2 BVN baseline + required checks; Tier 3 proof of address / full verification
plus provider- and compliance-required checks. **These remain subject to provider
and compliance validation before launch.**

**Exact requirements and monetary limits must NOT be hardcoded.**

Customer-facing verification states distinguish: **processing · manual review ·
mismatch/rejected · unable to verify · provider/service unavailable**.

Verification is a **focused flow**.

### 14.4 Limits

Customer limit concepts: **per-transfer limit**, **daily transfer limit**,
**maximum balance**. **Exact values are backend / configuration driven.** No
mockup value is ever adopted.

---

## 15. Support and disputes — LOCKED

Support is **transaction-contextual where possible**.

A durable case carries: **case / reference · status · timeline · related
transaction**.

**Do not invent** support phone numbers, 24/7 availability, response SLAs,
resolution SLAs, or unsupported fraud/security guarantees.

Disputes are a **focused flow**.

---

## 16. System states — LOCKED

Every surface supports, distinctly:

**loading · empty · offline · stale · degraded service · session expired ·
restricted capability · cooldown · verification states · transaction Confirming**

Rules:

- **Never fake ₦0.**
- **Cached financial data must not silently appear current.**
- **Offline money-moving operations are NEVER queued for later automatic
  submission.**
- **Generic service degradation is NOT the same thing as a transaction being
  `Confirming`.**

---

## 17. Accessibility — LOCKED

Baseline: adequate touch targets · screen reader semantics · dynamic text where
practical · semantic grouping · contrast · **no colour-only statuses** ·
reduced-motion support · logical focus · keyboard handling · accessible monetary
announcements.

**Preserve the existing good keyboard behaviour.**

---

## 18. Investor demo — LOCKED

**Target checkpoint: D0–D6 complete + M0–M8.**

The demo shows:

1. successful money flow
2. failure handling
3. timeout / UNKNOWN
4. customer-facing **Confirming**
5. later authoritative resolution
6. optionally, transaction → category → budget/spending → insight

**Uses the provider simulator**, which is intentionally part of the architecture
and the demo. **A real provider is not required** (M14 is on hold).

The full M0–M15 roadmap is in
`../architecture/SPENDWISE_FINTECH_ARCHITECTURE.md` §14.

---

## 19. Deferred and open

**DEFERRED** — awaiting investor, provider, or compliance input: provider and KYC
vendor identity; exact production limits and thresholds; compliance rules;
cooldown capability restrictions; KYC tier validation; settlement-time
expectations not supplied by the backend.

**OPEN** — implementation decisions:

| # | Question |
|---|---|
| O-1 | Whether NativeTabs achieves the target glass treatment on the pinned Expo version (§3.2) |
| O-2 | Reports' eventual placement (§12) |
| O-3 | Home's More destination contents beyond Groups (§4.4) |
| O-4 | Activity filter set and defaults (§7.1) |
| O-5 | Persistence representation behind §7.3 — a domain rule, not a storage mandate |

**Not in V1**, and not permanently excluded: QR receiving · Request Money ·
payment handles/links · generic customer 2FA controls · self-freeze · Goals.
