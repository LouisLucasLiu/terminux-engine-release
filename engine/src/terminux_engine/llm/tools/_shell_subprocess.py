# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`run_command`／`run_tool_with_dispatch` 共用的 shell subprocess helper。

Process-tree timeout：`shell=True` 啟動的是
一個 shell process，該 shell 可能再衍生出實際執行指令的子孫 process
（descendant）；單純對 `subprocess.run()`/`Popen` 回傳的 shell process 呼叫
`kill()` 只會終止 shell 本身，不保證其 descendant 一併終止。本模組改以
`start_new_session=True` 讓整個指令樹成為獨立的 process group（group id
等於 shell process 自身的 pid），逾時時對整個 process group 送
`SIGKILL`，確保 descendant 一併終止，而非只終止最外層 shell。
"""

from __future__ import annotations

import os
import signal
import subprocess


def run_shell_command(
    command: str,
    *,
    cwd: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """以獨立 process group 執行一條 shell 指令。

    成功時回傳與既有 `subprocess.run(...)` 相容的
    `subprocess.CompletedProcess`（`returncode`／`stdout`／`stderr`）。

    逾時時：對整個 process group 送 `SIGKILL`（`ProcessLookupError`
    視為 process group 已消失，非錯誤）；再呼叫 `communicate()` 完成
    直接 shell process 的 reap 與 pipe 回收；cleanup 完成後才
    `raise`（保留原始 `subprocess.TimeoutExpired`），交回 caller 既有的
    timeout 分支處理。cleanup 過程中若發生非預期例外（非
    `ProcessLookupError`），原樣往上拋，不吞掉、不誤報成功 cleanup。
    """
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group_and_reap(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _kill_process_group_and_reap(process: subprocess.Popen[str]) -> None:
    """對整個 process group 送 SIGKILL，再 reap 直接 shell process。

    `start_new_session=True` 使 shell process 成為新 session 的
    session leader，其 process group id 精確等於自身 pid，故
    `os.killpg(process.pid, ...)` 即可精確定位整棵指令樹的 process
    group，不需另外查詢 pgid。
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # 完成直接 shell process 的 reap 與 pipe 回收；process 已被 SIGKILL，
    # 此處呼叫不會再因指令本身執行時間而阻塞。任何非預期例外原樣往上拋。
    process.communicate()
