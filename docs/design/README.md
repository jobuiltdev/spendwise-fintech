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

## 5. `references/`

Non-binding visual material. Rules:

- Nothing here is a requirement.
- A reference that contradicts the spec is corrected in `MOCKUP_CORRECTIONS.md`.
- If a picture captures a real decision, that decision is **written into the
  spec**; the picture keeps no authority of its own.
- References must not display fabricated financial figures, invented limits,
  provider branding, or unsupported claims without a correction entry.

The directory is currently empty.

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
