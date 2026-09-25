# Contributing

## Prerequisites

- Python 3.11 or newer
- `uv`
- PostgreSQL only for database integration work

## Set up the checkout

```powershell
uv sync
uv run pytest -q
```

The default test path uses in-memory adapters, SQLite, and
`fixture/fixture.json`. It does not require PostgreSQL or an API key.

## Develop

Create a focused branch from an up-to-date `main`:

```powershell
git switch main
git pull --ff-only origin main
git switch -c <short-change-name>
```

Keep each change independently understandable. Avoid unrelated formatting
changes and do not commit `.env`, local databases, generated artifacts, or
credentials.

Before opening a pull request, run the complete local gate:

```powershell
.\scripts\check.ps1
```

If script execution is blocked:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

The standard gates are:

```powershell
uv run pytest -q
uv run ruff check .
uv run ty check .
```

## Verify the example

Changes to capture, storage, CLI analysis, explanation, or decision replay
should also run the local end-to-end example:

```powershell
uv run python examples/customer_approval.py
uv run casuality --fixture fixture/fixture.json explain A4 --no-llm
```

The first command uses SQLite and does not require a database server or model
API key.

## PostgreSQL changes

For schema, adapter, query, or PostgreSQL-specific changes, create a dedicated
testing database and provide its URL through `.env`:

```dotenv
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
```

Then run the marked integration tests:

```powershell
uv run --env-file .env pytest `
    tests/test_postgres_integration.py tests/test_phase2.py `
    -m integration -q
```

These tests create schema objects and leave test rows behind. Never point them
at a production or shared application database. Do not commit `.env`.

If a configured database is temporarily unavailable, report the integration
check as blocked rather than weakening or skipping the database change in the
implementation.

## Optional explanation changes

Changes to optional LLM explanation behavior should remain compatible with
offline operation. Run the fixture without `--no-llm` only when test credentials
are deliberately configured; never log or return the key.

## Pull requests

Before requesting review:

1. Inspect the intended diff.
2. Run the relevant focused tests.
3. Run the complete local gate.
4. Run PostgreSQL integration when the change can affect database behavior.
5. Explain observable behavior changes and known limitations in the pull
   request.

Useful inspection commands are:

```powershell
git status
git diff --check
git diff
git log --oneline --decorate -10
```

## CI

Every pull request runs the checks in `.github/workflows/ci.yml`, including
the applicable test suite, Ruff, `ty`, and PostgreSQL integration where
configured. A pull request should merge only after all required checks pass and
review feedback is resolved.
