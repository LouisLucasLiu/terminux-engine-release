# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""TraceChain 與環境變數 dict 之間的互轉 helpers。不做 I/O。"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from terminux_engine.execution_trace.trace_id import (
    ENV_TRACE_JOB,
    ENV_TRACE_PARENT,
    ENV_TRACE_QUEUE,
    ENV_TRACE_ROOT,
    ENV_TRACE_SELF,
    TraceChain,
    parse_trace_job,
    parse_trace_queue,
    parse_trace_root,
    parse_trace_self,
)

_logger = logging.getLogger(__name__)


def read_trace_chain_from_env(
    environ: Mapping[str, str] | None = None,
) -> TraceChain | None:
    """從環境變數讀取 trace chain。

    environ 預設為 os.environ；測試時可注入 dict。

    回傳 TraceChain（若 ROOT 與 SELF 都存在且全部欄位格式合法），否則 None。
    格式錯誤時 log WARNING；缺必要欄位時靜默（process 不在 Terminux 屬正常）。
    不 raise exception。
    """
    if environ is None:
        environ = os.environ

    trace_root = environ.get(ENV_TRACE_ROOT)
    trace_self = environ.get(ENV_TRACE_SELF)
    if not trace_root or not trace_self:
        return None

    trace_queue = environ.get(ENV_TRACE_QUEUE, "")
    trace_job = environ.get(ENV_TRACE_JOB, "")
    trace_parent = environ.get(ENV_TRACE_PARENT, "")

    try:
        parse_trace_root(trace_root)
        parse_trace_self(trace_self)
        parse_trace_queue(trace_queue)
        parse_trace_job(trace_job)
    except ValueError as exc:
        _logger.warning("invalid TERMINUX_TRACE_* in env: %s", exc)
        return None

    return TraceChain(
        trace_root=trace_root,
        trace_queue=trace_queue,
        trace_job=trace_job,
        trace_parent=trace_parent,
        trace_self=trace_self,
    )


def trace_chain_to_env_dict(chain: TraceChain) -> dict[str, str]:
    """將 TraceChain 轉成五個 TERMINUX_TRACE_* 環境變數的 dict。空欄位顯式為 ""。"""
    return {
        ENV_TRACE_ROOT: chain.trace_root,
        ENV_TRACE_QUEUE: chain.trace_queue,
        ENV_TRACE_JOB: chain.trace_job,
        ENV_TRACE_PARENT: chain.trace_parent,
        ENV_TRACE_SELF: chain.trace_self,
    }
