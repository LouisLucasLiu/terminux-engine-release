# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""依共享 owner 決定啟動本機 daemon、relay 與 GUI。"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from terminux_engine import learner_strict
from terminux_engine._client_binary import _find_client_binary
from terminux_engine.ipc.agent_socket import (
    client_agent_socket, client_lineage_socket, host_runtime_directory, local_nodename,
)
from terminux_engine.ipc.daemon_owner import owner_lock_path, owner_present, read_owner
from terminux_engine.ipc.relay import pid_exists, running_relay
from terminux_engine.llm.task_scope import loaded_authority_identity, profile_containment_active
from terminux_engine.version_cli import check_version_arg


def _command(_name: str, module: str) -> list[str]:
    return [sys.executable, "-m", module]


def _start_detached(name: str, module: str, log_path: Path) -> subprocess.Popen:
    # 私有主機目錄內的 log 也不跟隨既有 symlink。
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as output:
        return subprocess.Popen(_command(name, module), stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


def _stop_local_relay(directory: Path) -> None:
    path = directory / "relay.pid"
    pid = running_relay(path)
    if pid is None:
        if path.exists():
            try:
                existing = int(path.read_text())
            except (OSError, ValueError) as error:
                raise RuntimeError("TERM_LAUNCH_RELAY_PID_UNCONFIRMED") from error
            if pid_exists(existing):
                raise RuntimeError("TERM_LAUNCH_RELAY_PID_NOT_RELAY")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 8.0
    while running_relay(path) == pid:
        if time.monotonic() >= deadline:
            raise RuntimeError("TERM_LAUNCH_RELAY_STOP_TIMEOUT")
        time.sleep(0.05)


def _wait_relay(process: subprocess.Popen, directory: Path) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (running_relay(directory / "relay.pid") is not None
                and client_agent_socket().path.is_socket()
                and client_lineage_socket().path.is_socket()):
            return
        if process.poll() is not None:
            raise RuntimeError("TERM_LAUNCH_RELAY_START_FAILED")
        time.sleep(0.05)
    raise RuntimeError("TERM_LAUNCH_RELAY_START_TIMEOUT")


def _ensure_relay(directory: Path) -> None:
    if running_relay(directory / "relay.pid") is not None:
        return
    process = _start_detached("terminux-relay", "terminux_engine.ipc.relay", directory / "relay.stdout.log")
    _wait_relay(process, directory)


def _unavailable(owner: dict[str, Any]) -> int:
    print(f"TERM_DAEMON_OWNER_UNAVAILABLE host={owner.get('host')} pid={owner.get('pid')}；"
          "請到 owner 主機執行 Terminux，或執行 terminux-launch --reset。", file=sys.stderr)
    return 1


def _uncontained(owner: dict[str, Any]) -> int:
    print(f"TERM_DAEMON_OWNER_UNCONTAINED host={owner.get('host')} pid={owner.get('pid')}；"
          "既有 daemon 未以 Profile 模式公告啟動，launcher 不沿用也不停止它；"
          "請先停止該 daemon，再於帶有 Profile 的環境重新啟動。", file=sys.stderr)
    return 1


def _authority_mismatch(owner: dict[str, Any]) -> list[str]:
    """與既有 daemon 公告的 Profile／policy 身分不符的項目；空清單代表可沿用。

    預期身分由本程序以 daemon 相同的 loader 計算（`loaded_authority_identity`）；任一方缺值都視為不符。
    """
    expected = loaded_authority_identity()
    mismatched = []
    for kind in ("profile", "policy"):
        keys = (f"{kind}_sha256", f"{kind}_source_sha256")
        if any(expected[key] is None or owner.get(key) != expected[key] for key in keys):
            mismatched.append(kind)
    return mismatched


def _identity_mismatch(owner: dict[str, Any], fields: list[str]) -> int:
    print(f"TERM_DAEMON_OWNER_IDENTITY_MISMATCH host={owner.get('host')} pid={owner.get('pid')} "
          f"fields={','.join(fields)}；既有 daemon 啟動時載入的 Profile／policy 與本次設定不同或無法驗證，"
          "launcher 不沿用也不停止它；請先停止該 daemon，再以本次的 Profile 與 policy 重新啟動。",
          file=sys.stderr)
    return 1


def _wait_daemon_ready() -> None:
    deadline = time.monotonic() + 30.0
    while True:
        try:
            if read_owner().get("state") == "ready":
                return
        except (OSError, ValueError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("TERM_LAUNCH_DAEMON_NOT_READY：等待 30 秒後 daemon 尚未就緒，"
                  "hook 可能還沒部署。", file=sys.stderr)
            return
        time.sleep(min(0.5, remaining))


def _reset_owner() -> int:
    lock = owner_lock_path()
    if not owner_present():
        print("owner.lock 不存在，無須重設。")
        return 0
    try:
        content = (lock / "owner.json").read_text()
    except OSError:
        content = "owner.json 不存在或無法讀取。"
    print(content)
    print("此操作只刪除 owner.lock，不會停止遠端 daemon。")
    if input("確認刪除？請輸入 yes：").strip() != "yes":
        print("已取消。")
        return 1
    metadata = lock.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise RuntimeError("TERM_LAUNCH_OWNER_DIRECTORY_UNSAFE")
    # 使用者明確確認後，只刪除此帳號的精確 owner.lock 目錄。
    shutil.rmtree(lock)
    return 0


def main(argv: list[str] | None = None) -> int:
    """處理參數後才操作 runtime；reset 不隱含停止或啟動任何程序。"""
    check_version_arg(argv)
    parser = argparse.ArgumentParser(prog="terminux-launch")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--no-gui", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.reset:
            return _reset_owner()
        deadline = time.monotonic() + 30.0
        owner: dict[str, Any] = {}
        while owner_present():
            try:
                owner = read_owner()
            except (OSError, ValueError):
                return _unavailable(owner)
            if owner.get("state") == "starting":
                if time.monotonic() >= deadline:
                    return _unavailable(owner)
                time.sleep(0.5)
                continue
            if owner.get("state") != "ready":
                return _unavailable(owner)
            if os.environ.get("TERMINUX_UNIX_ONLY_IPC") == "1" and not owner.get("unix_only_ipc"):
                return _unavailable(owner)
            # 以 Profile 模式啟動 launcher 時，只沿用自己公告為 Profile 模式的 daemon。
            if profile_containment_active() and owner.get("profile_containment") is not True:
                return _uncontained(owner)
            # Learner 發行版只沿用同樣公告為 Learner 發行版的 daemon。
            if learner_strict.LEARNER_STRICT and owner.get("learner_strict") is not True:
                return _uncontained(owner)
            # Learner 發行版只沿用啟動時載入同一份 Profile 與 policy 的 daemon。
            if learner_strict.LEARNER_STRICT:
                mismatched = _authority_mismatch(owner)
                if mismatched:
                    return _identity_mismatch(owner, mismatched)
            if owner.get("nodename") == local_nodename():
                if pid_exists(owner.get("pid")):
                    break
                # 不由 launcher 刪鎖；daemon 的本機殘留判斷負責回收。
                directory = host_runtime_directory(create_dir=True)
                _stop_local_relay(directory)
                _start_detached("terminux-daemon", "terminux_engine", directory / "daemon.stdout.log")
                break
            if owner.get("unix_only_ipc"):
                return _unavailable(owner)
            try:
                with socket.create_connection((owner["host"], owner["core_port"]), timeout=3.0):
                    pass
            except (OSError, ValueError, TypeError, KeyError):
                return _unavailable(owner)
            _ensure_relay(host_runtime_directory(create_dir=True))
            break
        else:
            directory = host_runtime_directory(create_dir=True)
            _stop_local_relay(directory)
            _start_detached("terminux-daemon", "terminux_engine", directory / "daemon.stdout.log")
            _wait_daemon_ready()
        try:
            setup_owner = read_owner()
        except (OSError, ValueError):
            setup_owner = {}
        if setup_owner.get("wrapper_setup") in {"failed", "disabled"}:
            log = owner_lock_path().parent / str(setup_owner.get("nodename", "")) / "daemon.log"
            print(f"TERM_LAUNCH_WRAPPER_SETUP {setup_owner['wrapper_setup']}："
                  f"hook 可能沒有部署；{setup_owner.get('wrapper_setup_error', '')}；"
                  f"請查看 {log}", file=sys.stderr)
        if not args.no_gui:
            executable = _find_client_binary("terminux", "TERMINUX_GUI_BIN",
                                            excluded=(Path(__file__), Path(sys.argv[0])))
            if not executable:
                raise RuntimeError("TERM_LAUNCH_GUI_NOT_FOUND")
            subprocess.Popen([executable])
        return 0
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        print(f"TERM_LAUNCH_FAILED {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
