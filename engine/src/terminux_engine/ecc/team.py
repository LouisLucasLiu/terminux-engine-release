# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""TeamSnapshot 多人聚合 — 掃描同 project 工程師的 event_store.sqlite，建立團隊快照。

PM（或任何人）在 @@ 提問時，LLM 可看到全團隊的進度。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ─── Models ──────────────────────────────────────────────────────────────────

class TeamMemberSnapshot(BaseModel):
    """單個團隊成員的最近 session 快照。"""

    user_id: str
    last_active: float
    """最後一筆 event 的 timestamp"""
    last_session_id: str
    block_names: list[str]
    """該 user 的 events 裡出現過的 distinct block_names（排除 None，跨 session）"""
    recent_commands: int
    """最近 session 的 command count"""
    corners_run: list[str]
    """最近 session 跑過的 distinct corners"""
    corners_passed: list[str]
    """最近 session 最後狀態為 pass 的 corners"""
    corners_failed: list[str]
    """最近 session 最後狀態為 fail 的 corners"""
    latest_wns: dict[str, float]
    """corner → 最新 WNS 值（最近 session）"""


class TeamSnapshot(BaseModel):
    """全團隊快照。"""

    project_id: str
    computed_at: float
    members: list[TeamMemberSnapshot]
    """按 last_active 降序排列"""


# ─── discover_team_databases ─────────────────────────────────────────────────

def discover_team_databases(
    project_id: str,
    base_dir: Path | None = None,
) -> list[tuple[str, Path]]:
    """掃描 {base_dir}/{project_id}/*/.terminux/event_store.sqlite。

    回傳 [(user_id, db_path), ...]。user_id 從子目錄名取得。
    base_dir 不存在或 project_dir 不存在時回傳空 list。
    """
    if base_dir is None:
        base_dir = Path("/projusers")

    project_dir = base_dir / project_id
    if not project_dir.is_dir():
        return []

    results: list[tuple[str, Path]] = []
    try:
        for user_dir in sorted(project_dir.iterdir()):
            if not user_dir.is_dir():
                continue
            db_path = user_dir / ".terminux" / "event_store.sqlite"
            if db_path.exists():
                results.append((user_dir.name, db_path))
    except Exception as exc:
        logger.warning("discover_team_databases failed: %s", exc)

    return results


# ─── build_member_snapshot ───────────────────────────────────────────────────

def build_member_snapshot(user_id: str, db_path: Path) -> TeamMemberSnapshot | None:
    """Read-only 開啟某工程師的 SQLite，聚合最近 session 的資料。

    失敗時回傳 None（DB 損壞、不可讀、無 events 等）。
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

        # 找最新 session_id
        row = conn.execute(
            "SELECT session_id FROM session_events "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        latest_sid: str = row[0]

        # last_active：最新 session 的 MAX(timestamp)
        row = conn.execute(
            "SELECT MAX(timestamp) FROM session_events WHERE session_id = ?",
            (latest_sid,),
        ).fetchone()
        last_active: float = row[0] if row and row[0] is not None else 0.0

        # recent_commands：最新 session 的 command count
        row = conn.execute(
            "SELECT COUNT(*) FROM session_events "
            "WHERE session_id = ? AND event_type = 'command'",
            (latest_sid,),
        ).fetchone()
        recent_commands: int = row[0] if row else 0

        # block_names：全部 session 的 distinct block_name（排除 NULL）
        rows = conn.execute(
            "SELECT DISTINCT block_name FROM session_events "
            "WHERE block_name IS NOT NULL"
        ).fetchall()
        block_names = sorted(r[0] for r in rows)

        # corners_run：最新 session 中出現過的 distinct corner
        rows = conn.execute(
            "SELECT DISTINCT corner FROM session_events "
            "WHERE session_id = ? AND event_type IN ('metric', 'gate_eval', 'corner_update') "
            "AND corner IS NOT NULL",
            (latest_sid,),
        ).fetchall()
        corners_run = sorted(r[0] for r in rows)

        # corners_passed / corners_failed：從 corner_update events 取每個 corner 的最後狀態
        corners_passed: list[str] = []
        corners_failed: list[str] = []
        rows = conn.execute(
            "SELECT corner, payload FROM session_events "
            "WHERE session_id = ? AND event_type = 'corner_update' AND corner IS NOT NULL "
            "ORDER BY timestamp ASC",
            (latest_sid,),
        ).fetchall()
        # 每個 corner 的最後狀態（後面覆蓋前面）
        corner_status: dict[str, str] = {}
        for r in rows:
            payload = json.loads(r[1])
            status = payload.get("status", "")
            corner_status[r[0]] = status
        for corner, status in sorted(corner_status.items()):
            if status == "pass":
                corners_passed.append(corner)
            elif status == "fail":
                corners_failed.append(corner)

        # latest_wns：metric events 中 metric_name='wns_ns' 的每個 corner 最新值
        latest_wns: dict[str, float] = {}
        rows = conn.execute(
            "SELECT corner, payload FROM session_events "
            "WHERE session_id = ? AND event_type = 'metric' AND corner IS NOT NULL "
            "ORDER BY timestamp ASC",
            (latest_sid,),
        ).fetchall()
        for r in rows:
            payload = json.loads(r[1])
            if payload.get("metric_name") == "wns_ns":
                value = payload.get("value")
                if value is not None:
                    latest_wns[r[0]] = float(value)

        return TeamMemberSnapshot(
            user_id=user_id,
            last_active=last_active,
            last_session_id=latest_sid,
            block_names=block_names,
            recent_commands=recent_commands,
            corners_run=corners_run,
            corners_passed=corners_passed,
            corners_failed=corners_failed,
            latest_wns=latest_wns,
        )

    except Exception as exc:
        logger.warning("build_member_snapshot failed for %s (%s): %s", user_id, db_path, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ─── build_team_snapshot ─────────────────────────────────────────────────────

def build_team_snapshot(
    project_id: str,
    base_dir: Path | None = None,
) -> TeamSnapshot:
    """掃描 + 聚合所有工程師，回傳 TeamSnapshot。

    某個工程師的 SQLite 讀不到時跳過，不中斷。
    """
    import time

    databases = discover_team_databases(project_id, base_dir=base_dir)

    members: list[TeamMemberSnapshot] = []
    for uid, db_path in databases:
        snap = build_member_snapshot(uid, db_path)
        if snap is not None:
            members.append(snap)

    # 按 last_active 降序排列
    members.sort(key=lambda m: m.last_active, reverse=True)

    return TeamSnapshot(
        project_id=project_id,
        computed_at=time.time(),
        members=members,
    )
