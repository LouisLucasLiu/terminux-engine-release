# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent tool-use 的 bounded structured audit log。

此模組只寫既有檔案式 observability 的 JSONL，不建立 database 或儲存 raw
prompt、evidence、tool arguments、credentials。每筆 event 都可由 turn/session/
tool-call correlation 串起來；寫入失敗不可影響 Agent 或 runtime gate。
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from terminux_engine.ipc.request_context import try_get_current_request_context

_MAX_TEXT = 160
_MAX_TOOL_NAMES = 64
_write_lock = threading.Lock()
_current_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "terminux_agent_audit_turn_id", default=None
)


def _bounded_text(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)[:_MAX_TEXT]


def value_digest(value: object) -> str:
    """回傳資料 digest；絕不把資料本體放入 audit event。"""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def value_size(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(repr(value).encode("utf-8", errors="replace"))


def get_agent_audit_log_path() -> Path:
    """取得 bounded Agent audit JSONL path；僅供 host-side deployment 設定。"""
    configured = os.environ.get("TERMINUX_AGENT_AUDIT_LOG_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".terminux" / "agent_tool_audit.jsonl"


@contextlib.contextmanager
def agent_turn() -> Iterator[str]:
    """為一次 Agent run 設定唯一、request-scoped turn correlation。"""
    turn_id = uuid.uuid4().hex
    token = _current_turn_id.set(turn_id)
    try:
        yield turn_id
    finally:
        _current_turn_id.reset(token)


def current_turn_id() -> str | None:
    """目前 Agent turn 的 correlation id；不在 turn 內回 None。"""
    return _current_turn_id.get()


def emit(
    event: str,
    *,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    classification: str | None = None,
    policy_source: str | None = None,
    decision: str | None = None,
    denial_code: str | None = None,
    executor_entered: bool | None = None,
    result_status: str | None = None,
    result_value: object | None = None,
    duration_ms: float | None = None,
    argument_keys: list[str] | None = None,
    argument_value: object | None = None,
    run_id: str | None = None,
    advertised_tools: list[str] | None = None,
) -> None:
    """寫一行 stable JSON；所有 payload 均已 bounded 或 digest-only。"""
    try:
        # Registry 也被 direct API/測試使用；只有真正 AgentRunner turn 才是本
        # audit contract 的範圍，避免非 Agent 路徑意外寫入 production log。
        turn_id = _current_turn_id.get()
        if turn_id is None:
            return
        request_context = try_get_current_request_context()
        record: dict[str, Any] = {
            "v": 1,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "turn_id": turn_id,
            "session_id": request_context.session_id if request_context is not None else None,
            "tool_call_id": _bounded_text(tool_call_id),
            "tool_name": _bounded_text(tool_name),
            "classification": _bounded_text(classification),
            "policy_source": _bounded_text(policy_source),
            "decision": _bounded_text(decision),
            "denial_code": _bounded_text(denial_code),
            "executor_entered": executor_entered,
            "result_status": _bounded_text(result_status),
            "result_size": value_size(result_value) if result_value is not None else None,
            "result_digest": value_digest(result_value) if result_value is not None else None,
            "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
            "argument_keys": sorted(str(key)[:_MAX_TEXT] for key in (argument_keys or []))[:_MAX_TOOL_NAMES],
            "argument_digest": value_digest(argument_value) if argument_value is not None else None,
            "run_id": _bounded_text(run_id),
            "advertised_tools": sorted(str(name)[:_MAX_TEXT] for name in (advertised_tools or []))[:_MAX_TOOL_NAMES],
        }
        target = get_agent_audit_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock, target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Audit 不可改變 tool authorization 或 Agent 回答的行為。
        return


def monotonic_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0
