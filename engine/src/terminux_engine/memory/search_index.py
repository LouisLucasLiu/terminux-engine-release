# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""SearchIndex：FTS5 全文索引 + vector 索引。

使用獨立的 SQLite 資料庫（不是 event_store 的 DB）。
FTS5 提供 BM25 關鍵字搜索；vector 表提供 cosine similarity 搜索。
"""

import logging
import math
import sqlite3
import struct
import threading
from pathlib import Path

from terminux_engine.memory.types import IndexEntry, SearchResult

logger = logging.getLogger(__name__)


# ── embedding 序列化 ─────────────────────────────────────────────────────────

def _serialize_embedding(embedding: list[float]) -> bytes:
    """list[float] → BLOB（float32 array）。"""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _deserialize_embedding(blob: bytes) -> list[float]:
    """BLOB → list[float]。"""
    count = len(blob) // 4  # float32 = 4 bytes
    return list(struct.unpack(f"{count}f", blob))


# ── cosine similarity ────────────────────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """計算兩個向量的 cosine similarity。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── SearchIndex ──────────────────────────────────────────────────────────────

class SearchIndex:
    """管理 FTS5 全文索引和 vector 索引。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        """建立 FTS5 和 vector 表（若不存在）。"""
        self._conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                content,
                event_id UNINDEXED,
                event_type UNINDEXED,
                session_id UNINDEXED,
                terminal_id UNINDEXED,
                user_id UNINDEXED,
                timestamp UNINDEXED
            );

            CREATE TABLE IF NOT EXISTS memory_vectors (
                event_id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                terminal_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                embedding BLOB NOT NULL
            );
        """)
        self._conn.commit()

    def add_entry(self, entry: IndexEntry) -> None:
        """寫入一筆索引條目（FTS5 + vector）。Thread-safe。"""
        with self._lock:
            # FTS5
            self._conn.execute(
                "INSERT INTO memory_fts "
                "(content, event_id, event_type, session_id, terminal_id, user_id, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.content,
                    entry.event_id,
                    entry.event_type,
                    entry.session_id,
                    entry.terminal_id,
                    entry.user_id,
                    entry.timestamp,
                ),
            )
            # Vector（若有 embedding）
            if entry.embedding is not None:
                blob = _serialize_embedding(entry.embedding)
                self._conn.execute(
                    "INSERT OR REPLACE INTO memory_vectors "
                    "(event_id, event_type, session_id, terminal_id, user_id, timestamp, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.event_id,
                        entry.event_type,
                        entry.session_id,
                        entry.terminal_id,
                        entry.user_id,
                        entry.timestamp,
                        blob,
                    ),
                )
            self._conn.commit()

    def update_embedding(self, event_id: int, embedding: list[float]) -> None:
        """為已存在的 event 補填 embedding。

        - event_id 已有 embedding → UPDATE
        - event_id 在 FTS 表有但 vector 表沒有 → INSERT（metadata 從 FTS 查）
        - event_id 在 FTS 表也沒有 → 靜默忽略
        """
        blob = _serialize_embedding(embedding)
        with self._lock:
            # 檢查 vector 表是否已有
            row = self._conn.execute(
                "SELECT event_id FROM memory_vectors WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None:
                # UPDATE 既有 embedding
                self._conn.execute(
                    "UPDATE memory_vectors SET embedding = ? WHERE event_id = ?",
                    (blob, event_id),
                )
                self._conn.commit()
                return

            # 從 FTS 表查 metadata
            fts_row = self._conn.execute(
                "SELECT event_type, session_id, terminal_id, user_id, timestamp "
                "FROM memory_fts WHERE event_id = ? LIMIT 1",
                (event_id,),
            ).fetchone()
            if fts_row is None:
                # event_id 不存在 → 靜默忽略
                return

            event_type, session_id, terminal_id, user_id, timestamp = fts_row
            self._conn.execute(
                "INSERT INTO memory_vectors "
                "(event_id, event_type, session_id, terminal_id, user_id, timestamp, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, event_type, session_id, terminal_id, user_id, timestamp, blob),
            )
            self._conn.commit()

    def search_bm25(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        since_timestamp: float | None = None,
    ) -> list[SearchResult]:
        """FTS5 BM25 關鍵字搜索。"""
        if not query.strip():
            return []

        # FTS5 MATCH 查詢
        if since_timestamp is not None:
            sql = (
                "SELECT event_id, event_type, session_id, terminal_id, "
                "       timestamp, content, rank "
                "FROM memory_fts "
                "WHERE memory_fts MATCH ? AND user_id = ? AND timestamp >= ? "
                "ORDER BY rank "
                "LIMIT ?"
            )
            params: tuple = (query, user_id, since_timestamp, limit)
        else:
            sql = (
                "SELECT event_id, event_type, session_id, terminal_id, "
                "       timestamp, content, rank "
                "FROM memory_fts "
                "WHERE memory_fts MATCH ? AND user_id = ? "
                "ORDER BY rank "
                "LIMIT ?"
            )
            params = (query, user_id, limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # FTS5 MATCH 語法錯誤等 → 回空
            return []

        results: list[SearchResult] = []
        for row in rows:
            event_id, event_type, session_id, terminal_id, ts, content, rank = row
            results.append(SearchResult(
                event_id=int(event_id),
                event_type=str(event_type),
                session_id=str(session_id),
                terminal_id=str(terminal_id),
                timestamp=float(ts),
                content=str(content),
                score=-float(rank),  # FTS5 rank 是負的，越負越相關
                match_type="bm25",
            ))
        return results

    def search_vector(
        self,
        query_embedding: list[float],
        user_id: str,
        limit: int = 10,
        since_timestamp: float | None = None,
    ) -> list[SearchResult]:
        """Vector cosine similarity 搜索。"""
        # 撈出符合條件的所有 vectors
        if since_timestamp is not None:
            sql = (
                "SELECT v.event_id, v.event_type, v.session_id, v.terminal_id, "
                "       v.timestamp, v.embedding "
                "FROM memory_vectors v "
                "WHERE v.user_id = ? AND v.timestamp >= ?"
            )
            rows = self._conn.execute(sql, (user_id, since_timestamp)).fetchall()
        else:
            sql = (
                "SELECT v.event_id, v.event_type, v.session_id, v.terminal_id, "
                "       v.timestamp, v.embedding "
                "FROM memory_vectors v "
                "WHERE v.user_id = ?"
            )
            rows = self._conn.execute(sql, (user_id,)).fetchall()

        if not rows:
            return []

        # 計算 cosine similarity 並排序
        scored: list[tuple[float, tuple]] = []
        for row in rows:
            event_id, event_type, session_id, terminal_id, ts, emb_blob = row
            emb = _deserialize_embedding(emb_blob)
            sim = _cosine_similarity(query_embedding, emb)
            scored.append((sim, row))

        # 降序排列（similarity 越高越好）
        scored.sort(key=lambda x: x[0], reverse=True)
        top_n = scored[:limit]

        # 查 content（從 FTS 表 join）
        results: list[SearchResult] = []
        for sim, row in top_n:
            event_id, event_type, session_id, terminal_id, ts, _ = row
            # 查 FTS 取 content
            fts_row = self._conn.execute(
                "SELECT content FROM memory_fts WHERE event_id = ? LIMIT 1",
                (event_id,),
            ).fetchone()
            content = fts_row[0] if fts_row else ""
            results.append(SearchResult(
                event_id=int(event_id),
                event_type=str(event_type),
                session_id=str(session_id),
                terminal_id=str(terminal_id),
                timestamp=float(ts),
                content=content,
                score=sim,
                match_type="vector",
            ))
        return results

    def count(self, user_id: str | None = None) -> int:
        """FTS 表的條目數。"""
        if user_id is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory_fts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        """關閉 DB 連接。"""
        try:
            self._conn.close()
        except Exception:
            pass
