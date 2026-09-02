# SpendWise Fintech — UX Rules

**Status:** LOCKED unless marked otherwise.
**Decision source:** the approved D0–D6 decision series.
**Authority:** Second only to `../architecture/SPENDWISE_FINTECH_ARCHITECTURE.md`.
Wins over the UI/UX spec, screen contracts, the design system, and every visual
reference.

Every rule here is a **LOCKED UX REQUIREMENT**. Where a rule needs a mechanism to
be implemented, the mechanism is **not** specified here — see
`DESIGN_SYSTEM.md` §8 for clearly-labelled implementation proposals.

---

## 1. Financial truth — LOCKED

**R1.1** The **backend is the authoritative source** for balances, amounts, fees,
transaction facts, limits, capabilities, and verification status.

**R1.2** **No optimistic UI for money.** The client never advances a transfer's
state, never predicts a balance after an action, and never renders an outcome it
has not been told.

**R1.3** **The client never calculates authoritative money.** Not balances, not
fees, not totals on a review screen. Fees come from an **authoritative quote**.

**R1.4** **Never fabricate a money value.** No estimates, no interpolation, no
plausible placeholder that could be mistaken for a real figure.

**R1.5** **Never show a fake ₦0.** When financial data is unknown, loading, or
failed, the UI says so. `0.00` is shown only when zero is the authoritative value.

**R1.6** **Cached financial data must not silently appear current** (§4).

**R1.7** **Every displayed state maps to authoritative backend state.** A state the
client inferred — from elapsed time, a missing field, or a timeout — is a defect.

**R1.8** **Pre-financial-account customers must not see a fake active bank
account.** They see an **activation path**.

---

## 2. Money display precision — LOCKED

**R2.1** **No float arithmetic on authoritative money** in the client. Amounts
arrive exact and are formatted, never computed.

**R2.2** **Tabular numerals** for all money.

**R2.3** **Currency is always explicit** in customer-facing money display.

**R2.4** **Banking / money-core V1 is NGN.** Existing multi-currency tracker
functionality remains supported and is not removed.

**R2.5** **Never aggregate unlike currencies into a financially meaningless
total.**

**R2.6** **Never truncate or abbreviate an authoritative amount.** No `1.2k`, no
clipped digits, at any size or width.

**R2.7** **Balance terminology is exactly:** *Total balance*, *Available balance*,
*Being confirmed*. **Do not invent customer-facing terms** such as "ledger
balance" or "spendable projection".

**R2.8** **Money in and money out are distinguished by label and form**, never by
colour alone (R3.5).

---

## 3. Accessibility — LOCKED

**R3.1** Adequate touch targets.

**R3.2** Screen reader semantics on every interactive and informational element.

**R3.3** Dynamic text where practical.

**R3.4** Semantic grouping — related content is grouped for assistive technology,
not just visually.

**R3.5** **Adequate contrast, and no colour-only statuses.** Every state carries a
text label in addition to any colour.

**R3.6** **Reduced-motion support.** No meaning carried by motion alone.

**R3.7** **Logical focus order.**

**R3.8** **Keyboard handling.** The existing good keyboard behaviour in the auth
and form screens is **KEEP** — do not regress it.

**R3.9** **Accessible monetary announcements** — the full value with its currency.

---

## 4. Loading, stale, offline, and degraded — LOCKED

**R4.1** **Supported system states**, each with its own treatment: loading, empty,
offline, stale, degraded service, session expired, restricted capability,
cooldown, verification states, and transaction **Confirming**.

**R4.2** **These are distinct states.** Empty is not error. Stale is not offline.
Degraded service is not a failure.

**R4.3** **Cached financial data must not silently appear current.** Show what is
known, and show that it is cached.

**R4.4** **Never fake ₦0** while financial data is unknown or loading (R1.5).

**R4.5** **Offline money-moving operations must NEVER be queued for later
automatic submission.**

**R4.6** **Generic service degradation is NOT the same thing as a transaction
being `Confirming`.** They have different causes, different copy, and different
treatments, and must never share a presentation.

**R4.7** **Errors on financial data are surfaced**, never swallowed into an empty
state.

---

## 5. Transfer states — LOCKED

### 5.1 Normal flow

```
Recipient → Amount → Review → PIN → Processing → Successful
```

**R5.1** **Processing is normal and brief.**

**R5.2** **Do NOT routinely show a Pending or Awaiting Confirmation transfer
state.** The ordinary path ends in success.

### 5.2 Ambiguous flow

```
Processing → Confirming → Successful OR Failed
```

**R5.3** **`Confirming` is the customer-facing representation of backend
UNKNOWN.**

**R5.4** **UNKNOWN is never shown to the customer.**

**R5.5** **A timeout does NOT equal failure.**

### 5.3 Confirming — rules

**R5.6** `Confirming` **is exceptional**, not the normal path.

**R5.7** It **may be longer-lived** than Processing.

**R5.8** It has **NO Retry action**.

**R5.9** It **explains that SpendWise is confirming with the receiving bank or
provider**.

**R5.10** It **must not contain fabricated deterministic provider stages**. No
invented "Sent to bank → Bank processing → Almost there" sequence.

**R5.11** It **survives app close and backgrounding**.

**R5.12** It **resolves from authoritative backend state**.

**R5.13** It **must not resubmit the transfer**.

**R5.14** Reserved funds may be shown as **"Being confirmed"**.

### 5.4 Failure

**R5.15** **Known failure is shown only when failure is authoritative.**

**R5.16** **Insufficient balance and known limit violations should be caught at
Amount entry — before PIN and before provider submission** — wherever
authoritative information is available.

### 5.5 Cancellation

**R5.17** **No transfer cancellation semantics are approved.** Cancellation must
not appear in any screen, component, or copy unless the backend and product flow
explicitly support it.

---

## 6. Actions on money — LOCKED

**R6.1** **Review must display authoritative** recipient name, bank, masked
account, amount, fee, and total.

**R6.2** **Fees come from an authoritative quote. The frontend never calculates
authoritative fees.**

**R6.3** **PIN follows Review.** Money movement is never one tap from amount
entry.

**R6.4** **Duplicate taps must be prevented client-side and server-side.**

**R6.5** **Idempotency is required** on every money-moving request.

**R6.6** **No blind retry after an ambiguous timeout.**

**R6.7** **Limits and eligibility come from the backend.** No screen contains a
threshold, tier, or eligibility rule. Values are backend/configuration driven and
exact production limits are **DEFERRED**.

**R6.8** **Capability-driven restrictions**, not generic lockout copy. Where a
capability such as `canSend`, `canReceive`, `canChangeSecuritySettings`, or
`canUpgradeVerification` is restricted, the UI explains that specific restriction.
It never says the account is frozen. **Do not assume receiving money is disabled**
during a security cooldown.

---

## 7. Manual expense vs financial transaction — LOCKED

**R7.1** Manual expenses **remain supported, editable, and deletable**. They
**never create ledger activity** and **never become synthetic historical bank
transactions**.

**R7.2** Financial transactions **represent financial truth**, **cannot be deleted
by the customer**, and have **immutable financial facts**.

**R7.3** A financial transaction **may have editable SpendWise interpretation** —
category, note, include-in-spending. Transaction detail **separates immutable
financial facts from editable SpendWise interpretation** visually and structurally.

**R7.4** **Manual expenses must remain clearly distinguishable** in Activity — by
label and form, not colour alone.

**R7.5** SpendWise **may combine** financial transactions and manual expenses for
spending intelligence **where the semantics are explicit** — the customer can tell
what a figure covers.

**R7.6** What must **never** be blended is their **financial identity or ledger
truth**. A manual expense never contributes to a balance or any authoritative
financial figure.

---

## 8. Navigation — LOCKED

**R8.1** Authenticated root navigation is exactly **Home · Activity · Plan · You**.

**R8.2** **Bottom navigation exists ONLY on those root destinations.**

**R8.3** **Focused flows do not show root bottom navigation** — auth, onboarding,
verification/KYC, Send, recipient selection, amount, review, PIN,
processing/result, transaction detail, budget creation, security recovery,
disputes. They use a **back/close hierarchy**.

**R8.4** The root tab bar **preserves the subtle SpendWise glass/translucent
character**.

**R8.5** **Insights are not a fifth root tab.** A deeper Insights destination may
be reachable from Home.

**R8.6** **Groups remain a focused/root-stack flow without bottom navigation.**
Preferred future discovery is **Home → More → Groups**.

**R8.7** **Reports do not require a root tab.** Likely future placement is
secondary access from Activity / More or another contextual destination; exact
placement may be refined during implementation.

---

## 9. Home — LOCKED

**R9.1** Priority order:

1. customer / context
2. balance when financially activated
3. **Send / Receive / More**
4. recent activity
5. spending summary
6. one contextual SpendWise insight

**R9.2** **Recent Activity appears ABOVE spending summary.**

**R9.3** **Do not use a giant gradient wallet card.**

**R9.4** **Balance is typography-led.**

**R9.5** **Pre-financial-account customers see an activation path**, not a fake
active bank account (R1.8).

**R9.6** **Never show a fake ₦0** while financial data is unknown or loading.

---

## 10. Insights and intelligence — LOCKED

**R10.1** Insights answer three questions: **What happened? Why does it matter?
What can the customer do?**

**R10.2** **Rules-first intelligence is authoritative.** Optional LLM wording may
be layered on later.

**R10.3** **LLMs must never invent** balances, transaction facts, risk facts,
account status, or any financial truth.

**R10.4** **Insights are contextual throughout the app.**

**R10.5** **Do NOT expose an invented numeric Financial Health score**, and do not
expose invented numeric sub-scores for debt, saving, stability, or similar. The
old numeric customer-facing score is **removed from the customer UX**.

**R10.6** **Use a qualitative, explainable experience instead** — for example
*Good*, *You're doing well this month*, *Areas to watch*, *Strengths*,
*Recommendations*. Existing useful qualitative narrative may be retained.

**R10.7** **Anomaly detection is KEEP** where it is explainable. It is not retired
alongside the numeric score, and it may feed contextual SpendWise Insights.

---

## 11. Recurring — LOCKED

**R11.1** Recurring V1 is **planning and intelligence only. It is NOT automatic
payment or autopay.**

**R11.2** Allowed concepts: expected amount, cadence, next expected date, detected
recurring behaviour, *Mark as recurring*, *Not recurring*, *Expected Sep 14*,
*Usually occurs around…*.

**R11.3** **Do NOT imply** *Pay automatically*, *Payment method*, *Pause payment*,
or *Next debit*.

**R11.4** Avoid wording such as *Subscription automation* where it could imply
automated bank debiting.

**R11.5** Existing expense-generation functionality may remain **where it creates
expense records rather than financial payments**.

---

## 12. Budgets — LOCKED

**R12.1** Budgets are **advisory / planning only**.

**R12.2** **Budgets NEVER block banking transactions.**

**R12.3** **Banking limits and budgeting are separate systems.**

---

## 13. Copy — LOCKED

**R13.1** Say what is true. Never imply certainty the system does not have.

**R13.2** **No fabricated provider stages** in any progress copy (R5.10).

**R13.3** Errors say what happened, whether money moved, and what to do next.

**R13.4** **Do not invent** support phone numbers, 24/7 availability, response
SLAs, resolution SLAs, or unsupported fraud/security guarantees.

**R13.5** **No unsupported security marketing claims** — including "bank-level
security".

**R13.6** Ordinary successful transactions **should not be visually overwhelmed by
green success badges**. Exceptional states are the prominent ones.

---

## 14. Support and disputes — LOCKED

**R14.1** Support is **transaction-contextual where possible**.

**R14.2** A durable case carries: **case/reference, status, timeline, related
transaction**.

**R14.3** Nothing in R13.4 may be invented to fill a support screen.

---

## 15. Forbidden UX patterns — LOCKED

| # | Forbidden | Rule |
|---|---|---|
| F1 | Root bottom navigation inside a focused flow | R8.3 |
| F2 | Routine Pending / Awaiting Confirmation transfer state | R5.2 |
| F3 | Fabricated deterministic provider stages | R5.10 |
| F4 | A Retry action on `Confirming` | R5.8 |
| F5 | Treating a timeout as failure | R5.5 |
| F6 | Showing backend UNKNOWN to the customer | R5.4 |
| F7 | Resubmitting a transfer from a `Confirming` screen | R5.13 |
| F8 | Insufficient-balance failure surfaced after submission when it was catchable at Amount | R5.16 |
| F9 | Transfer cancellation semantics | R5.17 |
| F10 | Fake ₦0 while a value is unknown or loading | R1.5 |
| F11 | Cached financial data presented as current | R4.3 |
| F12 | Queuing a money-moving operation while offline | R4.5 |
| F13 | Generic service degradation presented as transaction `Confirming` | R4.6 |
| F14 | Client-calculated authoritative fees, balances, or totals | R1.3 |
| F15 | Invented balance terminology | R2.7 |
| F16 | Customer deletion of a financial transaction | R7.2 |
| F17 | A manual expense contributing to a balance or ledger figure | R7.6 |
| F18 | An invented numeric Financial Health score or sub-score | R10.5 |
| F19 | Autopay implication in Recurring | R11.3 |
| F20 | Budgets blocking a banking transaction | R12.2 |
| F21 | Hardcoded KYC or transaction limits, tiers, or provider names | R6.7 |
| F22 | Invented SLAs, support numbers, or security claims | R13.4, R13.5 |
| F23 | Assuming a security cooldown blocks receiving money | R6.8 |
| F24 | Colour-only status | R3.5 |
| F25 | Abbreviated or truncated authoritative money | R2.6 |

---

## 16. Deferred and out-of-current-scope

**DEFERRED** — awaiting provider, compliance, or investor input:

- Provider and KYC vendor identity, and anything derived from them
- Exact production limits, thresholds, and compliance rules
- Cooldown capability restrictions (configurable)
- KYC tier requirements and monetary limits (subject to validation)
- Settlement-time expectations not supplied by the backend

**Not in V1** — recorded as out of current scope, **not** as permanent product
exclusions:

- QR receiving
- Request Money protocol
- Payment handles / payment links
- Generic customer 2FA controls (TOTP, passkeys)
- Self-freeze
- Goals (introduced deliberately later, in Plan)

These may be designed later by deliberate decision. Until then they do not appear
in any screen, component, or mockup.
