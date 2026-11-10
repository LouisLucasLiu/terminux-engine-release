# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""沿 /proc/<pid>/status PPid 往上爬找有 TERMINUX_TRACE_* env 的 ancestor，
作為 Z6-2 JSONL 反查 miss 時的 fallback。Linux-only，所有錯誤回 None 不 raise。
"""

from __future__ import annotations

import logging
from pathlib import Path

from terminux_engine.execution_trace.proc_reader import read_trace_chain_from_proc
from terminux_engine.execution_trace.trace_id import TraceChain

_logger = logging.getLogger(__name__)

_DEFAULT_MAX_DEPTH = 10


def _proc_status_path(pid: int) -> Path:
    """產生 /proc/<pid>/status 的 Path。獨立函式方便測試重定向。"""
    return Path(f"/proc/{pid}/status")


def _get_ppid(pid: int) -> int | None:
    """從 /proc/<pid>/status 讀 PPid。任何失敗回 None，不 raise。"""
    try:
        text = _proc_status_path(pid).read_text()
    except FileNotFoundError:
        _logger.debug("PID %d /proc/status not found", pid)
        return None
    except PermissionError:
        _logger.warning("permission denied reading /proc/%d/status", pid)
        return None
    except OSError as exc:
        _logger.warning("OS error reading /proc/%d/status: %s", pid, exc)
        return None

    for line in text.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                _logger.debug("PPid parse failed for pid %d", pid)
                return None
    return None


def walk_ancestors_for_trace(
    pid: int,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> TraceChain | None:
    """從 pid 自己開始試（含），失敗才爬 PPid。爬到 PID<=1 / 用盡 max_depth / 讀
    取錯誤皆回 None，不 raise。PID reuse 風險（毫秒級時間窗）不做 starttime 防護。
    """
    current_pid = pid
    for depth in range(max_depth):
        chain = read_trace_chain_from_proc(current_pid)
        if chain is not None:
            if depth > 0:
                _logger.debug(
                    "trace chain found at ancestor depth=%d (pid=%d)",
                    depth, current_pid,
                )
            return chain

        ppid = _get_ppid(current_pid)
        if ppid is None or ppid <= 1:
            _logger.debug(
                "ancestor walk stopped at pid=%d (ppid=%s)", current_pid, ppid,
            )
            return None
        current_pid = ppid

    _logger.debug("ancestor walk exhausted max_depth=%d from pid=%d", max_depth, pid)
    return None
