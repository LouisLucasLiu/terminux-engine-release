# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Orchestration 狀態的 SQLite durable ledger。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal
from uuid import uuid4


_LOG = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS orchestration_runs (
    run_id TEXT PRIMARY KEY,
    workflow_type TEXT NOT NULL,
    domain_run_id TEXT,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    terminal_reason TEXT,
    owner TEXT,
    recovery_disposition TEXT,
    metadata_json TEXT,
    child_pid INTEGER,
    last_heartbeat_at REAL,
    kernel_checkpoint_json TEXT,
    pause_requested_at REAL,
    cancel_requested_at REAL,
    cancel_immediate INTEGER,
    parent_run_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_domain ON orchestration_runs(domain_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON orchestration_runs(workflow_type);
CREATE INDEX IF NOT EXISTS idx_runs_status ON orchestration_runs(status);

CREATE TABLE IF NOT EXISTS orchestration_steps (
    step_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    run_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    parameters_json TEXT,
    cwd TEXT NOT NULL,
    expected_packages_json TEXT,
    dispatch_key TEXT,
    command_status TEXT NOT NULL DEFAULT 'pending',
    dispatch_status TEXT NOT NULL DEFAULT 'pending',
    evidence_status TEXT NOT NULL DEFAULT 'pending',
    submitted_at REAL,
    command_finished_at REAL,
    dispatch_finished_at REAL,
    evidence_ready_at REAL,
    exit_code INTEGER,
    outcome_summary_json TEXT,
    terminal_reason TEXT,
    owner TEXT,
    recovery_disposition TEXT,
    attempt_reason TEXT,
    PRIMARY KEY (run_id, step_id, attempt),
    FOREIGN KEY (run_id) REFERENCES orchestration_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_steps_run ON orchestration_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_steps_run_step_attempt ON orchestration_steps(run_id, step_id, attempt);
CREATE INDEX IF NOT EXISTS idx_steps_dispatch_key ON orchestration_steps(dispatch_key);
CREATE INDEX IF NOT EXISTS idx_steps_tool ON orchestration_steps(tool_id);

CREATE TABLE IF NOT EXISTS orchestration_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_id TEXT,
    attempt INTEGER,
    state_kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    timestamp REAL NOT NULL,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_transitions_run ON orchestration_transitions(run_id);
CREATE INDEX IF NOT EXISTS idx_transitions_step ON orchestration_transitions(step_id, attempt);
"""

_TERMINAL_RUN_STATES = frozenset(
    {"completed", "failed", "timeout", "cancelled", "abandoned"}
)

_STEP_TABLE = "orchestration_steps"
_STEP_COLUMNS = (
    "step_id",
    "attempt",
    "run_id",
    "tool_id",
    "parameters_json",
    "cwd",
    "expected_packages_json",
    "dispatch_key",
    "command_status",
    "dispatch_status",
    "evidence_status",
    "submitted_at",
    "command_finished_at",
    "dispatch_finished_at",
    "evidence_ready_at",
    "exit_code",
    "outcome_summary_json",
    "terminal_reason",
    "owner",
    "recovery_disposition",
    "attempt_reason",
)
_STEP_V1_PRIMARY_KEY = ("step_id", "attempt")
_STEP_V2_PRIMARY_KEY = ("run_id", "step_id", "attempt")
_STEP_FINALIZE_STATUSES = frozenset({"failed", "timeout", "cancelled", "abandoned"})
_TERMINAL_COMMAND_STATES = frozenset({"completed", "failed", "timeout"})
_TERMINAL_DISPATCH_STATES = frozenset({"completed", "failed", "timeout"})
_TERMINAL_EVIDENCE_STATES = frozenset({"ready", "not_ready", "timeout"})

LIFECYCLE_PAUSE_REQUESTED = "pause_requested"
LIFECYCLE_PAUSED = "paused"
LIFECYCLE_RESUME_STARTED = "resume_started"
LIFECYCLE_RESUMED = "resumed"
LIFECYCLE_CANCEL_REQUESTED_SOFT = "cancel_requested_soft"
LIFECYCLE_CANCEL_REQUESTED_IMMEDIATE = "cancel_requested_immediate"
LIFECYCLE_CANCELLED_SOFT = "cancelled_soft"
LIFECYCLE_CANCELLED_IMMEDIATE = "cancelled_immediate"
LIFECYCLE_RETRY_SPAWNED = "retry_spawned"
LIFECYCLE_PENDING_REVIEW_DISMISSED = "pending_review_dismissed"

TERMINAL_REASON_CHECKPOINT_SERIALIZATION_FAILED = "checkpoint_serialization_failed"
TERMINAL_REASON_CHECKPOINT_DESERIALIZE_FAILED = "checkpoint_deserialize_failed"
TERMINAL_REASON_USER_CANCELLED = "user_cancelled"
TERMINAL_REASON_USER_CANCELLED_IMMEDIATE = "user_cancelled_immediate"
TERMINAL_REASON_SPAWN_FAILED = "spawn_failed"

RECOVERY_DISPOSITION_PENDING_REVIEW = "pending_review"
RECOVERY_DISPOSITION_RETRIED = "retried"
RECOVERY_DISPOSITION_DISMISSED = "dismissed"

_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "run": {
        "pending": frozenset({"running"}),
        "running": _TERMINAL_RUN_STATES | frozenset({"paused"}),
        "paused": frozenset({"running", "cancelled", "failed"}),
        "completed": frozenset(),
        "failed": frozenset(),
        "timeout": frozenset(),
        "cancelled": frozenset(),
        "abandoned": frozenset(),
    },
    "command": {
        "pending": frozenset({"running"}),
        "running": _TERMINAL_COMMAND_STATES,
        "completed": frozenset(),
        "failed": frozenset(),
        "timeout": frozenset(),
    },
    "dispatch": {
        "pending": frozenset({"running"}),
        "running": _TERMINAL_DISPATCH_STATES,
        "completed": frozenset(),
        "failed": frozenset(),
        "timeout": frozenset(),
    },
    "evidence": {
        "pending": frozenset({"ready", "not_ready", "timeout"}),
        "ready": frozenset(),
        "not_ready": frozenset(),
        "timeout": frozenset(),
    },
}


@dataclass(frozen=True)
class RunRecord:
    """一筆 orchestration run 的目前狀態。"""

    run_id: str
    workflow_type: str
    domain_run_id: str | None
    status: str
    started_at: float
    finished_at: float | None
    terminal_reason: str | None
    owner: str | None
    recovery_disposition: str | None
    metadata: dict[str, Any] | None
    child_pid: int | None = None
    last_heartbeat_at: float | None = None
    kernel_checkpoint_json: str | None = None
    pause_requested_at: float | None = None
    cancel_requested_at: float | None = None
    cancel_immediate: int | None = None
    parent_run_id: str | None = None


@dataclass(frozen=True)
class StepRecord:
    """一筆 orchestration step attempt 的目前狀態。"""

    step_id: str
    attempt: int
    run_id: str
    tool_id: str
    parameters: dict[str, Any] | None
    cwd: str
    expected_packages: list[str]
    dispatch_key: str | None
    command_status: str
    dispatch_status: str
    evidence_status: str
    submitted_at: float | None
    command_finished_at: float | None
    dispatch_finished_at: float | None
    evidence_ready_at: float | None
    exit_code: int | None
    outcome_summary: dict[str, Any] | None
    terminal_reason: str | None
    owner: str | None
    recovery_disposition: str | None
    attempt_reason: str | None = None


@dataclass(frozen=True)
class ConditionalRunResult:
    """條件式 run operation 的套用結果與目前 durable row。"""

    applied: bool
    run: RunRecord


@dataclass(frozen=True)
class RetryForkResult:
    """條件式 retry fork 的結果。"""

    applied: bool
    parent: RunRecord
    child: RunRecord | None


@dataclass(frozen=True)
class TransitionRecord:
    """一筆 append-only 的狀態轉移。"""

    transition_id: int
    run_id: str
    step_id: str | None
    attempt: int | None
    state_kind: str
    from_state: str | None
    to_state: str
    timestamp: float
    reason: str | None


class OrchestrationLedger:
    """Orchestration 狀態的 durable 儲存。"""

    DEFAULT_DB_PATH: ClassVar[Path] = (
        Path.home() / ".terminux" / "orchestration" / "ledger.sqlite"
    )
    _BUSY_TIMEOUT_MS: ClassVar[int] = 5000
    _MAX_RETRIES: ClassVar[int] = 3

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """建立 connection 並執行可重複的 schema migration。"""
        if db_path is not None:
            self._db_path = Path(db_path)
        else:
            override = os.environ.get("TERMINUX_ORCHESTRATION_LEDGER_PATH")
            if override:
                self._db_path = Path(override)
            else:
                self._db_path = Path.home() / ".terminux" / "orchestration" / "ledger.sqlite"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._write_lock = threading.RLock()
        self._closed = False
        self._connection = self._connect()
        try:
            self._preflight_existing_step_table(self._connection)
            self._connection.executescript(_DDL)
            self._migrate_schema()
        except sqlite3.OperationalError:
            _LOG.exception("Orchestration ledger schema migration failed: %s", self._db_path)
            self._connection.close()
            self._closed = True
            raise

    @staticmethod
    def _preflight_existing_step_table(connection: sqlite3.Connection) -> None:
        """避免未知舊 table 在 DDL 建 index 前被 SQLite 誤操作。"""
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_STEP_TABLE,),
        ).fetchone()
        if exists is None:
            return
        columns = connection.execute(f"PRAGMA table_info({_STEP_TABLE})").fetchall()
        names = {row["name"] for row in columns}
        primary_key = tuple(
            row["name"]
            for row in sorted(columns, key=lambda row: int(row["pk"]))
            if int(row["pk"]) > 0
        )
        if (
            not {"step_id", "attempt", "run_id"} <= names
            or primary_key not in {_STEP_V1_PRIMARY_KEY, _STEP_V2_PRIMARY_KEY}
        ):
            raise RuntimeError("unrecognized orchestration_steps schema; migration refused")

    def _migrate_schema(self) -> None:
        """以可重複方式補齊既有欄位，並將 step identity 升級至 v2。"""
        additions = {
            "orchestration_runs": {
                "child_pid": "INTEGER",
                "last_heartbeat_at": "REAL",
                "kernel_checkpoint_json": "TEXT",
                "pause_requested_at": "REAL",
                "cancel_requested_at": "REAL",
                "cancel_immediate": "INTEGER",
                "parent_run_id": "TEXT",
            },
            "orchestration_steps": {"attempt_reason": "TEXT"},
        }
        connection = self._require_connection()
        with connection:
            for table, columns in additions.items():
                existing = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, column_type in columns.items():
                    if name not in existing:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"
                        )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_heartbeat "
                "ON orchestration_runs(status, last_heartbeat_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_parent "
                "ON orchestration_runs(parent_run_id)"
            )
        step_schema = self._detect_step_schema(connection)
        if step_schema == "v1":
            self._backup_and_migrate_step_table(connection)
        elif step_schema != "v2":
            raise RuntimeError("unrecognized orchestration_steps schema; migration refused")

    @staticmethod
    def _identifier(name: str) -> str:
        """安全引用 SQLite schema identifier。"""
        return f'"{name.replace("\"", "\"\"")}"'

    def _detect_step_schema(self, connection: sqlite3.Connection) -> Literal["v1", "v2"] | None:
        """只接受完整已知的 v1 或 v2 step table shape。"""
        columns = connection.execute(f"PRAGMA table_info({_STEP_TABLE})").fetchall()
        names = tuple(row["name"] for row in columns)
        primary_key = tuple(
            row["name"]
            for row in sorted(columns, key=lambda row: int(row["pk"]))
            if int(row["pk"]) > 0
        )
        if names != _STEP_COLUMNS:
            return None
        if primary_key == _STEP_V1_PRIMARY_KEY:
            return "v1"
        if primary_key == _STEP_V2_PRIMARY_KEY:
            return "v2"
        return None

    @staticmethod
    def _integrity_ok(connection: sqlite3.Connection) -> bool:
        return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    @staticmethod
    def _foreign_keys_ok(connection: sqlite3.Connection) -> bool:
        return not connection.execute("PRAGMA foreign_key_check").fetchall()

    @staticmethod
    def _step_row_count(connection: sqlite3.Connection) -> int:
        return int(connection.execute(f"SELECT COUNT(*) FROM {_STEP_TABLE}").fetchone()[0])

    @staticmethod
    def _step_digest(connection: sqlite3.Connection) -> str:
        """取得所有 step 原始欄位的穩定 digest，供 migration 前後比對。"""
        import hashlib

        columns = ", ".join(_STEP_COLUMNS)
        rows = connection.execute(
            f"SELECT {columns} FROM {_STEP_TABLE} "
            "ORDER BY run_id ASC, step_id ASC, attempt ASC"
        ).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(repr(tuple(row)).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _verify_migration_preconditions(self, connection: sqlite3.Connection) -> tuple[int, str]:
        """驗證 source DB 可安全遷移，並回傳 preservation baseline。"""
        if self._detect_step_schema(connection) != "v1":
            raise RuntimeError("orchestration_steps is not the exact v1 schema")
        if not self._integrity_ok(connection):
            raise RuntimeError("ledger integrity check failed before migration")
        if not self._foreign_keys_ok(connection):
            raise RuntimeError("ledger foreign key check failed before migration")
        direct_dependents = []
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table_row in tables:
            table = str(table_row["name"])
            if table == _STEP_TABLE:
                continue
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({self._identifier(table)})"
            ).fetchall()
            if any(row["table"] == _STEP_TABLE for row in foreign_keys):
                direct_dependents.append(table)
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (_STEP_TABLE,),
        ).fetchall()
        if direct_dependents or triggers:
            raise RuntimeError("step-dependent schema requires an explicit migration plan")
        return self._step_row_count(connection), self._step_digest(connection)

    def _backup_step_migration_source(
        self,
        connection: sqlite3.Connection,
        *,
        expected_count: int,
        expected_digest: str,
    ) -> Path:
        """以 SQLite backup API 建立可恢復的完整 v1 snapshot。"""
        backup_path = self._db_path.with_name(
            f"{self._db_path.name}.v1-step-identity.{uuid4().hex}.backup.sqlite"
        )
        backup_connection = sqlite3.connect(str(backup_path))
        try:
            connection.backup(backup_connection)
        except Exception:
            backup_connection.close()
            raise
        backup_connection.row_factory = sqlite3.Row
        try:
            if (
                self._detect_step_schema(backup_connection) != "v1"
                or not self._integrity_ok(backup_connection)
                or not self._foreign_keys_ok(backup_connection)
                or self._step_row_count(backup_connection) != expected_count
                or self._step_digest(backup_connection) != expected_digest
            ):
                raise RuntimeError("ledger backup verification failed")
        finally:
            backup_connection.close()
        return backup_path

    def _backup_and_migrate_step_table(self, connection: sqlite3.Connection) -> None:
        """備份 exact v1 ledger 後，在單一 transaction 內重建為 v2。"""
        row_count, digest = self._verify_migration_preconditions(connection)
        self._backup_step_migration_source(
            connection,
            expected_count=row_count,
            expected_digest=digest,
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE orchestration_steps_v2 ("
                "step_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, "
                "run_id TEXT NOT NULL, tool_id TEXT NOT NULL, parameters_json TEXT, "
                "cwd TEXT NOT NULL, expected_packages_json TEXT, dispatch_key TEXT, "
                "command_status TEXT NOT NULL DEFAULT 'pending', "
                "dispatch_status TEXT NOT NULL DEFAULT 'pending', "
                "evidence_status TEXT NOT NULL DEFAULT 'pending', submitted_at REAL, "
                "command_finished_at REAL, dispatch_finished_at REAL, evidence_ready_at REAL, "
                "exit_code INTEGER, outcome_summary_json TEXT, terminal_reason TEXT, "
                "owner TEXT, recovery_disposition TEXT, attempt_reason TEXT, "
                "PRIMARY KEY (run_id, step_id, attempt), "
                "FOREIGN KEY (run_id) REFERENCES orchestration_runs(run_id)"
                ")"
            )
            columns = ", ".join(_STEP_COLUMNS)
            connection.execute(
                f"INSERT INTO orchestration_steps_v2 ({columns}) "
                f"SELECT {columns} FROM {_STEP_TABLE}"
            )
            connection.execute(f"DROP TABLE {_STEP_TABLE}")
            connection.execute("ALTER TABLE orchestration_steps_v2 RENAME TO orchestration_steps")
            self._create_step_indexes(connection)
            if (
                self._detect_step_schema(connection) != "v2"
                or not self._integrity_ok(connection)
                or not self._foreign_keys_ok(connection)
                or self._step_row_count(connection) != row_count
                or self._step_digest(connection) != digest
            ):
                raise RuntimeError("ledger post-migration verification failed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _create_step_indexes(connection: sqlite3.Connection) -> None:
        """建立 v2 step table 的非 unique 查詢索引。"""
        connection.execute("CREATE INDEX idx_steps_run ON orchestration_steps(run_id)")
        connection.execute(
            "CREATE INDEX idx_steps_run_step_attempt "
            "ON orchestration_steps(run_id, step_id, attempt)"
        )
        connection.execute("CREATE INDEX idx_steps_dispatch_key ON orchestration_steps(dispatch_key)")
        connection.execute("CREATE INDEX idx_steps_tool ON orchestration_steps(tool_id)")

    def _connect(self) -> sqlite3.Connection:
        """建立沿用 DQE 慣例的 SQLite connection。"""
        connection = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=self._BUSY_TIMEOUT_MS / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("orchestration ledger is closed")
        return self._connection

    @property
    def db_path(self) -> Path:
        """回傳此 ledger 使用的 SQLite 路徑。"""
        return self._db_path

    def _write_transaction(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        """以 immediate transaction 序列化寫入，避免 retry 取得重複 attempt。"""
        with self._write_lock:
            for retry_index in range(self._MAX_RETRIES):
                connection = self._require_connection()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        result = operation(connection)
                    except Exception:
                        connection.rollback()
                        raise
                    connection.commit()
                    return result
                except sqlite3.OperationalError as exc:
                    if connection.in_transaction:
                        connection.rollback()
                    if "locked" not in str(exc).lower() or retry_index >= self._MAX_RETRIES - 1:
                        raise
                    time.sleep(0.1 * (retry_index + 1))
        raise RuntimeError("unreachable")

    @staticmethod
    def _encode_json(value: dict[str, Any] | list[str] | None) -> str | None:
        return None if value is None else json.dumps(value, sort_keys=True)

    @staticmethod
    def _decode_json(value: str | None) -> Any:
        return None if value is None else json.loads(value)

    @staticmethod
    def _validate_transition(state_kind: str, from_state: str, to_state: str) -> None:
        try:
            allowed_states = _TRANSITIONS[state_kind][from_state]
        except KeyError as exc:
            raise ValueError(f"unknown {state_kind} state: {from_state}") from exc
        if to_state not in allowed_states:
            raise ValueError(
                f"illegal {state_kind} transition: {from_state} -> {to_state}"
            )

    @staticmethod
    def _append_transition(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        step_id: str | None,
        attempt: int | None,
        state_kind: str,
        from_state: str | None,
        to_state: str,
        timestamp: float,
        reason: str | None,
    ) -> None:
        connection.execute(
            "INSERT INTO orchestration_transitions "
            "(run_id, step_id, attempt, state_kind, from_state, to_state, timestamp, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                step_id,
                attempt,
                state_kind,
                from_state,
                to_state,
                timestamp,
                reason,
            ),
        )

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            workflow_type=row["workflow_type"],
            domain_run_id=row["domain_run_id"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            terminal_reason=row["terminal_reason"],
            owner=row["owner"],
            recovery_disposition=row["recovery_disposition"],
            metadata=OrchestrationLedger._decode_json(row["metadata_json"]),
            child_pid=row["child_pid"],
            last_heartbeat_at=row["last_heartbeat_at"],
            kernel_checkpoint_json=row["kernel_checkpoint_json"],
            pause_requested_at=row["pause_requested_at"],
            cancel_requested_at=row["cancel_requested_at"],
            cancel_immediate=row["cancel_immediate"],
            parent_run_id=row["parent_run_id"],
        )

    @staticmethod
    def _step_record(row: sqlite3.Row) -> StepRecord:
        return StepRecord(
            step_id=row["step_id"],
            attempt=row["attempt"],
            run_id=row["run_id"],
            tool_id=row["tool_id"],
            parameters=OrchestrationLedger._decode_json(row["parameters_json"]),
            cwd=row["cwd"],
            expected_packages=OrchestrationLedger._decode_json(
                row["expected_packages_json"]
            )
            or [],
            dispatch_key=row["dispatch_key"],
            command_status=row["command_status"],
            dispatch_status=row["dispatch_status"],
            evidence_status=row["evidence_status"],
            submitted_at=row["submitted_at"],
            command_finished_at=row["command_finished_at"],
            dispatch_finished_at=row["dispatch_finished_at"],
            evidence_ready_at=row["evidence_ready_at"],
            exit_code=row["exit_code"],
            outcome_summary=OrchestrationLedger._decode_json(
                row["outcome_summary_json"]
            ),
            terminal_reason=row["terminal_reason"],
            owner=row["owner"],
            recovery_disposition=row["recovery_disposition"],
            attempt_reason=row["attempt_reason"],
        )

    @staticmethod
    def _transition_record(row: sqlite3.Row) -> TransitionRecord:
        return TransitionRecord(
            transition_id=row["transition_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            attempt=row["attempt"],
            state_kind=row["state_kind"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            timestamp=row["timestamp"],
            reason=row["reason"],
        )

    def create_run(
        self,
        *,
        run_id: str | None = None,
        workflow_type: str,
        domain_run_id: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """建立 pending run 並記錄首次狀態轉移。"""
        resolved_run_id = run_id or f"run_{uuid4().hex}"
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO orchestration_runs "
                "(run_id, workflow_type, domain_run_id, status, started_at, owner, metadata_json) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (
                    resolved_run_id,
                    workflow_type,
                    domain_run_id,
                    now,
                    owner,
                    self._encode_json(metadata),
                ),
            )
            self._append_transition(
                connection,
                run_id=resolved_run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state=None,
                to_state="pending",
                timestamp=now,
                reason=None,
            )

        self._write_transaction(operation)
        return resolved_run_id

    def start_run(self, run_id: str, *, reason: str | None = None) -> None:
        """將 run 從 pending 遷移至 running。"""
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT status FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            self._validate_transition("run", row["status"], "running")
            connection.execute(
                "UPDATE orchestration_runs SET status = 'running' WHERE run_id = ?",
                (run_id,),
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state=row["status"],
                to_state="running",
                timestamp=now,
                reason=reason,
            )

        self._write_transaction(operation)

    def complete_run(
        self,
        run_id: str,
        *,
        final_status: Literal["completed", "failed", "timeout", "cancelled", "abandoned"],
        terminal_reason: str,
        recovery_disposition: str | None = None,
    ) -> None:
        """將 running run 遷移至指定 terminal 狀態。"""
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT status FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            self._validate_transition("run", row["status"], final_status)
            connection.execute(
                "UPDATE orchestration_runs SET status = ?, finished_at = ?, "
                "terminal_reason = ?, recovery_disposition = ? WHERE run_id = ?",
                (final_status, now, terminal_reason, recovery_disposition, run_id),
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state=row["status"],
                to_state=final_status,
                timestamp=now,
                reason=terminal_reason,
            )

        self._write_transaction(operation)

    #: 這是**唯一**被允許的 pending → failed 轉移，只給
    #: `fail_pending_run_before_start()` 使用。`complete_run()` 等通用 API
    #: 的轉移表完全沒有放寬。
    PRELAUNCH_FAILURE_REASON = "prelaunch_failure"

    def fail_pending_run_before_start(
        self,
        run_id: str,
        *,
        expected_workflow_type: str,
        expected_owner: str | None,
        expected_project_id: str | None,
        expected_task_id: str,
        expected_slot: str,
        expected_creator_instance: str | None,
        failure_phase: str,
        error: str,
    ) -> dict[str, Any]:
        """如實終結一筆**已建立但從未啟動**的 task-owned run。

        `running` 在本系統代表 daemon 已接管該 run（heartbeat scanner 據此
        判定 stale）。child 從未啟動時送 `start_run()` 只為了讓
        `complete_run()` 合法，等於記下一個從未發生的接管事實——本方法就是
        為了不必那樣做而存在。

        條件更新，全部在**同一個** write transaction 內完成，因此與
        `start_run()`／claim 走同一組互斥：

        - run 必須仍是 `pending`（已被 start 搶到就拒絕，不把執行中的 run
          標 failed）；
        - workflow／owner／project／`metadata.agent_task` 的 task/slot 必須
          與預期相符；帶 creator instance 時也必須相符；
        - 呼叫端必須提供可證明「未開始執行」的 prelaunch 失敗來源。

        成功時只寫 `failed` ＋ `finished_at` ＋ 固定 `terminal_reason`，並在
        metadata 加一個 `prelaunch_failure` 子樹保留原始錯誤。
        **不動 `started_at`**（那是 run 的建立時間，不是工具啟動時間），
        不寫 PID／container id／tool-start 事件，不產生成功 receipt。

        同一筆失敗重入時回傳已記錄的結果，不重複記錄、不假造第二次失敗。
        """
        if not str(failure_phase).strip() or not str(error).strip():
            raise ValueError("failure_phase and error are required")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT status, workflow_type, owner, metadata_json, terminal_reason, "
                "finished_at, child_pid, started_at FROM orchestration_runs "
                "WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            metadata = self._decode_json(row["metadata_json"]) or {}
            self._require_prelaunch_identity(
                run_id, row, metadata,
                expected_workflow_type=expected_workflow_type,
                expected_owner=expected_owner,
                expected_project_id=expected_project_id,
                expected_task_id=expected_task_id,
                expected_slot=expected_slot,
                expected_creator_instance=expected_creator_instance)

            recorded = metadata.get("prelaunch_failure")
            if row["status"] == "failed" and isinstance(recorded, dict):
                # 重入：同一筆失敗回已記錄結果，不增加執行也不假造第二次失敗。
                if (recorded.get("failure_phase") != failure_phase
                        or recorded.get("error") != error):
                    raise ValueError(
                        "this run was already failed before start with a different "
                        "recorded reason")
                return dict(recorded)
            if row["status"] != "pending":
                raise ValueError(
                    f"run {run_id} is {row['status']}, not pending; a starter has "
                    "already won or the outcome is unconfirmed")
            if row["child_pid"] is not None:
                raise ValueError(
                    f"run {run_id} already recorded a child pid; it is not a "
                    "prelaunch failure")

            record = {
                "schema_version": 1,
                "failure_phase": str(failure_phase),
                "child_started": False,
                "error": str(error),
                "recorded_at": now,
            }
            metadata["prelaunch_failure"] = record
            connection.execute(
                "UPDATE orchestration_runs SET status = 'failed', finished_at = ?, "
                "terminal_reason = ?, metadata_json = ? "
                "WHERE run_id = ? AND status = 'pending'",
                (now, self.PRELAUNCH_FAILURE_REASON, self._encode_json(metadata),
                 run_id))
            self._append_transition(
                connection, run_id=run_id, step_id=None, attempt=None,
                state_kind="run", from_state="pending", to_state="failed",
                timestamp=now, reason=self.PRELAUNCH_FAILURE_REASON)
            return record

        return self._write_transaction(operation)

    @staticmethod
    def _require_prelaunch_identity(
        run_id: str, row: sqlite3.Row, metadata: dict[str, Any], *,
        expected_workflow_type: str, expected_owner: str | None,
        expected_project_id: str | None, expected_task_id: str,
        expected_slot: str, expected_creator_instance: str | None,
    ) -> None:
        if row["workflow_type"] != expected_workflow_type:
            raise ValueError(
                f"run {run_id} is not a {expected_workflow_type!r} run")
        if expected_owner is not None and row["owner"] != expected_owner:
            raise ValueError(f"run {run_id} does not belong to this owner")
        scope = (metadata.get("orchestration") or {}).get("scope") or {}
        if (expected_project_id is not None
                and scope.get("project_id") != expected_project_id):
            raise ValueError(f"run {run_id} does not belong to this project scope")
        agent_task = metadata.get("agent_task")
        if not isinstance(agent_task, dict):
            raise ValueError(
                f"run {run_id} is not task-owned; this method only terminates "
                "task-owned runs")
        if (agent_task.get("task_id") != expected_task_id
                or agent_task.get("slot") != expected_slot):
            raise ValueError(
                f"run {run_id} does not belong to this task and slot")
        if expected_creator_instance is not None:
            actual = scope.get("created_daemon_instance_id")
            if actual is not None and actual != expected_creator_instance:
                raise ValueError(
                    f"run {run_id} was created by a different daemon instance")

    def update_child_pid(self, run_id: str, child_pid: int) -> None:
        """寫入 daemon 派生 child 的 PID。"""
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE orchestration_runs SET child_pid = ? WHERE run_id = ?",
                (child_pid, run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"run not found: {run_id}")

        self._write_transaction(operation)

    def update_heartbeat(self, run_id: str, timestamp: float | None = None) -> None:
        """寫入 child heartbeat 時間戳。"""
        resolved_timestamp = self._clock() if timestamp is None else timestamp

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE orchestration_runs SET last_heartbeat_at = ? WHERE run_id = ?",
                (resolved_timestamp, run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"run not found: {run_id}")

        self._write_transaction(operation)

    def update_domain_run_id(self, run_id: str, domain_run_id: str) -> None:
        """以 runner 產出的真值補寫預建 run 的 domain_run_id。"""
        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT status, domain_run_id FROM orchestration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            if row["status"] != "running":
                raise ValueError(f"cannot update domain run id for {row['status']} run: {run_id}")
            existing = row["domain_run_id"]
            if existing is not None and existing != domain_run_id:
                raise ValueError(f"domain run id already set for run: {run_id}")
            if existing is None:
                connection.execute(
                    "UPDATE orchestration_runs SET domain_run_id = ? WHERE run_id = ?",
                    (domain_run_id, run_id),
                )

        self._write_transaction(operation)

    def list_running_runs(self) -> list[RunRecord]:
        """列出所有 running runs，供啟動 recovery 使用。"""
        rows = self._require_connection().execute(
            "SELECT * FROM orchestration_runs WHERE status = 'running' "
            "ORDER BY started_at ASC, run_id ASC"
        ).fetchall()
        return [self._run_record(row) for row in rows]

    def list_stale_running_runs(self, threshold: float) -> list[RunRecord]:
        """列出具 child PID 且 heartbeat 已過期的 running runs。"""
        rows = self._require_connection().execute(
            "SELECT * FROM orchestration_runs WHERE status = 'running' "
            "AND child_pid IS NOT NULL AND last_heartbeat_at < ? "
            "ORDER BY last_heartbeat_at ASC, run_id ASC",
            (threshold,),
        ).fetchall()
        return [self._run_record(row) for row in rows]

    def record_lifecycle_transition(
        self,
        run_id: str,
        *,
        state_value: str,
        reason: str | None = None,
    ) -> None:
        """記錄 scanner 使用的 lifecycle transition。"""
        timestamp = self._clock()

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT 1 FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=state_value,
                timestamp=timestamp,
                reason=reason,
            )

        self._write_transaction(operation)

    def record_resource_cleanup(
        self,
        run_id: str,
        *,
        ok: bool,
        detail: dict[str, Any],
    ) -> None:
        """記錄管理端在 immediate cancel 後代為釋放外部資源的結果。

        只 append 一筆 `state_kind="resource_cleanup"` transition（`reason` 是
        結果的 JSON），**不**修改 run 的 status／terminal reason——清理成功與否
        獨立保存，不改寫原始結果，也不觸發任何重派。"""
        timestamp = self._clock()
        reason = json.dumps(detail, sort_keys=True, ensure_ascii=False, allow_nan=False)

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT 1 FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="resource_cleanup",
                from_state=None,
                to_state="released" if ok else "release_failed",
                timestamp=timestamp,
                reason=reason,
            )

        self._write_transaction(operation)

    def get_run(self, run_id: str) -> RunRecord | None:
        """讀取指定 run；找不到時回傳 None。"""
        row = self._require_connection().execute(
            "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else self._run_record(row)

    @staticmethod
    def _scope_sql_expression(path: str) -> str:
        """回傳 fail-closed 的 metadata scope JSON 擷取 SQL。

        舊版或損毀的 ``metadata_json`` 不得讓 SQLite JSON 函式拋出例外，也
        不得被當成任何 project scope；因此只有 ``json_valid`` 為真時才讀取
        指定 path。
        """
        return (
            "CASE WHEN json_valid(metadata_json) "
            f"THEN json_extract(metadata_json, '{path}') END"
        )

    @classmethod
    def _scope_predicate(cls) -> str:
        """回傳 authoritative member/project scope 的 SQL predicate。"""
        schema_version = cls._scope_sql_expression(
            "$.orchestration.scope.schema_version"
        )
        project_id = cls._scope_sql_expression(
            "$.orchestration.scope.project_id"
        )
        return (
            "owner = ? AND "
            f"{schema_version} = 1 AND "
            f"typeof({project_id}) = 'text' AND "
            f"{project_id} <> '' AND {project_id} = ?"
        )

    def get_run_for_scope(
        self,
        run_id: str,
        *,
        member_id: str,
        project_id: str,
    ) -> RunRecord | None:
        """只讀取 owner 與固定 scope 都完全相符的 run。

        不存在、跨 member、跨 project、legacy、metadata malformed 與不支援
        scope version 都回傳 ``None``，讓 service 對外統一映射為 unknown run。
        """
        row = self._require_connection().execute(
            "SELECT * FROM orchestration_runs WHERE run_id = ? AND "
            + self._scope_predicate(),
            (run_id, member_id, project_id),
        ).fetchone()
        return None if row is None else self._run_record(row)

    @staticmethod
    def _get_run_row_or_raise(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return row

    def _conditional_result(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        applied: bool,
    ) -> ConditionalRunResult:
        return ConditionalRunResult(
            applied=applied,
            run=self._run_record(self._get_run_row_or_raise(connection, run_id)),
        )

    def _validated_checkpoint(
        self,
        checkpoint_json: str,
        *,
        expected_domain_run_id: str | None,
        expected_safe_point: Literal["before_step", "after_step"] | None,
    ) -> None:
        """在 ledger operation 內驗證 checkpoint 與 run identity。"""

        from terminux_engine.orchestration.checkpoint import (
            decode_checkpoint_v1,
            inspect_checkpoint_v1,
        )

        if expected_domain_run_id is None:
            safe_point, _current_step_id, _domain_run_id = inspect_checkpoint_v1(
                checkpoint_json
            )
        else:
            restored = decode_checkpoint_v1(
                checkpoint_json,
                expected_domain_run_id=expected_domain_run_id,
            )
            safe_point = restored.safe_point
        if expected_safe_point is not None and safe_point != expected_safe_point:
            raise ValueError(
                f"checkpoint safe point must be {expected_safe_point}: {safe_point}"
            )

    def request_pause_if_running(self, run_id: str) -> ConditionalRunResult:
        """原子接受 running run 的 pause request。"""

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET pause_requested_at = ? "
                "WHERE run_id = ? AND status = 'running' "
                "AND pause_requested_at IS NULL AND cancel_requested_at IS NULL",
                (now, run_id),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=LIFECYCLE_PAUSE_REQUESTED,
                timestamp=now,
                reason=None,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def request_cancel_if_active(
        self,
        run_id: str,
        *,
        immediate: bool,
    ) -> ConditionalRunResult:
        """原子接受 running 或 paused run 的第一個 cancel request。"""

        lifecycle_request = (
            LIFECYCLE_CANCEL_REQUESTED_IMMEDIATE
            if immediate
            else LIFECYCLE_CANCEL_REQUESTED_SOFT
        )
        lifecycle_terminal = (
            LIFECYCLE_CANCELLED_IMMEDIATE if immediate else LIFECYCLE_CANCELLED_SOFT
        )
        terminal_reason = (
            TERMINAL_REASON_USER_CANCELLED_IMMEDIATE
            if immediate
            else TERMINAL_REASON_USER_CANCELLED
        )

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            row = self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            if row["status"] == "running":
                cursor = connection.execute(
                    "UPDATE orchestration_runs SET cancel_requested_at = ?, "
                    "cancel_immediate = ? WHERE run_id = ? AND status = 'running' "
                    "AND cancel_requested_at IS NULL",
                    (now, int(immediate), run_id),
                )
                if cursor.rowcount != 1:
                    return self._conditional_result(connection, run_id=run_id, applied=False)
                self._append_transition(
                    connection,
                    run_id=run_id,
                    step_id=None,
                    attempt=None,
                    state_kind="lifecycle",
                    from_state=None,
                    to_state=lifecycle_request,
                    timestamp=now,
                    reason=None,
                )
                return self._conditional_result(connection, run_id=run_id, applied=True)
            if row["status"] != "paused":
                return self._conditional_result(connection, run_id=run_id, applied=False)
            cursor = connection.execute(
                "UPDATE orchestration_runs SET cancel_requested_at = ?, cancel_immediate = ?, "
                "status = 'cancelled', finished_at = ?, terminal_reason = ? "
                "WHERE run_id = ? AND status = 'paused' AND cancel_requested_at IS NULL",
                (now, int(immediate), now, terminal_reason, run_id),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=lifecycle_request,
                timestamp=now,
                reason=None,
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="paused",
                to_state="cancelled",
                timestamp=now,
                reason=terminal_reason,
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=lifecycle_terminal,
                timestamp=now,
                reason=None,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def save_before_step_if_runnable(
        self,
        run_id: str,
        *,
        checkpoint_json: str,
    ) -> ConditionalRunResult:
        """儲存 before-step checkpoint，commit 即代表取得 step 開始權。"""

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            row = self._get_run_row_or_raise(connection, run_id)
            self._validated_checkpoint(
                checkpoint_json,
                expected_domain_run_id=row["domain_run_id"],
                expected_safe_point="before_step",
            )
            cursor = connection.execute(
                "UPDATE orchestration_runs SET kernel_checkpoint_json = ? "
                "WHERE run_id = ? AND status = 'running' "
                "AND pause_requested_at IS NULL AND cancel_requested_at IS NULL",
                (checkpoint_json, run_id),
            )
            return self._conditional_result(
                connection, run_id=run_id, applied=cursor.rowcount == 1
            )

        return self._write_transaction(operation)

    def pause_with_checkpoint_if_allowed(
        self,
        run_id: str,
        *,
        checkpoint_json: str,
        source: Literal["user", "daemon_shutdown"],
    ) -> ConditionalRunResult:
        """以 after-step checkpoint 原子將 run 轉為 paused。"""

        if source not in {"user", "daemon_shutdown"}:
            raise ValueError(f"unknown pause source: {source}")

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            row = self._get_run_row_or_raise(connection, run_id)
            self._validated_checkpoint(
                checkpoint_json,
                expected_domain_run_id=row["domain_run_id"],
                expected_safe_point="after_step",
            )
            if source == "user":
                predicate = (
                    "status = 'running' AND pause_requested_at IS NOT NULL "
                    "AND cancel_requested_at IS NULL"
                )
            else:
                predicate = "status = 'running' AND cancel_requested_at IS NULL"
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET kernel_checkpoint_json = ?, status = 'paused', "
                "child_pid = NULL, last_heartbeat_at = NULL "
                f"WHERE run_id = ? AND {predicate}",
                (checkpoint_json, run_id),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="running",
                to_state="paused",
                timestamp=now,
                reason=f"source={source}",
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=LIFECYCLE_PAUSED,
                timestamp=now,
                reason=f"source={source}",
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def claim_resume_if_paused(
        self,
        run_id: str,
        *,
        child_pid: int,
        heartbeat_at: float,
        expected_checkpoint_json: str,
    ) -> ConditionalRunResult:
        """讓唯一 resume child 原子取得 paused run 的 ownership。"""

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            row = self._get_run_row_or_raise(connection, run_id)
            self._validated_checkpoint(
                expected_checkpoint_json,
                expected_domain_run_id=row["domain_run_id"],
                expected_safe_point=None,
            )
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET status = 'running', child_pid = ?, "
                "last_heartbeat_at = ?, pause_requested_at = NULL "
                "WHERE run_id = ? AND status = 'paused' AND cancel_requested_at IS NULL "
                "AND kernel_checkpoint_json = ?",
                (child_pid, heartbeat_at, run_id, expected_checkpoint_json),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="paused",
                to_state="running",
                timestamp=now,
                reason=None,
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=LIFECYCLE_RESUMED,
                timestamp=now,
                reason=None,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def claim_retry_child_if_unclaimed(
        self,
        run_id: str,
        *,
        child_pid: int,
        heartbeat_at: float,
    ) -> ConditionalRunResult:
        """讓 retry child 在 restore 前原子回填自己的 PID。"""

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            cursor = connection.execute(
                "UPDATE orchestration_runs SET child_pid = ?, last_heartbeat_at = ? "
                "WHERE run_id = ? AND status = 'running' AND parent_run_id IS NOT NULL "
                "AND (child_pid IS NULL OR child_pid = ?) "
                "AND NOT (cancel_requested_at IS NOT NULL AND cancel_immediate = 1)",
                (child_pid, heartbeat_at, run_id, child_pid),
            )
            return self._conditional_result(
                connection, run_id=run_id, applied=cursor.rowcount == 1
            )

        return self._write_transaction(operation)

    def finalize_cancel_if_requested(
        self,
        run_id: str,
        *,
        immediate: bool,
        checkpoint_json: str | None,
    ) -> ConditionalRunResult:
        """以已接受的 cancel request 唯一終結 running run。"""

        if immediate:
            if checkpoint_json is not None:
                raise ValueError("immediate cancel 不得覆寫 before-step checkpoint")
        elif checkpoint_json is None:
            raise ValueError("soft cancel 必須提供 after-step checkpoint")

        lifecycle_value = (
            LIFECYCLE_CANCELLED_IMMEDIATE if immediate else LIFECYCLE_CANCELLED_SOFT
        )
        terminal_reason = (
            TERMINAL_REASON_USER_CANCELLED_IMMEDIATE
            if immediate
            else TERMINAL_REASON_USER_CANCELLED
        )

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            row = self._get_run_row_or_raise(connection, run_id)
            if checkpoint_json is not None:
                self._validated_checkpoint(
                    checkpoint_json,
                    expected_domain_run_id=row["domain_run_id"],
                    expected_safe_point="after_step",
                )
            now = self._clock()
            checkpoint_clause = (
                "kernel_checkpoint_json = ?, " if checkpoint_json is not None else ""
            )
            parameters: list[Any] = []
            if checkpoint_json is not None:
                parameters.append(checkpoint_json)
            parameters.extend((now, terminal_reason, run_id, int(immediate)))
            cursor = connection.execute(
                "UPDATE orchestration_runs SET "
                f"{checkpoint_clause}status = 'cancelled', finished_at = ?, "
                "terminal_reason = ?, child_pid = NULL, last_heartbeat_at = NULL "
                "WHERE run_id = ? AND status = 'running' "
                "AND cancel_requested_at IS NOT NULL AND cancel_immediate = ?",
                parameters,
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="running",
                to_state="cancelled",
                timestamp=now,
                reason=terminal_reason,
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=lifecycle_value,
                timestamp=now,
                reason=None,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def fail_on_checkpoint_error_if_active(
        self,
        run_id: str,
        *,
        phase: Literal["serialize", "deserialize"],
    ) -> ConditionalRunResult:
        """在未被 cancel 取得 ownership 時終結 checkpoint error。"""

        if phase not in {"serialize", "deserialize"}:
            raise ValueError(f"unknown checkpoint phase: {phase}")
        expected_status = "running" if phase == "serialize" else "paused"
        terminal_reason = (
            TERMINAL_REASON_CHECKPOINT_SERIALIZATION_FAILED
            if phase == "serialize"
            else TERMINAL_REASON_CHECKPOINT_DESERIALIZE_FAILED
        )

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET status = 'failed', finished_at = ?, "
                "terminal_reason = ? WHERE run_id = ? AND status = ? "
                "AND cancel_requested_at IS NULL",
                (now, terminal_reason, run_id, expected_status),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state=expected_status,
                to_state="failed",
                timestamp=now,
                reason=terminal_reason,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def complete_run_if_no_control_pending(
        self,
        run_id: str,
        *,
        final_status: Literal["completed", "failed"],
        terminal_reason: str,
    ) -> ConditionalRunResult:
        """只有尚未有 control request 時才提交自然終態。"""

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET status = ?, finished_at = ?, terminal_reason = ? "
                "WHERE run_id = ? AND status = 'running' "
                "AND pause_requested_at IS NULL AND cancel_requested_at IS NULL",
                (final_status, now, terminal_reason, run_id),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="running",
                to_state=final_status,
                timestamp=now,
                reason=terminal_reason,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def finalize_step_failure_if_no_immediate_cancel(
        self,
        run_id: str,
        *,
        final_status: Literal["failed", "timeout", "cancelled", "abandoned"],
        terminal_reason: str,
    ) -> ConditionalRunResult:
        """真實 step failure／timeout 優先於 pause 與 soft cancel。

        `cancelled`／`abandoned` 只給前景 CLI 的中斷分支使用：已確認中斷時
        `running → cancelled`，清理無法確認時 `running → abandoned`。兩者都是既有
        允許的轉換；條件式更新只作用在 `running` 的 run，不覆蓋既有終態。
        """
        if final_status not in _STEP_FINALIZE_STATUSES:
            raise ValueError(f"unsupported step finalization status: {final_status}")

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET status = ?, finished_at = ?, terminal_reason = ? "
                "WHERE run_id = ? AND status = 'running' "
                "AND NOT (cancel_requested_at IS NOT NULL AND cancel_immediate = 1)",
                (final_status, now, terminal_reason, run_id),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="running",
                to_state=final_status,
                timestamp=now,
                reason=terminal_reason,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def fail_retry_spawn_if_unstarted(self, run_id: str) -> ConditionalRunResult:
        """只終結已 fork 但尚未成功派生 child 的 retry run。"""

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET status = 'failed', finished_at = ?, "
                "terminal_reason = ? WHERE run_id = ? AND status = 'running' "
                "AND parent_run_id IS NOT NULL AND child_pid IS NULL "
                "AND NOT (cancel_requested_at IS NOT NULL AND cancel_immediate = 1)",
                (now, TERMINAL_REASON_SPAWN_FAILED, run_id),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="running",
                to_state="failed",
                timestamp=now,
                reason=TERMINAL_REASON_SPAWN_FAILED,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def mark_abandoned_if_running_and_no_cancel(
        self,
        run_id: str,
        *,
        expected_child_pid: int | None,
        heartbeat_not_after: float | None,
        terminal_reason: Literal[
            "child_died",
            "child_not_found",
            "child_zombie",
            "child_zombie_at_recovery",
            "no_child_pid",
        ],
    ) -> ConditionalRunResult:
        """scanner 以唯一 conditional ownership 標記 abandoned。"""

        allowed_reasons = {
            "child_died",
            "child_not_found",
            "child_zombie",
            "child_zombie_at_recovery",
            "no_child_pid",
        }
        if terminal_reason not in allowed_reasons:
            raise ValueError(f"unknown abandoned terminal reason: {terminal_reason}")
        if expected_child_pid is None and heartbeat_not_after is not None:
            raise ValueError("無 child PID 的 abandoned branch 不得帶 heartbeat cutoff")

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            if expected_child_pid is None:
                predicate = "child_pid IS NULL"
                parameters: list[Any] = [now, terminal_reason, run_id]
            elif heartbeat_not_after is None:
                predicate = "child_pid = ?"
                parameters = [now, terminal_reason, run_id, expected_child_pid]
            else:
                predicate = "child_pid = ? AND (last_heartbeat_at IS NULL OR last_heartbeat_at <= ?)"
                parameters = [
                    now,
                    terminal_reason,
                    run_id,
                    expected_child_pid,
                    heartbeat_not_after,
                ]
            cursor = connection.execute(
                "UPDATE orchestration_runs SET status = 'abandoned', finished_at = ?, "
                "terminal_reason = ?, recovery_disposition = ? "
                "WHERE run_id = ? AND status = 'running' "
                "AND cancel_requested_at IS NULL AND "
                f"{predicate}",
                [*parameters[:2], RECOVERY_DISPOSITION_PENDING_REVIEW, *parameters[2:]],
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="running",
                to_state="abandoned",
                timestamp=now,
                reason=terminal_reason,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    @staticmethod
    def _validate_retry_metadata(metadata: dict[str, Any] | None) -> None:
        """確認 retry 可安全複製的 workflow metadata namespace。"""

        from terminux_engine.orchestration.checkpoint import _validate_json_native

        if type(metadata) is not dict:
            raise ValueError("retry parent metadata 必須為 object")
        orchestration = metadata.get("orchestration")
        if type(orchestration) is not dict:
            raise ValueError("retry parent metadata 缺少 orchestration namespace")
        if type(orchestration.get("schema_version")) is not int or orchestration[
            "schema_version"
        ] != 1:
            raise ValueError("retry parent orchestration metadata schema_version 無效")
        workflow_config = orchestration.get("workflow_config")
        if type(workflow_config) is not dict:
            raise ValueError("retry parent workflow_config 必須為 object")
        _validate_json_native(workflow_config, path="$.orchestration.workflow_config")

    def fork_retry_from_terminal(
        self,
        parent_run_id: str,
        *,
        new_run_id: str,
        expected_step_id: str | None,
    ) -> RetryForkResult:
        """從可 retry terminal parent 原子建立唯一 checkpoint fork child。"""

        from terminux_engine.orchestration.checkpoint import decode_checkpoint_v1

        def operation(connection: sqlite3.Connection) -> RetryForkResult:
            parent_row = self._get_run_row_or_raise(connection, parent_run_id)
            parent = self._run_record(parent_row)
            if parent.status not in {"failed", "cancelled", "timeout"}:
                return self._retry_fork_loser(connection, parent_run_id, parent)
            if parent.domain_run_id is None or parent.kernel_checkpoint_json is None:
                raise ValueError("retry parent 缺少 domain identity 或 checkpoint")
            self._validate_retry_metadata(parent.metadata)
            restored = decode_checkpoint_v1(
                parent.kernel_checkpoint_json,
                expected_domain_run_id=parent.domain_run_id,
            )
            if restored.current_step_id != expected_step_id:
                raise ValueError("retry step_id 與 checkpoint current_step_id 不一致")
            if parent.status in {"failed", "timeout"} and restored.safe_point != "before_step":
                raise ValueError("failed/timeout retry 必須使用 before_step checkpoint")
            if parent.status == "cancelled":
                if parent.terminal_reason == TERMINAL_REASON_USER_CANCELLED:
                    if restored.safe_point != "after_step":
                        raise ValueError("soft-cancelled retry 必須使用 after_step checkpoint")
                elif parent.terminal_reason == TERMINAL_REASON_USER_CANCELLED_IMMEDIATE:
                    if restored.safe_point not in {"before_step", "after_step"}:
                        raise ValueError("immediate-cancelled checkpoint safe point 無效")
                else:
                    raise ValueError("cancelled retry terminal reason 不支援")

            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET recovery_disposition = ? "
                "WHERE run_id = ? AND status IN ('failed', 'cancelled', 'timeout') "
                "AND recovery_disposition IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM orchestration_runs child "
                "WHERE child.parent_run_id = orchestration_runs.run_id)",
                (RECOVERY_DISPOSITION_RETRIED, parent_run_id),
            )
            if cursor.rowcount != 1:
                current_parent = self._run_record(
                    self._get_run_row_or_raise(connection, parent_run_id)
                )
                return self._retry_fork_loser(connection, parent_run_id, current_parent)

            connection.execute(
                "INSERT INTO orchestration_runs "
                "(run_id, workflow_type, domain_run_id, status, started_at, owner, "
                "metadata_json, kernel_checkpoint_json, parent_run_id) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                (
                    new_run_id,
                    parent.workflow_type,
                    parent.domain_run_id,
                    now,
                    parent.owner,
                    parent_row["metadata_json"],
                    parent.kernel_checkpoint_json,
                    parent_run_id,
                ),
            )
            self._append_transition(
                connection,
                run_id=new_run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state=None,
                to_state="pending",
                timestamp=now,
                reason=None,
            )
            self._append_transition(
                connection,
                run_id=new_run_id,
                step_id=None,
                attempt=None,
                state_kind="run",
                from_state="pending",
                to_state="running",
                timestamp=now,
                reason=None,
            )
            self._append_transition(
                connection,
                run_id=parent_run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=LIFECYCLE_RETRY_SPAWNED,
                timestamp=now,
                reason=f"new_run_id={new_run_id}",
            )
            return RetryForkResult(
                applied=True,
                parent=self._run_record(
                    self._get_run_row_or_raise(connection, parent_run_id)
                ),
                child=self._run_record(self._get_run_row_or_raise(connection, new_run_id)),
            )

        return self._write_transaction(operation)

    def _retry_fork_loser(
        self,
        connection: sqlite3.Connection,
        parent_run_id: str,
        parent: RunRecord,
    ) -> RetryForkResult:
        children = connection.execute(
            "SELECT * FROM orchestration_runs WHERE parent_run_id = ? "
            "ORDER BY started_at ASC, run_id ASC",
            (parent_run_id,),
        ).fetchall()
        if len(children) > 1:
            raise RuntimeError("retry parent has multiple child runs")
        child = None if not children else self._run_record(children[0])
        return RetryForkResult(applied=False, parent=parent, child=child)

    def list_runs(
        self,
        *,
        workflow_type: str | None = None,
        status: str | None = None,
        recovery_disposition: str | None = None,
        member_id: str | None = None,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[RunRecord]:
        """依條件列出最新 runs。"""
        clauses: list[str] = []
        parameters: list[Any] = []
        if member_id is not None and project_id is not None:
            clauses.append(self._scope_predicate())
            parameters.extend((member_id, project_id))
        if workflow_type is not None:
            clauses.append("workflow_type = ?")
            parameters.append(workflow_type)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if recovery_disposition is not None:
            clauses.append("recovery_disposition = ?")
            parameters.append(recovery_disposition)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = self._require_connection().execute(
            f"SELECT * FROM orchestration_runs{where} ORDER BY started_at DESC, run_id DESC LIMIT ?",
            parameters,
        ).fetchall()
        return [self._run_record(row) for row in rows]

    def dismiss_pending_review_if_abandoned(
        self,
        run_id: str,
    ) -> ConditionalRunResult:
        """原子處置待人工審查的 abandoned run，不重開 terminal run。"""

        def operation(connection: sqlite3.Connection) -> ConditionalRunResult:
            self._get_run_row_or_raise(connection, run_id)
            now = self._clock()
            cursor = connection.execute(
                "UPDATE orchestration_runs SET recovery_disposition = ? "
                "WHERE run_id = ? AND status = 'abandoned' "
                "AND recovery_disposition = ?",
                (
                    RECOVERY_DISPOSITION_DISMISSED,
                    run_id,
                    RECOVERY_DISPOSITION_PENDING_REVIEW,
                ),
            )
            if cursor.rowcount != 1:
                return self._conditional_result(connection, run_id=run_id, applied=False)
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=None,
                attempt=None,
                state_kind="lifecycle",
                from_state=None,
                to_state=LIFECYCLE_PENDING_REVIEW_DISMISSED,
                timestamp=now,
                reason=None,
            )
            return self._conditional_result(connection, run_id=run_id, applied=True)

        return self._write_transaction(operation)

    def create_step(
        self,
        *,
        run_id: str,
        step_id: str,
        tool_id: str,
        parameters: dict[str, Any],
        cwd: str,
        expected_packages: list[str],
        owner: str | None = None,
        attempt_reason: Literal["initial", "manual_retry"] = "initial",
    ) -> tuple[int, str]:
        """建立新的 step attempt，並記錄三個 pending transitions。"""
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> tuple[int, str]:
            run = connection.execute(
                "SELECT 1 FROM orchestration_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt "
                "FROM orchestration_steps WHERE run_id = ? AND step_id = ?",
                (run_id, step_id),
            ).fetchone()
            attempt = int(row["next_attempt"])
            dispatch_key = f"{run_id}:{step_id}:{attempt}"
            connection.execute(
                "INSERT INTO orchestration_steps "
                "(step_id, attempt, run_id, tool_id, parameters_json, cwd, "
                "expected_packages_json, dispatch_key, owner, attempt_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    step_id,
                    attempt,
                    run_id,
                    tool_id,
                    self._encode_json(parameters),
                    cwd,
                    self._encode_json(expected_packages),
                    dispatch_key,
                    owner,
                    attempt_reason,
                ),
            )
            for state_kind in ("command", "dispatch", "evidence"):
                self._append_transition(
                    connection,
                    run_id=run_id,
                    step_id=step_id,
                    attempt=attempt,
                    state_kind=state_kind,
                    from_state=None,
                    to_state="pending",
                    timestamp=now,
                    reason=None,
                )
            return attempt, dispatch_key

        return self._write_transaction(operation)

    def next_attempt_number(self, run_id: str, step_id: str) -> int:
        """取得 step 的下一個 attempt 編號。"""
        row = self._require_connection().execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt "
            "FROM orchestration_steps WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()
        return int(row["next_attempt"])

    def transition_command(
        self,
        run_id: str,
        step_id: str,
        attempt: int,
        *,
        to_state: Literal["running", "completed", "failed", "timeout"],
        exit_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        """遷移 command 狀態並原子記錄 transition。"""
        self._transition_step(
            run_id,
            step_id,
            attempt,
            state_kind="command",
            to_state=to_state,
            exit_code=exit_code,
            outcome_summary=None,
            reason=reason,
        )

    def transition_dispatch(
        self,
        run_id: str,
        step_id: str,
        attempt: int,
        *,
        to_state: Literal["running", "completed", "failed", "timeout"],
        outcome_summary: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        """遷移 dispatch 狀態並原子記錄 transition。"""
        self._transition_step(
            run_id,
            step_id,
            attempt,
            state_kind="dispatch",
            to_state=to_state,
            exit_code=None,
            outcome_summary=outcome_summary,
            reason=reason,
        )

    def transition_evidence(
        self,
        run_id: str,
        step_id: str,
        attempt: int,
        *,
        to_state: Literal["ready", "not_ready", "timeout"],
        reason: str | None = None,
    ) -> None:
        """遷移 evidence 狀態並原子記錄 transition。"""
        self._transition_step(
            run_id,
            step_id,
            attempt,
            state_kind="evidence",
            to_state=to_state,
            exit_code=None,
            outcome_summary=None,
            reason=reason,
        )

    def _transition_step(
        self,
        run_id: str,
        step_id: str,
        attempt: int,
        *,
        state_kind: Literal["command", "dispatch", "evidence"],
        to_state: str,
        exit_code: int | None,
        outcome_summary: dict[str, Any] | None,
        reason: str | None,
    ) -> None:
        now = self._clock()
        status_column = f"{state_kind}_status"

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                f"SELECT {status_column} FROM orchestration_steps "
                "WHERE run_id = ? AND step_id = ? AND attempt = ?",
                (run_id, step_id, attempt),
            ).fetchone()
            if row is None:
                raise KeyError(f"step not found: {run_id}:{step_id}#{attempt}")
            from_state = row[status_column]
            self._validate_transition(state_kind, from_state, to_state)

            updates = [f"{status_column} = ?"]
            update_parameters: list[Any] = [to_state]
            if state_kind == "command":
                if to_state == "running":
                    updates.append("submitted_at = ?")
                    update_parameters.append(now)
                elif to_state in _TERMINAL_COMMAND_STATES:
                    updates.append("command_finished_at = ?")
                    update_parameters.append(now)
                    if exit_code is not None:
                        updates.append("exit_code = ?")
                        update_parameters.append(exit_code)
            elif state_kind == "dispatch" and to_state in _TERMINAL_DISPATCH_STATES:
                updates.extend(("dispatch_finished_at = ?", "outcome_summary_json = ?"))
                update_parameters.extend((now, self._encode_json(outcome_summary)))
            elif state_kind == "evidence" and to_state == "ready":
                updates.append("evidence_ready_at = ?")
                update_parameters.append(now)

            update_parameters.extend((run_id, step_id, attempt))
            connection.execute(
                f"UPDATE orchestration_steps SET {', '.join(updates)} "
                "WHERE run_id = ? AND step_id = ? AND attempt = ?",
                update_parameters,
            )
            self._append_transition(
                connection,
                run_id=run_id,
                step_id=step_id,
                attempt=attempt,
                state_kind=state_kind,
                from_state=from_state,
                to_state=to_state,
                timestamp=now,
                reason=reason,
            )

        self._write_transaction(operation)

    def mark_step_terminal(
        self,
        run_id: str,
        step_id: str,
        attempt: int,
        *,
        terminal_reason: str,
        recovery_disposition: str | None = None,
    ) -> None:
        """設定 step 的 terminal 與 recovery 資訊，不改三態。"""

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE orchestration_steps SET terminal_reason = ?, "
                "recovery_disposition = ? WHERE run_id = ? AND step_id = ? AND attempt = ?",
                (terminal_reason, recovery_disposition, run_id, step_id, attempt),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"step not found: {run_id}:{step_id}#{attempt}")

        self._write_transaction(operation)

    def get_step(self, run_id: str, step_id: str, attempt: int) -> StepRecord | None:
        """以完整 step identity 讀取指定 attempt。"""
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM orchestration_steps "
            "WHERE run_id = ? AND step_id = ? AND attempt = ?",
            (run_id, step_id, attempt),
        ).fetchone()
        return None if row is None else self._step_record(row)

    def list_steps_of_run(self, run_id: str) -> list[StepRecord]:
        """列出 run 的所有 steps，依 step 與 attempt 排序。"""
        rows = self._require_connection().execute(
            "SELECT * FROM orchestration_steps WHERE run_id = ? "
            "ORDER BY step_id ASC, attempt ASC",
            (run_id,),
        ).fetchall()
        return [self._step_record(row) for row in rows]

    def list_transitions(
        self,
        *,
        run_id: str,
        step_id: str | None = None,
        attempt: int | None = None,
        state_kind: str | None = None,
    ) -> list[TransitionRecord]:
        """依 timestamp 升序列出 transitions。"""
        clauses = ["run_id = ?"]
        parameters: list[Any] = [run_id]
        if step_id is not None:
            clauses.append("step_id = ?")
            parameters.append(step_id)
        if attempt is not None:
            clauses.append("attempt = ?")
            parameters.append(attempt)
        if state_kind is not None:
            clauses.append("state_kind = ?")
            parameters.append(state_kind)
        rows = self._require_connection().execute(
            "SELECT * FROM orchestration_transitions WHERE "
            f"{' AND '.join(clauses)} ORDER BY timestamp ASC, transition_id ASC",
            parameters,
        ).fetchall()
        return [self._transition_record(row) for row in rows]

    def close(self) -> None:
        """關閉 SQLite connection。"""
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "OrchestrationLedger":
        """支援 context manager。"""
        return self

    def __exit__(self, *args: Any) -> None:
        """離開 context manager 時關閉 connection。"""
        self.close()
