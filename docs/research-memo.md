# Research Memo: Causal Architecture, Failure Modes, and Breakthrough Directions for Multi-Agent Decision Attribution

> **Document status:** Research snapshot, not a current product contract or
> setup guide. Claims and proposed directions are retained for provenance. Use
> [README.md](../README.md) and [GETTING_STARTED.md](../GETTING_STARTED.md)
> for implemented behavior.

---

## System Model

Agent-Casuality is designed as a causal debugging and observability substrate for concurrent, branching, and merging multi-agent LLM systems. Tracing the architecture from execution capture through storage, graph representation, state reconstruction, and decision attribution reveals the following end-to-end model:

```
[Agent Execution Layer]
  │  sdk/client.py (CapturedClient / _CapturedStream)
  │  sdk/tools.py (capture_tool / advisory locks)
  │  sdk/memory.py (CapturedMemory get/set/delete)
  │  sdk/lifecycle.py (spawn_agent / lamport_offset)
  ▼
[Logical Ordering & Identity]
  │  sdk/events.py: AgentClock.allocate(causal_parent_seqs) -> Lamport Seq
  │  storage/postgres.py: allocate_logical_seq() with FOR UPDATE row locks
  ▼
[Storage & Ingestion Layer]
  │  storage/postgres.py: PostgresEventStore
  │  Tables: runs, agents, events, snapshots (thesis.md §30.1)
  │  Immutability: Append-only log with ON CONFLICT (agent_id, idempotency_key)
  ▼
[Graph & Dependency Representation]
  │  core/graph.py: assign_causal_parents(), ancestors()
  │  storage/postgres.py: Recursive CTE over events.causal_parent_ids (UUID[])
  │  Edges: Spawn edges, Same-agent progression, Explicit cross-agent parents
  ▼
[State Reconstruction Layer (Phase 3)]
  │  thesis.md §30.3: Nearest snapshot <= target_seq + forward reduce(state, event)
  │  Invariant check: hash(reconstructed_state) == snapshot.state_hash
  ▼
[Decision & Provenance Layer (Phase 2.5 & Phase 4)]
  │  conversation-summary.md §66-88: Decision Contract (input_event_ids, output, outcome)
  │  thesis.md §12, §30.8: Dual-grade provenance (Exact vs Coarse/LLM-boundary)
  ▼
[Attribution & Replay Engine (Phase 5)]
  │  thesis.md §14-17, §30.6-30.7: Merge-local recorded-output replay & ddmin
  │  Factorial intervention matrix over converging parents: {B+C, B, C, ∅}
  ▼
[Synthesis & Explanation Layer (Phase 6)]
     core/explain.py: Evidence package assembled -> Grounded LLM diagnosis
```

### 1. Data Invariants & Flow

1. **Logical Progression:** Every agent maintains an `AgentClock` (`sdk/events.py:14-40`). Sequence allocation follows the monotonic Lamport rule:
   $$\text{seq}_{\text{new}} = \max\left(\text{local\_counter}, \max_{p \in \text{parents}}(\text{seq}_p)\right) + 1$$
   In PostgreSQL, concurrency is serialized per agent using `SELECT lamport_offset FROM agents WHERE id = %s FOR UPDATE` in `allocate_logical_seq()` (`storage/postgres.py:175-208`).
2. **Explicit Dependency Graph ($G = (V, E)$):** Nodes $V$ are immutable events in `events`. Edges $E$ are stored in `events.causal_parent_ids uuid[]`. Structural ancestor queries walk $E$ backwards via recursive SQL CTEs in `ancestors()` (`storage/postgres.py:233-262`).
3. **Decision Centricity:** A decision event $D$ at a merge point ($A_3$ in `fixture/fixture.json:44-49`) aggregates outputs from converging branches $B_3, C_3$.
4. **Attribution Contract:** Declared dependency ($B_3 \in \text{Parents}(A_3)$) represents *data accessibility*. Decision influence is determined downstream by intervening on inputs to $A_3$ and observing changes in the outcome of $A_4$.

---

## Critical Assumptions

The validity and safety of the system rest on the following assumptions, which require scrutiny:

1. **Explicit Edge Completeness (No Hidden Channels):**
   *Assumption:* The host application or developer explicitly populates `causal_parent_ids` for every cross-agent or intra-agent causal dependency (e.g., in `assign_causal_parents()` in `core/graph.py:19-39`).
   *Reality:* Agents communicate implicitly through shared external environments (databases, shared key-value memory, local file systems, scratchpad buffers). An uncaptured environmental read/write breaks DAG completeness, creating unlinked causal chains.
2. **Well-Defined Exclusion Semantics ($do(X=\emptyset)$):**
   *Assumption:* In merge-local interaction analysis (`docs/thesis.md` §30.6), "excluding" branch $B$ from merge decision $A_3$ is a clean, well-defined mathematical intervention.
   *Reality:* In an LLM prompt or structured tool call, removing an input entirely changes prompt grammar, token lengths, few-shot formatting, or schema validation. A model failure under $do(B=\emptyset)$ often reflects prompt corruption or distribution shift rather than true causal necessity.
3. **State Determinism & Local Isolation:**
   *Assumption:* Agent state is completely captured by serializable JSON deltas (`reduce(state, event)` in `docs/thesis.md` §30.3).
   *Reality:* Real agents (especially coding agents) mutate external operating system state (files on disk, environment variables, git staging, background daemon processes). JSON state reduction cannot reconstruct external OS state without explicit workspace sandboxing.
4. **Stochastic Invariance of Decision Functions:**
   *Assumption:* Running an intervention four times (both, $A$-only, $B$-only, neither) yields a deterministic classification (`sufficient`, `interaction`, `redundant`).
   *Reality:* LLM inference is stochastic. Temperature, system load, batching kernels, and provider-side non-determinism cause stochastic transitions. A single 4-run evaluation can misclassify random token variance as a multi-branch causal interaction.
5. **Local Replay Safety (Side-Effect Freeness):**
   *Assumption:* Tagging tools with `@tool(side_effecting=True)` (`docs/thesis.md` §30.7) is sufficient to prevent destructive replay.
   *Reality:* External read tools (e.g., searching an API, querying a database, reading a web page) can be state-dependent or time-dependent. Replaying a merge with cached tool results vs live tools introduces temporal inconsistency.

---

## Failure Modes

Where could the current design produce plausible but incorrect causal explanations or break down?

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. THE INVISIBLE CHANNEL (Memory / File Race Condition)                    │
│    Agent B: sets memory['x'] = 1 ──► [Shared DB / FS]                       │
│                                           │ (No causal_parent_ids passed)   │
│    Agent C: reads memory['x'] == 1 ───────┘                                 │
│    Graph Result: ancestors(C) DOES NOT CONTAIN B. Cause misattributed.      │
├─────────────────────────────────────────────────────────────────────────────┤
│ 2. THE EXCLUSION ARTIFACT (Prompt Syntax Distortion)                        │
│    Merge Prompt Template: "Review {B_output} and {C_output}."               │
│    Intervention (Exclude B): "Review None and {C_output}."                  │
│    LLM Output: Refusal ("Invalid Input"). Debugger falsely claims B is vital│
├─────────────────────────────────────────────────────────────────────────────┤
│ 3. THE STOCHASTIC COINCIDENCE (False Interaction Detection)                 │
│    Decision P(fail | B, C) = 0.5; P(fail | B, ~C) = 0.5                     │
│    4-Run Matrix Sample: Both=Fail, B-only=Pass, C-only=Pass, Neither=Pass   │
│    Debugger Output: "Interaction B x C" (Plausible, but scientifically false)│
├─────────────────────────────────────────────────────────────────────────────┤
│ 4. THE INTRA-AGENT REACHABILITY CHASM                                      │
│    Agent B: Tool 1 Result (B2) ──► Tool 2 Call (B3, causal_parent_ids=[])   │
│    Recursive CTE from B4 only climbs to B3; Tool 1 is completely omitted.   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1. The Disconnected Shared-State Channel
In `sdk/memory.py:47-94`, `CapturedMemory.get()` and `CapturedMemory.set()` log `memory_read` and `memory_write` events. However, `get()` accepts an optional `causal_parent_ids` parameter defaulting to empty `()`. 
If Agent $B$ writes a corrupted value to key `customer_status` and Agent $C$ reads `customer_status`, Agent $C$'s `memory_read` event will have `causal_parent_ids=[]`. When a downstream failure $A_4$ is investigated, `ancestors(A4)` will trace through $C$, but will **never traverse to $B$'s write event**. The system will produce a structural slice that exonerates the real culprit ($B$).

### 2. The Intra-Agent Step Omission Bug
In `sdk/client.py:146-202` and `sdk/tools.py:107-150`, `causal_parent_ids` must be explicitly passed by the caller. If an agent executes three sequential steps:
$$\text{Model Call } B_1 \to \text{Tool Call } B_2 \to \text{Model Call } B_3$$
Unless the agent framework explicitly passes $[B_2.\text{id}]$ into $B_3$'s call, $B_3$ has `causal_parent_ids=[]`. The recursive SQL CTE in `ancestors()` (`storage/postgres.py:233-262`) strictly follows `causal_parent_ids`. Consequently, querying ancestors of $B_3$ returns only $\{B_3\}$, dropping $B_1$ and $B_2$ from the causal slice despite them forming the execution timeline.

### 3. Prompt Breakage via Null Interventions
In `core/graph.py` and `docs/thesis.md` §30.6, `test_interaction` evaluates `exclude={b}`. In real LLM applications, removing an upstream report causes the prompt template to render empty strings, invalid JSON schemas, or missing context markers. If the LLM throws a formatting exception or outputs a default refusal, the test registers `outcome == "failure"`. The attribution engine falsely reports that branch $B$ was causally sufficient or involved in a joint interaction, when the failure was an artifact of prompt malformation.

### 4. Stochastic Misattribution
If a downstream decision has a 30% baseline failure rate regardless of inputs, running the 4-condition matrix once has a $(0.3) \times (0.7)^3 \approx 10.3\%$ probability of returning `both=fail, only_a=pass, only_b=pass, neither=pass`. The debugger will output:
> *"Potential interaction: B3 x C3 (B3 + C3: failure, B3 alone: no failure, C3 alone: no failure)"*

This explanation is plausible, cleanly rendered, and completely wrong.

---

## Open Research Problems

1. **Formalization of Causal Interventions on Natural Language Contexts:**
   In classical SCMs (Pearl 2000), an intervention $do(X = x)$ sets a variable to a well-defined value in its domain. In multi-agent LLM systems, inputs are unstructured natural language strings embedded in prompts. What is the mathematically and semantically sound definition of an ablation intervention on a context window?
2. **Efficient Attribution at High-Fan-In Merges ($k \ge 4$):**
   A merge combining $k$ upstream agent branches requires $2^k$ re-executions for a full factorial design. For $k=6$, 64 replays are required. If statistical confidence requires 5 trials per cell, that is 320 LLM calls per merge point. How can the system identify $m$-way interactions ($m \ll k$) in polynomial or sub-linear time?
3. **Decoupling Stochastic Model Variance from True Causal Signal:**
   How can a causal debugger provide statistically bounded confidence intervals (e.g., Average Treatment Effect with $p$-values) on decision influence without incurring high inference costs?
4. **Complete Environmental Causality without Whole-System Virtualization:**
   How can causality be tracked across shared files, environment variables, and databases without instrumenting the entire Linux/Windows kernel or Docker hypervisor?

---

## Breakthrough Candidates

The following four technical breakthroughs address the architectural limitations identified above, ranked by potential impact:

```
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ RANK 1: RESOURCE-VERSION CAUSAL INVARIANT (Implicit Environmental Flow Capture)               │
│ Closes the largest safety hole: automatically binds shared memory, DB, and file writes to      │
│ subsequent reads, guaranteeing DAG completeness without developer annotation.                 │
├────────────────────────────────────────────────────────────────────────────────────────────────┤
│ RANK 2: VALUE-LEVEL SEMANTIC COUNTERFACTUAL PORTS (Phase 2.5 Decision SCM)                    │
│ Solves the "exclusion dilemma" by structuring merge points into typed semantic ports with      │
│ defined baseline distributions (Null / Canonical / Prior Pass).                                │
├────────────────────────────────────────────────────────────────────────────────────────────────┤
│ RANK 3: STATISTICAL SHAPLEY-OWEN INTERACTION ATTRIBUTION                                       │
│ Replaces brittle Boolean 2x2 matrices with cooperative game-theoretic attribution, bounded     │
│ by variance-aware Monte Carlo sampling for arbitrary k-branch merges.                          │
├────────────────────────────────────────────────────────────────────────────────────────────────┤
│ RANK 4: EMPIRICAL MESSAGE-BOUNDARY ATTRIBUTION (Differential Prompt Slicing)                  │
│ Bridges the "coarse provenance" gap for LLM calls by computing input-to-output sensitivity     │
│ across prompt sections without requiring access to internal neural network weights.            │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### Candidate 1: Resource-Version Causal Invariant (Implicit Environmental Flow Capture)

#### Current Limitation
`sdk/memory.py:47-94` and file tool calls treat operations as isolated agent events. If Agent $B$ mutates state and Agent $C$ reads it, no causal edge is created unless manually wired. This violates the Causal Markov Condition and breaks causal slicing.

#### Proposed Insight
Every mutable resource $R$ (e.g., memory key `mem://customer_status`, file path `file://src/auth.py`, DB row `db://users/123`) is assigned an append-only **Resource Version Tuple**:
$$\mathcal{V}(R) = \langle \text{version\_num}, \text{last\_writer\_event\_id} \rangle$$
Whenever an agent executes a read on $R$, the capture engine automatically injects $\text{last\_writer\_event\_id}$ into the reading event's `causal_parent_ids`.

```
[Agent B] ──► set("key", "val") ──► Emits Event E_b ──► Updates Resource Registry:
                                                        mem://key = <v2, E_b>
                                                                 │
                                                    (Automatic Edge Injection)
                                                                 ▼
[Agent C] ──► get("key") ────────► Emits Event E_c with causal_parent_ids = [E_b]
```

#### Why It Works
It requires zero changes to agent reasoning or developer manual wiring. It transforms implicit state dependencies into explicit DAG edges at the storage/SDK boundary, guaranteeing that `ancestors()` traverses across shared memory and file modifications.

#### What Needs to Be True
The SDK wrapper must intercept resource access points (memory getters/setters, file system tools, database wrappers).

#### Experimental Validation
Create Benchmark Scenario 5 (Memory Contamination from `docs/thesis.md` §23): Agent $B$ writes a corrupt value, Agent $C$ reads it, Planner $A$ merges $C$'s output. Verify that `ancestors(A4)` automatically includes $B$'s write event with zero manual `causal_parent_ids` supplied by the test code.

---

### Candidate 2: Value-Level Semantic Counterfactual Ports (The Phase 2.5 Decision SCM)

#### Current Limitation
In `docs/thesis.md` §30.6, `test_interaction` relies on ad-hoc exclusion of events, which breaks prompt templates and causes spurious failures.

#### Proposed Insight
Formalize the Phase 2.5 Decision Contract into a **Structural Causal Model (SCM) with Semantic Input Ports**.
A Decision $D$ declares explicit named input ports:
$$\mathcal{D} = \langle f_D, \mathbf{X} = \{X_1, X_2, \dots, X_k\}, Y \rangle$$
Where each port $X_i$ has:
1. `source_event_id`: The recorded event providing the value.
2. `recorded_value`: The value observed during the run.
3. `ablation_strategy`: One of:
   - `DEFAULT_SENTINEL`: Typed neutral placeholder (e.g., `risk_score = 0.0`, `status = "UNSPECIFIED"`).
   - `CANONICAL_BASELINE`: A domain-specific golden reference value.
   - `HISTORICAL_PRIOR`: The value from a previous passing run.

```python
# Phase 2.5 Decision Port Schema
@dataclass
class DecisionPort:
    name: str
    source_event_id: str
    recorded_value: Any
    baseline_value: Any  # Well-defined counterfactual replacement
    required: bool = True


@dataclass
class DecisionContract:
    decision_id: str
    decision_type: str
    ports: dict[str, DecisionPort]
    evaluate_fn: Callable[..., Any]
```

#### Why It Works
Interventions never "delete" text from prompt templates. They execute true counterfactual value substitutions:
$$do(X_i = \text{baseline\_value})$$
This preserves prompt grammatical validity and isolates semantic decision influence from syntactic failure artifacts.

#### What Needs to Be True
Host agent merge functions must parameterize their prompt construction or decision logic around typed input variables rather than concatenating arbitrary strings.

#### Experimental Validation
Run a prompt-based merge with 10 synthetic variations. Compare raw exclusion (which drops text blocks) against Port Substitution (which supplies typed neutral baselines). Measure the **False Positive Interaction Rate**; Port Substitution should eliminate prompt-syntax-induced false positives.

---

### Candidate 3: Statistical Shapley-Owen Interaction Attribution

#### Current Limitation
The current 4-run matrix (`docs/thesis.md` §30.6) only handles 2 branches, is binary, and cannot quantify interaction strength under stochastic LLM decoding.

#### Proposed Insight
Formulate branch attribution as a **Cooperative Game on Converging Branches** using **Shapley Interaction Indices** (Grabisch & Roubens 1999).
For a decision with upstream branch set $N = \{1, 2, \dots, k\}$ and characteristic value function $v(S) = P(\text{Failure} \mid \text{active branches in } S)$:

1. **Individual Branch Influence (Shapley Value $\phi_i$):**
   $$\phi_i = \sum_{S \subseteq N \setminus \{i\}} \frac{|S|!(|N| - |S| - 1)!}{|N|!} \left[ v(S \cup \{i\}) - v(S) \right]$$
2. **Pairwise Interaction Index ($I_{ij}$):**
   $$I_{ij} = \sum_{S \subseteq N \setminus \{i, j\}} \frac{|S|!(|N| - |S| - 2)!}{|N|! - 1} \left[ v(S \cup \{i, j\}) - v(S \cup \{i\}) - v(S \cup \{j\}) + v(S) \right]$$
3. **Statistical Confidence:** $v(S)$ is estimated via $M$ Monte Carlo replays per subset. Compute standard error $\sigma_{I_{ij}}$ and report a confidence interval (e.g., $I_{B,C} = 0.78 \pm 0.06, p < 0.01$).

```
                      Branch Influence Spectrum
─────────────────────────────────────────────────────────────────────
  [B alone: φ_B = 0.12]   [C alone: φ_C = 0.08]   [Joint Interaction: I_BC = 0.80]
  Result: Strong Joint Interaction (Confidence: 99.2%, p < 0.001)
```

#### Why It Works
- Generalizes naturally from 2 branches to $k$ branches.
- Quantifies interaction strength on a continuous scale $[0, 1]$ rather than a brittle Boolean string.
- Explicitly handles stochastic model outputs using statistical hypothesis testing.
- Connects the project directly to established game-theoretic explainability literature (Lundberg & Lee, NeurIPS 2017).

#### What Needs to Be True
The cost of sampling subsets must be manageable. For $k \le 4$, exact evaluation ($2^k \le 16$) is feasible. For $k > 4$, standard Monte Carlo permutation sampling (Castro et al. 2009) can estimate Shapley indices within a fixed replay budget.

#### Experimental Validation
Construct a 3-agent synthetic merge ($A, B, C$) where $A$ and $B$ interact to cause failure only when $C$ is a benign distractor. Demonstrate that $I_{AB} \to 1.0$, while $\phi_C \to 0.0$ and $I_{AC} \to 0.0$, with statistical significance across temperature variations ($T = 0.0, 0.3, 0.7$).

---

### Candidate 4: Empirical Message-Boundary Attribution (Differential Prompt Slicing)

#### Current Limitation
`docs/thesis.md` §12 concedes that all LLM links are coarse dead-ends (`source_path = None`). This leaves a significant gap: if an LLM receives a 4,000-token context containing 10 tool outputs, coarse provenance cannot indicate which tool output the model attended to.

#### Proposed Insight
Implement **Black-Box Semantic Slicing at the Context Block Boundary**.
In `sdk/client.py:146-202`, when an LLM call is recorded, the input context is decomposed into structured **Context Chunks** (system prompt, message history, tool results $T_1, \dots, T_m$).
During post-mortem debugging, the engine applies localized text perturbation / masking to individual context chunks while freezing the rest of the prompt:
$$\text{Sensitivity}(T_i) = \text{Distance}\left(\text{Output}(C), \text{Output}(C \setminus T_i)\right)$$
If masking $T_1$ changes the decision output from `Failure` to `Success`, but masking $T_2 \dots T_m$ produces zero semantic change, the provenance link from the decision output to $T_1$ is promoted from **Coarse** to **Empirically Verified Semi-Exact**.

```
[Context Window]
 ├── System Prompt ────────── (Perturbation: Invariant)
 ├── Tool Result 1 (B3) ───── (Perturbation: Output flips to Success) ──► Provenance Verified
 └── Tool Result 2 (C3) ───── (Perturbation: Output invariant)
```

#### Why It Works
It does not require white-box model weights or attention tensors. It treats the LLM as a black box and measures empirical input-output sensitivity at the block level, turning an open interpretability problem into an active empirical attribution test.

#### What Needs to Be True
Output distance must be measurable (e.g., exact match on structured JSON fields, or cosine similarity on embeddings for free-form text).

#### Experimental Validation
In the Customer Approval scenario (`fixture/fixture.json`), mask $B_3$'s search output vs $C_3$'s risk output in $A_3$'s context. Verify that masking $B_3$ changes $A_3$'s approval decision, establishing verified empirical attribution.

---

## Strongest Direction

### Defense of the Core Direction: The Phase 2.5 Semantic Decision SCM with Resource-Version Invariant

The single strongest direction that transforms Agent-Casuality from an ad-hoc tracing script into an academically defensible, breakthrough causal debugging system is:

> **The Formulation of Multi-Agent Decision Points as Structural Causal Models (SCMs) over Semantic Ports, coupled with Resource-Versioned State Invariants.**

```
                     UNIFIED ARCHITECTURAL CORE
 ┌─────────────────────────────────────────────────────────────────┐
 │ 1. Resource Version Invariant (sdk/memory.py, sdk/tools.py)     │
 │    Captures ALL implicit data flow through shared storage.      │
 ├─────────────────────────────────────────────────────────────────┤
 │ 2. Semantic Port Contract (core/decision.py - Phase 2.5)        │
 │    Replaces ad-hoc string drops with typed counterfactual ports.│
 ├─────────────────────────────────────────────────────────────────┤
 │ 3. Shapley Interaction Engine (core/replay.py - Phase 5)        │
 │    Statistically proves B x C interactions under nondeterminism.│
 └─────────────────────────────────────────────────────────────────┘
```

### Why This Direction Wins:

1. **It Solves the Foundational Flaw (The Illusion of Causality):**
   Without Resource Versioning, any communication through shared memory, databases, or scratchpad files creates disconnected graph components. Fixing this makes the DAG **sound and complete**.
2. **It Resolves the Exclusion Dilemma:**
   The biggest critique of counterfactual LLM replay is that deleting prompt text breaks grammar and causes hallucinated failures. Semantic Ports give $do(X = \text{baseline})$ a rigorous operational definition.
3. **It Makes the Research Claim Academically Defensible:**
   Moving from an ad-hoc 4-run Boolean check to **Shapley Interaction Indices with Bootstrap Confidence Bounds** elevates the project to top-tier systems and AI venues (e.g., OSDI, SOSP, NeurIPS, ICSE). It proves whether a failure was caused by $B$, $C$, or the non-linear interaction $B \times C$ under stochastic inference.
4. **Clean Phase 2.5 Alignment:**
   This concrete plan directly realizes Phase 2.5 without requiring premature graph databases or unconstrained production replay.

---

## Concrete Phase 2.5 Implementation Plan

To operationalize this breakthrough before Phase 3, Phase 2.5 should execute the following technical deliverables:

```
Phase 2 (Completed) ──► Phase 2.5 (Concrete Plan Below) ──► Phase 3 (State Recon & Slice)
```

### 1. Resource Version Tracker in `sdk/memory.py` & `sdk/tools.py`
- Add a thread-safe `ResourceRegistry` tracking `(resource_uri -> (version, last_event_id))`.
- When `CapturedMemory.set(key, ...)` executes:
  - Register `mem://{agent_id}/{key}` $\to (\text{seq}, \text{event.id})$.
- When `CapturedMemory.get(key, ...)` executes:
  - Read `mem://{agent_id}/{key}`'s last event ID and append it to `causal_parent_ids` automatically.
- Extend tool capture in `sdk/tools.py:65-104` to automatically inject the calling agent's latest event sequence if `causal_parent_ids` is omitted, eliminating intra-agent reachability breaks.

### 2. The Decision Contract Definition (`core/decision.py`)
- Define the `DecisionContract` schema:
  ```python
  @dataclass(frozen=True)
  class PortDefinition:
      port_id: str
      source_event_id: str
      field_path: str
      recorded_value: Any
      baseline_value: Any
      description: str


  @dataclass
  class DecisionRecord:
      decision_id: str
      run_id: str
      agent_id: str
      decision_event_id: str
      ports: list[PortDefinition]
      decision_type: str
      policy_version: str | None = None
  ```
- Store decisions in a dedicated metadata schema or typed view over `events` where `event_type = 'model_call'` or `'tool_call'` with `payload['is_decision'] = True`.

### 3. Capture-Completeness Validator (`core/validator.py`)
- Implement graph completeness validation:
  1. `check_dangling_parents(run_id)`: Verifies all `causal_parent_ids` exist in PostgreSQL.
  2. `check_cross_run_isolation(run_id)`: Ensures no edges cross run boundaries.
  3. `check_intra_agent_continuity(run_id)`: Flags any agent timeline gaps where $E_{t+1}$ has no causal link to $E_t$.
  4. `check_decision_port_resolution(decision_id)`: Confirms every decision port binds to a valid ancestor event.

---

## Experiments

The following concrete experiments will prove or disprove the validity of the causal architecture:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ EXPERIMENT 1: The B x C Interaction Sensitivity & Stochastic Robustness     │
│ Target: Prove Shapley Interaction Index isolates true interactions from     │
│         stochastic temperature noise.                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│ EXPERIMENT 2: Shared-State Causal Recovery (Implicit Memory Channel)       │
│ Target: Prove Resource-Version Invariant recovers 100% of unannotated       │
│         memory dependencies without manual wiring.                          │
├─────────────────────────────────────────────────────────────────────────────┤
│ EXPERIMENT 3: Prompt Perturbation vs Semantic Port Ablation                 │
│ Target: Prove Port Substitution eliminates syntax-induced false positive    │
│         causal attributions.                                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Experiment 1: The $B \times C$ Interaction Sensitivity & Stochastic Robustness

#### Goal
Prove that the Shapley Interaction Index correctly isolates joint branch interactions from independent causes under varying model temperatures, whereas the naive 4-run matrix generates false positives.

#### Setup
1. **Synthetic Decision Function:** Implement merge agent $A_3$ evaluating credit approval:
   - Scenario I (Joint Interaction): Fails iff $B_3 = \text{"eligible"}$ AND $C_3 = \text{"low\_risk"}$ (the customer scenario in `fixture/fixture.json`).
   - Scenario II (Single Cause): Fails whenever $B_3 = \text{"eligible"}$, regardless of $C_3$.
   - Scenario III (Stochastic Baseline): Decision made by an LLM with temperature $T \in \{0.0, 0.3, 0.7, 1.0\}$.
2. **Evaluation:** Run 100 trials of each scenario using:
   - Method A: Naive 4-run deterministic matrix (`docs/thesis.md` §30.6).
   - Method B: Statistical Shapley Interaction Index with 5 Monte Carlo samples per cell.

#### Success Metric
- In Scenario I, Method B reports $I_{BC} > 0.7$ with $p < 0.01$ in $\ge 98\%$ of runs across all temperatures.
- In Scenario III, Method A falsely detects an interaction $> 15\%$ of the time at $T = 0.7$, whereas Method B maintains a False Positive Rate $< 1\%$ with confidence intervals spanning zero ($I_{BC} \pm \sigma$).

---

### Experiment 2: Shared-State Causal Recovery (Implicit Memory Channel)

#### Goal
Verify that the Resource-Version Invariant automatically recovers dependencies through shared memory without manual developer instrumentation.

#### Setup
1. Agent $B$ executes `memory.set("threshold", 0.9)`.
2. Agent $C$ executes `memory.get("threshold")` and uses it to filter risk records.
3. Planner $A$ merges $C$'s output into final decision $A_4$.
4. The test code deliberately omits `causal_parent_ids` in all memory calls.
5. Run two debugger configurations:
   - Baseline: Current Phase 2 codebase (`sdk/memory.py`).
   - Treatment: Phase 2.5 Resource-Version Registry enabled.

#### Success Metric
- In Baseline, `ancestors(A4)` contains only $\{A_4, A_3, C_2, C_1, A_1\}$. $B$'s write event is missing (0% causal recall on $B$).
- In Treatment, `ancestors(A4)` contains $B$'s write event and $B$'s prior execution branch (100% causal recall).

---

### Experiment 3: Prompt Perturbation vs Semantic Port Ablation

#### Goal
Demonstrate that Semantic Counterfactual Port substitution prevents prompt-syntax collapse from distorting causal attribution.

#### Setup
1. Construct 20 diverse LLM merge prompts formatted as Markdown tables, YAML frontmatter, and JSON blocks.
2. For each prompt, test two intervention mechanisms for $do(X = \emptyset)$:
   - Mechanism 1 (Ablation by Deletion): Remove the text block corresponding to parent $B$.
   - Mechanism 2 (Semantic Port Substitution): Substitute $B$'s text block with a typed schema default (e.g., `{"status": "UNKNOWN", "score": 0.0}`).
3. Measure the **Prompt Malformation Rate** (schema validation errors, parser exceptions, or model refusal strings like *"Invalid prompt formatting"*).

#### Success Metric
- Mechanism 1 causes prompt malformation errors in $> 35\%$ of trials, generating false positive causal flags.
- Mechanism 2 maintains a 0% prompt malformation error rate across all 20 formats, isolating pure semantic decision attribution.

---

## Summary Assessment

| Dimension | Current Architecture (Phases 1–2) | Proposed Breakthrough (Phase 2.5 SCM + Invariants) |
|---|---|---|
| **Causal Foundation** | Explicit `causal_parent_ids` array | Structural Causal Model + Resource-Versioned Invariants |
| **Environmental Memory** | Unlinked (Implicit Channels Dropped) | Automatically linked via Resource Version Tuples |
| **Intra-Agent Edges** | Manual caller passing | Auto-chained per-agent execution timeline |
| **Intervention Semantics** | String/event deletion (Ad-hoc) | Typed Semantic Port Substitution ($do(X = x^*)$) |
| **Attribution Math** | Deterministic 4-cell Boolean matrix | Game-Theoretic Shapley-Owen Interaction Indices |
| **Stochastic Robustness** | None (Vulnerable to temperature noise) | Statistical hypothesis testing ($p$-values, confidence bounds) |
| **LLM Provenance** | Unconditionally Coarse | Context-Chunk Sensitivity Verification |

This formulation provides a concrete, rigorous foundation for Phase 2.5, directly unblocking Phase 3 and Phase 5 while establishing a novel, defensible standard for causal attribution in multi-agent systems.
