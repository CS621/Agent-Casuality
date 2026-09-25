# Getting Started

Agent-Casuality captures branching agent runs, persists their causal event
graphs, and explains failures from local SQLite. PostgreSQL, external model
providers, and API keys are optional.

## Prerequisites

- Python 3.11 or newer
- `uv`

## Install the source checkout

From the repository root:

```powershell
uv sync
```

This installs the project in editable mode and provides the `casuality` and
`agent-casuality` commands.

## Run the end-to-end demo

```powershell
uv run python examples/customer_approval.py
```

The command is self-contained and does not require PostgreSQL, an API key, or
network access. It performs a complete local flow:

1. `casuality.init()` creates `.casuality/example.db`.
2. Two decorated agents return eligibility and risk values.
3. A merge decision records semantic ports and canonical baselines.
4. A terminal `agent_finish` event marks the incorrect approval as a failure.
5. SQLite is closed.
6. A fresh `casuality` CLI process reloads the evaluator module, reopens the
   database, and renders the offline evidence package.

The key result is:

```text
Diagnosis: The terminal event is marked 'failure' because the decision returned 'approved' with customer_status='eligible' and risk_score=0.2.
Evidence:
- Minimal tested chain: customer_status + risk_score -> decision -> terminal failure (4 events).
- Recorded inputs were customer_status='eligible' and risk_score=0.2; their canonical baselines are 'ineligible' and 0.8.
- The reported interaction between customer_status and risk_score is 1.0.
- The structural slice contains 8 events; replay reduced it to the smaller tested subset above.
Limitations: the evidence does not establish why the upstream tools produced these values.
```

The demo also prints the run ID, failure event ID, database path, raw-evidence
command, and optional LLM command. Repeated runs append to the same ignored
database without linking events across `run_id` values.

## Inspect the persisted run

Use the failure event ID printed by the demo:

```powershell
uv run casuality --load-module examples.customer_approval `
    --db .casuality/example.db slice <failure-event-id>

uv run casuality --load-module examples.customer_approval `
    --db .casuality/example.db explain <failure-event-id> --no-llm
```

`--load-module` is required for persisted custom decisions because the decision
evaluator is application code and is not serialized into the event payload.

Use `--raw-evidence` when the complete JSON package is needed:

```powershell
uv run casuality --load-module examples.customer_approval `
    --db .casuality/example.db explain <failure-event-id> `
    --raw-evidence --no-llm
```

## Use the deterministic fixture

The fixture is a separate, readable control case:

```powershell
uv run casuality --fixture fixture/fixture.json explain A4 --no-llm
```

Useful fixture commands include:

```powershell
uv run casuality --fixture fixture/fixture.json agents
uv run casuality --fixture fixture/fixture.json slice A4
uv run casuality --fixture fixture/fixture.json why A4
uv run casuality --fixture fixture/fixture.json reconstruct B 4
uv run casuality --fixture fixture/fixture.json provenance A3.output.approve
uv run casuality --fixture fixture/fixture.json replay dec_customer_approval_A3 customer_status=ineligible
uv run casuality --fixture fixture/fixture.json interaction dec_customer_approval_A3
uv run casuality --fixture fixture/fixture.json minimize A4
```

The fixture's ground-truth annotations make it useful for tests and
presentations. It is not a substitute for demonstrating live capture and
cross-process persistence.

## Understand the convenience API

The complete example is `examples/customer_approval.py`. Its local flow uses:

- `casuality.init(path)` to create a SQLite runtime;
- `@casuality.agent(role=...)` to capture agent function calls and results; and
- `@casuality.merge_decision(...)` to create decision ports, baselines, a
  decision contract, and an evaluator registration.

For merge inputs, the convenience API looks for the most recent matching
`tool_result` in the active run. This is appropriate for the controlled demo but
is value-based inference, not explicit field provenance. Use `sdk/events.py`,
`sdk/tools.py`, and `core/decision.py` directly when causal parents, clocks,
field sources, or storage behavior must be controlled explicitly.

## CLI backends

The CLI supports these backend selectors:

- `--fixture PATH`: load a JSON fixture without a database;
- `--db PATH`: read a local SQLite database; or
- no selector with `DATABASE_URL` set: read PostgreSQL.

Without an explicit selector or `DATABASE_URL`, the CLI reads
`.casuality/events.db`. It does not create application events; Python capture
code writes the database.

The `agents` command currently supports only the fixture backend because SQLite
does not persist the agent metadata currently needed by that command.

## Optional OpenRouter explanation

Install the optional HTTP dependency and set a key only if you want a
model-generated explanation:

```powershell
uv sync --extra explain
$env:OPENROUTER_API_KEY = "your-key"
uv run casuality --fixture fixture/fixture.json explain A4 `
    --model "your/model"
```

The offline summary remains available with `--no-llm`. If the model call fails or
returns text that violates the output contract, the CLI falls back to the
grounded offline summary.

## Optional PostgreSQL setup

Install the PostgreSQL extra:

```powershell
uv sync --extra postgres
```

Create a local `.env` file for integration testing:

```dotenv
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
```

Do not commit `.env`. `DATABASE_URL` must point to a dedicated testing database
because integration tests create schema objects and leave test rows behind.

Run the marked PostgreSQL integration tests with:

```powershell
uv run --env-file .env pytest `
    tests/test_postgres_integration.py tests/test_phase2.py `
    -m integration -q
```

Run the full suite with PostgreSQL enabled only when a test database is
intentionally available:

```powershell
uv run --env-file .env pytest -q
```

Detailed schema and query verification is in [TEST.md](TEST.md). Those SQL
queries work in PostgreSQL generally; a hosted PostgreSQL SQL editor is
optional.

## Run all checks

Run the standard local gates directly:

```powershell
uv run pytest -q
uv run ruff check .
uv run ty check .
```

PowerShell users can run the same commands with:

```powershell
.\scripts\check.ps1
```

If script execution is blocked:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

`scripts/check.ps1` loads `.env` before testing. If `DATABASE_URL` is present,
database-aware tests may run against that configured database.

## Current boundaries

- Merge replay evaluates a registered decision function in recorded-output mode;
  downstream side effects are not re-executed.
- Exact interaction enumeration supports up to four decision ports.
- Bootstrap sign proportions are descriptive and are not p-values.
- Structural slices express declared dependency, not proven influence.
- Privacy redaction is opt-in, best-effort, and focused on configured top-level
  payload keys.
- Resource-version tracking requires an explicit resource URI; it does not
  discover arbitrary database or filesystem reads automatically.
- The high-level local API does not yet record the fixture's exact field-level
  provenance chain.
- The project currently exposes a CLI and Python API, not a frontend.
