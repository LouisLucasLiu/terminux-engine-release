# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Event-Sourcing Store — 將 ECC mutation 記錄為獨立事件，存入 SQLite。

SessionEvent model + EventStore class。
emit 時自動索引到 SearchIndex（hook）。
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from terminux_engine.memory.embedder import Embedder
    from terminux_engine.memory.search_index import SearchIndex

logger = logging.getLogger(__name__)

# ─── SessionEvent model ─────────────────────────────────────────────────────

class SessionEvent(BaseModel):
    """單一 ECC 事件。"""

    event_id: str
    """uuid4().hex"""
    session_id: str
    terminal_id: str = "default"
    """Terminal 識別碼。預設 "default" 向下相容單 terminal 場景。"""
    user_id: str
    project_id: str | None = None
    package_id: str | None = None
    timestamp: float
    event_type: str
    """session_start | command | stage_change | metric | gate_eval | corner_update"""
    payload: dict[str, Any]
    block_name: str | None = None
    """IC block 名稱；未提取時為 None。"""

    # ─── trace chain + layer ────────────────────────────────
    # 格式驗證由生產端負責（C++ terminal / shell hook / wrapper）；
    # event_store 是高頻寫入路徑，model 層不再驗證。
    trace_root: str = ""
    """sess_{uuid_hex}。空字串 = pre-Z1-3 legacy event 或非 Terminux 來源。"""
    trace_queue: str = ""
    """submit_{random_hex}。Workstation-only / MVP 階段固定空字串。"""
    trace_job: str = ""
    """slurm_job_{job_id}。Workstation-only / MVP 階段固定空字串。"""
    trace_parent: str = ""
    """上一層的 trace_self。根層級為空字串。"""
    trace_self: str = ""
    """inv_{epoch_ns}_{pid}。空字串 = pre-Z1-3 legacy event。"""
    layer: str | None = None
    """事件來源層。Canonical: terminal | shell | wrapper | bash_env。None = 未標記。"""


# ─── SQLite Schema ───────────────────────────────────────────────────────────

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS session_events (
    event_id     TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    terminal_id  TEXT DEFAULT 'default',
    user_id      TEXT NOT NULL,
    project_id   TEXT,
    package_id   TEXT,
    timestamp    REAL NOT NULL,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    tool_name    TEXT,
    corner       TEXT,
    stage_id     TEXT,
    block_name   TEXT,
    trace_root   TEXT DEFAULT '',
    trace_queue  TEXT DEFAULT '',
    trace_job    TEXT DEFAULT '',
    trace_parent TEXT DEFAULT '',
    trace_self   TEXT DEFAULT '',
    layer        TEXT
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_session ON session_events (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_time ON session_events (user_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_type ON session_events (event_type)",
    "CREATE INDEX IF NOT EXISTS idx_corner ON session_events (corner)",
    "CREATE INDEX IF NOT EXISTS idx_stage ON session_events (stage_id)",
    "CREATE INDEX IF NOT EXISTS idx_block ON session_events (block_name)",
    "CREATE INDEX IF NOT EXISTS idx_project ON session_events (project_id)",
    "CREATE INDEX IF NOT EXISTS idx_terminal ON session_events (terminal_id)",
    # trace chain 三個主查詢鍵
    # queue/job 在 MVP 永遠空字串，selectivity=0，等 Z4 開始填值再加 index
    # layer 值域 2-4 種，selectivity 太低，靠 session_id 主索引就夠
    "CREATE INDEX IF NOT EXISTS idx_trace_root ON session_events (trace_root)",
    "CREATE INDEX IF NOT EXISTS idx_trace_self ON session_events (trace_self)",
    "CREATE INDEX IF NOT EXISTS idx_trace_parent ON session_events (trace_parent)",
]


# 共用 SELECT column list 與 row → SessionEvent 重建邏輯
# 5 個 query method 共用，避免 6 欄位 × 5 處的複製貼上錯字風險
_SELECT_COLUMNS = (
    "event_id, session_id, terminal_id, user_id, project_id, package_id, "
    "timestamp, event_type, payload, block_name, "
    "trace_root, trace_queue, trace_job, trace_parent, trace_self, layer"
)


def _row_to_event(row: tuple) -> SessionEvent:
    """把 _SELECT_COLUMNS 順序的 SQLite row 還原為 SessionEvent。"""
    return SessionEvent(
        event_id=row[0],
        session_id=row[1],
        terminal_id=row[2] or "default",
        user_id=row[3],
        project_id=row[4],
        package_id=row[5],
        timestamp=row[6],
        event_type=row[7],
        payload=json.loads(row[8]),
        block_name=row[9],
        trace_root=row[10] or "",
        trace_queue=row[11] or "",
        trace_job=row[12] or "",
        trace_parent=row[13] or "",
        trace_self=row[14] or "",
        layer=row[15],
    )


# ─── Search index hook ────────────────────────────────────────────────────────

_INDEXABLE_EVENT_TYPES = frozenset({
    "chat_query",
    "ai_response",
    "nudge",
    "lineage_check",
    "conversation_insight",
    "period_digest",
    "daily_digest",
})


def _extract_indexable_content(event: SessionEvent) -> str | None:
    """從 event payload 提取要索引的文字。

    回傳 None 或空字串表示跳過索引。
    """
    p = event.payload
    et = event.event_type

    if et == "chat_query":
        query = p.get("query", "")
        response = p.get("response", "")
        content = f"{query}\n{response}".strip()
        return content or None

    if et == "ai_response":
        return p.get("response_text", "") or None

    if et == "nudge":
        return p.get("nudge_text", "") or p.get("text", "") or None

    if et == "lineage_check":
        return p.get("message", "") or None

    if et == "conversation_insight":
        insights = p.get("insights", [])
        if not insights:
            return None
        parts = [f"[{i.get('topic', 'general')}] {i.get('content', '')}" for i in insights]
        return "\n".join(parts) or None

    if et in ("period_digest", "daily_digest"):
        return p.get("llm_summary", "") or None

    return None


# ─── EventStore ──────────────────────────────────────────────────────────────

class EventStore:
    """SQLite-backed event store，thread-safe（WAL + Lock）。"""

    def __init__(
        self,
        db_path: str | Path,
        search_index: SearchIndex | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # WAL 允許多個 process 同時開同一份 store，但寫入仍是互斥的。沒有
        # busy timeout 時，第二個 writer 會立刻拿到 SQLITE_BUSY，讓「同時送達
        # 的兩個請求」看起來像寫入失敗，而不是排隊。這裡讓它等待。
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute(_CREATE_TABLE)
        self._migrate_schema()
        for idx_sql in _CREATE_INDEXES:
            self._conn.execute(idx_sql)
        self._conn.commit()

        self._write_lock = threading.Lock()
        self._search_index = search_index
        self._embedder = embedder
        self._embedding_executor: concurrent.futures.ThreadPoolExecutor | None = None

    # ─── schema migration ─────────────────────────────────────────────────

    def _migrate_schema(self) -> None:
        """檢查並執行 schema migration（冪等）。"""
        existing_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(session_events)").fetchall()
        }
        if "block_name" not in existing_cols:
            self._conn.execute("ALTER TABLE session_events ADD COLUMN block_name TEXT")
            logger.info("Schema migration: added block_name column")
        if "terminal_id" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE session_events ADD COLUMN terminal_id TEXT DEFAULT 'default'"
            )
            logger.info("Schema migration: added terminal_id column")
        if "package_id" not in existing_cols:
            self._conn.execute("ALTER TABLE session_events ADD COLUMN package_id TEXT")
            logger.info("Schema migration: added package_id column")
        # trace chain + layer
        # ALTER 必須給 DEFAULT，否則 SQLite 不允許 NOT NULL；舊 row 走 DEFAULT 值
        if "trace_root" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE session_events ADD COLUMN trace_root TEXT DEFAULT ''"
            )
            logger.info("Schema migration: added trace_root column")
        if "trace_queue" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE session_events ADD COLUMN trace_queue TEXT DEFAULT ''"
            )
            logger.info("Schema migration: added trace_queue column")
        if "trace_job" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE session_events ADD COLUMN trace_job TEXT DEFAULT ''"
            )
            logger.info("Schema migration: added trace_job column")
        if "trace_parent" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE session_events ADD COLUMN trace_parent TEXT DEFAULT ''"
            )
            logger.info("Schema migration: added trace_parent column")
        if "trace_self" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE session_events ADD COLUMN trace_self TEXT DEFAULT ''"
            )
            logger.info("Schema migration: added trace_self column")
        if "layer" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE session_events ADD COLUMN layer TEXT"
            )
            logger.info("Schema migration: added layer column")

    # ─── emit ────────────────────────────────────────────────────────────

    def emit(self, event: SessionEvent) -> None:
        """寫入單筆事件。自動從 payload 提取冗餘欄位。

        若已注入 search_index，符合條件的 event 會自動進 FTS5 索引。
        若已注入 embedder，會在 background thread 計算 embedding。
        """
        payload_str = json.dumps(event.payload, ensure_ascii=False)

        # 從 payload 提取冗餘欄位
        tool_name = event.payload.get("tool_name")
        corner = event.payload.get("corner")
        stage_id = event.payload.get("stage_id") or event.payload.get("to_stage")
        block_name = event.block_name

        with self._write_lock:
            # INSERT 開啟寫入交易。失敗時若不 rollback，這條連線會一直握著寫入
            # 鎖，讓同一份 store 的其他 writer 全部卡到 busy timeout 到期——
            # 重複 event id 這種可預期的失敗尤其致命。
            try:
                self._conn.execute(
                    "INSERT INTO session_events "
                    "(event_id, session_id, terminal_id, user_id, project_id, package_id, "
                    "timestamp, event_type, payload, tool_name, corner, stage_id, block_name, "
                    "trace_root, trace_queue, trace_job, trace_parent, trace_self, layer) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.session_id,
                        event.terminal_id,
                        event.user_id,
                        event.project_id,
                        event.package_id,
                        event.timestamp,
                        event.event_type,
                        payload_str,
                        tool_name,
                        corner,
                        stage_id,
                        block_name,
                        event.trace_root,
                        event.trace_queue,
                        event.trace_job,
                        event.trace_parent,
                        event.trace_self,
                        event.layer,
                    ),
                )
                # 取 SQLite rowid 作為 search index 的 event_id
                rowid = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

        # Search index hook（在 write_lock 外執行，避免延長鎖持有時間）
        if self._search_index is not None and event.event_type in _INDEXABLE_EVENT_TYPES:
            try:
                content = _extract_indexable_content(event)
                if content:
                    from terminux_engine.memory.types import IndexEntry
                    entry = IndexEntry(
                        event_id=rowid,
                        event_type=event.event_type,
                        session_id=event.session_id,
                        terminal_id=event.terminal_id,
                        user_id=event.user_id,
                        timestamp=event.timestamp,
                        content=content,
                    )
                    self._search_index.add_entry(entry)

                    # Async embedding（background thread）
                    if self._embedder is not None:
                        self._submit_embedding_job(rowid, content)
            except Exception as exc:
                logger.warning("Search index hook failed (non-fatal): %s", exc)

    def _submit_embedding_job(self, event_id: int, content: str) -> None:
        """在 background thread 計算 embedding 並回寫 search_index。"""
        if self._embedding_executor is None:
            self._embedding_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="terminux-embed",
            )
        self._embedding_executor.submit(self._do_embedding, event_id, content)

    def _do_embedding(self, event_id: int, content: str) -> None:
        """Background thread：計算 embedding → 寫入 search_index。"""
        try:
            embedding = self._embedder.embed(content)
            if embedding is not None and self._search_index is not None:
                self._search_index.update_embedding(event_id, embedding)
        except Exception as exc:
            logger.warning("Async embedding failed (non-fatal): %s", exc)

    # ─── query ───────────────────────────────────────────────────────────

    def query(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        event_type: str | None = None,
        package_id: str | None = None,
        tool_name: str | None = None,
        corner: str | None = None,
        stage_id: str | None = None,
        project_id: str | None = None,
        block_name: str | None = None,
        after: float | None = None,
        before: float | None = None,
        limit: int | None = None,
        order: str = "asc",
    ) -> list[SessionEvent]:
        """查詢事件。所有條件為 AND 組合。"""
        if order not in ("asc", "desc"):
            raise ValueError(f"order must be 'asc' or 'desc', got '{order}'")

        clauses: list[str] = []
        params: list[Any] = []

        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if package_id is not None:
            clauses.append("package_id = ?")
            params.append(package_id)
        if tool_name is not None:
            clauses.append("tool_name = ?")
            params.append(tool_name)
        if corner is not None:
            clauses.append("corner = ?")
            params.append(corner)
        if stage_id is not None:
            clauses.append("stage_id = ?")
            params.append(stage_id)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if block_name is not None:
            clauses.append("block_name = ?")
            params.append(block_name)
        if after is not None:
            clauses.append("timestamp > ?")
            params.append(after)
        if before is not None:
            clauses.append("timestamp < ?")
            params.append(before)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM session_events"
            f"{where} ORDER BY timestamp {order.upper()}"
        )

        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        # 同一個 SQLite connection 也會由 EAP autonomous driver 寫入；
        # Python connection 雖允許跨 thread，卻不允許 query 與 commit 在同一
        # connection 上同時進行。讀寫共用既有鎖，維持本類別宣告的 thread-safe
        # 邊界，避免 work.get 偶發收到 driver 寫入中的 internal error。
        with self._write_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    # ─── analysis task：AnalysisTask 專用的 scoped 唯讀查詢 ────────────────────
    #
    # 兩個 method 都是 **additive、唯讀、參數化 SQL**，沿用既有連線與
    # `_SELECT_COLUMNS`。不新增 table、不新增 index、不接受任意 SQL、不在
    # 查詢時寫任何東西。`user_id`／`project_id` 是 row-level scope 的必要
    # 條件；payload 內的 scope 由呼叫端（`analysis_tasks.reader`）再驗一次。

    def get_event_by_id_for_scope(
        self,
        event_id: str,
        *,
        user_id: str,
        project_id: str | None,
    ) -> SessionEvent | None:
        """以 host 產生的 `event_id` 取單筆，並要求 row scope 相符。

        找不到與跨 scope 一律回 ``None``——呼叫端對外收斂成同一個錯誤，
        不洩漏「這筆存在但不屬於你」。
        """
        if not isinstance(event_id, str) or not event_id:
            return None
        if project_id is None:
            sql = (f"SELECT {_SELECT_COLUMNS} FROM session_events "
                   "WHERE event_id = ? AND user_id = ? AND project_id IS NULL")
            params: list[Any] = [event_id, user_id]
        else:
            sql = (f"SELECT {_SELECT_COLUMNS} FROM session_events "
                   "WHERE event_id = ? AND user_id = ? AND project_id = ?")
            params = [event_id, user_id, project_id]
        row = self._conn.execute(sql, params).fetchone()
        return None if row is None else _row_to_event(row)

    def query_task_events(
        self,
        task_id: str,
        *,
        user_id: str,
        project_id: str | None,
        event_types: Sequence[str],
        limit: int,
        any_project: bool = False,
    ) -> tuple[list[SessionEvent], bool]:
        """取某個 task 的全部 lifecycle event。

        ``any_project=True`` 時**不**以 project 過濾
        （仍以 ``user_id`` 過濾）。給「未宣告 project 的 session」在 daemon 重啟後
        讀回自己以 Profile fallback 建立的 task 用；呼叫端（reader／service）必須
        再以 snapshot 的 scope 做 member／project_source 判定。

        回傳 ``(events, truncated)``。``truncated=True`` 代表命中 ``limit``，
        呼叫端**必須**把它當成 evidence incomplete，不得當作完整歷史
。

        `task_id` 走 payload 的 JSON 欄位；`json_valid` 先行，避免舊資料或
        損毀 payload 讓 SQLite JSON 函式拋例外。這是既有 ledger
        `_scope_sql_expression()` 已採用的同一個 fail-closed 手法。
        """
        if not isinstance(task_id, str) or not task_id:
            return [], False
        types = [t for t in event_types if isinstance(t, str) and t]
        if not types:
            return [], False
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")

        placeholders = ", ".join("?" for _ in types)
        clauses = [
            f"event_type IN ({placeholders})",
            "user_id = ?",
            *([] if any_project else
              ["project_id IS NULL" if project_id is None else "project_id = ?"]),
            "json_valid(payload)",
            "json_extract(payload, '$.task_id') = ?",
        ]
        params: list[Any] = [*types, user_id]
        if project_id is not None and not any_project:
            params.append(project_id)
        params.append(task_id)
        # limit + 1：多讀一筆用來偵測截斷，不把「剛好讀滿」誤判成完整。
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM session_events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp ASC, rowid ASC LIMIT ?"
        )
        params.append(limit + 1)
        rows = self._conn.execute(sql, params).fetchall()
        truncated = len(rows) > limit
        return [_row_to_event(r) for r in rows[:limit]], truncated

    # ─── get_latest_metrics ──────────────────────────────────────────────

    def get_latest_metrics(
        self,
        session_id: str,
        metric_name: str | None = None,
        corner: str | None = None,
        n: int = 3,
    ) -> list[SessionEvent]:
        """查詢 event_type='metric' 的最新 n 筆事件。"""
        clauses = ["session_id = ?", "event_type = 'metric'"]
        params: list[Any] = [session_id]

        if metric_name is not None:
            clauses.append("json_extract(payload, '$.metric_name') = ?")
            params.append(metric_name)
        if corner is not None:
            clauses.append("corner = ?")
            params.append(corner)

        where = " WHERE " + " AND ".join(clauses)
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM session_events"
            f"{where} ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(n)

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    # ─── get_session_ids ─────────────────────────────────────────────────

    def get_session_ids(
        self,
        user_id: str | None = None,
        n: int = 5,
    ) -> list[str]:
        """回傳最近 n 個 session_id（依最後事件時間排序）。"""
        clauses: list[str] = []
        params: list[Any] = []

        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT session_id FROM session_events{where} "
            "GROUP BY session_id ORDER BY MAX(timestamp) DESC LIMIT ?"
        )
        params.append(n)

        rows = self._conn.execute(sql, params).fetchall()
        return [r[0] for r in rows]

    # ─── query_by_terminal ──────────────────────────────────────────────

    def query_by_terminal(
        self,
        terminal_id: str,
        event_types: list[str] | None = None,
        since_timestamp: float | None = None,
        limit: int | None = None,
    ) -> list[SessionEvent]:
        """查詢指定 terminal 的 events。"""
        clauses = ["terminal_id = ?"]
        params: list[Any] = [terminal_id]

        if event_types is not None:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        if since_timestamp is not None:
            clauses.append("timestamp > ?")
            params.append(since_timestamp)

        where = " WHERE " + " AND ".join(clauses)
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM session_events"
            f"{where} ORDER BY timestamp ASC"
        )

        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    # ─── query_events_in_range ────────────────────────────────────────────

    def query_events_in_range(
        self,
        user_id: str,
        start_ts: float,
        end_ts: float,
        event_types: list[str] | None = None,
        terminal_id: str | None = None,
    ) -> list[SessionEvent]:
        """撈指定時間範圍內的 events（inclusive 兩端）。terminal_id 為 None 時不過濾。"""
        clauses = ["user_id = ?", "timestamp >= ?", "timestamp <= ?"]
        params: list[Any] = [user_id, start_ts, end_ts]

        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)

        if terminal_id is not None:
            clauses.append("terminal_id = ?")
            params.append(terminal_id)

        where = " WHERE " + " AND ".join(clauses)
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM session_events"
            f"{where} ORDER BY timestamp ASC"
        )

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    # ─── find_period_digests_by_range ──────────────────────────────────────

    def find_period_digests_by_range(
        self,
        user_id: str,
        period_start: float,
        period_end: float,
    ) -> list[SessionEvent]:
        """查同一 period_start + period_end 的所有 period_digest events。

        回傳按 timestamp ASC 排序（最舊的在前）。
        """
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM session_events "
            "WHERE event_type = 'period_digest' "
            "AND user_id = ? "
            "AND json_extract(payload, '$.period_start') = ? "
            "AND json_extract(payload, '$.period_end') = ? "
            "ORDER BY timestamp ASC"
        )
        rows = self._conn.execute(
            sql, (user_id, period_start, period_end),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    # ─── close ───────────────────────────────────────────────────────────

    def close(self) -> None:
        """關閉 SQLite 連線和 embedding executor（冪等）。"""
        if self._embedding_executor is not None:
            try:
                self._embedding_executor.shutdown(wait=False)
            except Exception:
                pass
            self._embedding_executor = None
        try:
            self._conn.close()
        except Exception:
            pass
