"""Three-line local capture API for Agent-Casuality."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from core.decision import (
    AblationStrategy,
    DecisionPort,
    create_decision_contract,
    register_decision_evaluator,
)
from sdk.events import AgentClock, Event, record_event
from sdk.lifecycle import AgentRecord
from sdk.tools import capture_tool
from storage.sqlite import SQLiteEventStore

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class Runtime:
    """Active local capture context returned by :func:`init`."""

    log: SQLiteEventStore
    run_id: str
    agent_id: str
    clock: AgentClock

    def agent(self, *, role: str | None = None) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            agent_id = str(uuid4())
            clock = AgentClock()
            self.log.agents[agent_id] = AgentRecord(
                id=agent_id, run_id=self.run_id, role=role
            )
            return capture_tool(
                fn,
                agent_id=agent_id,
                clock=clock,
                log=self.log,
                run_id=self.run_id,
            )

        return decorate

    def merge_decision(
        self,
        *,
        ports: list[str] | tuple[str, ...],
        baselines: dict[str, Any] | None = None,
        decision_type: str | None = None,
    ) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            resolved_decision_type = decision_type or f"{fn.__module__}.{fn.__name__}"
            register_decision_evaluator(
                resolved_decision_type,
                lambda values: fn(**values),
            )

            @wraps(fn)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                bound = inspect.signature(fn).bind(*args, **kwargs)
                bound.apply_defaults()
                values = bound.arguments
                parent_ids: list[str] = []
                source_events: dict[str, Event] = {}
                for port_id in ports:
                    if port_id not in values:
                        raise TypeError(f"merge decision port {port_id!r} is not an argument")
                    prior_events = self.log.events()
                    source_event = next(
                        (
                            event
                            for event in reversed(prior_events)
                            if event.payload.get("output") == values[port_id]
                        ),
                        None,
                    )
                    source_parent_ids = [source_event.id] if source_event is not None else []
                    event, _ = record_event(
                        agent_id=self.agent_id,
                        clock=self.clock,
                        log=self.log,
                        run_id=self.run_id,
                        event_type="context_update",
                        payload={"output": values[port_id], "port_id": port_id},
                        causal_parent_ids=source_parent_ids,
                    )
                    source_events[port_id] = event
                    parent_ids.append(event.id)
                outcome = fn(*args, **kwargs)
                decision_id = f"{fn.__name__}:{uuid4()}"
                decision_event = Event(
                    agent_id=self.agent_id,
                    logical_seq=self.clock.allocate(
                        [source_events[port].logical_seq for port in ports]
                    ),
                    event_type="model_call",
                    payload={},
                    causal_parent_ids=parent_ids,
                    run_id=self.run_id,
                )
                decision = create_decision_contract(
                    decision_id=decision_id,
                    run_id=self.run_id,
                    agent_id=self.agent_id,
                    decision_event_id=decision_event.id,
                    decision_type=resolved_decision_type,
                    outcome=str(outcome),
                    ports=[
                        DecisionPort(
                            port_id=port,
                            source_event_id=source_events[port].id,
                            field_path="output",
                            recorded_value=values[port],
                            baseline_value=(baselines or {}).get(port),
                            strategy=(
                                AblationStrategy.CANONICAL_BASELINE
                                if baselines and port in baselines
                                else AblationStrategy.DEFAULT_SENTINEL
                            ),
                        )
                        for port in ports
                    ],
                )
                decision_event = Event(
                    id=decision_event.id,
                    agent_id=decision_event.agent_id,
                    logical_seq=decision_event.logical_seq,
                    event_type=decision_event.event_type,
                    payload=decision.to_event_payload({"output": outcome}),
                    causal_parent_ids=decision_event.causal_parent_ids,
                    run_id=decision_event.run_id,
                )
                self.log.append(decision_event)
                self.clock.set_last_event_id(decision_event.id)
                return outcome

            return wrapped  # type: ignore[return-value]

        return decorate


_runtime: Runtime | None = None


def init(path: str | Path | None = None) -> Runtime:
    """Start local capture using SQLite, creating ``.casuality/events.db``."""
    global _runtime
    database_path = path or os.environ.get("CASUALITY_DB_PATH", ".casuality/events.db")
    log = SQLiteEventStore(database_path)
    runtime = Runtime(log=log, run_id=str(uuid4()), agent_id=str(uuid4()), clock=AgentClock())
    log.agents[runtime.agent_id] = AgentRecord(
        id=runtime.agent_id, run_id=runtime.run_id, role="runtime"
    )
    _runtime = runtime
    return runtime


def _require_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Call casuality.init() before using capture decorators")
    return _runtime


def agent(*, role: str | None = None) -> Callable[[F], F]:
    """Decorate a function as a captured agent."""
    return _require_runtime().agent(role=role)


def merge_decision(
    *,
    ports: list[str] | tuple[str, ...],
    baselines: dict[str, Any] | None = None,
    decision_type: str | None = None,
) -> Callable[[F], F]:
    """Decorate a function as a captured multi-input decision."""
    return _require_runtime().merge_decision(
        ports=ports, baselines=baselines, decision_type=decision_type
    )


__all__ = ["Runtime", "agent", "init", "merge_decision"]
