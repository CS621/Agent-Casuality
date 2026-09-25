from __future__ import annotations

from pathlib import Path

import casuality
from core.decision import DecisionContract
from core.slicing import structural_slice
from core.validator import GraphValidator
from sdk.events import record_event
from storage.sqlite import SQLiteEventStore


def test_sqlite_store_reopens_events(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    first = SQLiteEventStore(path)
    casuality.init(path)
    runtime = casuality._runtime
    assert runtime is not None
    event, _ = record_event(
        agent_id="agent",
        clock=runtime.clock,
        log=first,
        event_type="context_update",
        payload={"output": "value"},
        run_id=runtime.run_id,
    )
    first.close()

    reopened = SQLiteEventStore(path)
    assert reopened.get(event.id) == event
    reopened.close()


def test_public_decorators_capture_a_causal_decision(tmp_path: Path) -> None:
    casuality.init(tmp_path / "events.db")

    @casuality.agent(role="researcher")
    def research(query: str) -> dict[str, str]:
        return {"answer": query}

    @casuality.merge_decision(ports=["research_summary"])
    def approve(research_summary: dict[str, str]) -> str:
        return "ok" if research_summary["answer"] == "yes" else "no"

    assert approve(research("yes")) == "ok"
    runtime = casuality._runtime
    assert runtime is not None
    decision = runtime.log.events()[-1]
    assert len(structural_slice(decision.id, runtime.log)) == 4
    report = GraphValidator(runtime.log).validate_run(runtime.run_id)
    assert report.is_valid, report.violations


def test_public_decision_sources_are_isolated_to_current_run(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    first_runtime = casuality.init(path)

    @casuality.agent(role="researcher")
    def first_research(_: str) -> str:
        return "eligible"

    @casuality.merge_decision(ports=["status"], decision_type="tests.first_run")
    def first_decision(status: str) -> str:
        return status

    assert first_decision(first_research("customer X")) == "eligible"
    first_event = first_runtime.log.events()[-1]
    first_contract = DecisionContract.from_event(first_event)
    assert first_contract is not None
    first_source_id = first_contract.ports[0].source_event_id
    first_runtime.log.close()

    second_runtime = casuality.init(path)

    @casuality.agent(role="researcher")
    def second_research(_: str) -> str:
        return "eligible"

    @casuality.merge_decision(ports=["status"], decision_type="tests.second_run")
    def second_decision(status: str) -> str:
        return status

    assert second_decision(second_research("customer Y")) == "eligible"
    second_event = second_runtime.log.events()[-1]
    second_contract = DecisionContract.from_event(second_event)
    assert second_contract is not None
    second_source = second_runtime.log.get(second_contract.ports[0].source_event_id)
    assert second_source is not None
    assert second_source.id != first_source_id
    assert second_source.run_id == second_runtime.run_id
    assert second_runtime.run_id != first_contract.run_id

    decision_slice = structural_slice(second_event.id, second_runtime.log)
    assert all(
        (event := second_runtime.log.get(event_id)) is not None
        and event.run_id == second_runtime.run_id
        for event_id in decision_slice.event_ids
    )
    report = GraphValidator(second_runtime.log).validate_run(second_runtime.run_id)
    assert report.is_valid, report.violations
    second_runtime.log.close()


def test_merge_decision_persists_baselines(tmp_path: Path) -> None:
    casuality.init(tmp_path / "events.db")

    @casuality.merge_decision(
        ports=["status"],
        baselines={"status": "ineligible"},
        decision_type="tests.approval",
    )
    def approve(status: str) -> str:
        return status

    assert approve("eligible") == "eligible"
    runtime = casuality._runtime
    assert runtime is not None
    contract = runtime.log.events()[-1].payload["decision_contract"]
    assert contract["decision_type"] == "tests.approval"
    assert contract["ports"][0]["baseline_value"] == "ineligible"
