# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Tool 啟動前的獨立 launcher：在全新單執行緒行程內設定父行程死亡防護後 exec 工具。

為什麼需要這一層：daemon 停止時 `shutdown_registered_children()` 是對 child
runner 的 process group 送 SIGKILL，runner 沒有機會執行任何 handler；而 tool 以
`start_new_session=True` 自成 process group，不在那個 group 內。唯一能保證 tool
不變成孤兒的手段是 Linux 的 `PR_SET_PDEATHSIG`。

為什麼不用 `preexec_fn`：daemon child runner 是多執行緒行程（heartbeat writer）。
`preexec_fn` 在 fork 之後、exec 之前於 child 內執行 Python callback，而 fork 只複製
呼叫執行緒——其他執行緒在 fork 當下持有的 loader／malloc lock 永遠不會被釋放，任何
dlopen 或記憶體配置都可能死鎖。本模組改由 `Popen` 正常啟動一個**全新的**
Python 行程：它一開始就是單執行緒、沒有繼承任何鎖，設定防護後再 `execvp` 成工具本身。

exec 之後 pid 不變，因此 process group ownership、cancel（`killpg`）、timeout 與
退出碼語意都與直接啟動工具時相同。

已知界限：`PR_SET_PDEATHSIG` 以「fork 出本行程的那條**執行緒**」結束為觸發條件，
而非整個父行程；且在 exec setuid／setgid 執行檔時會被核心清除。
"""

from __future__ import annotations

import os
import signal
import sys

#: launcher 自身失敗時的退出碼。刻意避開常見的工具退出碼與 shell 慣用的 126／127。
EXIT_LAUNCH_FAILED = 121
#: 工具無法 exec（例如 exec 當下才發現不可執行）時的退出碼，沿用 shell 慣例。
EXIT_TOOL_NOT_EXECUTABLE = 126
#: stderr 標記；executor 與人工排查都以它辨識「工具根本沒被啟動」。
FAILURE_MARKER = "terminux-tool-launcher: "

_PR_SET_PDEATHSIG = 1


def _fail(message: str, code: int) -> int:
    """把失敗寫到 stderr 並回傳退出碼；絕不在防護未成立時繼續啟動工具。"""
    sys.stderr.write(FAILURE_MARKER + message + "\n")
    sys.stderr.flush()
    return code


def _arm_parent_death_signal() -> str | None:
    """設定 `PR_SET_PDEATHSIG`；成功回 None，失敗回可讀原因。

    本行程是剛被 fork／exec 出來的全新單執行緒行程，此時做 `CDLL` 是安全的
    ——多執行緒 fork 的 lock 繼承問題只存在於 `preexec_fn` 那條路徑。
    """
    if not sys.platform.startswith("linux"):
        return "PR_SET_PDEATHSIG is unavailable on this platform"
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.restype = ctypes.c_int
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        ctypes.set_errno(0)
        if prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            return f"prctl(PR_SET_PDEATHSIG) failed: errno={ctypes.get_errno()}"
    except Exception as exc:  # noqa: BLE001 - 取不到 prctl 等同防護不成立
        return f"prctl(PR_SET_PDEATHSIG) unavailable: {type(exc).__name__}: {exc}"
    return None


def main(argv: list[str] | None = None) -> int:
    """`<expected_parent_pid> -- <tool argv...>`：設定防護後 exec 工具。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        return _fail("usage: tool_launcher.py <expected_parent_pid> -- <argv...>",
                     EXIT_LAUNCH_FAILED)
    try:
        expected_parent_pid = int(args[0])
    except ValueError:
        return _fail(f"invalid expected parent pid: {args[0]!r}", EXIT_LAUNCH_FAILED)
    tool_argv = args[2:]

    reason = _arm_parent_death_signal()
    if reason is not None:
        return _fail(reason, EXIT_LAUNCH_FAILED)

    # 防護只對「設定之後」才發生的父行程結束有效。若父行程在設定前就已結束，
    # 訊號永遠不會送達，工具會變成孤兒——此時必須放棄啟動，而不是照常 exec。
    actual_parent_pid = os.getppid()
    if actual_parent_pid != expected_parent_pid:
        return _fail(
            "parent exited before the death signal was armed: "
            f"expected ppid={expected_parent_pid}, actual ppid={actual_parent_pid}",
            EXIT_LAUNCH_FAILED,
        )

    try:
        os.execvp(tool_argv[0], tool_argv)
    except OSError as exc:
        return _fail(f"exec failed: {tool_argv[0]!r}: {exc}", EXIT_TOOL_NOT_EXECUTABLE)
    return _fail("exec returned unexpectedly", EXIT_LAUNCH_FAILED)  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
