# Agent-Casuality
Causal Debugging for Branching Multi-Agent Systems

Agent-Casuality is a causal debugging and observability substrate for concurrent, branching, and merging multi-agent LLM systems. When an autonomous multi-agent run fails, conventional tracing only tells you *what* happened in linear time; Agent-Casuality answers **why** the decision became wrong, attributing failures to specific upstream branches, environmental state mutations, or non-linear joint interactions ($A \times B$).

## Install and Run

Install the package with no database or service setup:

```powershell
pip install agent-casuality
```

The CLI automatically stores local runs in `.casuality/events.db`:

```powershell
casuality slice <event-id>
casuality why <event-id>
casuality explain <event-id> --no-llm
```

Use `--db path/to/events.db` for a different local database. PostgreSQL is optional; install it with `pip install "agent-casuality[postgres]"` and set `DATABASE_URL` when you need a shared deployment. LLM explanations are also optional: install `pip install "agent-casuality[explain]"` and set `OPENROUTER_API_KEY`. Without an API key, `--no-llm` still produces the complete causal evidence package.

### Three-Line Python API

```python
import casuality

casuality.init()

@casuality.agent(role="researcher")
def research(query: str):
  return search_tool(query)

@casuality.merge_decision(ports=["research_summary", "risk_score"])
def approve_customer(research_summary, risk_score):
  return policy(research_summary, risk_score)
```

The decorators capture tool results, connect merge inputs to their producing events, and persist the causal graph locally. Existing low-level SDK APIs remain available when an application needs explicit clocks, agents, privacy policies, or PostgreSQL storage.

---

## Architecture & Implemented Phases

All core backend and analytical phases (Phases 1 through 6) are fully implemented and verified against PostgreSQL and the ground-truth fixture:

- **Phase 1: Transparent Capture SDK** (`sdk/`)
  - Intercepts model calls, tool invocations, memory mutations, and agent lifecycles (`sdk/client.py`, `sdk/tools.py`, `sdk/memory.py`, `sdk/lifecycle.py`).
  - Monotonic distributed sequence allocation via Lamport logical clocks (`sdk/events.py`).
  - Strict privacy scrubbing for prompts, tool arguments, outputs, and memory (`sdk/privacy.py`).
- **Phase 2: Causal Event Graph & Storage** (`storage/postgres.py`, `core/graph.py`)
  - Append-only event store in PostgreSQL with row-level locks and idempotency constraints.
  - Recursive CTE ancestor graph traversal (`ancestors()`), tracking intra-agent progression, cross-agent spawns, and explicit merge dependencies.
- **Phase 2.5: Decision SCM Contract & Resource Invariants** (`core/decision.py`, `core/validator.py`)
  - Formulates decision points as Structural Causal Models (SCMs) over typed `DecisionPort`s with baseline substitution distributions (`DEFAULT_SENTINEL`, `CANONICAL_BASELINE`, `HISTORICAL_PRIOR`).
  - **Resource-Version Invariant**: Automatically binds shared memory, database, and file mutations to subsequent reads without manual developer annotation, guaranteeing DAG completeness.
  - Complete graph validator catching dangling edges, cross-run leaks, and timeline continuity breaks.
- **Phase 3: State Reconstruction & Structural Slicing** (`core/reducer.py`, `core/snapshots.py`, `core/slicing.py`)
  - Deterministic state reconstruction (`reconstruct()`) folding event match-arms with canonical JSON SHA-256 state hashes.
  - Snapshot manager verifying state hashes on replay.
  - Structural slicing (`slice()`, `why()`) isolating the exact DAG subgraph reachable from any failure.
- **Phase 4: Dual-Grade Field-Level Provenance** (`core/provenance.py`)
  - Traces exact data origins through deterministic tools and policy functions (`exact`).
  - Explicitly bounds LLM interpretability by terminating chains at unverified neural boundaries (`coarse`), preventing false certainty.
- **Phase 5: Counterfactual Replay, Interaction Attribution & Minimal Slicing** (`core/replay.py`)
  - Merge-local counterfactual replay with hard side-effect safety guards (`ReplayUnsafe`).
  - **Shapley-Owen Interaction Attribution**: Computes single-port Shapley values $\phi_i$ and pairwise interaction indices $I_{ij}$ with bootstrap standard errors ($I_{ij} \pm \sigma$, $p$-value) to distinguish true multi-branch interactions from stochastic model noise.
  - **Delta Debugging (`ddmin`)**: Minimizes structural slices into 1-minimal causal event subsets with frozenset caching and replay budget caps.
- **Phase 6: Grounded Causal Explanation Layer** (`core/explain.py`)
  - Aggregates failure metadata, structural slices, state diffs, minimal slices, exact/coarse provenance chains, and Shapley interaction indices into a unified evidence package (`build_evidence_package`).
  - Generates faithful, natural-language explanations grounded strictly in the causal evidence, citing event IDs and explicitly disclosing coarse boundaries.
  - Integrates with OpenRouter (`qwen/qwen3.8-27b:free` or configurable via `--model`) with exponential backoff and retry on upstream rate limits. LLM explanations are optional and require the `explain` extra plus an `OPENROUTER_API_KEY`.

*(The frontend visualization track runs in parallel against `fixture/mock.py` and the CLI).*

---

## Querying a Run via CLI

### Against the Test Fixture (No Database Required)

```powershell
# List agents in the run
casuality --fixture fixture/fixture.json agents

# Structural backward slice (9 events: A1, B1, C1, B2, C2, B3, C3, A3, A4)
casuality --fixture fixture/fixture.json slice A4

# Inspect decision contract and declared semantic ports
casuality --fixture fixture/fixture.json why A4

# Reconstruct agent state at a specific logical sequence
casuality --fixture fixture/fixture.json reconstruct B 4

# Trace exact field-level provenance chain back to source tools
casuality --fixture fixture/fixture.json provenance A3.output.approve

# Compute Shapley values and Shapley-Owen joint interaction index (B3 x C3 = 1.0)
casuality --fixture fixture/fixture.json interaction dec_customer_approval_A3

# Delta debugging: reduce structural slice down to minimal causal subset (B3, C3, A3, A4)
casuality --fixture fixture/fixture.json minimize A4

# Test a counterfactual port intervention (flips failure to success)
casuality --fixture fixture/fixture.json replay dec_customer_approval_A3 customer_status=ineligible

# Generate grounded LLM failure explanation (using OpenRouter / Qwen)
casuality --fixture fixture/fixture.json explain A4

# Inspect the structured evidence package without making an LLM call
casuality --fixture fixture/fixture.json explain A4 --no-llm --raw-evidence
```

### Against Live PostgreSQL

Set `DATABASE_URL` in your `.env` file (or environment), then omit `--fixture`:

```powershell
casuality slice <event-uuid>
casuality why <decision-or-event-uuid>
casuality provenance <field-path>
casuality reconstruct <agent-uuid> <target-sequence>
casuality interaction <decision-uuid>
casuality minimize <event-uuid>
casuality explain <event-uuid>
```

---

## Testing in Real-Life Scenarios & Against Real Systems

How do you use Agent-Casuality to trace, reproduce, and debug failures in an actual multi-agent application? Follow this step-by-step workflow:

### Step 1: Configure Environment

Add your database and model provider keys to `.env`:

```ini
# PostgreSQL database (Neon serverless or local PostgreSQL)
DATABASE_URL=postgresql://user:password@host/dbname?sslmode=require

# OpenRouter API Key for Phase 6 explanation layer
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=qwen/qwen3.8-27b:free
```

### Step 2: Instrument Your Multi-Agent System with the SDK

Wrap your agent system using the Agent-Casuality capture layer:

```python
import psycopg
from uuid import uuid4
from storage.postgres import PostgresEventStore
from sdk.lifecycle import spawn_agent
from sdk.tools import capture_tool
from sdk.memory import CapturedMemory
from sdk.client import CapturedClient
from core.decision import create_decision_contract, DecisionPort, AblationStrategy
from core.replay import register_decision_evaluator

# 1. Connect to PostgreSQL event store
connection = psycopg.connect(DATABASE_URL)
store = PostgresEventStore(connection, lock_dsn=DATABASE_URL)
store.create_schema()

run_id = str(uuid4())
planner_id = str(uuid4())

# Initialize run and root agent in storage
with connection.cursor() as cur:
    cur.execute("INSERT INTO runs (id, name) VALUES (%s, %s)", (run_id, "customer-loan-approval"))
    cur.execute("INSERT INTO agents (id, run_id, role) VALUES (%s, %s, %s)", (planner_id, run_id, "planner"))
connection.commit()

# 2. Spawn worker agents with causal parent propagation
researcher_id, _, researcher_clock = spawn_agent(
    parent_agent_id=planner_id,
    parent_clock=planner_clock,
    run_id=run_id,
    role="researcher",
    log=store,
    agent_store=store,
)

# 3. Define and capture deterministic tools with exact field provenance
@capture_tool(
    tool_name="credit_check",
    log=store,
    agent_id=researcher_id,
    clock=researcher_clock,
    run_id=run_id,
    field_sources={"status": "tool_call"},
)
def credit_check(customer_id: str) -> dict:
    # Real tool logic (or service call)
    return {"status": "eligible"}

# 4. Use CapturedMemory for shared state (Resource-Version Invariant auto-links dependencies!)
shared_memory = CapturedMemory(agent_id=researcher_id, clock=researcher_clock, log=store, run_id=run_id)
shared_memory.set("risk_factor", 0.2)  # Any subsequent read by another agent auto-creates a causal edge

# 5. Capture model calls
client = CapturedClient(
    agent_id=planner_id,
    clock=planner_clock,
    log=store,
    run_id=run_id,
)
# client.messages.create(...) automatically logs model_call and model_response events
```

### Step 3: Register Decision SCM Contracts at Merge Points

When branches converge into a decision (e.g. planner evaluates results from researcher and risk evaluator):

```python
# Define how the decision evaluator behaves during counterfactual ablation
def policy_evaluator(inputs: dict) -> str:
    status = inputs.get("customer_status")
    risk = inputs.get("risk_score")
    if status == "eligible" and risk < 0.5:
        return "failure"  # Erroneous approval
    return "success"      # Correct rejection

register_decision_evaluator("loan_approval_policy", policy_evaluator)

# Record the decision contract linking upstream ports
contract = create_decision_contract(
    decision_id="dec_loan_approval",
    run_id=run_id,
    agent_id=planner_id,
    decision_event_id=merge_event_id,
    ports=[
        DecisionPort(
            port_id="customer_status",
            source_event_id=researcher_event_id,
            field_path="output.status",
            recorded_value="eligible",
            baseline_value="ineligible",
            strategy=AblationStrategy.CANONICAL_BASELINE,
        ),
        DecisionPort(
            port_id="risk_score",
            source_event_id=risk_event_id,
            field_path="output.risk_factor",
            recorded_value=0.2,
            baseline_value=0.8,
            strategy=AblationStrategy.CANONICAL_BASELINE,
        ),
    ],
    decision_type="loan_approval_policy",
    outcome="failure",
)
```

### Step 4: Diagnose Live Failures End-to-End

When a multi-agent run fails in production or testing:

1. **Find the Structural Slice**:
   ```powershell
   uv run python -m cli.main slice <failure-event-uuid>
   ```
   Filters out hundreds of unrelated events across your system, returning only the backward reachable DAG.

2. **Verify Field-Level Origins**:
   ```powershell
   uv run python -m cli.main provenance <decision-event-uuid>.output.decision
   ```
   Inspects whether inputs were transformed by deterministic code (`exact`) or passed through an LLM (`coarse`).

3. **Minimize with Delta Debugging**:
   ```powershell
   uv run python -m cli.main minimize <failure-event-uuid>
   ```
   Uses `ddmin` to prune distractor branches down to the exact 1-minimal subset that reproduces the failure.

4. **Isolate Interacting Causes**:
   ```powershell
   uv run python -m cli.main interaction <decision-uuid>
   ```
   Outputs Shapley values and the interaction index $I_{ij}$. If $I_{ij} \approx 1.0$, you have proven that neither branch was the sole cause—the bug was an interaction between independent agents!

5. **Generate the Grounded Causal Explanation**:
   ```powershell
   uv run python -m cli.main explain <failure-event-uuid>
   ```
   Calls the grounded explanation engine to synthesize the diagnosis into a clear, trustworthy report citing exact event IDs without hallucination.

---

## Installation & Test Suite

Install the package with no database or service setup:

```powershell
pip install agent-casuality
```

The CLI automatically stores local runs in `.casuality/events.db`:

```powershell
casuality slice <event-id>
casuality why <event-id>
casuality explain <event-id> --no-llm
```

For an LLM explanation, install the optional client and set an OpenRouter key:

```powershell
pip install "agent-casuality[explain]"
$env:OPENROUTER_API_KEY = "your-key"
casuality --fixture fixture/fixture.json explain A4 --model nex-agi/nex-n2.5-mini:free
```

For PostgreSQL, install the extra and set `DATABASE_URL`:

```powershell
pip install "agent-casuality[postgres]"
$env:DATABASE_URL = "postgresql://user:password@host/dbname?sslmode=require"
```

```powershell
# Install dependencies
uv sync

# Run all checks (pytest, Ruff linter, Ty type-checker)
.\scripts\check.ps1
```

To run only the integration tests against PostgreSQL:

```powershell
uv run pytest tests/test_postgres_integration.py -v
```

See [TEST.md](TEST.md) for detailed PostgreSQL verification queries and testing procedures.
