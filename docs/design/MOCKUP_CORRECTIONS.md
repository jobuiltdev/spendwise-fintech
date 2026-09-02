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

Reviewed 2026-09-02 against the nine approved reference images. §3 remains the
standing rejection register; this section records where each invention actually
appears.

**Every row is a "do not build as shown" instruction**, regardless of the fact
that it appears in an approved image.

### 6.1 `00-VISUAL-SOURCE-OF-TRUTH-home.png` — visual anchor

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 00-a | Copy typo: "SpeteirlWise insight" | Defect | "SpendWise insight" |
| 00-b | Balance hide/show (eye) affordance | Unspecified | Not in the written spec. Not approved by appearing here; decide deliberately or omit. |
| 00-c | "Netflix · Subscription" row label | Caution | Acceptable as a category label. Must not grow into subscription-management or autopay framing (R11.3). |

Otherwise conformant, and binding for visual direction per `README.md` §5.2:
Total balance / Available / Being confirmed, typography-led balance, no gradient
wallet card, Send/Receive/More, Recent Activity above spending summary, one
insight, four root tabs, `Confirming` shown as a distinct labelled state.

### 6.2 `01-auth-onboarding.png`

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 01-a | "Bank-level security. Your data is protected." | **REJECTED** — C4 | No unsupported security claims |
| 01-b | Login field "Phone number or email" | **Contradiction** | Written spec requires **phone for login** (`SPENDWISE_UI_UX_SPEC.md` §13) |

Conformant: phone → OTP → name + email → password ordering; PIN setup prompt;
no bottom navigation in any auth screen; first-time Home shows an **activation
path**, not a fake active account (R1.8).

### 6.3 `02-verification-kyc.png`

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 02-a | "Daily transfer limit ₦200,000.00"; tier limits ₦200,000 / ₦1,000,000 / ₦5,000,000; "New daily limit ₦1,000,000.00" | **REJECTED** — C1 | Limits are backend/configuration driven; exact values **DEFERRED** |
| 02-b | Named regulator and compliance assurance copy | **DEFERRED** | Compliance rules and disclosures are deferred; no regulatory claim ships unreviewed |
| 02-c | Deterministic verification stages ("Checking… / Validating… / Finalizing") | **Caution** | The locked ban on fabricated stages (R5.10) is written for transfers. Do not extend invented deterministic stages to verification either — show only backend-authoritative progress |
| 02-d | Tier names "Basic / Verified / Premium" | **Unapproved naming** | Written spec defines Tiers 0–3 by baseline (pre-KYC / NIN / BVN / proof of address), not by these names |
| 02-e | "This usually takes a few hours" / "a few seconds" | **Caution** — C3 | Timing expectations only where backend-supplied |

Conformant: verification states cover processing, manual review, and
mismatch/rejected. **Missing**: distinct "unable to verify" and
"provider/service unavailable" states (`SPENDWISE_UI_UX_SPEC.md` §14.3).

### 6.4 `03-send-transfer.png`

Best conformance to the locked transfer semantics of any reference.

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 03-a | "Transfers are secured and encrypted…" | **Caution** — C4 | Avoid security assurance copy |
| 03-b | `Confirming`: "This usually takes a few minutes." | **Caution** | `Confirming` may be longer-lived (R5.7). No timing promise without a backend estimate |

Conformant and worth preserving: Amount → Review → PIN → Processing → Successful;
available balance visible at Amount entry; Review shows recipient name, bank,
masked account, amount, fee, total; `Confirming` has **no Retry**, explains
confirmation with the receiving bank, states the app may be safely closed, and
documents later resolution to Successful or Failed.

### 6.5 `04-receive.png` — highest density of rejected inventions

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 04-a | **QR code** on Receive | **REJECTED** — not in V1 (§4) | V1 Receive: account name, account number, bank/provider name, Copy, Share |
| 04-b | **"Request money"** screen with amount, note, "Send request" | **REJECTED** — not in V1 (§4) | No Request Money protocol in V1 |
| 04-c | **Payment handle** "Nickname @josephedward" | **REJECTED** — not in V1 (§4) | No handles/links in V1 |
| 04-d | **"Save as draft — Save and continue later"** | **REJECTED** — T4 | No draft transfer concept |
| 04-e | **Post-submit insufficient balance**: Processing → Failed → "Why it failed: Insufficient balance" | **REJECTED** — T3 | Catch at Amount, before PIN and provider submission |
| 04-f | The insufficient-balance figures are self-contradictory (available ₦218,600 vs required ₦30,000) | Defect | Fabricated data; do not copy any figure from a mockup |
| 04-g | "protected by bank-level security" | **REJECTED** — C4 | No such claim |

Note: "Try again" on an **authoritative** failure is acceptable — the ban is on
Retry during `Confirming` (R5.8), which this reference correctly omits. The
"Cancel" on recipient-account verification is verification lookup, **not** transfer
cancellation, and does not license transfer cancellation (R5.17).

### 6.6 `05-activity.png`

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 05-a | Status badge set includes **"Pending"** | **REJECTED** — T1 | Ordinary path is Processing → Successful; no routine Pending |
| 05-b | Transaction detail mixes facts and interpretation without separation | **Gap** | Separate immutable financial facts from editable SpendWise interpretation (`SPENDWISE_UI_UX_SPEC.md` §7.4) |
| 05-c | Summary totals combine datasets without stating which | **Gap** | Combining is allowed **where semantics are explicit** (R7.5) — label the dataset |
| 05-d | Derived "Savings ₦170,100" (money in − money out) | **Caution** | Must be backend-authoritative (R1.3), and must not imply a savings product that does not exist |
| 05-e | "Your data is secure and private. We never share your financial information." | **Caution** — C4 | Avoid unsupported assurance copy |

Conformant: manual expense is a distinct transaction type alongside Money in /
Money out / Transfer; "Add manual expense" remains editable; transaction detail is
a focused flow with no bottom navigation; "Report an issue" is
transaction-contextual support.

### 6.7 `06-plan-budgets-recurring.png`

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 06-a | **Numeric Financial Health score "76 / Good"** with gauge and "How we calculate this" | **REJECTED** — I1 | Qualitative explainable experience only (R10.5–R10.6) |
| 06-b | **Sub-scores**: Spend 80/100, Save 72/100, Debt 60/100, Stability 90/100 | **REJECTED** — I2 | Same |
| 06-c | Recurring detail: **"Payment method — SpendWise Wallet"** | **REJECTED** — R2 | Recurring V1 is planning only |
| 06-d | Recurring detail: **"Next payment"** and "Upcoming payments" | **REJECTED** — R4 | "Expected Sep 14", "Usually occurs around…" |
| 06-e | Recurring detail: **"Pause"** action | **REJECTED** — R3 | "Not recurring" / "Mark as recurring" |
| 06-f | "History: 12 payments made" | **REJECTED** — R1 | Implies the app made payments |
| 06-g | "Smart recurring — Never miss a bill or subscription" | **REJECTED** — R1 | Implies autopay |
| 06-h | "Upcoming bills" on Plan overview | **Caution** | Bill framing edges toward autopay implication |
| 06-i | "bank-level security" in the footer strip | **REJECTED** — C4 | No such claim |

Conformant and worth preserving: Spending overview (period total, category
breakdown, trend, compare) matches `SPENDWISE_UI_UX_SPEC.md` §10.1; the Insights
and Smart Alert screens answer what happened / why it matters / what to do
(R10.1); budget screens are advisory and focused-flow correct.

### 6.8 `08-you-security-support.png`

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 08-a | Limits: daily ₦200,000, monthly ₦2,000,000, airtime ₦20,000, bill payment ₦100,000 | **REJECTED** — C1 | Backend-driven; values **DEFERRED** |
| 08-b | **Airtime and bill-payment limits** imply airtime and bill-pay products | **REJECTED** — invention | Neither is in scope; do not introduce a product via a limits screen |
| 08-c | **"Call us +234 700 123 4567"**, "Available 24/7" | **REJECTED** — C2 | No invented contacts or availability claims |
| 08-d | "We typically reply in minutes"; "get back to you within 24 hours" | **REJECTED** — C3 | No SLAs |
| 08-e | "We use bank-level security"; "24/7 Fraud monitoring"; "Your money is always protected" | **REJECTED** — C4 | No unsupported security or fraud guarantees |
| 08-f | **"Two-factor authentication — On"** control; "2FA enabled" | **REJECTED** — A3 | Generic customer 2FA controls not in V1 |
| 08-g | **"Freeze your account" / "Confirm freeze"** | **REJECTED** — A2 | Self-freeze not in V1 |
| 08-h | Bottom navigation shown on **Support** | **REJECTED** — N1 | Support is a focused flow (R8.3) |
| 08-i | Profile photo prominent with edit affordance | **Caution** — A1 | Avatar stays optional |

Conformant: dispute structure (case number, status, timeline, related
transaction) matches `SPENDWISE_UI_UX_SPEC.md` §15; "Change password" correctly
scopes a cooldown to that specific action.

### 6.9 `09-system-edge-states-reference.png`

| # | What it shows | Verdict | Correct behaviour |
|---|---|---|---|
| 09-a | Security cooldown: "you won't be able to: **Send or receive money**" | **REJECTED** — C5 | **Do not assume receiving is disabled.** Capability-driven (`canSend`, `canReceive`, …) and **DEFERRED** |
| 09-b | System state labelled **"Degraded / Unknown"** using the copy "We're confirming your transaction" | **REJECTED** | Degraded service is **not** transaction `Confirming` (R4.6). Two states, two treatments, two copy sets |
| 09-c | The literal word **"Unknown"** as a customer-facing label | **REJECTED** — R5.4 | Backend UNKNOWN is never shown; `Confirming` is its only customer-facing expression |
| 09-d | **"Account restricted — We've restricted your account"** | **REJECTED** — R6.8 | Name the specific restricted capability; never generic lockout framing |
| 09-e | Limits: daily ₦200,000, single ₦100,000, daily spend ₦300,000 — **contradicting `08`** | **REJECTED** — C1 | Neither set is real. Backend-driven |
| 09-f | "Two-factor authentication — Off" control | **REJECTED** — A3 | Not in V1 |
| 09-g | "responds within minutes"; "Live chat — Available" | **REJECTED** — C3 | No SLAs or availability claims |
| 09-h | "Bank-level security"; "industry-standard security"; "Real human support" | **REJECTED** — C4 | No such claims |
| 09-i | "We're working with **licensed partners**…" | **DEFERRED** | No provider or partner claim; provider selection is deferred |

System-state strip (Loading / Empty / Offline / Degraded / Error) is a useful
start but **omits** stale-or-cached, restricted-capability, cooldown, verification
states, and transaction `Confirming` as distinct states (R4.1), and mislabels
degraded as Confirming (09-b).

### 6.10 Cross-reference contradictions

| # | Contradiction | Resolution |
|---|---|---|
| X-1 | `08` and `09` state different limit values for the same account | Both are fabricated. Limits are backend-driven and **DEFERRED** |
| X-2 | `08` and `09` both depict the You/Account/Security/Support surface with differing structure | They **complement** each other; `09` does not supersede `08`. `08` is the broader You/Security/Support reference; `09` is secondary and covers system/edge states. Neither is a specification — `SCREEN_CONTRACTS.md` governs |
| X-3 | `01` allows email-or-phone login; written spec requires phone | Written spec wins — phone for login |
| X-4 | `04` shows insufficient balance discovered post-submit; `03` shows available balance at Amount entry | `03` is correct — catch pre-submit (R5.16) |
| X-5 | `05` shows a `Pending` badge; `00` and `03` correctly show `Confirming` | `00`/`03` are correct — no routine Pending |

---

## 7. Reference inventory

Nine files, reviewed 2026-09-02. **The images themselves are not edited**;
corrections live in §6.

| File | Depicts | Reviewed | Verdict |
|---|---|---|---|
| `00-VISUAL-SOURCE-OF-TRUTH-home.png` | Home | ✔ | **USABLE — visual anchor** (§6.1) |
| `01-auth-onboarding.png` | Auth, onboarding, login, recovery | ✔ | USABLE IN PART (§6.2) |
| `02-verification-kyc.png` | Activation, KYC, tier upgrade | ✔ | USABLE IN PART (§6.3) |
| `03-send-transfer.png` | Send, Processing, Confirming | ✔ | USABLE IN PART — best transfer conformance (§6.4) |
| `04-receive.png` | Recipients, Receive, failure, Confirming | ✔ | USABLE IN PART — heavy inventions (§6.5) |
| `05-activity.png` | Activity, detail, insights, categories | ✔ | USABLE IN PART (§6.6) |
| `06-plan-budgets-recurring.png` | Plan, budgets, recurring, spending, health, insights | ✔ | USABLE IN PART — heavy inventions (§6.7) |
| `08-you-security-support.png` | You, security, limits, support, disputes, freeze | ✔ | USABLE IN PART — heavy inventions (§6.8) |
| `09-system-edge-states-reference.png` | System & edge states (primary purpose); secondary You/Security/Support coverage | ✔ | USABLE IN PART — heavy inventions (§6.9) |

### 7.1 Inventory observations — settled

- **There is intentionally no `07-` reference.** The sequence runs 00–06, 08–09
  by design. **Do not create one, renumber files, duplicate an image, or generate
  a replacement to fill the sequence.**
- **Groups, Home → More, stale/offline, and other incomplete states have no visual
  coverage, and that is accepted.** The written UX specification and screen
  contracts govern those surfaces. **Do not generate new mockups.**
- **`09` complements `08`; it does not supersede it.** `08` remains the broader
  You / Security / Support visual reference.
  `09-system-edge-states-reference.png` is a **secondary** visual reference for
  system and edge states, and is **especially subordinate** to the written
  system-state rules (`UX_RULES.md` §4, `SPENDWISE_UI_UX_SPEC.md` §16) and to this
  document. Where `09` depicts a system state, the written rule governs — see
  §6.9, where its two most serious entries (cooldown blocking receiving, and
  degraded service dressed as `Confirming`) are rejected.
- **All image extensions are normalised to lowercase `.png`.** `09` was also
  renamed to `09-system-edge-states-reference.png`, because its previous
  system-states name implied it was *the* system-states reference, which it is
  not — the written rules are. No other image has been renamed, and no image
  content has been edited.

---

## 8. If in doubt

If a visual reference shows something you cannot find in the governing documents:

**It does not exist yet. Do not build it. Log it.**

Then decide whether it is a **REJECTED invention** or a legitimate **NOT IN V1**
idea — and if it is worth having, get it written into the spec rather than
inferred from a picture.
