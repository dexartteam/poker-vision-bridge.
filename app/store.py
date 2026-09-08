from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from app.models import DecisionView, SelectedAction, SolverResult, Street, VisionEvent, utc_now


@dataclass(slots=True)
class EventInsert:
    status: Literal["accepted", "duplicate", "rejected"]
    highest_seq: int | None
    resync_required: bool = False
    reason: str | None = None
    received_at: datetime | None = None


@dataclass(slots=True)
class ProjectionRecord:
    source_kind: Literal["vision", "platform"]
    source_id: str
    table_id: str
    boot_id: str
    frame_seq: int
    frame_hash: str
    hand_id: str
    source_seq: int
    state_hash: str
    stable_count: int
    observation: dict[str, Any]
    confidence: dict[str, Any]
    updated_at: datetime


class Store:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.database_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS ingest_events (
                event_id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                boot_id TEXT NOT NULL,
                source_seq INTEGER NOT NULL,
                table_id TEXT NOT NULL,
                hand_id TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL,
                UNIQUE(source_kind, source_id, boot_id, source_seq)
            );
            CREATE INDEX IF NOT EXISTS idx_events_stream
                ON ingest_events(source_kind, source_id, boot_id, source_seq);

            CREATE TABLE IF NOT EXISTS projections (
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                boot_id TEXT NOT NULL DEFAULT '',
                frame_seq INTEGER NOT NULL DEFAULT -1,
                frame_hash TEXT NOT NULL DEFAULT '',
                hand_id TEXT NOT NULL,
                source_seq INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                stable_count INTEGER NOT NULL,
                observation_json TEXT NOT NULL,
                confidence_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source_kind, source_id, table_id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                action_key TEXT NOT NULL UNIQUE,
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                hand_id TEXT NOT NULL,
                street TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                solver_request_json TEXT,
                solver_result_json TEXT,
                selected_json TEXT,
                execution_status TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_table
                ON decisions(table_id, hand_id, created_at);
            """
        )
        columns = await self.db.execute_fetchall("PRAGMA table_info(decisions)")
        if "attempts" not in {row["name"] for row in columns}:
            await self.db.execute(
                "ALTER TABLE decisions ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1"
            )
        projection_columns = await self.db.execute_fetchall("PRAGMA table_info(projections)")
        projection_column_names = {row["name"] for row in projection_columns}
        if "boot_id" not in projection_column_names:
            await self.db.execute(
                "ALTER TABLE projections ADD COLUMN boot_id TEXT NOT NULL DEFAULT ''"
            )
        if "frame_seq" not in projection_column_names:
            await self.db.execute(
                "ALTER TABLE projections ADD COLUMN frame_seq INTEGER NOT NULL DEFAULT -1"
            )
        if "frame_hash" not in projection_column_names:
            await self.db.execute(
                "ALTER TABLE projections ADD COLUMN frame_hash TEXT NOT NULL DEFAULT ''"
            )
        now = utc_now().isoformat()
        await self.db.execute(
            """
            UPDATE decisions
            SET status = 'interrupted',
                execution_status = COALESCE(execution_status, 'not_executed'),
                error = COALESCE(error, 'process stopped while solver request was pending'),
                updated_at = ?
            WHERE status = 'solver_pending'
            """,
            (now,),
        )
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    def _conn(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("store is not open")
        return self.db

    async def insert_event(
        self,
        event: VisionEvent,
        source_kind: Literal["vision", "platform"],
        state_hash: str,
    ) -> EventInsert:
        db = self._conn()
        payload_json = event.model_dump_json()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await db.execute_fetchall(
                    """
                    SELECT source_kind, source_id, boot_id, source_seq, state_hash,
                           payload_json, received_at
                    FROM ingest_events WHERE event_id = ?
                    """,
                    (event.event_id,),
                )
                highest_row = await db.execute_fetchall(
                    """
                    SELECT MAX(source_seq) AS highest
                    FROM ingest_events
                    WHERE source_kind = ? AND source_id = ? AND boot_id = ?
                    """,
                    (source_kind, event.source.agent_id, event.source.boot_id),
                )
                highest = highest_row[0]["highest"] if highest_row else None
                if existing:
                    row = existing[0]
                    exact = (
                        row["source_kind"] == source_kind
                        and row["source_id"] == event.source.agent_id
                        and row["boot_id"] == event.source.boot_id
                        and row["source_seq"] == event.source.seq
                        and row["state_hash"] == state_hash
                        and row["payload_json"] == payload_json
                    )
                    await db.rollback()
                    if exact:
                        return EventInsert(
                            "duplicate",
                            highest,
                            received_at=datetime.fromisoformat(row["received_at"]),
                        )
                    return EventInsert(
                        "rejected",
                        highest,
                        resync_required=True,
                        reason="event_id collision with different payload or source identity",
                    )

                collision = await db.execute_fetchall(
                    """
                    SELECT event_id FROM ingest_events
                    WHERE source_kind = ? AND source_id = ? AND boot_id = ? AND source_seq = ?
                    """,
                    (source_kind, event.source.agent_id, event.source.boot_id, event.source.seq),
                )
                if collision:
                    await db.rollback()
                    return EventInsert(
                        "rejected",
                        highest,
                        resync_required=True,
                        reason="source sequence was already used by another event",
                    )
                if highest is not None and event.source.seq < highest:
                    await db.rollback()
                    return EventInsert(
                        "rejected",
                        highest,
                        resync_required=True,
                        reason="out-of-order source sequence",
                    )

                gap = highest is not None and event.source.seq > highest + 1
                received_at = utc_now()
                await db.execute(
                    """
                    INSERT INTO ingest_events(
                        event_id, source_kind, source_id, boot_id, source_seq,
                        table_id, hand_id, state_hash, payload_json, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        source_kind,
                        event.source.agent_id,
                        event.source.boot_id,
                        event.source.seq,
                        event.table.table_id,
                        event.observation.hand_id,
                        state_hash,
                        payload_json,
                        received_at.isoformat(),
                    ),
                )
                await db.commit()
                return EventInsert(
                    "accepted",
                    event.source.seq,
                    resync_required=gap,
                    reason="sequence gap; full snapshot accepted" if gap else None,
                    received_at=received_at,
                )
            except BaseException:
                await db.rollback()
                raise

    async def get_projection(
        self,
        source_kind: Literal["vision", "platform"],
        source_id: str,
        table_id: str,
    ) -> ProjectionRecord | None:
        rows = await self._conn().execute_fetchall(
            """
            SELECT * FROM projections
            WHERE source_kind = ? AND source_id = ? AND table_id = ?
            """,
            (source_kind, source_id, table_id),
        )
        if not rows:
            return None
        row = rows[0]
        return ProjectionRecord(
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            table_id=row["table_id"],
            boot_id=row["boot_id"],
            frame_seq=row["frame_seq"],
            frame_hash=row["frame_hash"],
            hand_id=row["hand_id"],
            source_seq=row["source_seq"],
            state_hash=row["state_hash"],
            stable_count=row["stable_count"],
            observation=json.loads(row["observation_json"]),
            confidence=json.loads(row["confidence_json"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def upsert_projection(
        self,
        *,
        source_kind: Literal["vision", "platform"],
        source_id: str,
        table_id: str,
        boot_id: str,
        frame_seq: int,
        frame_hash: str,
        hand_id: str,
        source_seq: int,
        state_hash: str,
        stable_count: int,
        observation: dict[str, Any],
        confidence: dict[str, Any],
    ) -> ProjectionRecord:
        updated_at = utc_now()
        db = self._conn()
        async with self._write_lock:
            try:
                await db.execute(
                    """
                    INSERT INTO projections(
                        source_kind, source_id, table_id, boot_id, frame_seq, frame_hash,
                        hand_id, source_seq, state_hash, stable_count,
                        observation_json, confidence_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_kind, source_id, table_id) DO UPDATE SET
                        boot_id = excluded.boot_id,
                        frame_seq = excluded.frame_seq,
                        frame_hash = excluded.frame_hash,
                        hand_id = excluded.hand_id,
                        source_seq = excluded.source_seq,
                        state_hash = excluded.state_hash,
                        stable_count = excluded.stable_count,
                        observation_json = excluded.observation_json,
                        confidence_json = excluded.confidence_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_kind,
                        source_id,
                        table_id,
                        boot_id,
                        frame_seq,
                        frame_hash,
                        hand_id,
                        source_seq,
                        state_hash,
                        stable_count,
                        json.dumps(observation, sort_keys=True, separators=(",", ":")),
                        json.dumps(confidence, sort_keys=True, separators=(",", ":")),
                        updated_at.isoformat(),
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return ProjectionRecord(
            source_kind=source_kind,
            source_id=source_id,
            table_id=table_id,
            boot_id=boot_id,
            frame_seq=frame_seq,
            frame_hash=frame_hash,
            hand_id=hand_id,
            source_seq=source_seq,
            state_hash=state_hash,
            stable_count=stable_count,
            observation=observation,
            confidence=confidence,
            updated_at=updated_at,
        )

    async def create_pending_decision(
        self,
        *,
        decision_id: str,
        action_key: str,
        source_kind: Literal["vision", "platform"],
        source_id: str,
        table_id: str,
        hand_id: str,
        street: Street,
        state_hash: str,
        solver_request: dict[str, Any],
    ) -> tuple[DecisionView, bool]:
        db = self._conn()
        now = utc_now()
        async with self._write_lock:
            try:
                rows = await db.execute_fetchall(
                    "SELECT * FROM decisions WHERE action_key = ?",
                    (action_key,),
                )
                if rows:
                    existing = self._row_to_decision(rows[0])
                    if (
                        existing.status in {"interrupted", "solver_failed"}
                        and existing.state_hash == state_hash
                        and existing.attempts < 3
                    ):
                        await db.execute(
                            """
                            UPDATE decisions SET
                                status = 'solver_pending', attempts = attempts + 1,
                                solver_request_json = ?, solver_result_json = NULL,
                                selected_json = NULL, execution_status = NULL,
                                error = NULL, updated_at = ?
                            WHERE decision_id = ?
                            """,
                            (
                                json.dumps(
                                    solver_request, sort_keys=True, separators=(",", ":")
                                ),
                                now.isoformat(),
                                existing.decision_id,
                            ),
                        )
                        await db.commit()
                        refreshed = await db.execute_fetchall(
                            "SELECT * FROM decisions WHERE decision_id = ?",
                            (existing.decision_id,),
                        )
                        return self._row_to_decision(refreshed[0]), True
                    return existing, False
                try:
                    await db.execute(
                        """
                        INSERT INTO decisions(
                            decision_id, action_key, source_kind, source_id, table_id,
                            hand_id, street, state_hash, status, attempts, solver_request_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'solver_pending', 1, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            action_key,
                            source_kind,
                            source_id,
                            table_id,
                            hand_id,
                            street.value,
                            state_hash,
                            json.dumps(solver_request, sort_keys=True, separators=(",", ":")),
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    await db.commit()
                except aiosqlite.IntegrityError:
                    await db.rollback()
                    rows = await db.execute_fetchall(
                        "SELECT * FROM decisions WHERE action_key = ?",
                        (action_key,),
                    )
                    if rows:
                        return self._row_to_decision(rows[0]), False
                    raise
            except BaseException:
                await db.rollback()
                raise
            rows = await db.execute_fetchall(
                "SELECT * FROM decisions WHERE decision_id = ?",
                (decision_id,),
            )
            if not rows:
                raise RuntimeError("decision insert did not persist")
            return self._row_to_decision(rows[0]), True

    async def complete_decision(
        self,
        decision_id: str,
        *,
        status: str,
        solver_result: SolverResult | None = None,
        selected: SelectedAction | None = None,
        execution_status: str | None = None,
        error: str | None = None,
    ) -> DecisionView:
        db = self._conn()
        async with self._write_lock:
            try:
                await db.execute(
                    """
                    UPDATE decisions SET
                        status = ?,
                        solver_result_json = COALESCE(?, solver_result_json),
                        selected_json = COALESCE(?, selected_json),
                        execution_status = COALESCE(?, execution_status),
                        error = ?,
                        updated_at = ?
                    WHERE decision_id = ?
                    """,
                    (
                        status,
                        solver_result.model_dump_json() if solver_result else None,
                        selected.model_dump_json() if selected else None,
                        execution_status,
                        error,
                        utc_now().isoformat(),
                        decision_id,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        decision = await self.get_decision(decision_id)
        if decision is None:
            raise KeyError(decision_id)
        return decision

    async def update_execution(
        self,
        decision_id: str,
        execution_status: str,
        *,
        error: str | None = None,
    ) -> DecisionView:
        db = self._conn()
        async with self._write_lock:
            try:
                await db.execute(
                    """
                    UPDATE decisions
                    SET execution_status = ?, error = COALESCE(?, error), updated_at = ?
                    WHERE decision_id = ?
                    """,
                    (execution_status, error, utc_now().isoformat(), decision_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        decision = await self.get_decision(decision_id)
        if decision is None:
            raise KeyError(decision_id)
        return decision

    async def get_decision(self, decision_id: str) -> DecisionView | None:
        rows = await self._conn().execute_fetchall(
            "SELECT * FROM decisions WHERE decision_id = ?",
            (decision_id,),
        )
        return self._row_to_decision(rows[0]) if rows else None

    async def get_decision_by_action_key(self, action_key: str) -> DecisionView | None:
        rows = await self._conn().execute_fetchall(
            "SELECT * FROM decisions WHERE action_key = ?",
            (action_key,),
        )
        return self._row_to_decision(rows[0]) if rows else None

    async def list_decisions(self, limit: int = 50) -> list[DecisionView]:
        rows = await self._conn().execute_fetchall(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_decision(row) for row in rows]

    @staticmethod
    def _row_to_decision(row: aiosqlite.Row) -> DecisionView:
        solver_result = (
            SolverResult.model_validate_json(row["solver_result_json"])
            if row["solver_result_json"]
            else None
        )
        selected = (
            SelectedAction.model_validate_json(row["selected_json"])
            if row["selected_json"]
            else None
        )
        return DecisionView(
            decision_id=row["decision_id"],
            action_key=row["action_key"],
            status=row["status"],
            source_kind=row["source_kind"],
            table_id=row["table_id"],
            hand_id=row["hand_id"],
            street=Street(row["street"]),
            state_hash=row["state_hash"],
            attempts=row["attempts"],
            solver_request=(
                json.loads(row["solver_request_json"]) if row["solver_request_json"] else None
            ),
            solver_result=solver_result,
            selected=selected,
            execution_status=row["execution_status"],
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
