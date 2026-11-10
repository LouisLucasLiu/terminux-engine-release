# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Orchestration kernel 使用的同步 subprocess 執行器。"""

from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SubprocessResult:
    """一次 subprocess 執行的原始結果。"""

    exit_code: int
    stdout_tail: list[str]
    stderr_tail: list[str]
    started_at: float
    finished_at: float
    timed_out: bool
    # 以下是前景 CLI 的中斷欄位，只有 `InterruptibleSubprocessExecutor` 會設定；
    # 預設值讓既有的建構方式與 daemon 路徑完全不變。
    interrupted: bool = False
    interrupt_signal: int | None = None
    #: False 代表收到 signal 時工具尚未啟動，`exit_code` 沒有意義。
    process_started: bool = True
    #: 中斷後是否已確認整個 process group 不存在；沒有中斷時為 None。
    process_group_confirmed_gone: bool | None = None


_LAUNCHER_PATH = str(Path(__file__).resolve().with_name("tool_launcher.py"))


def _resolve_tool_path(argv: list[str], cwd: Path, child_env: dict[str, str]) -> str:
    """在父行程先解析工具路徑，保留「找不到工具就拋 FileNotFoundError」的既有語意。

    改走 launcher 之後，exec 失敗發生在 launcher 內（只剩退出碼可回報），因此這裡
    先做一次與 `Popen` 等價的解析，讓呼叫端看到的行為不變。

    解析基準必須與原本一致：`Popen` 是在 child 完成 `chdir(cwd)` 之後才 execvp，
    所以帶路徑分隔字元的 argv[0]（例如 `./tool`）是相對於 `cwd`，不是相對於
    daemon 當下的工作目錄。
    """
    program = argv[0]
    if os.sep in program or (os.altsep and os.altsep in program):
        candidate = Path(program)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), program)
        return str(candidate)
    resolved = shutil.which(program, path=child_env.get("PATH"))
    if resolved is None:
        raise FileNotFoundError(
            errno.ENOENT, os.strerror(errno.ENOENT), program)
    return resolved


def _launch_argv(argv: list[str], cwd: Path, child_env: dict[str, str]) -> list[str]:
    """把工具 argv 包成 launcher argv；非 Linux 平台沒有 PDEATHSIG，直接啟動工具。

    非 Linux 平台不套 launcher 是刻意的：該平台本來就沒有這層防護，硬包一層只會
    多一個行程而得不到保證，而 launcher 在防護不成立時是拒絕啟動的。這個限制記在
    `tool_launcher` 模組 docstring 與 release matrix。
    """
    tool_argv = [_resolve_tool_path(argv, cwd, child_env), *argv[1:]]
    if not sys.platform.startswith("linux"):
        return tool_argv
    return [sys.executable, "-S", _LAUNCHER_PATH, str(os.getpid()), "--", *tool_argv]


class SubprocessExecutor:
    """由 kernel 自行管理的同步 subprocess 執行層。"""

    _TAIL_LINES = 40

    def execute(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float,
    ) -> SubprocessResult:
        """執行 command，逾時時終止子行程並保留 stdout/stderr 尾端。"""
        child_env = os.environ.copy()
        if env is not None:
            child_env.update(env)
        started_at = time.time()
        process = subprocess.Popen(
            _launch_argv(argv, cwd, child_env),
            cwd=str(cwd),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # 每次 tool 都有自己的 session/process group；timeout 時不能只殺掉
            # docker client 而把 container 或其子孫留在背景。launcher 於此
            # 成為 group leader，`execvp` 後 pid 不變，故 killpg 仍命中工具。
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # child 已在 timeout 與 kill 之間正常結束；以下 communicate
                # 仍會收齊可得的 stdout/stderr。
                pass
            stdout, stderr = process.communicate()
            exit_code = -1
        finished_at = time.time()
        return SubprocessResult(
            exit_code=exit_code if exit_code is not None else -1,
            stdout_tail=stdout.splitlines()[-self._TAIL_LINES :],
            stderr_tail=stderr.splitlines()[-self._TAIL_LINES :],
            started_at=started_at,
            finished_at=finished_at,
            timed_out=timed_out,
        )


class InterruptibleSubprocessExecutor(SubprocessExecutor):
    """前景 CLI 專用：收到中斷後終止整個 process group，並確認它已不存在。

    啟動方式與 `SubprocessExecutor` 相同（launcher、`start_new_session`、
    PDEATHSIG）。`controller` 提供 `interrupted`、`forced()`、`grace_deadline()`、
    `process_group_deadline()`、`signal_number`、`enter_phase()`、
    `note_process_exit()`（見 `cli_interruption.CliInterruptController`）。

    - 收到 signal 時工具尚未啟動：不啟動，回 `process_started=False`；
    - 執行中收到 signal：送 SIGTERM，graceful 期限到或 forced 時改送 SIGKILL，
      再以 `killpg(pgid, 0)` 確認 group 已不存在；
    - 逾時：與 `SubprocessExecutor` 相同（SIGKILL、`timed_out=True`、exit -1）。
    """

    _POLL_SECONDS = 0.05

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    def execute(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float,
    ) -> SubprocessResult:
        controller = self._controller
        if controller.interrupted:
            now = time.time()
            return SubprocessResult(
                exit_code=-1, stdout_tail=[], stderr_tail=[], started_at=now,
                finished_at=now, timed_out=False, interrupted=True,
                interrupt_signal=controller.signal_number, process_started=False,
                process_group_confirmed_gone=True)
        child_env = os.environ.copy()
        if env is not None:
            child_env.update(env)
        started_at = time.time()
        started_mono = time.monotonic()
        process = subprocess.Popen(
            _launch_argv(argv, cwd, child_env), cwd=str(cwd), env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def collect(wait: float) -> bool:
            try:
                out, err = process.communicate(timeout=wait)
            except subprocess.TimeoutExpired:
                return False
            stdout_parts.append(out or "")
            stderr_parts.append(err or "")
            return True

        def result(exit_code: int, **extra: Any) -> SubprocessResult:
            stdout = "".join(stdout_parts)
            stderr = "".join(stderr_parts)
            return SubprocessResult(
                exit_code=exit_code, stdout_tail=stdout.splitlines()[-self._TAIL_LINES:],
                stderr_tail=stderr.splitlines()[-self._TAIL_LINES:],
                started_at=started_at, finished_at=time.time(), **extra)

        while True:
            if collect(self._POLL_SECONDS):
                exit_code = process.returncode if process.returncode is not None else -1
                controller.note_process_exit(exit_code)
                return result(exit_code, timed_out=False)
            if controller.interrupted:
                return self._terminate(process, collect, result)
            if time.monotonic() - started_mono >= timeout:
                _killpg(process.pid, signal.SIGKILL)
                collect(None)  # type: ignore[arg-type]
                controller.note_process_exit(-1)
                return result(-1, timed_out=True)

    def _terminate(self, process: subprocess.Popen[str], collect: Any,
                   result: Any) -> SubprocessResult:
        from terminux_engine.orchestration.cli_interruption import PHASE_INTERRUPTING

        controller = self._controller
        controller.enter_phase(PHASE_INTERRUPTING)
        pgid = process.pid
        _killpg(pgid, signal.SIGKILL if controller.forced() else signal.SIGTERM)
        killed = controller.forced()
        exited = False
        while not exited:
            now = time.monotonic()
            grace_deadline = controller.grace_deadline()
            if not killed and (controller.forced() or grace_deadline is None
                               or now >= grace_deadline):
                _killpg(pgid, signal.SIGKILL)
                killed = True
            group_deadline = controller.process_group_deadline()
            if group_deadline is not None and now >= group_deadline:
                break
            exited = collect(self._POLL_SECONDS)
        confirmed = False
        while True:
            if not _group_exists(pgid):
                confirmed = True
                break
            group_deadline = controller.process_group_deadline()
            if group_deadline is not None and time.monotonic() >= group_deadline:
                break
            _killpg(pgid, signal.SIGKILL)
            time.sleep(self._POLL_SECONDS)
        exit_code = process.returncode if exited and process.returncode is not None else -1
        return result(exit_code, timed_out=False, interrupted=True,
                      interrupt_signal=controller.signal_number, process_started=True,
                      process_group_confirmed_gone=confirmed and exited)


def _killpg(pgid: int, signo: int) -> None:
    try:
        os.killpg(pgid, signo)
    except ProcessLookupError:
        pass


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
