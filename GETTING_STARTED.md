# Getting Started

Agent-Casuality captures and explains failures in branching multi-agent
systems. The default workflow uses local SQLite, so you can run the CLI and
Python API without PostgreSQL or an API key.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)

## Install

From the repository root:

```powershell
uv sync
```

Run the complete local test suite:

```powershell
uv run pytest -q
```

For the simplest end-to-end example:

```powershell
uv run python examples/customer_approval.py
```

The example records a two-agent approval decision in
`.casuality/example.db`, prints the failure event ID, and prints commands for
both offline and optional LLM explanations.

Run the offline explanation with the printed command, or use the fixture:

```powershell
uv run casuality --fixture fixture/fixture.json explain A4 --no-llm
```

For an LLM explanation, install the optional client and set an OpenRouter key:

```powershell
uv sync --extra explain
$env:OPENROUTER_API_KEY = "your-key"
uv run casuality --fixture fixture/fixture.json explain A4 `
    --model nex-agi/nex-n2.5-mini:free
```

For persisted custom decisions, include the module that registers the
evaluator. The example prints this option automatically:

```powershell
uv run casuality --load-module examples.customer_approval `
    --db .casuality/example.db explain <failure-event-id> --no-llm
```

Free model availability depends on OpenRouter's shared capacity. The offline
explanation remains available without a key or network access.

## Advanced PostgreSQL setup

PostgreSQL is optional and intended for shared or deployed runs. Install the
extra and configure a dedicated database:

```powershell
uv sync --extra postgres
```

Create a local `.env` file. Do not commit it:

```dotenv
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
ANTHROPIC_API_KEY=your-anthropic-api-key
```

`DATABASE_URL` must point to the branch intended for testing. Use a dedicated
database because integration tests create tables and leave test rows behind.

## Run the checks

Run all local checks with one command:

```powershell
.\scripts\check.ps1
```

If PowerShell blocks local scripts, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

The script runs pytest, Ruff, and ty. If `.env` exists, it loads the file so
the PostgreSQL integration test runs as well.

Run the real Phase 1 and Phase 2 PostgreSQL integration tests with the `.env`
file loaded:

```powershell
uv run --env-file .env pytest tests/test_postgres_integration.py tests/test_phase2.py -m integration -q
```

Expected result:

```text
3 passed
```

To run the complete suite with PostgreSQL enabled:

```powershell
uv run --env-file .env pytest -q
```

The equivalent one-command version is:

```powershell
.\scripts\check.ps1
```

If `DATABASE_URL` is not loaded, the PostgreSQL test is skipped rather than
run against a database.

## Minimal in-memory capture example

This example exercises tool and memory capture without making a network call:

```python
from sdk.events import AgentClock, InMemoryEventLog
from sdk.memory import CapturedMemory
from sdk.tools import capture_tool

log = InMemoryEventLog()
clock = AgentClock()


@capture_tool
def lookup_customer(customer_id: str) -> dict[str, str]:
    return {"customer_id": customer_id, "status": "eligible"}


result = lookup_customer(
    "customer-123",
    agent_id="worker-1",
    clock=clock,
    log=log,
    invocation_id="lookup-customer-123",
)

memory = CapturedMemory(agent_id="worker-1", clock=clock, log=log)
memory.set("customer_status", result["status"])
assert memory.get("customer_status") == "eligible"

for event in log.events():
    print(event.logical_seq, event.event_type, event.causal_parent_ids)
```

The tool produces a `tool_call` followed by a `tool_result`. The result has
the call event ID in `causal_parent_ids`. Memory operations produce
`memory_read` and `memory_write` events.

## Capture a real Anthropic call

`CapturedClient` exposes the familiar `messages.create` call:

```python
import os

from sdk.client import CapturedClient
from sdk.events import AgentClock, InMemoryEventLog

client = CapturedClient(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    agent_id="planner",
    clock=AgentClock(),
    log=InMemoryEventLog(),
)

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=100,
    messages=[{"role": "user", "content": "Summarize this result."}],
)
```

The wrapper returns the original Anthropic response and records a
`model_call` event.
When using PostgreSQL, pass a `PostgresEventStore` with the original database
DSN as `lock_dsn`, a real `run_id`, and make sure the agent row already exists.
The DSN is needed because psycopg omits the password from
`connection.info.dsn`, while concurrent tool retries use a separate
transaction-scoped PostgreSQL advisory lock.

## Verify the data in Neon

Open the Neon SQL Editor for the same branch used by `DATABASE_URL`. Run the
queries in [TEST.md](TEST.md). They verify the Phase 1 capture data and the
Phase 2 PostgreSQL graph:

- required Phase 2 tables, columns, indexes, and foreign keys
- expected agent and event counts for the integration runs
- workers reference the planner and their spawn events
- worker model, tool-call, and tool-result branches are linked
- the planner merge preserves both worker result IDs
- graph ancestors include both worker branches
- every causal parent resolves to a real event
- no agent has duplicate logical sequence numbers

The event count query uses `COUNT(DISTINCT ...)` because joining agents and
events multiplies rows. `TEST.md` explains the purpose and expected result of
each query, including why logical sequence numbers must not be used as causal
edges.

## Current scope

The package currently includes:

- `Event` and thread-safe `AgentClock`
- Anthropic `messages.create` capture
- tool invocation/result capture with retry idempotency
- captured memory `get`, `set`, and `delete`
- agent spawning with `spawned_at_event_id`
- in-memory and PostgreSQL event/agent stores
- explicit cross-agent causal-parent assignment
- PostgreSQL-backed `ancestors(event_id)` queries
- local SQLite persistence with automatic directory creation
- state reconstruction and structural slicing
- exact/coarse provenance traversal
- counterfactual replay, interaction attribution, and minimal slicing
- grounded offline and optional LLM explanations
- `casuality` and `agent-casuality` console commands

## Contributing

Contributions use Git-based stacked pull requests for dependent changes. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the branch layout, PR base selection,
rebasing commands, and safe force-push workflow.
