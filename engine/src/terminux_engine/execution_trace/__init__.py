# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""execution_trace package：跨 process 執行追蹤鏈基礎設施。

本 package 第一輪（Z1-1）僅提供 Trace ID 資料結構與生成/解析函式，
不含任何 I/O、IPC、環境變數讀寫或 shell hook 邏輯。
"""

from terminux_engine.execution_trace.ancestor_lookup import (
    walk_ancestors_for_trace,
)
from terminux_engine.execution_trace.env_helper import (
    read_trace_chain_from_env,
    trace_chain_to_env_dict,
)
from terminux_engine.execution_trace.proc_reader import (
    read_trace_chain_from_proc,
)
from terminux_engine.execution_trace.trace_id import (
    ENV_TRACE_JOB,
    ENV_TRACE_PARENT,
    ENV_TRACE_QUEUE,
    ENV_TRACE_ROOT,
    ENV_TRACE_SELF,
    TraceChain,
    advance_trace_chain,
    generate_trace_root,
    generate_trace_self,
    parse_trace_job,
    parse_trace_queue,
    parse_trace_root,
    parse_trace_self,
)

__all__ = [
    "TraceChain",
    "generate_trace_root",
    "generate_trace_self",
    "advance_trace_chain",
    "parse_trace_root",
    "parse_trace_self",
    "parse_trace_queue",
    "parse_trace_job",
    "read_trace_chain_from_env",
    "trace_chain_to_env_dict",
    "read_trace_chain_from_proc",
    "walk_ancestors_for_trace",
    "ENV_TRACE_ROOT",
    "ENV_TRACE_QUEUE",
    "ENV_TRACE_JOB",
    "ENV_TRACE_PARENT",
    "ENV_TRACE_SELF",
]
