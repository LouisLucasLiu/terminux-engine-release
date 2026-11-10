# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Orchestration child process-group 的 fail-closed signal helper。"""

from __future__ import annotations

import os
import signal
from enum import Enum


class ProcessGroupSignalResult(Enum):
    """process-group signal 的可判讀結果。"""

    SENT = "sent"
    MISSING = "missing"
    NOT_OWNER = "not_owner"


def signal_owned_process_group(
    child_pid: int,
    *,
    sig: int = signal.SIGKILL,
) -> ProcessGroupSignalResult:
    """僅在 child 自己是 process-group leader 時送出 group signal。"""

    if child_pid <= 0:
        raise ValueError("child_pid 必須為正整數")
    try:
        process_group_id = os.getpgid(child_pid)
    except ProcessLookupError:
        return ProcessGroupSignalResult.MISSING
    if process_group_id != child_pid:
        return ProcessGroupSignalResult.NOT_OWNER
    try:
        os.killpg(child_pid, sig)
    except ProcessLookupError:
        return ProcessGroupSignalResult.MISSING
    return ProcessGroupSignalResult.SENT
