"""Phase 6: Grounded Causal Explanation layer.

Assembles a structured evidence package from:
- Structural slice (Phase 3)
- Reconstructed state and diffs (Phase 3)
- Dual-grade field-level provenance chains (Phase 4)
- Minimal slice via delta debugging (Phase 5)
- Shapley-Owen interaction attribution with bootstrap confidence (Phase 5)

Generates natural-language failure explanations strictly grounded in this evidence,
preventing hallucinated causal claims and explicitly reporting provenance grades.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

from core.decision import DecisionContract, create_fixture_decision
from core.provenance import provenance
from core.reducer import reconstruct
from core.replay import compute_shapley_interaction, ddmin, test_fn_from
from core.slicing import structural_slice

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.8-27b:free"

EXPLAIN_SYSTEM_PROMPT = """You are an expert causal debugging assistant analyzing a
multi-agent system failure.
Your explanation MUST be strictly grounded in the causal evidence package provided.

Follow these strict principles:
1. Cite specific event IDs (e.g. A3, B3, C3, A4) for every claim and causal link you describe.
2. Strictly distinguish empirical observations (what the event graph, state diff, and
   interaction test show) from hypotheses or inferences.
3. If any provenance link is marked 'coarse' (an LLM reasoning boundary without exact field
   tracking), explicitly state this and do not treat it with the same certainty as an
   'exact' provenance link.
4. When reporting why a failure occurred, distinguish whether the cause was:
   - A single upstream branch alone,
   - Multiple independent causes, or
   - A non-linear joint interaction between converging branches (e.g., B3 x C3 where
     neither alone caused failure, but their combination did).
5. State plainly when the provided evidence is insufficient to explain something, rather
   than filling the gap with plausible-sounding guesses.
6. Never invent or assume causal relationships that are not present in the supplied
   evidence package."""


def load_env_file() -> None:
    """Load key-value pairs from .env into os.environ if not already set."""
    env_path = Path(".env")
    if not env_path.exists():
        # Try parent directory
        env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass


def _resolve_contract_for_evidence(event_id: str, log: Any) -> DecisionContract | None:
    """Attempt to locate or infer a DecisionContract for the event or its parents."""
    # 1. Check if event itself is a decision
    getter = getattr(log, "get", None)
    if callable(getter):
        ev = getter(event_id)
        if ev is not None:
            c = DecisionContract.from_event(ev)
            if c is not None:
                return c
            # Check causal parents for a decision
            for parent_id in getattr(ev, "causal_parent_ids", []):
                parent_ev = getter(parent_id)
                if parent_ev is not None:
                    c = DecisionContract.from_event(parent_ev)
                    if c is not None:
                        return c

    # 2. Check fixture data
    fixture_data = getattr(log, "data", None)
    if fixture_data is not None:
        fixture_ids = ("dec_customer_approval_A3", "A3", "A4")
        if event_id in fixture_ids:
            return create_fixture_decision(fixture_data)
        for ev_rec in fixture_data.get("events", []):
            if ev_rec["id"] == event_id:
                for parent_id in ev_rec.get("causal_parent_ids", []):
                    if parent_id in fixture_ids:
                        return create_fixture_decision(fixture_data)

    return None


def build_evidence_package(
    event_id: str,
    log: Any,
    decision_id: str | None = None,
    ddmin_budget: int = 200,
    samples_per_cell: int = 5,
) -> dict[str, Any]:
    """Assemble a comprehensive causal evidence package for an event.

    Gathers:
    1. Failure event metadata and status
    2. Structural causal slice (backward DAG reachability)
    3. Associated DecisionContract (if applicable)
    4. Minimal causal slice via delta debugging (ddmin)
    5. Dual-grade field-level provenance chains with exact vs coarse grades
    6. Shapley-Owen interaction attribution with bootstrap confidence
    7. State reconstruction for involved agents
    """
    # 1. Target Event Details
    target_event: dict[str, Any] = {}
    getter = getattr(log, "get", None)
    if callable(getter):
        ev = getter(event_id)
        if ev is not None:
            target_event = {
                "id": ev.id,
                "agent_id": ev.agent_id,
                "logical_seq": ev.logical_seq,
                "event_type": ev.event_type,
                "payload": ev.payload,
                "causal_parent_ids": ev.causal_parent_ids,
                "run_id": ev.run_id,
            }
    if not target_event and hasattr(log, "data") and isinstance(log.data, dict):
        for e in log.data.get("events", []):
            if e.get("id") == event_id:
                target_event = dict(e)
                break

    # 2. Structural Slice
    s_slice = structural_slice(event_id, log)
    slice_event_ids = list(s_slice.event_ids)

    # 3. Decision Contract Resolution
    contract: DecisionContract | None = None
    if decision_id:
        if callable(getter):
            dev = getter(decision_id)
            if dev is not None:
                contract = DecisionContract.from_event(dev)
        if contract is None and hasattr(log, "data") and isinstance(log.data, dict):
            contract = create_fixture_decision(log.data)
    if contract is None:
        contract = _resolve_contract_for_evidence(event_id, log)

    decision_summary: dict[str, Any] | None = None
    minimal_slice_summary: dict[str, Any] | None = None
    interaction_summary: dict[str, Any] | None = None

    if contract is not None:
        decision_summary = {
            "decision_id": contract.decision_id,
            "decision_event_id": contract.decision_event_id,
            "agent_id": contract.agent_id,
            "decision_type": contract.decision_type,
            "outcome": contract.outcome,
            "ports": {
                port.port_id: {
                    "source_event_id": port.source_event_id,
                    "field_path": port.field_path,
                    "recorded_value": port.recorded_value,
                    "baseline_value": port.baseline_value,
                }
                for port in contract.ports
            },
        }

        # 4. Minimal Slice (ddmin)
        try:
            test_fn = test_fn_from(contract, failure_event_id=event_id)
            min_ids = ddmin(slice_event_ids, test_fn, budget=ddmin_budget)
            minimal_slice_summary = {
                "event_ids": min_ids,
                "count": len(min_ids),
                "structural_count": len(slice_event_ids),
                "reduction_ratio": round(1.0 - (len(min_ids) / max(len(slice_event_ids), 1)), 3),
                "method": "ddmin",
            }
        except Exception as e:
            minimal_slice_summary = {"error": str(e), "method": "ddmin"}

        # 5. Shapley Interaction Analysis
        try:
            interaction_summary = compute_shapley_interaction(
                contract, samples_per_cell=samples_per_cell
            )
        except Exception as e:
            interaction_summary = {"error": str(e)}

    # 6. Provenance Chains
    provenance_paths_to_check: list[str] = []
    # If contract exists, check its decision outputs and inputs
    if contract is not None:
        if contract.decision_event_id:
            provenance_paths_to_check.append(f"{contract.decision_event_id}.output.approve")
            for port in contract.ports:
                if port.field_path:
                    provenance_paths_to_check.append(f"{port.source_event_id}.{port.field_path}")
    if target_event:
        provenance_paths_to_check.append(f"{event_id}.final_answer")
        provenance_paths_to_check.append(f"{event_id}.output")

    provenance_results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for fpath in provenance_paths_to_check:
        if fpath in seen_paths:
            continue
        seen_paths.add(fpath)
        try:
            chain = provenance(fpath, log)
            if len(chain) > 0:
                edges = [
                    {
                        "field_path": edge.field_path,
                        "source_event_id": edge.source_event_id,
                        "source_path": edge.source_path,
                        "grade": edge.grade.value
                        if hasattr(edge.grade, "value")
                        else str(edge.grade),
                        "transform": edge.transform,
                    }
                    for edge in chain.edges
                ]
                grades = [e["grade"] for e in edges]
                provenance_results.append(
                    {
                        "target_field": fpath,
                        "edges": edges,
                        "grades": grades,
                        "all_exact": all(g == "exact" for g in grades),
                        "has_coarse": any(g == "coarse" for g in grades),
                    }
                )
        except Exception:
            pass

    # 7. Agent State Reconstruction
    agent_states: dict[str, Any] = {}
    if target_event and "agent_id" in target_event and "logical_seq" in target_event:
        try:
            st = reconstruct(target_event["agent_id"], target_event["logical_seq"], log=log)
            agent_states[target_event["agent_id"]] = {
                "target_seq": target_event["logical_seq"],
                "status": st.status,
                "memory": st.memory,
                "tool_outputs": st.tool_outputs,
                "open_tools": st.open_tools,
                "context": st.context,
            }
        except Exception as e:
            agent_states[target_event["agent_id"]] = {"error": str(e)}

    return {
        "target_event_id": event_id,
        "target_event": target_event,
        "structural_slice": {
            "root_event_id": event_id,
            "event_ids": slice_event_ids,
            "count": len(slice_event_ids),
            "source": s_slice.source,
            "note": s_slice.to_dict().get("note", ""),
        },
        "decision": decision_summary,
        "minimal_slice": minimal_slice_summary,
        "interaction_attribution": interaction_summary,
        "provenance": provenance_results,
        "agent_states": agent_states,
    }


def explain(
    evidence_package: dict[str, Any] | str,
    api_key: str | None = None,
    model: str | None = None,
    client: httpx.Client | None = None,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    load_env: bool = True,
) -> str:
    """Generate a causal failure explanation grounded in an evidence package.

    Calls OpenRouter chat completions with the grounded explanation prompt.
    """
    if load_env:
        load_env_file()

    resolved_api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not resolved_api_key:
        raise ValueError(
            "OPENROUTER_API_KEY is not set. "
            "Please provide --api-key, export OPENROUTER_API_KEY in your environment, "
            "or place it in .env."
        )

    resolved_model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    evidence_text = (
        evidence_package
        if isinstance(evidence_package, str)
        else json.dumps(evidence_package, indent=2, default=str)
    )

    headers = {
        "Authorization": f"Bearer {resolved_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Psionic-labs/Agent-Casuality",
        "X-Title": "Agent-Casuality Causal Debugger",
    }

    payload = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Explain this agent failure based strictly on the provided causal "
                    f"evidence package.\n\n{evidence_text}"
                ),
            },
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
    }

    own_client = False
    if client is None:
        import httpx

        client = httpx.Client(timeout=60.0)
        own_client = True

    try:
        delay = initial_delay
        for attempt in range(max_retries + 1):
            response = client.post(OPENROUTER_API_URL, headers=headers, json=payload)
            if response.status_code == 200:
                data = response.json()
                choices = data.get("choices", [])
                if not choices:
                    raise RuntimeError(f"OpenRouter returned no completion choices: {data}")
                msg = choices[0].get("message", {})
                content = (
                    msg.get("content")
                    or msg.get("reasoning_content")
                    or msg.get("reasoning")
                    or msg.get("text")
                    or ""
                )
                return str(content).strip()

            if response.status_code == 429:
                if attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                err_msg = response.text
                try:
                    err_json = response.json()
                    err_msg = err_json.get("error", {}).get("message", err_msg)
                except Exception:
                    pass
                raise RuntimeError(
                    f"OpenRouter model '{resolved_model}' rate-limited (HTTP 429): {err_msg}. "
                    "You can specify a different model via --model "
                    "(e.g. nex-agi/nex-n2.5-mini:free or a paid model) or retry in a few moments."
                )

            # Other HTTP errors
            raise RuntimeError(
                f"OpenRouter request failed with status {response.status_code}: {response.text}"
            )
    finally:
        if own_client:
            client.close()

    raise RuntimeError("Failed to generate explanation after retries.")
