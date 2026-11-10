# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""從 /proc/<pid>/environ 讀取 trace chain。

Linux-only。Linux kernel 把 process environment 以 NUL-separated bytes 暴露在
/proc/<pid>/environ；本模組封裝讀取與解析，並把實際的格式驗證委託給 env_helper。
"""

from __future__ import annotations

import logging
from pathlib import Path

from terminux_engine.execution_trace.env_helper import read_trace_chain_from_env
from terminux_engine.execution_trace.trace_id import (
    ENV_TRACE_ROOT,
    ENV_TRACE_SELF,
    TraceChain,
)

_logger = logging.getLogger(__name__)


def _proc_environ_path(pid: int) -> Path:
    """產生 /proc/<pid>/environ 的 Path。獨立函式方便測試重定向。"""
    return Path(f"/proc/{pid}/environ")


def read_trace_chain_from_proc(pid: int) -> TraceChain | None:
    """從 /proc/<pid>/environ 讀取 trace chain。

    Args:
        pid: 目標 process ID。

    Returns:
        TraceChain（成功且格式合法）；None（任何失敗情境）。

    不 raise exception。失敗類型分級 log：
    - PID 已消失 / 沒有 TERMINUX_TRACE_*：DEBUG（正常情境）
    - 權限不足 / 其他 OSError / 格式錯誤：WARNING（異常）
    """
    path = _proc_environ_path(pid)

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        _logger.debug("PID %d /proc entry not found (already exited)", pid)
        return None
    except PermissionError:
        _logger.warning("permission denied reading /proc/%d/environ", pid)
        return None
    except OSError as exc:
        _logger.warning("OS error reading /proc/%d/environ: %s", pid, exc)
        return None

    if not raw:
        _logger.debug("PID %d environ empty", pid)
        return None

    # /proc/<pid>/environ 是 NUL-separated bytes，不是 newline-separated text
    text = raw.decode("utf-8", errors="replace")
    environ: dict[str, str] = {}
    for entry in text.split("\x00"):
        if not entry or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        environ[key] = value

    # 沒有 TRACE_* 是正常情境（unparented child）；提早 short-circuit
    # 避免 env_helper 把缺漏當成靜默 None，呼叫端無法區分
    if ENV_TRACE_ROOT not in environ or ENV_TRACE_SELF not in environ:
        _logger.debug("PID %d has no TERMINUX_TRACE_* in environ", pid)
        return None

    return read_trace_chain_from_env(environ)
