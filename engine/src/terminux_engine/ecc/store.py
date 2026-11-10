# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ECC Object 的 JSONL 持久化存儲 + EventStore 聚合函數。

JSONL：每次 save 都 append 一行完整的 ECCObject JSON snapshot。
EventStore：從 SQLite event store 聚合 SessionSummary / SessionContinuity。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from terminux_engine.ecc.ecc_object import ECCObject, SessionContinuity, SessionSummary

if TYPE_CHECKING:
    from terminux_engine.store.event_store import EventStore

logger = logging.getLogger(__name__)


class ECCStore:
    """ECC Object 的 JSONL 持久化存儲。"""

    def __init__(self, store_path: str) -> None:
        """
        Parameters:
            store_path: JSONL 檔案路徑。若檔案或目錄不存在，save 時自動建立。
        """
        self._path = Path(store_path)

    def save(self, ecc: ECCObject) -> None:
        """將 ECCObject 序列化為一行 JSON，append 到 JSONL 檔案。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(ecc.model_dump_json() + "\n")

    def load_session(self, session_id: str) -> ECCObject | None:
        """載入指定 session_id 的最新 snapshot（從尾端往前搜尋）。"""
        if not self._path.exists():
            return None
        # 從尾端往前搜尋
        lines = self._path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("session_id") == session_id:
                return ECCObject.model_validate(data)
        return None

    def load_recent_sessions(
        self,
        n: int = 3,
        exclude_session_id: str | None = None,
    ) -> list[SessionSummary]:
        """載入最近 n 個不同 session 的精簡摘要。

        從 JSONL 尾端往前掃描，每個 session_id 只取最新一筆 snapshot，
        轉換為 SessionSummary。排除 exclude_session_id（通常是當前 session）。
        回傳依 created_at 降序排列（最新在前）。
        """
        if not self._path.exists():
            return []

        lines = self._path.read_text(encoding="utf-8").splitlines()
        seen: set[str] = set()
        results: list[SessionSummary] = []

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = data.get("session_id")
            if not sid or sid in seen:
                continue
            if sid == exclude_session_id:
                seen.add(sid)
                continue

            seen.add(sid)
            try:
                ecc = ECCObject.model_validate(data)
                results.append(SessionSummary.from_ecc(ecc))
            except Exception as exc:
                logger.warning("Failed to parse session %s: %s", sid, exc)
                continue

            if len(results) >= n:
                break

        return results

    def load_continuity(
        self,
        exclude_session_id: str | None = None,
    ) -> SessionContinuity | None:
        """載入最近一個 session 的 SessionContinuity（排除 exclude_session_id）。

        從 JSONL 尾端往前掃描，找到第一個不是 exclude_session_id 的 session，
        取其最新 snapshot 轉為 SessionContinuity。找不到回傳 None。
        """
        if not self._path.exists():
            return None

        lines = self._path.read_text(encoding="utf-8").splitlines()
        seen: set[str] = set()

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = data.get("session_id")
            if not sid or sid in seen:
                continue
            if sid == exclude_session_id:
                seen.add(sid)
                continue

            try:
                ecc = ECCObject.model_validate(data)
                return SessionContinuity.from_ecc(ecc)
            except Exception as exc:
                logger.warning("Failed to parse session %s for continuity: %s", sid, exc)
                return None

        return None

    def load_latest(self) -> ECCObject | None:
        """載入最後一行的 ECCObject。若 JSONL 為空或不存在，回傳 None。"""
        if not self._path.exists():
            return None
        lines = self._path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                return ECCObject.model_validate_json(line)
            except Exception:
                continue
        return None


# ─── EventStore 聚合函數──────────────────────────────────────

def load_recent_sessions_from_events(
    event_store: "EventStore",
    user_id: str,
    n: int = 3,
    exclude_session_id: str | None = None,
) -> list[SessionSummary]:
    """從 EventStore 聚合最近 n 個不同 session 的精簡摘要。

    取代舊的 ECCStore.load_recent_sessions()（JSONL 掃描）。
    回傳依最後活動時間降序排列（最新在前）。
    """
    # 多取一個以防需要排除當前 session
    session_ids = event_store.get_session_ids(user_id=user_id, n=n + 1)

    if exclude_session_id:
        session_ids = [sid for sid in session_ids if sid != exclude_session_id]

    session_ids = session_ids[:n]

    results: list[SessionSummary] = []
    for sid in session_ids:
        try:
            events = event_store.query(session_id=sid)
            if not events:
                continue

            created_at = min(e.timestamp for e in events)

            # last_stage_id：最新 stage_change event 的 to_stage
            stage_changes = [e for e in events if e.event_type == "stage_change"]
            last_stage_id = None
            if stage_changes:
                latest_stage = max(stage_changes, key=lambda e: e.timestamp)
                last_stage_id = latest_stage.payload.get("to_stage")

            # command count
            command_count = sum(1 for e in events if e.event_type == "command")

            # gate results
            gate_events = [e for e in events if e.event_type == "gate_eval"]
            gate_pass_count = sum(1 for g in gate_events if g.payload.get("passed") is True)
            gate_fail_count = sum(1 for g in gate_events if g.payload.get("passed") is False)

            # corner coverage：從 corner_update events 聚合（以最新狀態為準）
            corner_events = sorted(
                [e for e in events if e.event_type == "corner_update"],
                key=lambda e: e.timestamp,
            )
            corner_status: dict[str, str] = {}
            for ce in corner_events:
                corner = ce.payload.get("corner")
                status = ce.payload.get("status")
                if corner and status:
                    corner_status[corner] = status

            corners_passed = sum(1 for s in corner_status.values() if s == "pass")
            corners_failed = sum(1 for s in corner_status.values() if s == "fail")
            corners_total = 0
            if corner_events:
                latest_corner = max(corner_events, key=lambda e: e.timestamp)
                corners_total = latest_corner.payload.get("total", len(corner_status))

            # final_wns / final_tns：最新的 metric events
            metric_events = sorted(
                [e for e in events if e.event_type == "metric"],
                key=lambda e: e.timestamp,
                reverse=True,
            )
            final_wns: float | None = None
            final_tns: float | None = None
            for me in metric_events:
                mn = me.payload.get("metric_name")
                if final_wns is None and mn == "wns_ns":
                    final_wns = me.payload.get("value")
                if final_tns is None and mn == "tns_ns":
                    final_tns = me.payload.get("value")
                if final_wns is not None and final_tns is not None:
                    break

            results.append(SessionSummary(
                session_id=sid,
                created_at=created_at,
                last_stage_id=last_stage_id,
                corners_passed=corners_passed,
                corners_failed=corners_failed,
                corners_total=corners_total,
                gate_pass_count=gate_pass_count,
                gate_fail_count=gate_fail_count,
                command_count=command_count,
                final_wns=final_wns,
                final_tns=final_tns,
            ))
        except Exception as exc:
            logger.warning("Failed to aggregate session %s: %s", sid, exc)
            continue

    return results


def load_continuity_from_events(
    event_store: "EventStore",
    user_id: str,
    exclude_session_id: str | None = None,
) -> SessionContinuity | None:
    """從 EventStore 聚合最近一個 session 的 SessionContinuity。

    取代舊的 ECCStore.load_continuity()（JSONL 掃描）。
    corners_pending 需要 playbook 的 required_corners，EventStore 無此資訊，
    故回傳空 list。
    """
    # 多取一個以防需要排除當前 session
    session_ids = event_store.get_session_ids(user_id=user_id, n=2)

    if exclude_session_id:
        session_ids = [sid for sid in session_ids if sid != exclude_session_id]

    if not session_ids:
        return None

    sid = session_ids[0]
    try:
        events = event_store.query(session_id=sid)
    except Exception as exc:
        logger.warning("Failed to query events for session %s: %s", sid, exc)
        return None

    if not events:
        return None

    created_at = min(e.timestamp for e in events)

    # last_stage_id
    stage_changes = [e for e in events if e.event_type == "stage_change"]
    last_stage_id = None
    if stage_changes:
        latest_stage = max(stage_changes, key=lambda e: e.timestamp)
        last_stage_id = latest_stage.payload.get("to_stage")

    # corner coverage：從 corner_update events 聚合
    corner_events = sorted(
        [e for e in events if e.event_type == "corner_update"],
        key=lambda e: e.timestamp,
    )
    corner_status: dict[str, str] = {}
    for ce in corner_events:
        corner = ce.payload.get("corner")
        status = ce.payload.get("status")
        if corner and status:
            corner_status[corner] = status

    corners_passed = [c for c, s in corner_status.items() if s == "pass"]
    corners_failed = [c for c, s in corner_status.items() if s == "fail"]
    # corners_pending：需要 playbook 的 required_corners，EventStore 無此資訊
    corners_pending: list[str] = []

    # metric_trends：按 metric_name 分組，取最近 3 筆（最新在前）
    metric_events = [e for e in events if e.event_type == "metric"]
    metric_groups: dict[str, list[tuple[float, float]]] = {}
    for me in metric_events:
        mn = me.payload.get("metric_name")
        val = me.payload.get("value")
        if mn and val is not None:
            metric_groups.setdefault(mn, []).append((me.timestamp, val))

    metric_trends: dict[str, list[float]] = {}
    for name, entries in metric_groups.items():
        entries.sort(key=lambda x: x[0], reverse=True)
        metric_trends[name] = [v for _, v in entries[:3]]

    # recent_commands：最後 5 筆 command events
    command_events = sorted(
        [e for e in events if e.event_type == "command"],
        key=lambda e: e.timestamp,
    )
    recent_commands = [
        e.payload.get("command", "") for e in command_events[-5:]
    ]

    # failed_gates：unique gate_id from gate_eval 且 passed=False
    gate_events = [e for e in events if e.event_type == "gate_eval"]
    failed_gates = list({
        e.payload["gate_id"]
        for e in gate_events
        if e.payload.get("passed") is False and "gate_id" in e.payload
    })

    # last_ai_suggestion：最新 ai_response event 的 response_text
    ai_events = [e for e in events if e.event_type == "ai_response"]
    last_ai_suggestion: str | None = None
    if ai_events:
        latest_ai = max(ai_events, key=lambda e: e.timestamp)
        last_ai_suggestion = latest_ai.payload.get("response_text")

    return SessionContinuity(
        session_id=sid,
        created_at=created_at,
        last_stage_id=last_stage_id,
        corners_passed=corners_passed,
        corners_failed=corners_failed,
        corners_pending=corners_pending,
        metric_trends=metric_trends,
        recent_commands=recent_commands,
        failed_gates=failed_gates,
        last_ai_suggestion=last_ai_suggestion,
    )
