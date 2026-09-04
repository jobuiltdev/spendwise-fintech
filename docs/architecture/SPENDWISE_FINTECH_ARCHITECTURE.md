# SpendWise Fintech Architecture

**Status:** Foundation document. Authoritative for architectural intent.
**Repository:** `spendwise-fintech`
**Baseline:** `pre-fintech-baseline` → `3e0853f60b539155f2a7ebb3426c5a8d262098d5`
**Decision source:** the approved D0–D6 decision series.
**Implementation state:** None. No fintech code has been written. M0 has not begun.

---

## 0. How to read this document

| Marker | Meaning |
|---|---|
| **CURRENT** | True of the code in this repository today. Verifiable by reading the source. |
| **TARGET** | The agreed destination. Not yet built. |
| **LOCKED** | A closed decision from the D0–D6 series. Changing it requires an explicit recorded decision. |
| **DEFERRED** | Deliberately unanswered. Depends on investor, provider, or compliance input that does not exist yet. |
| **OPEN** | An implementation decision that has not been made and is not owned by this document. |
| **IMPLEMENTATION PROPOSAL** | A suggestion offered for consideration. **Not approved.** Never to be cited as a requirement. |

Two distinctions matter more than any other in this document:

1. **A locked product invariant is not an implementation mandate.** Where a
   decision fixes *what must be true*, this document does not invent *how it is
   stored, named, or structured*. Those stay **OPEN**.
2. **"Not in V1" is not "never".** Out-of-scope capability is recorded as out of
   current scope, never as a permanent product prohibition.

**Nothing in this document authorises implementation.**

---

## 1. Repository and migration principle — LOCKED

### 1.1 The original repository is preserved

| | Path |
|---|---|
| Original SpendWise | `C:/Users/Joeyy/dev/spendwise` — **preserved, must remain untouched by fintech development** |
| Fintech evolution | `C:/Users/Joeyy/dev/spendwise-fintech` — this repository |

The fintech repository was **intentionally created from the original SpendWise
baseline** `3e0853f60b539155f2a7ebb3426c5a8d262098d5`, tagged
`pre-fintech-baseline`. Its single remote is
`https://github.com/jobuiltdev/spendwise-fintech.git`.

### 1.2 This is not a greenfield rewrite — LOCKED

SpendWise is an existing, shipped product. The fintech work is an **evolution of
it**, not a replacement for it.

| Disposition | Meaning |
|---|---|
| **KEEP** | Survives as-is. |
| **ADAPT** | Concept and implementation are sound; behaviour extends to meet the money bar. |
| **REPLACE** | A new implementation is required. **Requires concrete justification** — a specific, named limitation. |
| **REMOVE** | Does not belong in the product. Requires a recorded reason. |
| **ADD** | Genuinely new. |

**Prefer KEEP and ADAPT.** "It is old", "the API is marked unstable", "it looks
different from the new screens", and "we are rewriting anyway" are **not**
justifications for REPLACE.

---

## 2. Product direction — LOCKED

SpendWise evolves from a personal-finance / expense-tracking product into an
**integrated banking-enabled personal-finance platform**.

It should feel **premium, calm, intelligent, confident, trustworthy, and
financially precise**.

It must feel like **one product**:

```
money → activity → spending understanding → planning → insights
```

**The existing personal-finance product is the intelligence layer on top of the
new money core.** It is not legacy to be worked around; it is the differentiator
the money core exists to serve.

---

## 3. SpendWise as it exists today — CURRENT

Grounded in the source at the baseline commit.

### 3.1 Stack

| Layer | Technology |
|---|---|
| Mobile client | Expo `~54`, React Native `0.81.5`, React `19.1.0`, `expo-router` `~6`, TypeScript `~5.9` |
| Client state | TanStack Query `^5`, React Hook Form + Zod, Axios |
| Client storage | `expo-secure-store` (tokens), local storage service |
| Backend | Django `4.2`, Django REST Framework `3.16`, `django-filter`, `django-cors-headers` |
| Auth | `djangorestframework-simplejwt` `5.5`, refresh rotation + blacklist (access 30m / refresh 30d); Google sign-in via `google-auth` |
| Database | SQLite |
| Media | Pillow; receipt images to `receipts/` |

### 3.2 Backend domains

`accounts`, `expenses`, `categories`, `budgets`, `groups`, `analytics`,
`recurring`, `reports`.

### 3.3 Client navigation — CURRENT

`mobile/src/components/app-tabs.tsx` uses `expo-router` **NativeTabs** with five
triggers: `Home · Expenses · Budgets · Analytics · Profile`. `groups/` is a
separate stack outside the tab group.

### 3.4 The expense record — CURRENT

`expenses.Expense` is a **user-authored record of spending**. Fully mutable.
`amount` is `DecimalField(10, 2)`; `payment_status` is
`pending | completed | cancelled`; it carries optional group linkage, location,
notes, a receipt image, and anomaly-detection fields (`is_anomaly`,
`anomaly_score`, `anomaly_reason`).

### 3.5 Groups — CURRENT

`Group`, `GroupMembership` (with a denormalised `balance`), `GroupExpense`,
`GroupExpenseSplit`, `GroupSettlement`. Settlements record that people squared up
outside the app. No money moves through SpendWise today.

### 3.6 Money formatting — CURRENT

`mobile/src/utils/format.ts` `formatCurrency` coerces to a JavaScript `number` and
uses `toFixed(2)`. Adequate for user-entered tracking figures; **not adequate for
authoritative money**. See §7 — **ADAPT**.

---

## 4. Target core architecture — TARGET

| Concern | Target |
|---|---|
| Backend | **Django modular monolith** |
| Database | **PostgreSQL** |
| Async / scheduling | **Redis + Celery** initially |
| Event delivery | **Transactional outbox** |
| Object storage | **Private S3-compatible storage** |
| Customer app | **React Native / Expo** (the existing app, evolved) |
| Ops / admin | **Next.js** ops/admin application |
| Build / CI | **Docker / GitHub Actions** |

SQLite → PostgreSQL is a **REPLACE with concrete justification**: a double-entry
ledger requires transactional and concurrency guarantees SQLite does not provide,
and the current schema already works around SQLite's JSON limitations
(`Expense.receipt_data` stored as `TextField`).

### 4.1 Production posture — LOCKED

**Production / live-money implementation is ON HOLD until investor funding or an
alliance is secured.** Provider selection is **DEFERRED**. The architecture must
remain **provider-agnostic**.

### 4.2 Do not lock — DEFERRED

Until investor, provider, and compliance requirements are known, the following
must not be fixed in code, config, fixtures, documentation, or mockups:

- Provider / banking partner
- KYC vendor
- Live-money infrastructure
- Cloud platform
- Exact production limits
- Compliance rules
- Production operating thresholds
- Scale-dependent infrastructure

Architectural obligation: provider specifics enter through an abstraction
boundary (M6), and **provider statuses never leak directly into product or
domain states**.

---

## 5. Financial model — LOCKED

**The ledger is central.**

### 5.1 Conceptual layers

| Layer | Contains |
|---|---|
| **1. Money core** | Ledger, balances, holds, transaction engine, transfers, provider abstraction |
| **2. Financial products** | Capabilities built on the money core |
| **3. Experience / intelligence** | Expenses, categories, budgets, recurring, analytics, insights — the existing SpendWise product |

### 5.2 Responsibility separation

| Component | Answers |
|---|---|
| **Ledger** | What money exists / is owed |
| **Transaction engine** | What happened |
| **Expense engine** | What it was spent on |
| **Analytics** | What it means |

Each answers only its own question. Analytics never writes authoritative
financial truth. KYC and risk never mutate ledger balances.

### 5.3 Ledger rules

- **Double-entry ledger.**
- **Posted ledger history is immutable.**
- **Corrections use reversals / new entries** — never mutation of posted entries.
- **Holds / reservations are supported.**
- All ledger writes go through the **approved ledger service boundary**. No direct
  writes outside it.

### 5.4 Money representation

- **Integer minor units internally.**
- **API-facing amounts use decimal strings where appropriate.**
- **Never float arithmetic for authoritative money.**
- **No `wallet.balance += …` style authoritative mutation.**
- **No arbitrary admin balance edits.**

---

## 6. Manual expense vs financial transaction — LOCKED INVARIANT

**Manual expenses are NOT financial transactions.** This is a **domain**
invariant.

### 6.1 Manual expenses

- **Remain supported.** The existing capability is **KEEP**.
- **Remain editable and deletable** by the customer.
- **Never create ledger activity.**
- **Never become synthetic historical bank transactions.**

Today's `expenses.Expense` is this record (§3.4).

### 6.2 Actual financial transactions

- **Represent financial truth.**
- **Cannot be deleted by the customer.**
- Have **immutable financial facts**.
- **May have editable SpendWise interpretation** — for example category, note,
  and include-in-spending.

### 6.3 Persistence representation — OPEN

**The domain separation is locked. The persistence representation is an
implementation decision and is not fixed here.**

This document does **not** require separate tables, separate identifier spaces, or
any other specific storage shape. Such a requirement may only arise if the
architecture independently demands it during M1–M4, and it would be recorded there
with its justification.

### 6.4 Aggregation — LOCKED

SpendWise **may legitimately combine** financial transactions and manual expenses
for **user-facing spending intelligence**, provided the semantics are explicit —
the customer must be able to tell what a figure covers.

What must **never** be blended is their **financial identity or ledger truth**. A
manual expense never contributes to a balance, a ledger position, or any
authoritative financial figure.

### 6.5 Manual expense status — CURRENT

`Expense.payment_status` (`pending | completed | cancelled`) describes a manual
expense record. It is not a transfer state and carries no money-movement meaning
(§8).

### 6.6 Withdrawn statements

An earlier draft of this document over-specified this section. The following were
**never approved and are withdrawn**: a requirement for separate tables; a
requirement for separate ID spaces; a blanket prohibition on blended spending
aggregates. §6.3 and §6.4 replace them.

---

## 7. Money, currency, and precision — LOCKED

1. **No float arithmetic for authoritative money**, anywhere in the stack.
2. **Integer minor units internally; decimal strings on the API where
   appropriate** (§5.4).
3. **Banking / money-core V1 is NGN.**
4. **Existing multi-currency tracker functionality is KEEP.** It is not removed
   because banking V1 is NGN-only. The tracker and the money core are different
   scopes.
5. **Never aggregate unlike currencies into a financially meaningless total.**
6. **Currency is always explicit** in customer-facing money display.
7. **Tabular numerals** for all money display (§13).
8. `formatCurrency` (§3.6) is **ADAPT** — it must stop coercing authoritative
   amounts to `number`.

Rounding mode and remainder allocation for authoritative money: **OPEN**, to be
settled with the ledger (M2) as a single server-side implementation.

---

## 8. Transfer semantics — LOCKED

### 8.1 Normal flow

```
Recipient → Amount → Review → PIN → Processing → Successful
```

**Processing is normal and brief.**

**Do NOT routinely show a Pending or Awaiting Confirmation transfer state.** The
ordinary path ends in success.

### 8.2 Ambiguous flow

```
Processing → Confirming → Successful OR Failed
```

**Confirming is the customer-facing representation of backend UNKNOWN.**

**UNKNOWN is never shown to the customer.** It is a backend state; `Confirming` is
its customer-facing expression.

**A timeout does NOT equal failure.**

### 8.3 Confirming — rules

`Confirming`:

- **is exceptional** — not the normal path
- **may be longer-lived** than Processing
- has **NO Retry action**
- **explains that SpendWise is confirming with the receiving bank / provider**
- **must not contain fabricated deterministic provider stages**
- **survives app close and backgrounding**
- **resolves from authoritative backend state**
- **must not resubmit the transfer**

### 8.4 Failure

**Known failure is shown only when failure is authoritative.** An absence of
response is not failure (§8.2).

**Insufficient balance and known limit violations should be caught at Amount
entry, before PIN and before provider submission**, wherever authoritative
information is available.

### 8.5 Cancellation — not in scope

**No transfer cancellation semantics are approved.** Cancellation must not be
introduced — in the domain, the API, or the UI — unless the backend and product
flow explicitly support it.

An earlier draft invented a universal rule that "cancel is removed, not
disabled", along with `Draft`, `Submitted`, `Cancelled`, and `Returned` transfer
states. **All of that is withdrawn.** The approved customer-facing transfer states
are those in §8.1–§8.2.

**M4 follows this section as written.** `FinancialTransaction` has **no
cancellation state and no abandonment transition of any name** — not in the
domain, not in the API, not in the UI. Backing out before execution is accepted
produces no financial transaction at all, so there is nothing to cancel. If
persisted drafts are ever required they belong to the transfer/product layer,
outside the money transaction engine, and **M5 must preserve that separation**.

### 8.6 Balance terminology — LOCKED

Customer-facing terms are exactly:

| Term | Meaning |
|---|---|
| **Total balance** | |
| **Available balance** | |
| **Being confirmed** | How reserved funds may be shown |

**Do not invent customer-facing terms** such as "ledger balance" or "spendable
projection".

### 8.7 Submission integrity — LOCKED

- **Duplicate taps must be prevented client-side and server-side.**
- **Idempotency is required.**
- **No blind retry after an ambiguous timeout.**
- **Review must display authoritative** recipient name, bank, masked account,
  amount, fee, and total.
- **Fees come from an authoritative quote. The frontend never calculates
  authoritative fees.**

---

## 9. Auth and transaction security — TARGET

- **Phone required for login.**
- **Phone AND email collected during initial onboarding.**
- **OTP / device verification.**
- **Transaction PIN.**
- **Existing secure auth/session infrastructure is preserved where compatible.**
  The existing username/password and Google-auth implementation is **ADAPT**, not
  a casual rewrite.
- Customer-facing V1 **does not require** speculative generic TOTP or passkey
  settings. (Not in V1 — not a permanent exclusion.)

### 9.1 Security cooldown — LOCKED

A **24-hour heightened-security cooldown** follows:

- a new device
- password change / recovery
- PIN change / recovery
- recovery or security-sensitive changes

**Exact capability restrictions are CONFIGURABLE and DEFERRED.**

**Do not assume receiving money is disabled.**

Restrictions are **capability-driven**, expressed as capabilities such as
`canSend`, `canReceive`, `canChangeSecuritySettings`, `canUpgradeVerification` —
**not** a generic "account frozen".

---

## 10. KYC / verification — TARGET, provider-validated

Conceptual tiers exist:

| Tier | Baseline |
|---|---|
| **Tier 0** | Pre-KYC / non-financial |
| **Tier 1** | NIN baseline |
| **Tier 2** | BVN baseline + required checks |
| **Tier 3** | Proof of address / full verification + provider- and compliance-required checks |

**These remain subject to provider and compliance validation before launch.**

**Exact requirements and monetary limits must NOT be hardcoded** — not in source,
config, fixtures, documentation, or mockups.

Customer-facing verification states must distinguish:

- processing
- manual review
- mismatch / rejected
- unable to verify
- provider / service unavailable

---

## 11. Limits — TARGET

Core customer limit concepts:

- **per-transfer limit**
- **daily transfer limit**
- **maximum balance**

**Exact values are backend / configuration driven.** No mockup value is ever
copied into code or documentation. Limits are server-evaluated and
server-communicated; the client renders what the server says and never contains a
threshold.

---

## 12. Preservation inventory — dispositions

Prefer KEEP and ADAPT (§1.2).

| Area | Disposition | Note |
|---|---|---|
| Manual expense tracking (`expenses`) | **KEEP** | Editable, deletable, no ledger activity (§6.1) |
| Categories, including curated category colours | **KEEP** | Shipped, legitimate functionality. No arbitrary colour restriction is imposed. |
| Budgets (`budgets`) | **KEEP** | Advisory/planning only; never blocks banking transactions (§12.1) |
| Recurring (`recurring`) | **KEEP / ADAPT** | V1 is planning and intelligence only — not autopay (§12.2) |
| Analytics — trends, category infrastructure (`analytics`) | **KEEP** | Useful infrastructure preserved |
| Anomaly detection | **KEEP** | Retained as an explainable insight/evidence source. **Not** retired alongside the health score. |
| Numeric customer-facing Financial Health score | **REMOVE from customer UX** | Replaced by qualitative, explainable experience. Stored legacy fields are not deleted where migration compatibility requires them. |
| Reports (`reports`) | **KEEP, de-emphasised** | Implementation is not deleted. No root tab. Placement refined during implementation. |
| Groups / shared expenses (`groups`) | **KEEP** | Not removed and not structurally rewritten as part of the IA migration. |
| Auth — JWT, rotation, blacklist, Google sign-in | **ADAPT** | Preserved where compatible; extended per §9 |
| Root navigation | **ADAPT** | Four root destinations (§13). NativeTabs adapted, not replaced, unless a concrete limitation is found. |
| Dark mode | **KEEP** | Product is light-first, **not** light-only. The dark theme is not removed. |
| Existing keyboard handling | **KEEP** | Good behaviour already shipped |
| Skeletons, empty/error states, toasts, form primitives | **KEEP** | Extended, not rebuilt |
| `formatCurrency` float coercion | **ADAPT** | §7 |
| SQLite | **REPLACE** → PostgreSQL | Justified in §4 |
| Ledger, holds, transaction engine, transfers | **ADD** | §5, §8 |
| Provider abstraction + simulator | **ADD** | M6 |
| Transactional outbox, reconciliation | **ADD** | §4, M8 |
| Ops / admin application | **ADD** | Next.js (§4) |
| Transaction PIN, OTP, cooldown capabilities | **ADD** | §9 |
| KYC / verification | **ADD** | §10, provider-validated |

### 12.1 Budgets and banking are separate systems — LOCKED

Budgets are **advisory / planning only**. **Budgets NEVER block banking
transactions.** Banking limits (§11) and budgeting are separate systems that do
not interact.

### 12.2 Recurring V1 — LOCKED

Recurring V1 is **planning and intelligence only. It is NOT automatic payment or
autopay.**

Allowed concepts: expected amount, cadence, next expected date, detected recurring
behaviour, "Mark as recurring", "Not recurring", "Expected Sep 14", "Usually
occurs around…".

Existing expense-generation functionality **may remain where it creates expense
records rather than financial payments**.

Avoid wording such as "Subscription automation" where it could imply automated
bank debiting. Do not imply: *Pay automatically*, *Payment method*, *Pause
payment*, *Next debit*.

---

## 13. Client architecture direction

### 13.1 Root navigation — LOCKED

Authenticated root navigation is exactly:

```
Home        Activity        Plan        You
```

**Bottom navigation exists ONLY on those root destinations.** Focused flows do not
show root bottom navigation — auth, onboarding, verification/KYC, Send, recipient
selection, amount, review, PIN, processing/result, transaction detail, budget
creation, security recovery, and disputes all use a back/close hierarchy instead.

The root tab bar should **preserve the subtle SpendWise glass / translucent
character**.

**The existing NativeTabs implementation should be ADAPTED** if it can reliably
achieve the target on the pinned Expo version. **It must not be replaced merely
because the API is marked unstable** — replacement requires a concrete,
documented limitation.

Detailed IA, per-surface behaviour, and screen contracts live in `docs/design/`.

### 13.2 Existing client patterns are KEEP

TanStack Query, React Hook Form + Zod, the existing skeleton/empty/error component
family, the theme provider and token system, and the toast host all survive. New
financial surfaces are built **with** them.

### 13.3 Written specification governs visuals — LOCKED

`docs/design/` is authoritative over any mockup, render, or screenshot. The
approved Home visual direction is the primary **visual** anchor for hierarchy,
density, whitespace, typography, restrained indigo usage, card treatment, and
subtle glass navigation — but **written UX rules override any screenshot**
(`docs/design/MOCKUP_CORRECTIONS.md`).

---

## 14. Roadmap — LOCKED

| Milestone | Scope |
|---|---|
| **M0** | Foundation |
| **M1** | Customer / Financial Account / Wallet |
| **M2** | Double-entry Ledger |
| **M3** | Holds / Balance Projection |
| **M4** | Transaction Engine |
| **M5** | Transfers |
| **M6** | Provider Abstraction + Simulator |
| **M7** | Webhooks / Status Recovery |
| **M8** | Reconciliation |
| **M9** | Auth / Transaction Security |
| **M10** | KYC + Risk |
| **M11** | Ops / Financial Control |
| **M12** | Intelligence Integration |
| **M13** | Legacy Migration |
| **M14** | Real Provider Integration — **HOLD until investors / provider** |
| **M15** | Production Hardening & Certification — **HOLD until investor funding / production decisions** |

**M0 has not begun.**

---

## 15. Investor demo — LOCKED

**Target demo checkpoint: D0–D6 complete + M0–M8.**

The demo shows:

1. A successful money flow
2. Failure handling
3. Timeout / UNKNOWN
4. Customer-facing **Confirming**
5. Later authoritative resolution
6. Optionally, the transaction → category → budget/spending → insight connection

**Use the provider simulator.** The simulator is **intentionally part of the
architecture and the demo**. **A real provider is not required for the investor
demo** (M14 is on hold).

---

## 16. Implementation quality gate — LOCKED

For later implementation milestones:

```
Implementation → tests → full suite → migration checks → static analysis
→ diff inspection → architecture invariant review → agent report
→ STOP → human approval
```

**No automatic continuation between milestones.**

---

## 17. Global forbidden implementation patterns — LOCKED

- float money
- `wallet.balance += …` style authoritative mutation
- direct ledger writes outside the approved ledger service boundary
- blind transfer retry
- timeout = failure
- mutable posted journal entries
- provider statuses leaking directly into product / domain states
- provider HTTP calls inside DB transactions
- Django signals moving money
- serializers orchestrating money movement
- manual DB balance correction
- analytics writing authoritative financial truth
- KYC / risk directly mutating ledger balances
- arbitrary admin financial edits
- guessed production decisions

---

## 17A. Decisions closed in M0 — LOCAL/CI IMPLEMENTATION DECISIONS

These were settled while building the M0 foundation. Every one is a
**local and CI** decision. **None of them decides anything about production**,
and none closes an item in §18.

| # | Decision | Detail |
|---|---|---|
| M0-1 | **Database configuration is environment-driven** | `DATABASE_URL` selects the database. When it is unset, the configuration is the historical SQLite file verbatim, so existing local development needs nothing installed. The fallback is an explicit branch in `settings.py` rather than a parsed default URL, so it cannot drift. |
| M0-2 | **Local/CI PostgreSQL version: 17** | `postgres:17-alpine` in `docker-compose.yml` (verified server_version 17.10) and the same major version in CI. Host port **55433** so the container coexists with any PostgreSQL already on 5432. Local credentials are throwaway. **This does not choose a production database, host, or managed service.** |
| M0-3 | **PostgreSQL driver: psycopg 3** | `psycopg[binary]==3.3.5`, the maintained driver Django 4.2 supports. Loaded only when `DATABASE_URL` points at PostgreSQL. |
| M0-4 | **The money core is one Django app: `moneycore`** | A modular monolith gets one intentional boundary, not several speculative ones. It has no `models.py` and no `migrations/`, so it contributes no schema; a test asserts both. Domain code lives in `moneycore/domain/`, API adapters in `moneycore/api/`. |
| M0-5 | **Authoritative money representation: integer minor units + ISO-4217 code** | `moneycore.domain.money.Money` is a frozen dataclass. Floats, `Decimal`, strings and `bool` are rejected at construction; cross-currency arithmetic raises. It is unused by the existing tracker, whose `Expense.amount` stays a `Decimal` field. Rounding policy, allocation of remainders, formatting and persistence are **not** decided here. |
| M0-6 | **Domain-error structure for new code** | `moneycore.domain.errors.DomainError` and subclasses carry a stable `code`, a safe `message`, optional `details`, and an explicit HTTP status (400/403/404/409/422/502). Rendered as `{"error": {code, message, details?, correlation_id?}}` by a DRF handler that passes every non-domain exception to DRF untouched. **Legacy endpoint error shapes are unchanged** and are not migrated. |
| M0-7 | **Correlation ids** | `X-Correlation-ID` on every request and response. An inbound id is echoed only when it matches `^[A-Za-z0-9-]{8,64}$`; anything else is replaced with a fresh UUID4, which carries no user, host or timing information. Held in a `ContextVar` so domain code and the error handler can read it, and available to logging as `%(correlation_id)s`. Not distributed tracing. |
| M0-8 | **CI gates** | `.github/workflows/ci.yml` runs the backend suite, system check and migration-drift check on **both** engines, plus the mobile suite, TypeScript, lint and expo-doctor. No deployment. |

### What M0 deliberately did not build

Celery, Redis, the transactional outbox, an event bus, idempotency keys, storage
abstraction, and every financial model (account, wallet, ledger, journal entry,
holds, transfers, providers). Each belongs to the milestone that needs it, and
building any of them against imagined requirements would guarantee rework.

---

## 17B. Decisions closed in M1 — customer / financial account / wallet

M1 introduced the first persistent financial schema. It establishes *identity and
lifecycle only*. **It establishes no balance**: the ledger (M2) becomes the
authoritative source for what money exists, and balance projection (M3) for what
is spendable.

| # | Decision | Detail |
|---|---|---|
| M1-1 | **`FinancialCustomer` is separate from `User` and `Profile`** | `User 1 — 0..1 FinancialCustomer`, enforced by a `OneToOneField`. A user can use every legacy SpendWise feature without one; that is Tier 0. It duplicates **no** identity data (no email, phone or names) — those stay owned by the accounts/profile domain. Statuses: `active`, `closed`. |
| M1-2 | **Provisioning is explicit — never a signal** | No receiver watches `User`, and `MoneyCoreConfig` has no `ready()` hook, so existing SpendWise users do not silently acquire a financial relationship. Tests assert both, and that registering through the API creates no financial rows. |
| M1-3 | **`FinancialAccount` — one per customer** | `FinancialCustomer 1 — 0..1 FinancialAccount`, `OneToOneField`. Multiple accounts or product types are not modelled; nothing in the architecture needs them yet. |
| M1-4 | **Account lifecycle: `pending_activation → active ⇄ suspended → closed`** | `pending_activation` (provisioned, not usable — the activation-path state), `active`, `suspended` (reversible), `closed` (terminal). The initial state is named explicitly rather than a bare `pending`, because SpendWise will later carry pending KYC, provider, compliance and transaction states, and an unqualified `pending` would not say which of them it meant. **`restricted` is deliberately absent**: the architecture requires restrictions to be capability-driven (`canSend`, `canReceive`, …) rather than a generic "account restricted", and those capabilities arrive at M9. At M1 there are no operations to restrict, so a `restricted` state would carry no behavioural distinction from `suspended`. |
| M1-5 | **Transitions go through a service, and self-transitions are refused** | `moneycore.services.lifecycle.transition_account` is the only supported way to move an account. Unknown targets, illegal moves and no-op self-transitions all raise. `activated_at` is stamped on **first** activation only; `closed_at` on closure. |
| M1-6 | **`Wallet` is a currency container with `1..N` per account** | `UniqueConstraint(financial_account, currency)`. Currency uses the **same** ISO-4217 alpha-3 rule as the `Money` value type, shared through `moneycore.domain.currency` and enforced by a field validator *and* a database check constraint. Statuses: `active`, `closed` — a wallet is not independently suspendable in the current architecture, so it has no `suspended`. |
| M1-7 | **A wallet holds no balance — non-negotiable** | No `balance`, `available_balance`, `ledger_balance`, `spendable_balance`, `pending_balance`, `reserved_balance`, `held_balance` or `total_balance`; no numeric money column of any name; no `credit()`/`debit()` helper; no signal maintaining any such column. A test asserts the concrete field set is exactly `{id, financial_account, currency, status, created_at, updated_at}`. |
| M1-8 | **Provisioning is result-idempotent** | `provision_financial_account(user)` converges on exactly one customer, account and wallet per currency. It is ordinary domain idempotence — `get_or_create` inside one `transaction.atomic`, with the unique constraints as the final guard against concurrent duplicates. It is **not** the M4 idempotency-key framework. |
| M1-9 | **Provisioning ≠ activation** | Provisioning leaves the account `pending_activation`. Activation is a separate lifecycle transition that asserts nothing about verification; KYC is M10, and no customer-facing activation flow exists. |
| M1-10 | **One authenticated read endpoint, no write surface** | `GET /api/financial-account/` returns the caller's own relationship. There is no router CRUD for `FinancialCustomer`, `FinancialAccount` or `Wallet` — a client cannot POST `status=active` or create wallets. Write methods return 405. |
| M1-11 | **Absence is a 200, not a 404** | An unprovisioned user gets `{"customer": null, "account": null, "wallets": []}`. Being Tier 0 is an expected state the Home experience renders an activation path for — a 404 would make a normal state look like a failure, and a zero balance would be a fabricated financial figure (`UX_RULES` R1.5, R1.8). |
| M1-12 | **Ownership is structural, not checked** | The view resolves the relationship from `request.user` and accepts no identifier, so there is no id to tamper with and no lookup that could reach another user's data. The serialised response exposes no primary keys at all. |
| M1-13 | **No provider references, no account numbers** | No `provider_*`, `bank_account_number`, `bank_code`, `virtual_account_number`, `iban` or `external_id` on any model or in any response. Provider integration is M6; fabricating an account number before receive-money infrastructure exists would put a fake bank account in front of a customer. |
| M1-14 | **Error codes added** | `financial_account_not_found` (404), `invalid_account_transition` (409), `invalid_wallet_transition` (409), `wallet_already_exists` (409) — all using the M0 `DomainError` shape and correlation-id integration. No second error format. |

### What M1 deliberately did not build

Ledger accounts, journals, entries, a posting service, debit/credit accounting,
authoritative or available balance, holds, reservations, balance projection,
transfers, recipients, a transaction engine or history, provider abstraction, a
simulator, webhooks, reconciliation, fees, settlement, KYC, risk rules, limits, a
transaction PIN, capability restrictions, ops tooling, Celery, Redis, an outbox,
an idempotency-key framework, and any mobile UI. No legacy expense became
financial history, and no existing user was migrated.

---

## 17C. Decisions closed in M2 — the double-entry ledger

M2 makes posted ledger entries the single source of financial truth. It answers
one question — *what is the posted balance?* — and deliberately not the M3
question of what is spendable.

| # | Decision | Detail |
|---|---|---|
| M2-1 | **Taxonomy: the five conventional account classes** | `asset`, `liability`, `equity`, `revenue`, `expense`. Product concepts (Food, Transport) are expense intelligence and never appear as ledger types. |
| M2-2 | **`LedgerAccount` is distinct from `FinancialAccount` and `Wallet`** | `FinancialAccount` is a customer's product relationship; `Wallet` is a currency container; `LedgerAccount` is bookkeeping machinery. Three names, three jobs. |
| M2-3 | **Wallet ↔ ledger mapping: `LedgerAccount.wallet` is a nullable `OneToOne`** | A wallet has at most one ledger account and vice versa, enforced by the database. An account with no wallet is internal. The direction was chosen so `Wallet` gains no field at all — it stays exactly as M1 left it. |
| M2-4 | **The ledger supports the conventional taxonomy; the wallet's accounting classification is NOT decided** | `open_wallet_ledger_account` requires `account_type` as a keyword argument **with no default**, so nothing in M2 picks a class for customer funds. Whether those funds are a liability of SpendWise, an asset held on a customer's behalf, or something else follows from a custody, provider and accounting design that has not been settled — defaulting would quietly settle it. `posted_balance` reads whatever type the account actually carries, so normal-balance semantics stay correct for every class. Tests assert a wallet may map to any conventional classification and **must not** assert that a wallet account is always a liability. **OPEN — see O-13.** |
| M2-5 | **No bank, settlement, custody or provider account is defined or seeded** | The counterpart side of a customer posting is supplied by the caller. No migration creates one, and tests build their own neutral internal counterpart. |
| M2-6 | **Amounts are positive 64-bit integer minor units** | `BigIntegerField` with a `> 0` check constraint; direction is a separate field. A debit is never a negative credit. `MAX_AMOUNT_MINOR` is the storage bound (2⁶³−1) and **is not a product limit** — what a customer may move is a later policy decision. |
| M2-7 | **No float or Decimal anywhere in the ledger** | Rejected at the service boundary (float, `Decimal`, `str`, `bool` and out-of-range all raise) and structurally absent from the schema. Tests assert no `FloatField`/`DecimalField` exists on any ledger model. |
| M2-8 | **Sign convention** | `posted_balance` returns a `Money` that is positive when the account holds a balance in its **normal** direction: debit-normal (`asset`, `expense`) = debits − credits; credit-normal (`liability`, `equity`, `revenue`) = credits − debits. A wallet holding money therefore reads positive. |
| M2-9 | **Journals are single-currency** | `Journal.currency`, `LedgerAccount.currency`, and every entry's account must match the journal. This is what makes "debits equal credits" meaningful; balancing across currencies would require an exchange rate, and there is no conversion path. FX stays **DEFERRED**. |
| M2-10 | **Lifecycle `draft → posted`, created and posted in one transaction** | The service never leaves a draft behind. `draft` exists so "posted" is a property the balance query filters on rather than assumes, and so a half-built journal is representable and provably excluded. A check constraint ties `posted_at` to status. |
| M2-11 | **Balancing is a service invariant, not a CHECK constraint** | A CHECK cannot express a cross-row sum. The database guards single-row facts (positive amount, valid direction); `post_journal` guards ≥2 entries and debits == credits. A test states this division explicitly. |
| M2-12 | **All posting goes through `moneycore.services.ledger`** | No serializer, view, signal or model method creates financial truth. Posting is `@transaction.atomic`, so a rejection leaves no journal and no entries. |
| M2-18 | **An unmapped wallet has no balance — it does not have a balance of zero** | `wallet_posted_balance` raises `wallet_ledger_account_not_found` (404) when the wallet has no ledger account. "No authoritative ledger relationship exists" and "the ledger says zero" are different facts, and returning zero for the first would state a financial fact the ledger never established — the same fabrication as a fake ₦0 on a screen. A **mapped** account with no posted entries still returns exactly `Money(0, currency)`. Reading a balance never provisions the missing account, so **O-14 stays open**. |
| M2-13 | **Posted history is immutable** | `save()`/`delete()` on `Journal` and `JournalEntry`, plus `update()`/`delete()` on their querysets, refuse to touch posted rows. Entries are write-once even before posting. `PROTECT` stops accounts and wallets being deleted out from under history. |
| M2-14 | **Corrections are reversals, never edits** | `reverse_journal` posts a *new* journal of exact opposites and links back via `Journal.reverses`. The original is never edited, marked or deleted — and gains no `is_reversed` flag, because the `OneToOne` on `reverses` is what limits it to one reversal, at the database level. A reversal cannot itself be reversed. |
| M2-15 | **Concurrency: derived balances plus one deliberate lock** | Nothing caches a balance, so there is no lost update to race for. The single `select_for_update` is on the original journal during reversal, so parallel reversal attempts queue rather than both seeing "not yet reversed"; the `OneToOne` is the final guard. Verified with real threaded transactions on PostgreSQL. |
| M2-16 | **The ledger performs no rounding** | It operates on already-resolved integer minor units. A caller that needs to split an amount that does not divide evenly owns that remainder policy. This narrows **O-5** without closing it. |
| M2-17 | **No customer-facing ledger API** | No journal, entry, account or balance endpoint exists, and the M1 `GET /api/financial-account/` response is unchanged and still balance-free. Exposing total/available/reserved to product surfaces belongs to M3. |

### What M2 deliberately did not build

Holds, reservations, available/spendable/reserved/held balance, balance
projection, transfers, recipients, a transaction engine or states, provider
abstraction, a simulator, provider IDs, account numbers, virtual accounts,
webhooks, reconciliation, settlement, fees, KYC, risk, Celery, Redis, an outbox,
an idempotency-key framework, and any UI. No legacy `Expense` became a ledger
entry, and no ledger row references an `Expense`.

---

## 17D. Decisions closed in M3 — holds and balance projection

M3 answers a second balance question — *what is spendable?* — without disturbing
the first. The ledger stays the only place money exists.

| # | Decision | Detail |
|---|---|---|
| M3-1 | **A hold reserves posted funds; it never moves them** | Creating, releasing and expiring a hold each post **zero** journal entries. The ledger is untouched for a hold's entire life. Money moves only when a later domain operation explicitly posts entries, and that orchestration is M4/M5. |
| M3-2 | **One reservation concept, named `FundsHold`** | No parallel `Reservation` model. The persistence name avoids colliding with the `hold` verb used across the service layer; the domain concept is simply a hold. |
| M3-3 | **Lifecycle `active → released` or `active → expired`, both terminal** | Released and expired both stop reserving funds but are kept apart because *why* a reservation ended is operational history. Deliberately absent: `captured`, `settled`, `completed`, `failed`, `pending`, `processing`, `confirming` — each describes a transaction or provider outcome, and a hold knows about neither. |
| M3-4 | **Effective expiry is a read-time predicate, not a stored flag** | A hold reserves funds when it is `active` **and** has not passed `expires_at`. An overdue hold stops reserving the moment it passes, whether or not anything has written `status=expired`. **Balance correctness therefore never depends on a cleanup job having run** — which is precisely why M3 introduces no scheduler, no Celery and no Redis. `expire_due_holds` exists as housekeeping to keep stored status honest; it changes no balance. |
| M3-5 | **`posted` / `held` / `available`, all derived** | `posted` from the M2 ledger, `held` by integer aggregation over effectively-active holds, `available = posted − held`. **No `available_balance`, `held_balance`, `reserved_balance`, `spendable_balance` or `projected_balance` column exists** on `Wallet`, `LedgerAccount`, `FinancialAccount`, `FundsHold` or anywhere else. Nothing caches a total, so nothing can drift. |
| M3-6 | **`BalanceProjection` is a frozen value object** | Three `Money` values, one enforced invariant (`available = posted − held`) and one enforced currency. Constructing an inconsistent or mixed-currency projection raises. |
| M3-7 | **Amounts are positive 64-bit integer minor units** | Identical rules to the ledger: `BigIntegerField`, `amount_minor > 0` check constraint, no float, no `Decimal`, no negative-amount convention, no zero-value hold, no rounding. |
| M3-8 | **Hold currency must equal wallet currency** | Denormalised onto the row so the rule is a database constraint, and refused by the service before a row is written. |
| M3-9 | **The `Wallet` row is the serialisation gate for every operational change to spendability** | `create_hold`, `release_hold` and `expire_hold` take `SELECT … FOR UPDATE` on it, **and so does any ledger posting touching a wallet-backed account** (M3-10). Whoever reaches the lock first commits; the other sees the committed reality. The wallet row is a natural gate — it already exists, is exactly one per wallet, and is otherwise uncontended. **It is not a balance row, and none was invented to have something to lock** — a test asserts `Wallet`'s field set is still the six M1 columns. PostgreSQL is authoritative; SQLite's `FOR UPDATE` is a no-op and the threaded tests skip there with that stated reason. |
| M3-10 | **Ledger posting participates in the same wallet lock, and respects reservations** | **This corrects an earlier, incorrect claim** that posting could safely skip the lock because it only ever made the posted balance larger. It does not: a journal can *decrease* a wallet-backed account's normal balance — a debit against a credit-normal wallet account does exactly that — so posting and reserving contend for the same funds. Without a shared gate, a 7 000 hold and a 7 000 debit could both commit against a 10 000 balance, leaving held above posted. `post_journal` therefore identifies every wallet-backed account in the journal, locks those `Wallet` rows **in ascending primary-key order** (deterministic, so two journals touching the same pair cannot deadlock), computes each wallet's net balance delta from the account's **actual** normal-balance semantics, and refuses the posting if any wallet's resulting balance would fall below its effectively-active held total. Internal accounts with no wallet carry no reservations and are neither locked nor checked. |
| M3-19 | **The invariant `effective_held <= posted` holds after every supported operation** | Enforced from both sides, so the race resolves safely whichever way it lands: a hold reaching the lock first makes a conflicting debit fail with `ledger_posting_conflicts_with_holds`; a debit reaching it first makes the hold fail with `insufficient_available_balance`. Never clamped, never resolved by silently releasing a hold, never by rewriting ledger history. |
| M3-20 | **Reversal uses the same guarded path — there is no bypass** | `reverse_journal` posts through `post_journal`, so it acquires the same locks and the same reservation guard. A reversal that would claw back funds now reserved is refused and the original is left untouched. M3 implements no finance-ops override; if one is ever needed it is a later, deliberate design. |
| M3-21 | **Future hold consumption stays possible without changing this** | A later transaction service can, inside one outer `transaction.atomic`, acquire the wallet lock, release or consume the reservation under whatever semantics M4/M5 define, post the balanced journal, and commit atomically. M3 implements none of that — no `capture_hold`, no `CAPTURED`/`SETTLED` status — the point is only that today's boundary does not make it impossible. |
| M3-11 | **An unmapped wallet has no projection** | Inherits the M2 rule unchanged: `wallet_balance_projection` raises `wallet_ledger_account_not_found`. It never reports 0/0/0, because "no ledger relationship" is not "zero funds". Reading a projection provisions nothing, so **O-14 stays open**. |
| M3-12 | **Incoherent state is raised, never clamped** | Held exceeding posted, or a negative posted balance on a customer wallet, raises `balance_projection_invalid`. Reporting a tidy zero would hide the fault behind a plausible number. M3 invents no overdraft: a wallet already negative cannot take a hold. |
| M3-13 | **Release is explicitly NOT idempotent** | A second release raises `hold_not_active` rather than quietly succeeding, so a duplicate is surfaced as the caller mistake it is. Safe-retry semantics belong to M4's idempotency keys; a private version here would pre-empt that. |
| M3-14 | **Expiry only when actually due; ending a hold early is a release** | `expire_hold` refuses a hold that is not past `expires_at`. The two record different operational facts and are not interchangeable. |
| M3-15 | **Terminal holds are immutable and holds are never deleted** | Model `save()` refuses to alter a released or expired row; queryset `update()` refuses when terminal rows are in scope; `delete()` is refused outright at both levels. A hold is operational history. |
| M3-16 | **No capture, settlement or hold-to-journal conversion** | `capture_hold`, `settle_hold`, `commit_hold` and `convert_hold_to_journal` do not exist, asserted by test. Turning a reservation into accounting entries is M4/M5 orchestration. |
| M3-17 | **Long-lived holds are supported, without transaction vocabulary** | A hold may stay `active` indefinitely, which is what will let a later milestone keep funds reserved while an outcome is UNKNOWN. Customer-facing wording such as "Being confirmed" belongs to the transaction and product layers, and no provider terminology appears on the model. |
| M3-18 | **No customer-facing surface** | No hold or balance endpoint exists, and the M1 `GET /api/financial-account/` response is unchanged and still balance-free — verified even with funds posted and a hold active. Exposing projections to product surfaces is a later decision. |

### What M3 deliberately did not build

Transfers, recipients, a transaction model or state machine, transaction
history, provider abstraction, a simulator, provider IDs, account numbers,
webhooks, reconciliation, settlement, fees, KYC, risk, a transaction PIN,
cooldown rules, Celery, Redis, an outbox, an idempotency-key framework,
scheduled hold cleanup, capture or settlement orchestration, and any UI. No
legacy `Expense` gained a hold, and no hold references one.

## 17E. Decisions closed in M4 — the transaction engine

M4 answers a third question, kept strictly apart from the first two: *what
operation is happening, and what does SpendWise authoritatively know about it?*
The ledger still owns what money exists (M2) and the hold still owns what posted
money is reserved (M3). A transaction owns neither; it owns state.

The single most consequential decision in this milestone is that **UNKNOWN is a
first-class, persisted, non-terminal state**. Everything else follows from
refusing to let an absent answer be recorded as a failure.

| # | Decision | Detail |
|---|---|---|
| M4-1 | **Three concepts, three modules, no blending** | Ledger = what posted. Hold = what is reserved. Transaction = what is being attempted. A transaction carries no accounting entries of its own, and neither the ledger nor the hold gained a transaction column. The only structural link is on the transaction side: `FinancialTransaction.hold` and `FinancialTransaction.journal`, both `OneToOne`. |
| M4-2 | **Direction is two-valued: `outgoing` / `incoming`** | Whether money leaves or enters the wallet is all the engine needs. Card payment, refund, cash-out, fee and the rest are *operation types* — what kind of thing is being done — which is M5's vocabulary. No `TransactionType`, `OperationType` or `TransferType` exists, asserted by test. |
| M4-3 | **Five states: `created`, `processing`, `unknown`, `succeeded`, `failed`** | `TERMINAL = {succeeded, failed}`. **`unknown` is deliberately absent from that set**: it is unresolved, not concluded. `EXECUTION_MAY_HAVE_STARTED = {processing, unknown}` names the states where the operation might already have happened externally. There is no cancellation or draft state — see M4-12. |
| M4-4 | **The transition table is explicit, closed, and rejects self-transitions** | `created → {processing, failed}`; `processing → {succeeded, failed, unknown}`; `unknown → {succeeded, failed}`; the two terminals go nowhere. `created → failed` is reserved for an **authoritative known pre-execution failure** — a check that genuinely failed — and is not an abandonment route. Re-marking a `processing` transaction as `processing` is a caller mistake surfaced as `invalid_transaction_transition`, not a silent success. |
| M4-5 | **There is no path from ambiguity to failure** | `unknown → failed` exists only as an *authoritative* conclusion — someone established that nothing moved. Nothing in the engine converts a timeout, an absent response or an ambiguous one into `failed`; that is `mark_unknown`. This is the rule §8.2 and §8.4 describe, implemented as a state machine rather than a convention. |
| M4-6 | **`unknown → processing` does not exist** | "Try again" from ambiguity would discard the fact that an execution may already have happened, which is exactly how the same money gets sent twice. Resolution moves forward to an authoritative answer or it waits. |
| M4-7 | **`mark_unknown` changes nothing except the state — and the hold stays active** | No journal, no release, no balance movement. Keeping the reservation is the point: the funds stay unavailable while SpendWise works out what happened, so the customer cannot spend money that may already have left. The state is a database column, so it survives any process, worker or app restart. |
| M4-8 | **Success releases the reservation and posts the journal in ONE atomic block, release first** | This ordering is not incidental. While a hold is active, M3's posting guard (M3-10) correctly refuses a journal that would take the wallet below its reserved funds — so an active hold blocks the very posting it exists to enable. Releasing in a separately committed step instead would leave a window in which the funds are neither reserved nor spent, and briefly spendable twice. Both happen inside one `transaction.atomic`: either both land or neither does. A test proves the same entries are refused through `post_journal` while the hold is active, and accepted through `succeed_transaction`. |
| M4-9 | **Success accepts prepared entries; the engine invents no ledger accounts** | `succeed_transaction(txn, *, entries, …)` posts what the calling domain supplies. M4 does not know what a transfer is, so deciding which accounts one debits and credits would put transfer knowledge in the wrong milestone. `LedgerAccountType`, `open_ledger_account` and `open_wallet_ledger_account` do not appear in the service, asserted by test. M5 supplies transfer accounting. |
| M4-10 | **"Succeeded" and "has posted truth" are the same fact, enforced by the database** | A check constraint requires `status = succeeded ⟺ journal IS NOT NULL`: no transaction may claim success without a posted journal, and no non-succeeded transaction may carry one. A second constraint requires `terminal ⟺ resolved_at IS NOT NULL`. These are database constraints, not service conventions. |
| M4-11 | **Known failure releases the reservation in the same transaction** | Nothing moved, so nothing may stay reserved. A transaction cannot end `failed` with the customer's money still locked away. This is the *only* pre-execution exit, and it is authoritative — it records that a check genuinely failed, not that someone changed their mind. |
| M4-12 | **There is no cancellation, from any state** | This follows the locked §8.5 rather than reinterpreting it. `CANCELLED`, the `created → cancelled` transition and `cancel_transaction()` do not exist — the status check constraint rejects `cancelled` at the database level, and a test asserts no cancellation vocabulary survives anywhere in the money core. **A `FinancialTransaction` is not a persisted draft or an abandoned UI flow**: backing out before execution is accepted produces no financial transaction at all. Pre-execution product drafts, if ever required, belong to the future transfer/product layer, and **M5 must preserve that separation**. No replacement state was added in cancellation's place. |
| M4-13 | **Starting an outgoing operation requires an *effectively active* reservation** | `start_processing` refuses without a hold (`transaction_hold_required`) and refuses an expired one (`transaction_hold_invalid`), using M3's read-time expiry predicate rather than a stored flag. **M4 neither renews nor extends a reservation** — how long one should live is O-17, still open — so an expired hold is a refusal, never something to quietly repair. Incoming transactions reserve nothing. |
| M4-14 | **A hold's amount is not required to equal the transaction's** | A reservation may legitimately cover more than the principal; fees are the obvious future case. M4 has no business inventing that arithmetic, so it validates wallet and currency and stops there. |
| M4-15 | **One reservation funds at most one operation** | `hold` is a `OneToOne`, so two transactions can never spend the same reserved money — enforced by the database and proved under a real concurrent race. `journal` is likewise `OneToOne`: one posted journal certifies at most one success. |
| M4-16 | **Idempotency is scoped to `(wallet, idempotency_key)` and is intent-level only** | The same key with the same intent returns the existing transaction; the same key with a *different* intent raises `transaction_idempotency_conflict` rather than quietly ignoring what was asked for. Scoped per wallet so two customers cannot collide. This is **not** a general idempotency framework and says nothing about provider idempotency (M6) or webhook replay (M7). |
| M4-17 | **Concurrent creation rests on the unique constraint, not a read-then-write check** | Both callers attempt the insert; one wins, the loser catches the integrity error inside a savepoint and returns the winner's row. Exactly one transaction exists either way — proved with eight simultaneous callers on PostgreSQL. |
| M4-18 | **Lock order: wallets (ascending PK) → transaction row → hold → ledger posting → transaction update** | M4 introduces no second ordering. It reuses M2/M3's rule that the `Wallet` row is the serialisation gate for everything affecting spendability, and reuses M2's ascending-primary-key helper for multiple wallets. **No path in the codebase takes a transaction or hold lock before a wallet lock**, which is what keeps M4 from introducing a deadlock cycle. Success also locks every wallet named by the supplied entries, so the locks it holds already cover everything `post_journal` will touch. |
| M4-19 | **Every status change goes through the service** | Nothing assigns `transaction.status` directly, no serializer orchestrates a transition, and no signal is involved. Model `save()` refuses to alter intent (`wallet`, `direction`, `amount_minor`, `currency`, `idempotency_key`) as a backstop, not as the supported path. |
| M4-20 | **Transactions are permanent history and are never deleted** | `delete()` is refused at model and queryset level; bulk `update()` is refused when resolved rows are in scope. |
| M4-21 | **Amounts are positive 64-bit integer minor units** | Identical to M2 and M3: `BigIntegerField`, `amount_minor > 0` check constraint, no float, no `Decimal`, no negative-amount convention, no zero-value transaction, no rounding. Direction carries the sign; the number never does. |
| M4-22 | **`failure_code` is a normalised internal code, and nothing else is stored** | `CharField(max_length=64)`. No provider text, payload, status string, response body, stack trace or credential is stored anywhere on the transaction. The code vocabulary itself is M6's to define when provider errors are first mapped. |
| M4-23 | **No provider, no network, no scheduler, no signals, no API, no mobile** | The money core imports no HTTP client. No provider vocabulary appears in the transaction modules. No transaction, transfer or payment route is registered, and the M1 `GET /api/financial-account/` response is unchanged and still balance-free — verified with a live `processing` transaction and an active hold. No dependency was added. No mobile file was touched. |
| M4-24 | **"Confirming" is product wording and never appears in the backend** | `unknown` is the stored state; §8.2's `Confirming` is how it is expressed to a customer, and §8.6's "Being confirmed" is how reserved funds may be shown. Neither string exists in the domain, the model or the service. |
| M4-25 | **Still no stored balance, anywhere** | M4 added no `balance`, `available_balance`, `held_balance` or `reserved_balance` column to any model. `posted / held / available` remain derived (M3-5). |
| M4-26 | **Migration `0004` is purely additive; `0001`–`0003` are byte-unchanged** | Verified forward, reversed to `0003`, and re-applied on PostgreSQL. Tests assert the app has exactly four migrations and that no earlier one mentions `FinancialTransaction`. |
| M4-27 | **M3's hold-schema assertion is narrowed, not weakened** | M3 asserted that no field on `FundsHold` names a transaction. `FinancialTransaction.hold` gives the hold a *reverse* accessor, so the assertion now covers **concrete columns**: the hold table still stores no transaction reference. The column lives on the transaction, which is the direction that matters — a hold knows nothing about what it funds, and reserving funds remains meaningful on its own. New assertions pin the reverse link to exactly one relation, and require it to be one-to-one. |

### What M4 deliberately did not build

Transfers, recipients, bank details, an operation-type taxonomy, a cancellation
or abandonment state, a persisted transfer draft, provider abstraction, a
provider simulator, a transfer simulator, retry or resubmission logic, an
UNKNOWN resolver or reconciliation job, webhooks, an outbox, a general
idempotency framework, fee arithmetic, hold renewal or extension, settlement,
KYC, risk, a transaction PIN, cooldown rules, Celery, Redis, any scheduler, any
API endpoint, any serializer, any signal, any admin surface, and any UI. No
legacy `Expense` gained a transaction, and no transaction references one.

---

## 18. Deferred and open register

**DEFERRED** — awaiting investor, provider, or compliance input:

| # | Item | Section |
|---|---|---|
| D-1 | Provider / banking partner selection | §4.1, §4.2 |
| D-2 | KYC vendor | §4.2, §10 |
| D-3 | Live-money infrastructure | §4.2 |
| D-4 | Cloud platform | §4.2 |
| D-5 | Exact production limits | §4.2, §11 |
| D-6 | Compliance rules | §4.2 |
| D-7 | Production operating thresholds | §4.2 |
| D-8 | Scale-dependent infrastructure | §4.2 |
| D-9 | KYC tier requirements and monetary limits (validation) | §10 |
| D-10 | Cooldown capability restrictions (configurable) | §9.1 |
| D-11 | Production / live-money implementation (ON HOLD) | §4.1 |

**OPEN** — implementation decisions not owned by this document:

| # | Item | Section |
|---|---|---|
| O-1 | Persistence representation of manual expense vs financial transaction | §6.3 |
| O-5 | Rounding mode, allocation of remainders, and the display precision of `Money`. Narrowed by M2, not closed: **ledger persistence and posting operate on already-resolved integer minor units and perform no rounding**, so the policy belongs to whichever domain initiates a split | §17A M0-5, §17C M2-16 |
| O-6 | Client-side handling of the new `{"error": {...}}` object shape, which differs from the legacy `{"error": "<string>"}` the mobile helper expects | §17A M0-6 |
| O-7 | Whether legacy `TextField`-as-JSON columns move to `JSONField` now that PostgreSQL is available | §4.1 |
| O-8 | What triggers provisioning in the product — M1 built the service, not the moment it is called | §17B M1-2 |
| O-9 | Whether `restricted` becomes an account state, or restriction stays purely capability-driven at M9 | §17B M1-4 |
| O-10 | Whether a customer may ever hold more than one financial account or product type | §17B M1-3 |
| O-11 | Which currencies beyond NGN are operational, and what opens a non-NGN wallet | §17B M1-6 |
| O-12 | Whether Django admin gains read-only money-core visibility, or that waits for the M11 ops surface | §17B |
| O-13 | **The accounting classification used for the customer-wallet representation itself**, and the counterpart taxonomy facing it — sponsor-bank cash, provider settlement, trust structure. Both follow from the custody, provider and accounting design. M2 supports the conventional taxonomy but takes no position on which class a wallet uses | §17C M2-4, M2-5 |
| O-14 | Whether wallet ledger accounts are opened at provisioning or on first posting; M2 built the service but wires it to no trigger | §17C M2-3 |
| O-15 | Whether ledger immutability is eventually hardened with database triggers, or continues to rest on the ORM boundary | §17C M2-13 |
| O-16 | Whether reversal-of-reversal is ever needed; M2 refuses it | §17C M2-14 |
| O-17 | **Default hold duration.** M3 sets none: `expires_at` is optional and has no default, because how long a reservation should live depends on transfer timeout windows and provider reservation semantics that are not decided | §17D M3-4 |
| O-18 | Whether a wallet may ever carry a negative posted balance (overdraft). M3 invents none and rejects holds against one | §17D M3-12 |
| O-19 | Whether and when the posted/held/available projection is exposed to product surfaces, and in what shape | §17D M3-18 |
| O-20 | Whether hold expiry ever gains a background sweeper. M3 needs none — correctness is read-time — so this is housekeeping preference, not correctness | §17D M3-4 |
| O-21 | Whether hold release gains idempotent retry semantics. **Narrowed by M4, not closed:** M4 introduced idempotency at the level of *transaction intent* only, and deliberately did not extend it to hold release, which still raises `hold_not_active` on a second call | §17D M3-13, §17E M4-16 |
| O-22 | **How an UNKNOWN transaction is eventually resolved.** M4 makes the state safe, persistent and reservation-preserving, but nothing in M4 resolves one: whether resolution arrives by provider polling, webhook, reconciliation sweep or manual operations is M6/M7 | §17E M4-5, M4-7 |
| O-23 | How long a transaction may remain UNKNOWN before operational escalation, and what that escalation is. M4 imposes no time limit — an unresolved operation stays unresolved rather than being concluded on a timer | §17E M4-5 |
| O-24 | The `failure_code` vocabulary. M4 stores a normalised internal code but defines no values; provider errors are first mapped in M6 | §17E M4-22 |
| O-25 | Whether a reservation may exceed the transaction principal in practice, and the fee arithmetic that would justify it. M4 permits the difference structurally and computes nothing | §17E M4-14 |
| O-26 | Whether and how transaction state is exposed to product surfaces, and what shape that takes. M4 exposes none | §17E M4-23 |
| O-27 | The fresh-intent policy for a new operation after a terminal failure — whether the product generates a fresh idempotency key or requires the caller to. `FAILED` is terminal and remains historical; M4 requires a new key for a genuinely new attempt, which is what stops a retry loop from spending twice, and **invents no retry semantics of its own**. M5 defines the policy | §17E M4-16 |
| O-2 | Rounding mode and remainder allocation for authoritative money | §7 |
| O-3 | Reports' eventual placement | §12 |
| O-4 | Whether NativeTabs can achieve the target glass treatment on the pinned Expo version | §13.1 |

---

## 19. Change control

- **LOCKED** items change only by an explicit recorded decision that updates this
  document first. Code never leads a locked decision.
- **DEFERRED** items are closed by a decision, not by a plausible value appearing
  in a config file.
- **OPEN** items are settled in the milestone that needs them, with the reasoning
  recorded there.
- **IMPLEMENTATION PROPOSAL** content is never cited as a requirement.
