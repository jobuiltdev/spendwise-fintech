# SpendWise Fintech — Design Documentation

**Status:** Foundation. No design has been implemented. M0 has not begun.
**Decision source:** the approved D0–D6 decision series.

This directory holds the **authoritative** design and UX definition for the
SpendWise fintech product.

---

## 1. The authority rule — LOCKED

**The written specification is authoritative over every visual reference.**

Mockups, renders, screenshots, and generated images help people *feel* a
direction. They do not define the product. When a picture disagrees with these
documents, **the picture is wrong** — it is recorded in `MOCKUP_CORRECTIONS.md`,
not built.

The **approved Home visual direction is the primary visual anchor** for hierarchy,
density, whitespace, typography, restrained indigo usage, card treatment, and
subtle glass navigation. **Written UX rules still override any screenshot.**

### Order of precedence

1. `../architecture/SPENDWISE_FINTECH_ARCHITECTURE.md`
2. `UX_RULES.md`
3. `SPENDWISE_UI_UX_SPEC.md`
4. `SCREEN_CONTRACTS.md`
5. `DESIGN_SYSTEM.md`
6. `MOCKUP_CORRECTIONS.md`
7. `references/` — pictures. Lowest authority. Never binding.

---

## 2. Documents

| File | What it is |
|---|---|
| `SPENDWISE_UI_UX_SPEC.md` | The product's UX definition: direction, navigation, and every surface. |
| `DESIGN_SYSTEM.md` | Approved visual direction and existing tokens, plus clearly-separated implementation proposals. |
| `SCREEN_CONTRACTS.md` | The screen-contract template and the register of screens requiring one. |
| `UX_RULES.md` | Binding cross-cutting rules. |
| `MOCKUP_CORRECTIONS.md` | The register of rejected mockup inventions. |
| `references/` | Non-binding visual references. |

---

## 3. Two distinctions that govern this directory — LOCKED

### 3.1 LOCKED UX REQUIREMENT vs IMPLEMENTATION PROPOSAL

| Label | Meaning |
|---|---|
| **LOCKED UX REQUIREMENT** | An approved decision. Binding. |
| **IMPLEMENTATION PROPOSAL** | A suggested mechanism — a token name, a component name, a structure. **Not approved.** Never cite it as a requirement. |

A locked requirement says *what must be true*. It does not automatically approve a
particular token, component, table, or identifier scheme for achieving it.
Proposed names are proposals until someone decides otherwise.

### 3.2 "Not in V1" is not "never"

Capability that is out of current scope is recorded as **out of current V1
scope** — never as a permanent product prohibition. QR receiving, Request Money,
payment handles, generic customer 2FA controls, self-freeze, cards, and similar
are not banned; they are simply not now.

---

## 4. Standing rules restated

- **The original SpendWise repository is preserved.** `C:/Users/Joeyy/dev/spendwise`
  must remain untouched by fintech development. This repository is the
  intentional evolution from `pre-fintech-baseline`.
- **This is not a greenfield rewrite.** Every surface carries a
  **KEEP / ADAPT / REPLACE / REMOVE / ADD** disposition; **prefer KEEP and
  ADAPT**, and **REPLACE requires concrete justification**.
- **Do not invent product scope.** If a capability is not specified here, it does
  not exist yet.
- **Manual expenses are not financial transactions** — a locked *domain*
  invariant. Persistence representation is an implementation decision, not fixed
  here.
- **Transfer semantics** are the locked normal and ambiguous flows in
  `UX_RULES.md` §5. `Confirming` is the customer-facing representation of backend
  UNKNOWN; timeout is not failure.
- **Mark deferrals as deferrals.** No provider names, no KYC limits, no SLAs, no
  production thresholds.
- **Distinguish CURRENT from TARGET** in every statement about a screen.

---

## 5. `references/` — Visual Reference Index

Approved prototype / reference images. **All are visual references. None is a
literal implementation specification.**

### 5.1 Authority hierarchy — LOCKED

```
1. Written UX / product rules      ← UX_RULES.md, SPENDWISE_UI_UX_SPEC.md,
                                     SCREEN_CONTRACTS.md, and the architecture doc
2. MOCKUP_CORRECTIONS.md           ← what the pictures got wrong
3. Approved Home visual anchor     ← 00-VISUAL-SOURCE-OF-TRUTH-home.png
4. Other prototype / reference images
```

Each level **overrides everything below it**. A reference image never overrides a
written rule, and never overrides a correction entry.

### 5.2 The Home visual anchor — LOCKED

`00-VISUAL-SOURCE-OF-TRUTH-home.png` is **authoritative for visual direction
only**:

- hierarchy
- density
- whitespace
- typography
- restrained indigo usage
- card treatment
- subtle glass root navigation

**It is NOT allowed to override written behavioural or product rules.** It settles
how the product should *look and feel*. It never settles what the product *does*,
what states exist, what a control is allowed to do, what copy may claim, or what a
number means. Where it conflicts with a written rule — including its own visible
copy — the written rule wins.

### 5.3 Index

Authority level is one of: **Visual anchor** (§5.2) · **Visual reference**
(direction only, not a spec).

| File | Surface / journey | Authority | Correction notes |
|---|---|---|---|
| `00-VISUAL-SOURCE-OF-TRUTH-home.png` | Home — balance, Send/Receive/More, recent activity, spending summary, insight, root tab bar | **Visual anchor** — binding for visual direction only | Strongly conformant: Total balance / Available / Being confirmed, typography-led balance, no gradient wallet card, Recent Activity above spending summary, four root tabs, `Confirming` shown distinctly. Contains a copy typo ("SpeteirlWise insight") — do not reproduce. Balance hide/show affordance is not in the written spec — unspecified, not approved. |
| `01-auth-onboarding.png` | Auth: welcome, phone signup, OTP, details, password, activation prompts; login; password recovery | Visual reference | Contains **"Bank-level security"** claim — rejected (C4). Login shows "Phone number or email"; written spec requires **phone for login** (`SPENDWISE_UI_UX_SPEC.md` §13). Correctly shows an **activation path**, not a fake bank account. |
| `02-verification-kyc.png` | Financial activation, NIN verification, processing, manual review, tier upgrade | Visual reference | Contains **invented KYC limits and tier limits** (₦200,000 / ₦1,000,000 / ₦5,000,000) — rejected (C1). Names a regulator and uses **deterministic verification stages**; both **DEFERRED**. Tier names ("Basic / Verified / Premium") are not approved. Timing promises ("a few hours") approach an SLA claim (C3). |
| `03-send-transfer.png` | Send — amount, review, PIN, processing, success; Confirming and later resolution | Visual reference | **Best conformance to the locked transfer semantics.** Review shows recipient, bank, masked account, amount, fee, total. `Confirming` has no Retry, explains confirmation with the receiving bank, states the app may be closed. Cautions: security/encryption claim, and a timing promise on `Confirming`. |
| `04-receive.png` | Recipient selection, add/verify recipient, Receive details, request; known-failure path; Confirming | Visual reference | **Highest density of rejected inventions.** Contains **QR receiving**, **Request Money**, a **payment handle** (`@nickname`), **Save as draft**, and a **post-submit insufficient-balance failure** — all rejected. Also "bank-level security". See `MOCKUP_CORRECTIONS.md` §6. |
| `05-activity.png` | Activity feed, filters, transaction detail, merchant, insights, export, categories, manual expense, summary | Visual reference | Contains a **routine `Pending` status badge** — rejected (T1). Transaction detail does not visibly separate immutable financial facts from editable interpretation (`SPENDWISE_UI_UX_SPEC.md` §7.4). Combined spending totals need explicit dataset semantics. A derived "Savings" figure must be backend-authoritative. Good: manual expense is a distinct transaction type. |
| `06-plan-budgets-recurring.png` | Plan, budgets, recurring, spending overview, financial health, insights | Visual reference | Contains the **numeric Financial Health score (76) and sub-scores** (Spend/Save/Debt/Stability) — rejected (I1, I2). Recurring shows **Payment method**, **Next payment**, **Pause**, and "12 payments made" — rejected (R2, R3, R4). "Never miss a bill" implies autopay. Spending overview and Insights sections are conformant. |
| `08-you-security-support.png` | You, account, security, devices, limits, notifications, support, FAQs, disputes, cooldown, freeze | Visual reference | Contains **invented limits**, an **invented support phone number**, **24/7 availability**, **response/resolution SLAs**, **"bank-level security" and fraud-monitoring claims**, a **generic 2FA control**, and **self-freeze** — all rejected. Shows bottom navigation on Support, which is a focused flow (N1). Disputes structure (case, status, timeline, related transaction) is conformant. |
| `09-system-edge-states-reference.png` | System & edge states (primary purpose); secondary You/Security/Support coverage — complements `08`, does not supersede it | Visual reference — **secondary**, especially subordinate to the written system-state rules | Contains **cooldown blocking "Send or receive money"** — rejected (C5); **degraded/unknown state using `Confirming` copy** — rejected; the literal word **"Unknown"** as a state label (R5.4); **generic "Account restricted"** framing (R6.8); invented limits that **contradict those in `08`**; 2FA control; SLA and security claims; a "licensed partners" claim. System-state strip is useful but omits stale/cached, restricted-capability, cooldown, and verification as distinct states. |

### 5.4 Rules for using these references

- **Nothing here is a requirement.** Direction only.
- **Do not reproduce anything listed in `MOCKUP_CORRECTIONS.md` §3 or §6**, even
  though it appears in an approved reference image. Appearing in a picture is not
  approval.
- A reference that contradicts the spec is corrected in `MOCKUP_CORRECTIONS.md`,
  not obeyed.
- If a picture captures a real decision, that decision is **written into the
  spec**; the picture keeps no authority of its own.
- **Do not edit the images.** Corrections are recorded in writing.

### 5.5 Implementation agents: do not reproduce these

Every item below appears in at least one approved reference image and is
**rejected**. Building it because "the mockup shows it" is a defect.

- bottom navigation inside focused flows
- QR receiving
- Request Money
- Save Transfer as Draft
- routine `Pending` / `Awaiting Confirmation` state
- fabricated provider processing stages
- post-submit insufficient-balance failure where it is catchable pre-submit
- automatic recurring payments
- recurring payment-method UI
- Pause Payment
- Next Debit / next-payment framing
- numeric Financial Health score or sub-scores
- invented KYC or transaction limits
- invented support contacts or SLAs
- unsupported security claims ("bank-level security", fraud-monitoring guarantees)
- generic 2FA controls
- security cooldown automatically blocking **receiving**
- mandatory profile photo
- assumed self-freeze
- degraded service presented as transaction `Confirming`

### 5.6 Accepted gaps — LOCKED

These are **settled decisions**, not outstanding work.

- **There is intentionally no `07-` reference.** The sequence runs 00–06, then
  08–09, by design. **Do not create one, renumber files, duplicate an image, or
  generate a replacement to fill the sequence.**
- **Groups, Home → More, stale/offline, and other incomplete states have no visual
  coverage, and that is acceptable.** The written UX specification and screen
  contracts govern those surfaces
  (`SPENDWISE_UI_UX_SPEC.md` §4.4, §11, §16; `SCREEN_CONTRACTS.md`).
  **Do not generate new mockups** to cover them.
- **`09` complements `08`; it does not supersede it.** `08` remains the broader
  You / Security / Support visual reference. `09` is a **secondary** visual
  reference for system and edge states, and is **especially subordinate** to the
  written system-state rules (`UX_RULES.md` §4,
  `SPENDWISE_UI_UX_SPEC.md` §16) and to `MOCKUP_CORRECTIONS.md`.
- Where `08` and `09` disagree — notably on limit values — **neither is
  authoritative**. Both sets are fabricated; limits are backend-driven and
  **DEFERRED**.

---

## 6. Status markers

| Marker | Meaning |
|---|---|
| **CURRENT** | Exists in this repository today. |
| **TARGET** | Being built. Not yet implemented. |
| **LOCKED** | Closed decision from the D0–D6 series. |
| **DEFERRED** | Awaiting investor, provider, or compliance input. |
| **OPEN** | An implementation decision not owned by these documents. |
| **IMPLEMENTATION PROPOSAL** | Suggested mechanism. Not approved. |
