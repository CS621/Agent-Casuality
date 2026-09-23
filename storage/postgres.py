"""Minimal psycopg 3 storage adapter for Phase 1 through Phase 4.

The adapter owns the Phase 1 event/agent writes, the Phase 2 graph query,
the Phase 3 event range fetch plus snapshot persistence, and the Phase 4
field-level provenance persistence and queries.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from core.provenance import ProvenanceEdge, ProvenanceGrade
from core.snapshots import SnapshotRecord
from sdk.events import AgentClock, Event
from sdk.lifecycle import AgentRecord

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
DO $$ BEGIN
    CREATE TYPE event_type AS ENUM (
        'model_call', 'tool_call', 'tool_result', 'memory_read',
        'memory_write', 'context_update', 'agent_spawn', 'agent_finish',
        'agent_error', 'run_start', 'run_finish'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS agents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs(id),
    parent_agent_id uuid REFERENCES agents(id),
    spawned_at_event_id uuid,
    role text,
    status text NOT NULL DEFAULT 'active',
    lamport_offset bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs(id),
    agent_id uuid NOT NULL REFERENCES agents(id),
    logical_seq bigint NOT NULL,
    wall_time timestamptz NOT NULL DEFAULT now(),
    event_type event_type NOT NULL,
    causal_parent_ids uuid[] NOT NULL DEFAULT '{}',
    payload jsonb NOT NULL,
    idempotency_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, logical_seq)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid REFERENCES runs(id),
    agent_id uuid NOT NULL REFERENCES agents(id),
    logical_seq bigint NOT NULL,
    state jsonb NOT NULL,
    state_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
DO $$ BEGIN
    CREATE TYPE provenance_grade AS ENUM ('exact', 'coarse');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE TABLE IF NOT EXISTS provenance_edges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES runs(id),
    field_path text NOT NULL,
    source_event_id uuid NOT NULL REFERENCES events(id),
    source_path text,
    grade provenance_grade NOT NULL,
    transform text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE agents DROP CONSTRAINT IF EXISTS fk_spawned_at_event;
ALTER TABLE agents ADD CONSTRAINT fk_spawned_at_event
    FOREIGN KEY (spawned_at_event_id) REFERENCES events(id);
ALTER TABLE snapshots ALTER COLUMN run_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
    ON events (agent_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_agent_seq ON events (agent_id, logical_seq);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events (run_id, logical_seq);
CREATE INDEX IF NOT EXISTS idx_snapshots_agent_seq
    ON snapshots (agent_id, logical_seq DESC);
CREATE INDEX IF NOT EXISTS idx_provenance_field
    ON provenance_edges (run_id, field_path);
CREATE INDEX IF NOT EXISTS idx_provenance_source
    ON provenance_edges (source_event_id);
"""



class PostgresEventStore:
    def __init__(self, connection: Any, *, lock_dsn: str | None = None) -> None:
        self.connection = connection
        self.lock_dsn = lock_dsn

    def create_schema(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
        self.connection.commit()

    def append(self, event: Event) -> Event:
        record = event.to_record()
        event_id = self._uuid(record["id"], "Event.id")
        run_id = self._uuid(record["run_id"], "Event.run_id")
        agent_id = self._uuid(record["agent_id"], "Event.agent_id")
        try:
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - dependency is declared by the project
            raise RuntimeError("psycopg is required for PostgresEventStore") from exc
        columns = (
            "id, run_id, agent_id, logical_seq, wall_time, event_type, "
            "causal_parent_ids, payload, idempotency_key"
        )
        sql = f"""
            INSERT INTO events ({columns})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL DO NOTHING
            RETURNING id, run_id, agent_id, logical_seq, wall_time,
                      event_type, causal_parent_ids, payload, idempotency_key
        """
        try:
            with self.connection.cursor() as cursor:
                if event.idempotency_key is not None:
                    cursor.execute(
                        "SELECT id, run_id, agent_id, logical_seq, wall_time, event_type, "
                        "causal_parent_ids, payload, idempotency_key FROM events "
                        "WHERE agent_id = %s AND idempotency_key = %s",
                        (agent_id, event.idempotency_key),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        self.connection.rollback()
                        return self._row_to_event(existing_row)

                parent_ids = [
                    self._uuid(value, "Event.causal_parent_ids")
                    for value in record["causal_parent_ids"]
                ]
                if len(parent_ids) != len(set(parent_ids)):
                    raise ValueError("causal parent IDs must be unique")
                if parent_ids:
                    cursor.execute(
                        "SELECT id FROM events WHERE id = ANY(%s) AND run_id = %s",
                        (parent_ids, run_id),
                    )
                    owned_parent_ids = {row[0] for row in cursor.fetchall()}
                    if owned_parent_ids != set(parent_ids):
                        raise ValueError("causal parent events must belong to the event run")
                cursor.execute(
                    sql,
                    (
                        event_id,
                        run_id,
                        agent_id,
                        record["logical_seq"],
                        record["wall_time"],
                        record["event_type"],
                        parent_ids,
                        Jsonb(record["payload"]),
                        record["idempotency_key"],
                    ),
                )
                row = cursor.fetchone()
                if row is None and event.idempotency_key is not None:
                    cursor.execute(
                        "SELECT id, run_id, agent_id, logical_seq, wall_time, event_type, "
                        "causal_parent_ids, payload, idempotency_key FROM events "
                        "WHERE agent_id = %s AND idempotency_key = %s",
                        (agent_id, event.idempotency_key),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        # Roll back a sequence allocated in this transaction
                        # when another writer already stored this idempotent event.
                        self.connection.rollback()
                        return self._row_to_event(row)
        except Exception:
            self.connection.rollback()
            raise
        if row is None:
            raise RuntimeError("event insert did not return an event")
        self.connection.commit()
        return self._row_to_event(row)

    def allocate_logical_seq(
        self,
        agent_id: str,
        clock: AgentClock,
        causal_parent_seqs: Any = (),
    ) -> int:
        """Allocate an agent sequence atomically before event persistence."""
        agent_uuid = self._uuid(agent_id, "agent_id")
        parent_max = max(causal_parent_seqs, default=clock.current())
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT lamport_offset FROM agents WHERE id = %s FOR UPDATE",
                    (agent_uuid,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(f"agent {agent_id} does not exist")
                current_offset = row[0]
                cursor.execute(
                    "SELECT COALESCE(MAX(logical_seq), 0) FROM events WHERE agent_id = %s",
                    (agent_uuid,),
                )
                latest_event_seq = cursor.fetchone()[0]
                sequence = max(current_offset, latest_event_seq, parent_max) + 1
                cursor.execute(
                    "UPDATE agents SET lamport_offset = %s WHERE id = %s",
                    (sequence, agent_uuid),
                )
        except Exception:
            self.connection.rollback()
            raise
        clock.observe(sequence)
        return sequence

    def get(self, event_id: str) -> Event | None:
        event_uuid = self._uuid(event_id, "event_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, run_id, agent_id, logical_seq, wall_time, event_type, "
                "causal_parent_ids, payload, idempotency_key FROM events WHERE id = %s",
                (event_uuid,),
            )
            row = cursor.fetchone()
        return None if row is None else self._row_to_event(row)

    def get_by_idempotency_key(self, agent_id: str, key: str) -> Event | None:
        agent_uuid = self._uuid(agent_id, "agent_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, run_id, agent_id, logical_seq, wall_time, event_type, "
                "causal_parent_ids, payload, idempotency_key FROM events "
                "WHERE agent_id = %s AND idempotency_key = %s",
                (agent_uuid, key),
            )
            row = cursor.fetchone()
        return None if row is None else self._row_to_event(row)

    def ancestors(self, event_id: str) -> list[str]:
        """Return an event and all events reachable through causal parents."""
        event_uuid = self._uuid(event_id, "event_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                WITH RECURSIVE ancestors AS (
                    SELECT id, run_id, causal_parent_ids
                    FROM events
                    WHERE id = %s

                    UNION

                    SELECT e.id, e.run_id, e.causal_parent_ids
                    FROM events e
                    JOIN ancestors a
                      ON e.id = ANY(a.causal_parent_ids)
                     AND e.run_id = a.run_id
                )
                SELECT id::text
                FROM ancestors
                ORDER BY id::text
                """,
                (event_uuid,),
            )
            rows = cursor.fetchall()
        if not rows:
            raise ValueError(f"event {event_id} does not exist")
        return [str(row[0]) for row in rows]

    def fetch_events(
        self,
        agent_id: str,
        *,
        since: int = 0,
        until: int | None = None,
    ) -> list[Event]:
        """Return one agent's events with ``since < logical_seq <= until``.

        Ordered by ``logical_seq`` ascending, which is the replay order the
        reducer requires (thesis section 30.3).
        """
        agent_uuid = self._uuid(agent_id, "agent_id")
        sql = (
            "SELECT id, run_id, agent_id, logical_seq, wall_time, event_type, "
            "causal_parent_ids, payload, idempotency_key FROM events "
            "WHERE agent_id = %s AND logical_seq > %s"
        )
        params: list[Any] = [agent_uuid, since]
        if until is not None:
            sql += " AND logical_seq <= %s"
            params.append(until)
        sql += " ORDER BY logical_seq ASC"
        with self.connection.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
        return [self._row_to_event(row) for row in rows]

    def save(self, record: SnapshotRecord) -> SnapshotRecord:
        """Persist a snapshot row and return it with its database identity."""
        agent_id = self._uuid(record.agent_id, "SnapshotRecord.agent_id")
        run_id = (
            None
            if record.run_id is None
            else self._uuid(record.run_id, "SnapshotRecord.run_id")
        )
        try:
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - dependency is declared by the project
            raise RuntimeError("psycopg is required for PostgresEventStore") from exc
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO snapshots (run_id, agent_id, logical_seq, state, state_hash)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (
                        run_id,
                        agent_id,
                        record.logical_seq,
                        Jsonb(record.state),
                        record.state_hash,
                    ),
                )
                row = cursor.fetchone()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        if row is None:
            raise RuntimeError("snapshot insert did not return a row")
        created_at = row[1]
        if not isinstance(created_at, datetime):
            raise TypeError("Postgres returned a non-datetime snapshot created_at")
        return SnapshotRecord(
            agent_id=record.agent_id,
            logical_seq=record.logical_seq,
            state=dict(record.state),
            state_hash=record.state_hash,
            run_id=record.run_id,
            id=str(row[0]),
            created_at=created_at,
        )

    def latest_at_or_before(self, agent_id: str, logical_seq: int) -> SnapshotRecord | None:
        """Return the nearest snapshot at or before ``logical_seq``, or None."""
        agent_uuid = self._uuid(agent_id, "agent_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, run_id::text, agent_id::text, logical_seq, state,
                       state_hash, created_at
                FROM snapshots
                WHERE agent_id = %s AND logical_seq <= %s
                ORDER BY logical_seq DESC
                LIMIT 1
                """,
                (agent_uuid, logical_seq),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        created_at = row[6]
        if not isinstance(created_at, datetime):
            raise TypeError("Postgres returned a non-datetime snapshot created_at")
        return SnapshotRecord(
            id=str(row[0]),
            run_id=None if row[1] is None else str(row[1]),
            agent_id=str(row[2]),
            logical_seq=row[3],
            state=dict(row[4]),
            state_hash=str(row[5]),
            created_at=created_at,
        )

    @contextmanager
    def tool_invocation_lock(self, agent_id: str, invocation_id: str) -> Iterator[None]:
        """Serialize one invocation across processes and database connections."""
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency is declared by the project
            raise RuntimeError("psycopg is required for PostgreSQL tool locking") from exc
        dsn = self.lock_dsn or getattr(getattr(self.connection, "info", None), "dsn", None)
        if not dsn:
            raise RuntimeError(
                "PostgresEventStore requires a connection with a DSN for tool locking"
            )
        # Agent IDs are UUIDs in PostgreSQL, and the length prefix keeps the
        # two identities unambiguous without putting a NUL byte in the text
        # parameter sent to PostgreSQL.
        lock_key = f"{len(agent_id)}:{agent_id}{invocation_id}"
        with psycopg.connect(dsn) as lock_connection:
            with lock_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (lock_key,),
                )
            yield

    def create_agent(self, agent: AgentRecord) -> AgentRecord:
        agent_id = self._uuid(agent.id, "AgentRecord.id")
        run_id = self._uuid(agent.run_id, "AgentRecord.run_id")
        parent_agent_id = (
            None
            if agent.parent_agent_id is None
            else self._uuid(agent.parent_agent_id, "AgentRecord.parent_agent_id")
        )
        spawned_at_event_id = (
            None
            if agent.spawned_at_event_id is None
            else self._uuid(agent.spawned_at_event_id, "AgentRecord.spawned_at_event_id")
        )
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agents (id, run_id, parent_agent_id, spawned_at_event_id, role, "
                    "status, lamport_offset) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING RETURNING id",
                    (
                        agent_id,
                        run_id,
                        parent_agent_id,
                        spawned_at_event_id,
                        agent.role,
                        agent.status,
                        agent.lamport_offset,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute("SELECT id FROM agents WHERE id = %s", (agent_id,))
                    row = cursor.fetchone()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        if row is None:
            raise RuntimeError("agent insert did not return an agent")
        persisted = self.get_agent(str(row[0]))
        if persisted is None:
            raise RuntimeError("agent insert returned an unknown agent")
        return persisted

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        agent_uuid = self._uuid(agent_id, "agent_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, run_id, parent_agent_id, spawned_at_event_id, role, status, "
                "lamport_offset FROM agents WHERE id = %s",
                (agent_uuid,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return AgentRecord(
            id=str(row[0]),
            run_id=str(row[1]),
            parent_agent_id=None if row[2] is None else str(row[2]),
            spawned_at_event_id=None if row[3] is None else str(row[3]),
            role=row[4],
            status=row[5],
            lamport_offset=row[6],
        )

    def get_by_spawn_event(self, event_id: str) -> AgentRecord | None:
        event_uuid = self._uuid(event_id, "event_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, run_id, parent_agent_id, spawned_at_event_id, role, status, "
                "lamport_offset FROM agents WHERE spawned_at_event_id = %s",
                (event_uuid,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self.get_agent(str(row[0]))

    def get_latest_logical_seq(self, agent_id: str) -> int | None:
        agent_uuid = self._uuid(agent_id, "agent_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT MAX(logical_seq) FROM events WHERE agent_id = %s",
                (agent_uuid,),
            )
            row = cursor.fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _uuid(value: Any, field_name: str) -> UUID:
        if value is None:
            raise ValueError(f"{field_name} is required for PostgreSQL persistence")
        try:
            return UUID(str(value))
        except (AttributeError, ValueError) as exc:
            raise ValueError(
                f"{field_name} must be a UUID for PostgreSQL persistence: {value!r}"
            ) from exc

    @staticmethod
    def _row_to_event(row: tuple[Any, ...]) -> Event:
        wall_time = row[4]
        if not isinstance(wall_time, datetime):
            raise TypeError("Postgres returned a non-datetime wall_time")
        return Event(
            id=str(row[0]),
            run_id=None if row[1] is None else str(row[1]),
            agent_id=str(row[2]),
            logical_seq=row[3],
            wall_time=wall_time,
            event_type=str(row[5]),
            causal_parent_ids=[str(value) for value in (row[6] or [])],
            payload=dict(row[7]),
            idempotency_key=row[8],
        )

    def record_provenance_edge(self, edge: ProvenanceEdge) -> ProvenanceEdge:
        edge_id = self._uuid(edge.id, "ProvenanceEdge.id")
        run_id = self._uuid(edge.run_id, "ProvenanceEdge.run_id")
        source_event_id = self._uuid(edge.source_event_id, "ProvenanceEdge.source_event_id")
        sql = """
            INSERT INTO provenance_edges (
                id, run_id, field_path, source_event_id, source_path, grade, transform, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING id, run_id, field_path, source_event_id, source_path,
                      grade, transform, created_at
        """

        with self.connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    edge_id,
                    run_id,
                    edge.field_path,
                    source_event_id,
                    edge.source_path,
                    edge.grade,
                    edge.transform,
                    edge.created_at,
                ),
            )
            row = cursor.fetchone()
        self.connection.commit()
        if row is None:
            return edge
        return self._row_to_provenance_edge(row)

    def get_provenance_edges_for_field(
        self, field_path: str, run_id: str | None = None
    ) -> list[ProvenanceEdge]:
        with self.connection.cursor() as cursor:
            if run_id is None:
                cursor.execute(
                    "SELECT id, run_id, field_path, source_event_id, source_path, grade, "
                    "transform, created_at FROM provenance_edges "
                    "WHERE field_path = %s ORDER BY created_at ASC",
                    (field_path,),
                )
            else:
                run_uuid = self._uuid(run_id, "run_id")
                cursor.execute(
                    "SELECT id, run_id, field_path, source_event_id, source_path, grade, "
                    "transform, created_at FROM provenance_edges "
                    "WHERE field_path = %s AND run_id = %s ORDER BY created_at ASC",
                    (field_path, run_uuid),
                )
            rows = cursor.fetchall()
        return [self._row_to_provenance_edge(row) for row in rows]

    def get_provenance_edges_by_source_event(
        self, source_event_id: str
    ) -> list[ProvenanceEdge]:
        event_uuid = self._uuid(source_event_id, "source_event_id")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, run_id, field_path, source_event_id, source_path, grade, "
                "transform, created_at FROM provenance_edges "
                "WHERE source_event_id = %s ORDER BY created_at ASC",
                (event_uuid,),
            )
            rows = cursor.fetchall()
        return [self._row_to_provenance_edge(row) for row in rows]

    def query_provenance_chain(
        self, field_path: str, run_id: str | None = None
    ) -> list[ProvenanceEdge]:
        run_uuid = self._uuid(run_id, "run_id") if run_id is not None else None
        sql = """
            WITH RECURSIVE prov_cte AS (
                SELECT id, run_id, field_path, source_event_id, source_path, grade,
                       transform, created_at, 1 as depth
                FROM provenance_edges
                WHERE field_path = %s AND (%s::uuid IS NULL OR run_id = %s::uuid)
                UNION ALL
                SELECT p.id, p.run_id, p.field_path, p.source_event_id, p.source_path,
                       p.grade, p.transform, p.created_at, c.depth + 1
                FROM provenance_edges p
                INNER JOIN prov_cte c
                    ON p.field_path = c.source_path
                   AND p.run_id = c.run_id
                WHERE c.grade = 'exact' AND c.source_path IS NOT NULL AND c.depth < 50
            )
            SELECT DISTINCT ON (id) id, run_id, field_path, source_event_id, source_path,
                   grade, transform, created_at
            FROM prov_cte
            ORDER BY id, depth
        """
        with self.connection.cursor() as cursor:
            cursor.execute(sql, (field_path, run_uuid, run_uuid))
            rows = cursor.fetchall()
        return [self._row_to_provenance_edge(row) for row in rows]

    @staticmethod
    def _row_to_provenance_edge(row: tuple[Any, ...]) -> ProvenanceEdge:
        created_at = row[7]
        if not isinstance(created_at, datetime):
            raise TypeError("Postgres returned a non-datetime created_at")
        grade: ProvenanceGrade = "coarse" if str(row[5]) == "coarse" else "exact"
        return ProvenanceEdge(
            id=str(row[0]),
            run_id=str(row[1]),
            field_path=str(row[2]),
            source_event_id=str(row[3]),
            source_path=None if row[4] is None else str(row[4]),
            grade=grade,
            transform=None if row[6] is None else str(row[6]),
            created_at=created_at,
        )

