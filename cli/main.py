"""Causal debugger CLI for Phase 3 and Phase 4: slice, why, reconstruct, provenance.

Backends:
    --fixture PATH   run against a fixture.json file (no database needed)
    --db PATH        use a local SQLite database (default: .casuality/events.db)
    (default)        use SQLite, or PostgreSQL when DATABASE_URL is set

Examples:
    uv run python -m cli.main --fixture fixture/fixture.json agents
    uv run python -m cli.main --fixture fixture/fixture.json slice A4
    uv run python -m cli.main --fixture fixture/fixture.json why A4
    uv run python -m cli.main --fixture fixture/fixture.json provenance A3.output.approve
    uv run python -m cli.main reconstruct <agent-uuid> 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from core.decision import DecisionContract, create_fixture_decision
from core.explain import (
    build_evidence_package,
    explain,
    explanation_matches_format,
    load_env_file,
    render_evidence_summary,
)
from core.provenance import provenance
from core.reducer import canonical_json, hash_state, reconstruct
from core.replay import (
    PortIntervention,
    compute_shapley_interaction,
    counterfactual_replay,
    ddmin,
    test_fn_from,
)
from core.slicing import structural_slice, why
from sdk.events import Event, InMemoryEventLog
from storage.sqlite import SQLiteEventStore


class FixtureEventLog(InMemoryEventLog):
    """InMemoryEventLog preloaded from a fixture.json file, IDs preserved."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__()
        self.data = data
        self.agents = {agent["id"]: agent for agent in data["agents"]}
        self.run_info = data["run"]
        self.provenance_edges = list(data.get("provenance_edges", []))
        for record in data["events"]:
            self.append(
                Event(
                    id=record["id"],
                    agent_id=record["agent_id"],
                    logical_seq=record["logical_seq"],
                    event_type=record["event_type"],
                    payload=record["payload"],
                    causal_parent_ids=list(record["causal_parent_ids"]),
                    run_id=data["run"]["id"],
                )
            )


def load_fixture_backend(path: Path) -> FixtureEventLog:
    return FixtureEventLog(json.loads(path.read_text(encoding="utf-8")))


def load_postgres_backend() -> Any:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set")
    import psycopg

    from storage.postgres import PostgresEventStore

    connection = psycopg.connect(dsn)
    return PostgresEventStore(connection, lock_dsn=dsn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent-Casuality causal debugger")
    parser.add_argument("--fixture", type=Path, default=None, help="path to fixture.json")
    parser.add_argument(
        "--db", type=Path, default=None, help="local SQLite path (default: .casuality/events.db)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("agents", help="list agents (fixture backend only)")

    p_slice = sub.add_parser("slice", help="structural slice of an event")
    p_slice.add_argument("event_id")

    p_why = sub.add_parser("why", help="structural decision evidence for a merge event")
    p_why.add_argument("event_id")

    p_recon = sub.add_parser("reconstruct", help="rebuild one agent's state at a sequence")
    p_recon.add_argument("agent_id")
    p_recon.add_argument("target_seq", type=int)

    p_prov = sub.add_parser("provenance", help="trace field-level provenance chain")
    p_prov.add_argument("field_path", help="field path to trace (e.g. A3.output.approve)")

    p_replay = sub.add_parser("replay", help="counterfactual replay under port interventions")
    p_replay.add_argument("decision_id", help="decision contract id or event id")
    p_replay.add_argument(
        "interventions",
        nargs="*",
        help="port interventions in port_id=value format (e.g. customer_status=ineligible)",
    )

    p_inter = sub.add_parser("interaction", help="Shapley-Owen interaction analysis for a decision")
    p_inter.add_argument("decision_id", help="decision contract id or event id")
    p_inter.add_argument(
        "--samples", type=int, default=5, help="Monte Carlo samples per cell (default: 5)"
    )

    p_min = sub.add_parser("minimize", help="minimal slice via ddmin")
    p_min.add_argument("event_id", help="target event id to minimize slice for (e.g. A4)")
    p_min.add_argument(
        "--budget", type=int, default=200, help="maximum replay evaluations budget (default: 200)"
    )

    p_exp = sub.add_parser("explain", help="generate grounded LLM explanation for an event")
    p_exp.add_argument("event_id", help="failure or decision event id (e.g. A4)")
    p_exp.add_argument(
        "--api-key", default=None, help="OpenRouter API key (or set OPENROUTER_API_KEY)"
    )
    p_exp.add_argument(
        "--model", default=None, help="OpenRouter model (default: qwen/qwen3.8-27b:free)"
    )
    p_exp.add_argument(
        "--raw-evidence", action="store_true", help="output JSON evidence package"
    )
    p_exp.add_argument(
        "--no-llm", action="store_true", help="output evidence summary without calling LLM"
    )

    return parser


def resolve_decision_contract(decision_or_event_id: str, log: Any) -> DecisionContract:
    """Find a DecisionContract for a given decision_id or event_id.

    Resolves the contract from the log backend (fixture or PostgreSQL).
    Raises ValueError for unknown IDs rather than falling back silently to the
    bundled fixture, which would make misspelled commands appear successful and
    silently use incorrect data for PostgreSQL backends.
    """
    # Try to resolve from the log backend first (fixture or PostgreSQL)
    getter = getattr(log, "get", None)
    if callable(getter):
        ev = getter(decision_or_event_id)
        if ev is not None:
            contract = DecisionContract.from_event(ev)
            if contract is not None:
                return contract

    # For fixture-only special IDs, only use bundled fixture if explicitly using
    # fixture backend
    fixture_data = getattr(log, "data", None)
    fixture_ids = ("dec_customer_approval_A3", "A3", "A4")
    if fixture_data is not None and decision_or_event_id in fixture_ids:
        # Event was not found in log, but fixture backend is available
        # For A4 (downstream event), resolve A3 and return it
        if decision_or_event_id == "A4":
            return create_fixture_decision(fixture_data)
        if decision_or_event_id in ("A3", "dec_customer_approval_A3"):
            return create_fixture_decision(fixture_data)

    raise ValueError(f"Decision contract '{decision_or_event_id}' not found")


def cmd_agents(log: Any) -> None:
    agents = getattr(log, "agents", None)
    if not isinstance(agents, dict):
        sys.exit("agents is only supported with the --fixture backend")
    for agent_id, agent in agents.items():
        print(f"{agent_id}  {agent.get('role')}")


def cmd_slice(log: Any, event_id: str) -> None:
    slice_result = structural_slice(event_id, log)
    print(f"Structural slice of {event_id}: {len(slice_result)} events ({slice_result.source})")
    print(", ".join(slice_result.event_ids))
    print(f"({slice_result.to_dict()['note']})")


def cmd_why(log: Any, event_id: str) -> None:
    evidence = why(event_id, log)
    print(json.dumps(evidence.to_dict(), indent=2, default=str))


def cmd_reconstruct(log: Any, agent_id: str, target_seq: int) -> None:
    state = reconstruct(agent_id, target_seq, log=log)
    print(f"agent={agent_id} seq<={target_seq} status={state.status} hash={hash_state(state)}")
    print(canonical_json(state))


def cmd_provenance(log: Any, field_path: str) -> None:
    chain = provenance(field_path, log)
    print(chain.render())


def cmd_replay(log: Any, decision_id: str, intervention_args: list[str]) -> None:
    contract = resolve_decision_contract(decision_id, log)
    interventions: list[PortIntervention] = []
    for item in intervention_args:
        if "=" not in item:
            sys.exit(
                f"Intervention '{item}' must be in port_id=value format "
                "(e.g. customer_status=ineligible)"
            )
        port_id, raw_val = item.split("=", 1)
        try:
            val = json.loads(raw_val)
        except Exception:
            val = raw_val
        interventions.append(PortIntervention(port_id=port_id, substitute_value=val))

    cf_outcome = counterfactual_replay(contract, interventions, mode="recorded_output", log=log)
    print(f"Decision: {contract.decision_id}")
    print(f"Original outcome: {contract.outcome}")
    print(f"Counterfactual outcome: {cf_outcome}")


def cmd_interaction(log: Any, decision_id: str, samples: int) -> None:
    contract = resolve_decision_contract(decision_id, log)
    interaction = compute_shapley_interaction(contract, samples_per_cell=samples)
    print(json.dumps(interaction, indent=2, default=str))


def cmd_minimize(log: Any, event_id: str, budget: int) -> None:
    slice_result = structural_slice(event_id, log)
    contract = resolve_decision_contract(event_id, log)
    test_fn = test_fn_from(contract, failure_event_id=event_id)
    minimal_events = ddmin(slice_result.event_ids, test_fn, budget=budget)
    print(f"Minimal slice of {event_id}: {len(minimal_events)} events (ddmin)")
    print(", ".join(minimal_events))


def cmd_explain(
    log: Any,
    event_id: str,
    api_key: str | None = None,
    model: str | None = None,
    raw_evidence: bool = False,
    no_llm: bool = False,
) -> None:
    pkg = build_evidence_package(event_id, log)
    if raw_evidence:
        print(json.dumps(pkg, indent=2, default=str))

    if no_llm:
        if raw_evidence:
            print("\n" + "=" * 50 + "\n")
        print(render_evidence_summary(pkg))
        return

    if raw_evidence:
        print("\n" + "=" * 50 + "\n")

    try:
        text = explain(pkg, api_key=api_key, model=model)
    except (RuntimeError, ValueError) as exc:
        print(f"[casuality] LLM explanation unavailable: {exc}")
        print(
            "[casuality] Showing the offline evidence summary instead. "
            "Try --no-llm for this mode."
        )
        print(render_evidence_summary(pkg))
        return
    if explanation_matches_format(text, pkg):
        print(text)
    else:
        print("[casuality] LLM returned an unusable explanation; showing the evidence summary.")
        print(render_evidence_summary(pkg))


def main(argv: list[str] | None = None) -> None:
    load_env_file()
    args = build_parser().parse_args(argv)
    if args.fixture is not None:
        log: Any = load_fixture_backend(args.fixture)
    elif args.db is not None:
        log = SQLiteEventStore(args.db)
    elif os.environ.get("DATABASE_URL"):
        log = load_postgres_backend()
    else:
        log = SQLiteEventStore()
    if args.command == "agents":
        cmd_agents(log)
    elif args.command == "slice":
        cmd_slice(log, args.event_id)
    elif args.command == "why":
        cmd_why(log, args.event_id)
    elif args.command == "reconstruct":
        cmd_reconstruct(log, args.agent_id, args.target_seq)
    elif args.command == "provenance":
        cmd_provenance(log, args.field_path)
    elif args.command == "replay":
        cmd_replay(log, args.decision_id, args.interventions)
    elif args.command == "interaction":
        cmd_interaction(log, args.decision_id, args.samples)
    elif args.command == "minimize":
        cmd_minimize(log, args.event_id, args.budget)
    elif args.command == "explain":
        cmd_explain(
            log,
            args.event_id,
            api_key=args.api_key,
            model=args.model,
            raw_evidence=args.raw_evidence,
            no_llm=args.no_llm,
        )


if __name__ == "__main__":
    main()


