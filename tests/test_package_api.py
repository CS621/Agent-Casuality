from __future__ import annotations

from pathlib import Path

import casuality
from core.slicing import structural_slice
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
