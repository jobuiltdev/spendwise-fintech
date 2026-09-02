# SpendWise Fintech — Design System

**Status:** Foundation.
**Decision source:** the approved D0–D6 decision series.
**Implementation state:** None. No token or component has been added to the
codebase.

**Read §1 before anything else.** This document contains both approved
requirements and unapproved suggestions, and it labels which is which.

---

## 1. Two kinds of statement — LOCKED

| Label | Meaning |
|---|---|
| **LOCKED UX REQUIREMENT** | Approved. Binding. |
| **IMPLEMENTATION PROPOSAL** | A suggested mechanism — a token name, a component name, a structure. **Not approved.** Never cite it as a requirement, and never present it as existing architecture. |

Sections 2–7 are **LOCKED UX REQUIREMENTS**. Section 8 is **IMPLEMENTATION
PROPOSAL** in its entirety.

---

## 2. Visual direction — LOCKED UX REQUIREMENT

The product should feel **premium, calm, intelligent, confident, trustworthy, and
financially precise**.

| Aspect | Direction |
|---|---|
| Background | White / very light neutral |
| Typography | Strong near-black |
| Accent | Restrained indigo/violet around the existing `#4F46E5` and the approved `#5B4FE9` direction |
| Density | Generous whitespace |
| Cards | Restrained |
| Elevation | Minimal |
| Root navigation | Subtle glass |
| Icons | One icon family — **Lucide direction** |
| Status | Semantic status treatment |
| Money | **Tabular numerals** |
| Spacing | **4pt foundation** |
| Primary buttons | Large, approximately **52–56px** |
| Theme | **Light-first with dark-mode support** |
| Motion | Subtle, with **reduced-motion support** |

### 2.1 Dark mode — LOCKED

**KEEP dark mode.** The product is **light-first, not light-only**. The existing
dark theme is **not removed**. Every token addition is defined in **both** modes so
that both remain structurally supported.

### 2.2 Category colours — LOCKED

**Existing curated category colours are legitimate shipped functionality and are
KEEP.** **Do not create arbitrary category-colour restrictions**, and do not
replace or remove the existing palette as part of the fintech evolution.

### 2.3 Visual anchor

The **approved Home visual direction** is the primary visual anchor for hierarchy,
density, whitespace, typography, restrained indigo usage, card treatment, and
subtle glass navigation. **Written UX rules override any screenshot**
(`MOCKUP_CORRECTIONS.md`).

Specifically: **no giant gradient wallet card**; **balance is typography-led**.

---

## 3. Existing tokens — CURRENT

Defined in `mobile/src/constants/theme.ts`. Real, in use, and **KEEP**.

### 3.1 Colour

| Token | Light | Dark |
|---|---|---|
| `text` | `#000000` | `#ffffff` |
| `textSecondary` | `#60646C` | `#B0B4BA` |
| `background` | `#ffffff` | `#000000` |
| `backgroundElement` | `#F0F0F3` | `#212225` |
| `backgroundSelected` | `#E0E1E6` | `#2E3135` |
| `border` | `#E5E7EB` | `#2E3135` |
| `primary` | `#4F46E5` | `#818CF8` |
| `primarySoft` | `#EEF2FF` | `#312E81` |
| `success` | `#16A34A` | `#4ADE80` |
| `successSoft` | `#DCFCE7` | `#14532D` |
| `warning` | `#D97706` | `#FBBF24` |
| `warningSoft` | `#FEF3C7` | `#78350F` |
| `danger` | `#DC2626` | `#F87171` |
| `dangerSoft` | `#FEE2E2` | `#7F1D1D` |

The existing `primary` `#4F46E5` sits within the approved indigo direction
alongside `#5B4FE9`. Reconciling the two is an **OPEN** implementation decision,
not a mandate to change either.

### 3.2 Spacing

`half: 2` · `one: 4` · `two: 8` · `three: 16` · `four: 24` · `five: 32` · `six: 64`

Consistent with the approved **4pt foundation**.

### 3.3 Radius

`small: 8` · `medium: 12` · `large: 20` · `pill: 999`

### 3.4 Type

`Fonts` resolves per platform to `sans` / `serif` / `rounded` / `mono`.

### 3.5 Layout

`MaxContentWidth: 800` · `BottomTabInset` (iOS 50 / Android 80 / web 90).

---

## 4. Money typography — LOCKED UX REQUIREMENT

1. **Tabular numerals** for all money.
2. **Currency always explicit** in customer-facing money display.
3. **Never truncated or abbreviated** — no `1.2k`, no clipped digits, at any size.
4. **Balance is typography-led**, not carried by a decorative container.
5. Balance terminology is exactly **Total balance**, **Available balance**, **Being
   confirmed**.
6. Money is announced accessibly in full, with its currency.

---

## 5. Status treatment — LOCKED UX REQUIREMENT

1. **Semantic status treatment** — status colour carries meaning consistently.
2. **No colour-only statuses.** Every status carries a text label.
3. **Ordinary successful transactions are not visually overwhelmed by green
   success badges.** Exceptional states are the prominent ones.
4. **`Confirming` and generic degraded service are visually and textually
   distinct.** They are different conditions with different causes.
5. **No fabricated deterministic provider stages** in any progress presentation.

---

## 6. Navigation treatment — LOCKED UX REQUIREMENT

1. The root tab bar **preserves the subtle SpendWise glass / translucent
   character**.
2. **Bottom navigation appears only on the four root destinations.** Focused flows
   use back/close hierarchy.
3. The existing **NativeTabs** implementation is **ADAPTED** where it can reliably
   achieve the target on the pinned Expo version. **It is not replaced merely
   because the API is marked unstable** — replacement requires a concrete,
   documented limitation (**OPEN**).

---

## 7. Motion, theming, and accessibility — LOCKED UX REQUIREMENT

- **Subtle motion.** Motion reinforces; it never carries sole meaning.
- **Reduced-motion support** throughout.
- No animation implies progress the system has not confirmed.
- Light and dark are both structurally supported; every new token is defined in
  both at definition time.
- Contrast is adequate in both modes.
- Adequate touch targets, semantic grouping, logical focus, keyboard handling.
- **Preserve the existing good keyboard behaviour.**

---

## 8. Implementation proposals — NOT APPROVED

> **Everything in this section is an IMPLEMENTATION PROPOSAL.** No token name,
> component name, or structure below has been approved. They are offered as one
> way to satisfy §2–§7 and must not be cited as architecture, requirements, or
> existing design-system content.

### 8.1 Possible token additions — PROPOSAL

Additive only; no existing token renamed or repurposed. Concrete values would need
to be chosen against contrast checks in both modes.

| Need (locked) | Possible token family (proposal) |
|---|---|
| Distinguish transfer `Processing` from `Confirming` presentation | `stateProcessing*`, `stateConfirming*` |
| Distinguish authoritative failure | reuse existing `danger*` |
| Mark cached / stale / offline financial data | `dataStale*`, `dataOffline*` |
| Mark unknown value (never a fake ₦0) | `dataUnknown*` |
| Restricted capability / cooldown surfaces | `capabilityRestricted*` |
| Non-production environment marker | `envSimulated*` |

Whether these belong as tokens at all — versus composition of existing
`warning*`/`danger*`/`backgroundElement` — is **OPEN**.

### 8.2 Possible component additions — PROPOSAL

| Possible component | Would satisfy |
|---|---|
| A single money-rendering primitive | §4 — tabular numerals, explicit currency, no truncation, accessible announcement |
| A balance block | §4.4–§4.5, plus the no-fake-₦0 and cached-data rules |
| A status presentation primitive | §5 — semantic, never colour-only |
| A `Confirming` surface | `UX_RULES.md` §5.3 — no Retry, no fabricated stages, survives backgrounding, resolves from backend |
| A review/confirm surface | `UX_RULES.md` R6.1–R6.5 — authoritative recipient, bank, masked account, amount, fee, total |
| A capability-restriction notice | `UX_RULES.md` R6.8 — specific capability, never "account frozen" |

**Rationale for the proposal (not a requirement):** routing money display and
status through shared primitives makes the locked rules easier to hold under
deadline pressure than repeating them per screen. Whether to build them, and what
to call them, is decided in implementation.

### 8.3 Existing components — KEEP

`card`, `primary-button`, `text-field`, `password-field`, `select-modal`,
`confirm-dialog`, `progress-bar`, `section-header`, `empty-state`, `error-state`,
`skeleton` (+ list skeletons), `toast-host`, `fab`, `avatar`, `color-picker`,
`icon-picker`, `themed-text`, `themed-view`, form primitives, and the existing
domain components for budgets, categories, expenses, groups, recurring, reports,
analytics, and dashboard.

New surfaces are assembled from these. Do not fork them or restyle them to match a
mockup.

---

## 9. Deferred and open

**DEFERRED** — awaiting provider or compliance input: provider or partner visual
treatments and attribution; verification/KYC status visual language beyond the
state names in `SPENDWISE_UI_UX_SPEC.md` §14.3.

**OPEN** — implementation decisions:

| # | Question |
|---|---|
| O-1 | Reconciling existing `#4F46E5` with the approved `#5B4FE9` direction (§3.1) |
| O-2 | Whether NativeTabs achieves the target glass treatment on the pinned Expo version (§6) |
| O-3 | Whether §8.1 needs tokens at all, and any names |
| O-4 | Whether §8.2 components are built, and any names |
| O-5 | Lucide adoption path alongside the existing `@expo/vector-icons` usage |
| O-6 | Illustration and empty-state art direction |

**Not in V1**, and not permanently excluded: card visual treatments — no card
product is in current scope.
