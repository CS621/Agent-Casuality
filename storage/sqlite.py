"""Zero-configuration SQLite event store for local Agent-Casuality runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from core.provenance import ProvenanceEdge
from sdk.events import AgentClock, Event, InMemoryEventLog
from sdk.lifecycle import AgentRecord


class SQLiteEventStore(InMemoryEventLog):
    """Persist the event-log contract in a local SQLite database.

    The analytical layer can use this store exactly like the in-memory and
    PostgreSQL adapters, while a new CLI process can reopen the same file.
    """

    def __init__(self, path: str | Path = ".casuality/events.db") -> None:
        super().__init__()
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self._db_lock = Lock()
        self.agents: dict[str, AgentRecord] = {}
        self._create_schema()
        self._load_events()
        self._load_provenance()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    run_id TEXT,
                    agent_id TEXT NOT NULL,
                    logical_seq INTEGER NOT NULL,
                    wall_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    causal_parent_ids TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    idempotency_key TEXT,
                    UNIQUE(agent_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_events_agent_seq
                    ON events(agent_id, logical_seq);
                CREATE TABLE IF NOT EXISTS provenance_edges (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _event_from_row(row: tuple[Any, ...]) -> Event:
        wall_time = datetime.fromisoformat(row[4])
        if wall_time.tzinfo is None:
            wall_time = wall_time.replace(tzinfo=UTC)
        return Event(
            id=row[0],
            run_id=row[1],
            agent_id=row[2],
            logical_seq=row[3],
            wall_time=wall_time,
            event_type=row[5],
            causal_parent_ids=json.loads(row[6]),
            payload=json.loads(row[7]),
            idempotency_key=row[8],
        )

    def _load_events(self) -> None:
        rows = self.connection.execute(
            "SELECT id, run_id, agent_id, logical_seq, wall_time, event_type, "
            "causal_parent_ids, payload, idempotency_key FROM events ORDER BY rowid"
        ).fetchall()
        for row in rows:
            super().append(self._event_from_row(row))

    def _load_provenance(self) -> None:
        rows = self.connection.execute(
            "SELECT data FROM provenance_edges ORDER BY rowid"
        ).fetchall()
        self.provenance_edges = [ProvenanceEdge.from_dict(json.loads(row[0])) for row in rows]

    def append(self, event: Event) -> Event:
        existing = (
            self.get_by_idempotency_key(event.agent_id, event.idempotency_key)
            if event.idempotency_key
            else None
        )
        if existing is not None:
            return existing
        with self._db_lock:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO events (id, run_id, agent_id, logical_seq, wall_time, event_type, "
                    "causal_parent_ids, payload, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.id,
                        event.run_id,
                        event.agent_id,
                        event.logical_seq,
                        event.wall_time.isoformat(),
                        event.event_type,
                        json.dumps(event.causal_parent_ids),
                        json.dumps(event.payload),
                        event.idempotency_key,
                    ),
                )
        return super().append(event)

    def fetch_events(
        self, agent_id: str, *, since: int = 0, until: int | None = None
    ) -> list[Event]:
        events = [
            event
            for event in self.events()
            if event.agent_id == agent_id and event.logical_seq > since
        ]
        if until is not None:
            events = [event for event in events if event.logical_seq <= until]
        return sorted(events, key=lambda event: event.logical_seq)

    def get_latest_logical_seq(self, agent_id: str) -> int | None:
        event = max(self.fetch_events(agent_id), key=lambda item: item.logical_seq, default=None)
        return event.logical_seq if event else None

    def allocate_logical_seq(
        self, agent_id: str, clock: AgentClock, causal_parent_seqs: Any
    ) -> int:
        sequence = max(clock.current(), self.get_latest_logical_seq(agent_id) or 0)
        parent_max = max(causal_parent_seqs, default=sequence)
        clock.observe(max(sequence, parent_max))
        return clock.allocate()

    def ancestors(self, event_id: str) -> list[str]:
        if self.get(event_id) is None:
            return []
        result: list[str] = []
        pending = [event_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            event = self.get(current)
            if event is not None:
                pending.extend(event.causal_parent_ids)
        return result

    def record_provenance_edge(self, edge: ProvenanceEdge) -> ProvenanceEdge:
        with self._db_lock:
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO provenance_edges (id, data) VALUES (?, ?)",
                    (edge.id, json.dumps(edge.to_dict())),
                )
        existing = next((item for item in self.provenance_edges if item.id == edge.id), None)
        if existing is None:
            self.provenance_edges.append(edge)
        return existing or edge

    def get_provenance_edges_for_field(
        self, field_path: str, run_id: str | None = None
    ) -> list[ProvenanceEdge]:
        return [
            edge
            for edge in self.provenance_edges
            if edge.field_path == field_path and (run_id is None or edge.run_id == run_id)
        ]

    def close(self) -> None:
        self.connection.close()
