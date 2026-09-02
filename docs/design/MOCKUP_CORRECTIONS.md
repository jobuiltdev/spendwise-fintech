# SpendWise Fintech — Mockup Corrections

**Status:** Foundation. Rules are LOCKED. The rejection register below is
populated with known inventions.
**Decision source:** the approved D0–D6 decision series.
**Purpose:** Record what visual references get wrong, so it is never mistaken for
a requirement.

---

## 1. The authority rule — LOCKED

**The written specification is authoritative over every visual reference.**

Mockups, renders, screenshots, and generated images are aids to conversation. They
are not requirements and not a source of scope. When a picture disagrees with the
spec, **the picture is wrong** — it is recorded here, not built.

### 1.1 The Home visual anchor — LOCKED

**The approved Home visual direction is the primary visual anchor** for:

- hierarchy
- density
- whitespace
- typography
- restrained indigo usage
- card treatment
- subtle glass navigation

**But written UX rules override any screenshot.** The Home reference settles *how
it should look and feel*. It never settles *what the product does*, what states
exist, what the copy claims, or what a number means.

### 1.2 Governing documents, in order

1. `../architecture/SPENDWISE_FINTECH_ARCHITECTURE.md`
2. `UX_RULES.md`
3. `SPENDWISE_UI_UX_SPEC.md`
4. `SCREEN_CONTRACTS.md`
5. `DESIGN_SYSTEM.md`
6. **This file**
7. `references/` — pictures. Lowest authority. Never binding.

---

## 2. What a mockup invention is

Anything appearing in a visual reference that is **not** specified in the
governing documents: a feature, a number, a state, a limit, a claim, or a
navigation behaviour.

**An invention creates no obligation.** It is not a nice-to-have and does not go
in a backlog. It is recorded here and ignored.

### 2.1 Invention vs out-of-scope — LOCKED

Two different verdicts, and they must not be confused:

| Verdict | Meaning |
|---|---|
| **REJECTED — invention** | Contradicts a locked decision, or fabricates fact. Never build it as shown. |
| **NOT IN V1** | A legitimate idea that is simply not current scope. **Not a permanent product ban.** May be designed later by deliberate decision. Until then it does not appear in any screen, component, or mockup. |

---

## 3. Rejected mockup inventions — register

Known inventions identified in review. Each is rejected wherever it appears.

### 3.1 Navigation

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| N1 | **Bottom navigation shown inside focused flows** | Root bottom navigation exists only on the four root destinations | `UX_RULES.md` R8.2–R8.3 | Focused flows (auth, onboarding, verification, Send, recipient, amount, review, PIN, processing/result, transaction detail, budget creation, security recovery, disputes, groups) use a back/close hierarchy |

### 3.2 Transfer and money movement

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| T1 | **Routine Pending / Awaiting Confirmation transfer state** | The ordinary path is Processing → Successful; Processing is normal and brief | R5.1–R5.2 | Do not routinely show a pending state |
| T2 | **Fabricated provider processing stages** (e.g. "Sent to bank → Bank processing → Almost there") | Invents deterministic backend behaviour that does not exist | R5.10 | Confirming explains that SpendWise is confirming with the receiving bank/provider, and nothing more |
| T3 | **Insufficient-balance failure shown after submission** when it was catchable pre-submit | Wastes the customer's PIN entry and provider submission on a knowable failure | R5.16 | Catch insufficient balance and known limit violations at Amount, before PIN and provider submission, where authoritative information is available |
| T4 | **Save Transfer as Draft** | No draft transfer concept is approved | `SPENDWISE_UI_UX_SPEC.md` §5 | The approved flow is Recipient → Amount → Review → PIN → Processing → Successful |
| T5 | **Transfer cancellation affordance** | No cancellation semantics are approved | R5.17 | Cancellation appears only if the backend and product flow explicitly support it |
| T6 | **Retry on Confirming** | Confirming has no Retry, and blind retry after an ambiguous timeout is forbidden | R5.8, R6.6 | Confirming resolves from authoritative backend state and never resubmits |

### 3.3 Financial data and system states

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| S1 | **Generic degraded-service state treated as transfer Confirming** | Different conditions with different causes | R4.6 | Distinct treatments and distinct copy |
| S2 | **Fake ₦0 while financial data is unknown or loading** | Presents an unknown as an authoritative zero | R1.5 | Explicit unknown/loading treatment |
| S3 | **Cached financial data shown as current** | Misrepresents freshness | R4.3 | Mark cached data as cached |

### 3.4 Recurring

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| R1 | **Automatic recurring payments** | Recurring V1 is planning and intelligence only — not autopay | R11.1 | Expected amount, cadence, next expected date, detected behaviour |
| R2 | **Recurring payment-method UI** | Implies automated bank debiting | R11.3 | No payment method on a recurring item |
| R3 | **Pause Payment** | Same | R11.3 | "Not recurring" / "Mark as recurring" |
| R4 | **Next Debit language** | Same | R11.3 | "Expected Sep 14", "Usually occurs around…" |

### 3.5 Intelligence and analytics

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| I1 | **Invented numeric Financial Health score** | Not an approved customer-facing artefact | R10.5 | Qualitative explainable experience — *Good*, *You're doing well this month*, *Areas to watch*, *Strengths*, *Recommendations* |
| I2 | **Invented health sub-scores** (debt, save, stability, etc.) | Same | R10.5 | Same |

Note: **anomaly detection is KEEP** and is not retired alongside the numeric
score. It may feed contextual insights (R10.7).

### 3.6 Design system

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| D1 | **Arbitrary category-colour replacement or removal** | Existing curated category colours are legitimate shipped functionality | `DESIGN_SYSTEM.md` §2.2 | KEEP the existing palette; impose no arbitrary colour restrictions |
| D2 | **Giant gradient wallet card** | Contradicts the approved balance treatment | `UX_RULES.md` R9.3–R9.4 | Balance is typography-led |
| D3 | **Green success badges dominating ordinary transactions** | Exceptional states should be the prominent ones | R13.6 | Restrained treatment for ordinary success |

### 3.7 Limits, compliance, and claims

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| C1 | **Invented KYC or transaction limits** | Exact requirements and monetary limits must not be hardcoded; production limits are DEFERRED | R6.7 | Backend/configuration driven; rendered from what the backend says |
| C2 | **Invented support phone numbers** | Fabricated contact detail | R13.4 | Transaction-contextual support with case, status, timeline, related transaction |
| C3 | **Invented SLAs** (response or resolution) | Fabricated commitment | R13.4 | No SLA claims |
| C4 | **Unsupported security or "bank-level security" marketing claims** | Unsupported guarantee | R13.5 | No such claims |
| C5 | **Assuming a security cooldown blocks receiving money** | Capability restrictions are configurable and deferred; receiving is not assumed disabled | R6.8 | Capability-driven copy — `canSend`, `canReceive`, `canChangeSecuritySettings`, `canUpgradeVerification` — never "account frozen" |

### 3.8 Account and profile

| # | Invention | Why it is wrong | Rule | Correct behaviour |
|---|---|---|---|---|
| A1 | **Mandatory profile photo** | Not an approved requirement | `SPENDWISE_UI_UX_SPEC.md` §13 | Avatar remains optional as today (KEEP) |
| A2 | **Self-freeze presented as an assumed V1 feature** | Not in current scope | §16 below | Not shown in V1 |
| A3 | **Generic customer 2FA controls not actually in V1** | Customer-facing V1 does not require speculative generic TOTP/passkey settings | `SPENDWISE_UI_UX_SPEC.md` §13 | Not shown in V1 |

---

## 4. Not in V1 — scope, not prohibition — LOCKED

These are **not banned**. They are simply not current scope, and they must not
appear in a screen, component, or mockup until deliberately designed.

| Capability | Status |
|---|---|
| QR receiving | Not in V1 |
| Request Money protocol | Not in V1 |
| Payment handles / payment links | Not in V1 unless deliberately designed later |
| Generic customer 2FA controls (TOTP, passkeys) | Not in V1 |
| Self-freeze | Not in V1 |
| Goals (in Plan) | Introduced deliberately later |
| Cards | Not in current scope |

Anything else that has never been specified is simply **not yet specified** —
record it here, ask, and let the answer be written into the spec.

---

## 5. Reference intake process — LOCKED

Every reference added to `references/` is reviewed **before** anyone builds from
it.

1. **Log it** in §7 with its filename and what it depicts.
2. **Check it** against §3, §4, and the governing documents.
3. **Record every discrepancy** in §6 — one row each, with the verdict from §2.1.
4. **Mark the reference's verdict**: `USABLE` (with corrections noted),
   `USABLE IN PART`, or `REJECTED`.
5. If a reference contains something genuinely worth adopting, **write it into the
   spec first**. The picture never becomes the record.

A reference that has not been through this process is not a basis for
implementation.

---

## 6. Per-reference correction log

Filled as references arrive. §3 stands as the standing rejection register in the
meantime.

| # | Reference | What it shows | Verdict | Rule | Correct behaviour |
|---|---|---|---|---|---|
| — | *(no reference files supplied yet)* | — | — | — | — |

---

## 7. Reference inventory

`references/` is currently empty.

| File | Depicts | Reviewed | Verdict |
|---|---|---|---|
| — | *(none)* | — | — |

---

## 8. If in doubt

If a visual reference shows something you cannot find in the governing documents:

**It does not exist yet. Do not build it. Log it.**

Then decide whether it is a **REJECTED invention** or a legitimate **NOT IN V1**
idea — and if it is worth having, get it written into the spec rather than
inferred from a picture.
