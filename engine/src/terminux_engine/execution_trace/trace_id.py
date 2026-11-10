# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Trace ID 五層階層式資料結構與生成函式。

提供 Terminux session → queue submit → slurm job → parent invocation → self
invocation 的五層追蹤鏈。child process 透過環境變數繼承 chain，每進入新一層時
parent 取代為上一層的 self、self 重新生成；root/queue/job 在整條鏈上不變。

本模組只做純資料結構與字串生成/解析，不涉及 I/O、IPC、環境變數讀寫或 shell hook。
這些責任屬於後續輪次（Z1-2 之後）。
"""

from __future__ import annotations

import os
import re
import time
import uuid

from pydantic import BaseModel, Field

# 環境變數名稱常數。本模組僅定義字串，不做任何 os.environ 讀寫。
ENV_TRACE_ROOT = "TERMINUX_TRACE_ROOT"
ENV_TRACE_QUEUE = "TERMINUX_TRACE_QUEUE"
ENV_TRACE_JOB = "TERMINUX_TRACE_JOB"
ENV_TRACE_PARENT = "TERMINUX_TRACE_PARENT"
ENV_TRACE_SELF = "TERMINUX_TRACE_SELF"

# 各層 ID 格式正規表達式
_TRACE_ROOT_RE = re.compile(r"^sess_[0-9a-f]{32}$")
_TRACE_SELF_RE = re.compile(r"^inv_(\d+)_(\d+)$")
_TRACE_QUEUE_RE = re.compile(r"^submit_[0-9a-f]+$")
_TRACE_JOB_RE = re.compile(r"^slurm_job_\d+$")


class TraceChain(BaseModel):
    """五層階層式 Trace ID。

    DAG 重建時用 trace_parent → trace_self 邊串接 invocation；
    trace_queue 與 trace_job 為 session/job 層級標記，進入 child process
    時原值不變往下帶。
    """

    trace_root: str = Field(..., min_length=1)
    """sess_{uuid_hex}。Terminux session 建立時產生，貫穿整個 session。不可為空。"""

    trace_queue: str = ""
    """submit_{random_hex}。Workstation-only 場景下為空字串。MVP 階段不填值。"""

    trace_job: str = ""
    """slurm_job_{job_id}。Workstation-only 場景下為空字串。MVP 階段不填值。"""

    trace_parent: str = ""
    """上一層的 trace_self。根層級時為空字串。"""

    trace_self: str = Field(..., min_length=1)
    """inv_{epoch_ns}_{pid}。每個新 process 啟動時產生。不可為空。"""


def generate_trace_root() -> str:
    """產生 session-level root ID。

    Returns:
        格式為 sess_{uuid4_hex} 的字串（後綴為 32 個 hex chars）。
    """
    return f"sess_{uuid.uuid4().hex}"


def generate_trace_self(
    pid: int | None = None,
    epoch_ns: int | None = None,
) -> str:
    """產生 invocation-level self ID。

    Args:
        pid: process ID。預設為 os.getpid()。
        epoch_ns: 納秒級 epoch 時間。預設為 time.time_ns()。

    Returns:
        格式為 inv_{epoch_ns}_{pid} 的字串。
    """
    if pid is None:
        pid = os.getpid()
    if epoch_ns is None:
        epoch_ns = time.time_ns()
    return f"inv_{epoch_ns}_{pid}"


def advance_trace_chain(
    current: TraceChain,
    pid: int | None = None,
    epoch_ns: int | None = None,
) -> TraceChain:
    """產生下一層的 TraceChain。

    語意：
    - trace_root 不變
    - trace_queue 不變（session/job 層級標記，不隨 child process 改變）
    - trace_job 不變（同上）
    - trace_parent = current.trace_self
    - trace_self = 新產生的 invocation ID

    Args:
        current: 當前層的 TraceChain。
        pid: 傳遞給 generate_trace_self 的 pid。
        epoch_ns: 傳遞給 generate_trace_self 的 epoch_ns。
    """
    return TraceChain(
        trace_root=current.trace_root,
        trace_queue=current.trace_queue,
        trace_job=current.trace_job,
        trace_parent=current.trace_self,
        trace_self=generate_trace_self(pid=pid, epoch_ns=epoch_ns),
    )


def parse_trace_root(value: str) -> str:
    """驗證 trace_root 格式（sess_{32 hex chars}），回傳原值。

    Raises:
        ValueError: 格式不符合。
    """
    if not _TRACE_ROOT_RE.match(value):
        raise ValueError(f"invalid trace_root format: {value!r}")
    return value


def parse_trace_self(value: str) -> tuple[int, int]:
    """解析 trace_self 格式（inv_{epoch_ns}_{pid}）。

    Returns:
        (epoch_ns, pid) tuple。

    Raises:
        ValueError: 格式不符合 inv_{digits}_{digits}。
    """
    m = _TRACE_SELF_RE.match(value)
    if not m:
        raise ValueError(f"invalid trace_self format: {value!r}")
    return int(m.group(1)), int(m.group(2))


def parse_trace_queue(value: str) -> str:
    """驗證 trace_queue 格式（submit_{hex}），回傳原值。

    空字串視為合法（表示非 queue 場景），直接回傳。

    Raises:
        ValueError: 非空但格式不符合 submit_{hex}。
    """
    if value == "":
        return value
    if not _TRACE_QUEUE_RE.match(value):
        raise ValueError(f"invalid trace_queue format: {value!r}")
    return value


def parse_trace_job(value: str) -> str:
    """驗證 trace_job 格式（slurm_job_{digits}），回傳原值。

    空字串視為合法（表示非 queue 場景），直接回傳。

    Raises:
        ValueError: 非空但格式不符合 slurm_job_{digits}。
    """
    if value == "":
        return value
    if not _TRACE_JOB_RE.match(value):
        raise ValueError(f"invalid trace_job format: {value!r}")
    return value
