# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Z6-2：從 ~/.terminux/lineage.jsonl 尾部反查 wrapper trace chain。已知限制：並行同 tool 同 cwd 可能 cross-match，MVP 不解。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from terminux_engine.execution_trace.ancestor_lookup import walk_ancestors_for_trace
from terminux_engine.execution_trace.trace_id import TraceChain

logger = logging.getLogger(__name__)


def lookup_trace_for_post_exec(
    tool_name: str,
    command: str,
    cwd: str,
    post_exec_ts: float,
    jsonl_path: Path | str,
    tail_bytes: int = 1_000_000,
    fallback_pid: int | None = None,
) -> TraceChain | None:
    """JSONL 反查；miss 時若給 fallback_pid 走 ancestor walk。皆無回 None。"""
    chain = _lookup_in_jsonl(tool_name, command, cwd, post_exec_ts, jsonl_path, tail_bytes)
    if chain is not None:
        return chain
    if fallback_pid is not None:
        ancestor = walk_ancestors_for_trace(fallback_pid)
        if ancestor is not None:
            logger.info("trace chain found via ancestor walk (pid=%d)", fallback_pid)
            return ancestor
    return None


def _lookup_in_jsonl(
    tool_name: str,
    command: str,
    cwd: str,
    post_exec_ts: float,
    jsonl_path: Path | str,
    tail_bytes: int,
) -> TraceChain | None:
    """既有 JSONL 反查邏輯。比對：tool+cwd+ts 必要；argv join==command 強匹配；
    皆無弱匹配取最新。所有錯誤回 None，不 raise。"""
    jsonl_path = Path(jsonl_path)
    events = _read_jsonl_tail(jsonl_path, tail_bytes)
    if not events:
        return None

    candidates = [
        e for e in events
        if e.get("tool") == tool_name
        and e.get("cwd") == cwd
        and isinstance(e.get("ts"), (int, float))
        and e["ts"] <= post_exec_ts
    ]
    if not candidates:
        logger.debug(
            "no trace candidate for tool=%s cwd=%s ts<=%.3f",
            tool_name, cwd, post_exec_ts,
        )
        return None

    # 強匹配：argv join 後 == command
    exact = [
        e for e in candidates
        if isinstance(e.get("argv"), list)
        and " ".join(str(x) for x in e["argv"]) == command
    ]
    if exact:
        return _build_trace_chain(exact[-1])

    # 弱匹配
    return _build_trace_chain(candidates[-1])


def _read_jsonl_tail(path: Path, window_bytes: int) -> list[dict]:
    """讀 JSONL 尾部 → list[dict]。防禦：FileNotFoundError DEBUG / OSError WARNING / partial last line 丟 / seek>0 丟首行 / JSONDecodeError skip+WARNING。"""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        logger.debug("lineage.jsonl not found: %s", path)
        return []
    except OSError as exc:
        logger.warning("stat lineage.jsonl failed: %s", exc)
        return []

    offset = max(0, size - window_bytes)
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError as exc:
        logger.warning("read lineage.jsonl failed: %s", exc)
        return []

    if not data:
        return []

    # 顯式丟 partial last line（writer mid-write 或檔案 split 末尾的空 token）
    lines = data.split(b"\n")
    lines = lines[:-1]  # 兩種情況都丟最後一個
    if offset > 0 and lines:
        lines = lines[1:]

    out: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            out.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            logger.warning("skipping malformed JSONL line: %s", exc)
    return out


def _build_trace_chain(event: dict) -> TraceChain | None:
    """從 JSONL event 構造 TraceChain；ValidationError → None + WARNING。"""
    try:
        return TraceChain(
            trace_root=event.get("trace_root", ""),
            trace_queue=event.get("trace_queue", ""),
            trace_job=event.get("trace_job", ""),
            trace_parent=event.get("trace_parent", ""),
            trace_self=event.get("trace_self", ""),
        )
    except ValidationError as exc:
        logger.warning("failed to build TraceChain from JSONL event: %s", exc)
        return None
