# Testing

Test infrastructure for the existing SpendWise app. This describes how to run
tests — it does not describe product behaviour or architecture (see
`docs/architecture/` and `docs/design/` for those).

---

## Backend

Django 4.2 · pytest + pytest-django · SQLite (as the app currently uses).

### Setup

```bash
cd backend
python -m venv venv                 # Python 3.12 (Django 4.2 does not support 3.13+)
venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt
```

### Commands

| Command | What it does |
|---|---|
| `venv/Scripts/python -m pytest` | Run the test suite |
| `venv/Scripts/python manage.py check` | Django system check |

Tests need **no `.env` file**. `pytest.ini` supplies default values for the
environment variables `settings.py` requires, using pytest-env's `D:` (default)
prefix so a real environment variable always wins. Running the *app* still
requires a real `.env` — copy `backend/.env.example` to `backend/.env`.

Django's own runner (`manage.py test`) still works and is unchanged.

### Location and naming

Tests live with the code they cover:

| Location | For |
|---|---|
| `backend/<app>/tests.py` or `backend/<app>/tests/` | Tests for a Django app |
| `backend/tests/` | Project-level, infrastructure, and cross-cutting characterization tests (`test_<area>.py`), with shared fixtures in `backend/tests/conftest.py` |

Discovery picks up both `test_*.py` and Django's conventional `tests.py`, so
existing per-app `tests.py` files are collected without being moved.

---

## Mobile

Expo SDK 57 · React Native 0.86 · React 19.2 · jest-expo.

### Setup

```bash
cd mobile
npm install
```

### Commands

| Command | What it does |
|---|---|
| `npm test` | Run the test suite |
| `npm run test:watch` | Run tests in watch mode |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | `expo lint` |
| `npx expo-doctor` | Verify the Expo install is coherent |

### Configuration

`jest.config.js` uses the `jest-expo` preset, which supplies the babel
transform (no `babel.config.js` needed), React Native resolution, asset
handling, and the `@/` path aliases read from `tsconfig.json`. The only addition
is a stub for stylesheet imports, since Jest has no CSS transformer and
`src/constants/theme.ts` imports `@/global.css`.

Jest globals are typed via `@types/jest`, enabled by `"types": ["jest"]` in
`tsconfig.json` — TypeScript 6 does not include ambient `@types` packages
automatically here, so the entry is required.

### Location and naming

| Location | For |
|---|---|
| `src/**/__tests__/*.test.ts(x)` | Tests grouped next to the code they cover |
| `src/**/*.test.ts(x)` | Tests beside a single module |

---

## Scope

Two kinds of test exist today.

**Infrastructure smoke tests** (`backend/tests/test_infrastructure.py`,
`mobile/src/__tests__/infrastructure.test.ts`) prove discovery, transformation,
and configuration work. They assert nothing about product behaviour.

**Characterization tests** describe how SpendWise behaves *today*, so the
fintech evolution changes things deliberately rather than by accident. They
cover the areas marked KEEP or ADAPT — auth and session handling, manual
expenses, categories, budgets, recurring schedules, group split and balance
maths, ownership boundaries, the API client's token-refresh behaviour, and the
reusable client-side utilities and form schemas.

They deliberately do **not** pin behaviour that the fintech source of truth
marks for removal or replacement. Where current behaviour conflicts with that
spec, the conflict is documented in the test's docstring rather than frozen by
an assertion; where a genuine bug was found, it is marked `xfail` with the
correct behaviour asserted, so the marker fails loudly once the bug is fixed.
