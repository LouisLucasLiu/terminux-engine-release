# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

from terminux_engine.ecc.models import (
    CommandType,
    EventQuery,
    ProvenanceObs,
    ProvenanceSource,
    ProvenanceSpec,
    QueryOrder,
    ShellEvent,
)
from terminux_engine.store.repository import Repository, RepositoryError

logger = logging.getLogger(__name__)

# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS shell_events (
    event_id           TEXT     PRIMARY KEY,
    session_id         TEXT     NOT NULL,
    member_id          TEXT     NOT NULL,
    project_id         TEXT     NOT NULL,
    timestamp          TEXT     NOT NULL,
    work_directory     TEXT     NOT NULL,
    block              TEXT,
    command            TEXT     NOT NULL,
    command_type       TEXT     NOT NULL,
    targets            TEXT     NOT NULL,
    stdout_summary     TEXT,
    stderr_summary     TEXT,
    exit_code          INTEGER,
    duration_ms        INTEGER,
    extracted_data     TEXT     NOT NULL,
    artifacts_detected TEXT     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_member_id
    ON shell_events(member_id);
CREATE INDEX IF NOT EXISTS idx_events_project_id
    ON shell_events(project_id);
CREATE INDEX IF NOT EXISTS idx_events_block
    ON shell_events(block);
CREATE INDEX IF NOT EXISTS idx_events_command_type
    ON shell_events(command_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON shell_events(timestamp);

CREATE TABLE IF NOT EXISTS provenance_spec (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    step        TEXT     NOT NULL,
    member_id   TEXT     NOT NULL,
    data        TEXT     NOT NULL,
    source      TEXT     NOT NULL,
    written_at  TEXT     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pspec_step_member
    ON provenance_spec(step, member_id);

CREATE TABLE IF NOT EXISTS provenance_obs (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    step        TEXT     NOT NULL,
    member_id   TEXT     NOT NULL,
    data        TEXT     NOT NULL,
    event_id    TEXT     NOT NULL,
    observed_at TEXT     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pobs_step_member
    ON provenance_obs(step, member_id);
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dt_to_str(dt: datetime) -> str:
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime:
    if not isinstance(s, str):
        logger.warning("_str_to_dt: expected str, got %r; using now(utc)", s)
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_shell_event(row: sqlite3.Row) -> ShellEvent:
    try:
        extracted_data = json.loads(row["extracted_data"])
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse extracted_data for event_id=%s; using {}", row["event_id"])
        extracted_data = {}

    return ShellEvent(
        event_id=row["event_id"],
        session_id=row["session_id"],
        member_id=row["member_id"],
        project_id=row["project_id"],
        timestamp=_str_to_dt(row["timestamp"]),
        work_directory=row["work_directory"],
        block=row["block"],
        command=row["command"],
        command_type=CommandType(row["command_type"]) if row["command_type"] else CommandType.OTHER,
        targets=json.loads(row["targets"]),
        stdout_summary=row["stdout_summary"],
        stderr_summary=row["stderr_summary"],
        exit_code=row["exit_code"],
        duration_ms=row["duration_ms"],
        extracted_data=extracted_data,
        artifacts_detected=json.loads(row["artifacts_detected"]),
    )


def _row_to_provenance_spec(row: sqlite3.Row) -> ProvenanceSpec:
    return ProvenanceSpec(
        step=row["step"],
        member_id=row["member_id"],
        data=json.loads(row["data"]),
        source=ProvenanceSource(row["source"]),
        written_at=_str_to_dt(row["written_at"]),
    )


def _row_to_provenance_obs(row: sqlite3.Row) -> ProvenanceObs:
    return ProvenanceObs(
        step=row["step"],
        member_id=row["member_id"],
        data=json.loads(row["data"]),
        event_id=row["event_id"],
        observed_at=_str_to_dt(row["observed_at"]),
    )


# ── SQLiteRepository ──────────────────────────────────────────────────────────

class SQLiteRepository(Repository):

    def __init__(self, db_path: str) -> None:
        """
        db_path: SQLite 檔案絕對路徑。
                 若檔案不存在，自動建立。
                 若父目錄不存在，拋出 RepositoryError。
        """
        parent = os.path.dirname(db_path)
        if parent and not os.path.isdir(parent):
            raise RepositoryError(f"directory not found: {parent}")

        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_DDL)
            self._conn.commit()
        except sqlite3.Error as e:
            raise RepositoryError(str(e)) from e

        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        """關閉 DB 連線。多次呼叫靜默忽略。"""
        if self._closed:
            return
        try:
            self._conn.close()
        except sqlite3.Error as e:
            raise RepositoryError(str(e)) from e
        self._closed = True

    # ── Guard ─────────────────────────────────────────────────────────────────

    def _check_open(self) -> None:
        if self._closed:
            raise RepositoryError("repository is closed")

    # ── insert_event ──────────────────────────────────────────────────────────

    def insert_event(self, event: ShellEvent) -> None:
        self._check_open()
        sql = """
            INSERT INTO shell_events (
                event_id, session_id, member_id, project_id, timestamp,
                work_directory, block, command, command_type, targets,
                stdout_summary, stderr_summary, exit_code, duration_ms,
                extracted_data, artifacts_detected
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            event.event_id,
            event.session_id,
            event.member_id,
            event.project_id,
            _dt_to_str(event.timestamp),
            event.work_directory,
            event.block,
            event.command,
            event.command_type.value,
            json.dumps(event.targets),
            event.stdout_summary,
            event.stderr_summary,
            event.exit_code,
            event.duration_ms,
            json.dumps(event.extracted_data),
            json.dumps(event.artifacts_detected),
        )
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise RepositoryError(f"duplicate event_id: {event.event_id}") from e
            except sqlite3.Error as e:
                raise RepositoryError(str(e)) from e

    # ── update_event_extracted_data ───────────────────────────────────────────

    def update_event_extracted_data(
        self,
        event_id: str,
        extracted_data: dict,
        block: str | None,
    ) -> None:
        self._check_open()
        sql = """
            UPDATE shell_events
            SET extracted_data = ?, block = ?
            WHERE event_id = ?
        """
        # 確保存入 DB 的是合法 JSON 字串
        if isinstance(extracted_data, str):
            try:
                json.loads(extracted_data)
                data_str = extracted_data
            except json.JSONDecodeError:
                logger.warning("update_event_extracted_data: invalid JSON string for event_id=%s, using {}", event_id)
                data_str = "{}"
        else:
            try:
                data_str = json.dumps(extracted_data)
            except (TypeError, ValueError):
                logger.warning("update_event_extracted_data: cannot serialize extracted_data for event_id=%s, using {}", event_id)
                data_str = "{}"
        params = (data_str, block, event_id)
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.Error as e:
                raise RepositoryError(str(e)) from e

    # ── query_events ──────────────────────────────────────────────────────────

    def query_events(self, filters: EventQuery) -> list[ShellEvent]:
        self._check_open()

        conditions: list[str] = []
        params: list[object] = []

        if filters.member_id is not None:
            conditions.append("member_id = ?")
            params.append(filters.member_id)
        if filters.project_id is not None:
            conditions.append("project_id = ?")
            params.append(filters.project_id)
        if filters.session_id is not None:
            conditions.append("session_id = ?")
            params.append(filters.session_id)
        if filters.block is not None:
            conditions.append("block = ?")
            params.append(filters.block)
        if filters.command_type is not None:
            conditions.append("command_type = ?")
            params.append(filters.command_type.value)
        if filters.tool_signature is not None:
            conditions.append("json_extract(extracted_data, '$.tool_signature') = ?")
            params.append(filters.tool_signature)
        if filters.after is not None:
            conditions.append("timestamp >= ?")
            params.append(_dt_to_str(filters.after))
        if filters.before is not None:
            conditions.append("timestamp <= ?")
            params.append(_dt_to_str(filters.before))
        if filters.exit_code is not None:
            conditions.append("exit_code = ?")
            params.append(filters.exit_code)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        order = "ASC" if filters.order == QueryOrder.ASC else "DESC"
        limit = f"LIMIT {filters.limit}" if filters.limit is not None else ""

        sql = f"SELECT * FROM shell_events {where} ORDER BY timestamp {order} {limit}"

        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                rows = cursor.fetchall()
            except sqlite3.Error as e:
                raise RepositoryError(str(e)) from e

        return [_row_to_shell_event(row) for row in rows]

    # ── insert_provenance_spec ─────────────────────────────────────────────────

    def insert_provenance_spec(self, record: ProvenanceSpec) -> None:
        self._check_open()
        sql = """
            INSERT INTO provenance_spec (step, member_id, data, source, written_at)
            VALUES (?, ?, ?, ?, ?)
        """
        params = (
            record.step,
            record.member_id,
            json.dumps(record.data),
            record.source.value,
            _dt_to_str(record.written_at),
        )
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.Error as e:
                raise RepositoryError(str(e)) from e

    # ── _query_provenance_spec（template method 內部實作）─────────────────────

    def _query_provenance_spec(
        self, step: str, member_id: str
    ) -> ProvenanceSpec | None:
        self._check_open()
        sql = """
            SELECT * FROM provenance_spec
            WHERE step = ? AND member_id = ?
            ORDER BY written_at DESC
            LIMIT 1
        """
        with self._lock:
            try:
                cursor = self._conn.execute(sql, (step, member_id))
                row = cursor.fetchone()
            except sqlite3.Error as e:
                raise RepositoryError(str(e)) from e

        if row is None:
            return None
        return _row_to_provenance_spec(row)

    # ── insert_provenance_obs ─────────────────────────────────────────────────

    def insert_provenance_obs(self, record: ProvenanceObs) -> None:
        self._check_open()
        sql = """
            INSERT INTO provenance_obs (step, member_id, data, event_id, observed_at)
            VALUES (?, ?, ?, ?, ?)
        """
        params = (
            record.step,
            record.member_id,
            json.dumps(record.data),
            record.event_id,
            _dt_to_str(record.observed_at),
        )
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.Error as e:
                raise RepositoryError(str(e)) from e

    # ── _query_provenance_obs（template method 內部實作）──────────────────────

    def _query_provenance_obs(
        self, step: str, member_id: str
    ) -> list[ProvenanceObs]:
        self._check_open()
        sql = """
            SELECT * FROM provenance_obs
            WHERE step = ? AND member_id = ?
            ORDER BY observed_at DESC
        """
        with self._lock:
            try:
                cursor = self._conn.execute(sql, (step, member_id))
                rows = cursor.fetchall()
            except sqlite3.Error as e:
                raise RepositoryError(str(e)) from e

        return [_row_to_provenance_obs(row) for row in rows]
