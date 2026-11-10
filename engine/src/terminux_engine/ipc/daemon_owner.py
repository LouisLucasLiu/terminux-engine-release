# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""以帳號 home 下的目錄鎖管理 daemon 所有權。"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import socket
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from terminux_engine._version import __version__
from terminux_engine.ipc.agent_socket import home_for_uid, host_runtime_directory, local_nodename

_log = logging.getLogger(__name__)


def _runtime_log_handler(filename: str) -> RotatingFileHandler:
    """僅供已取得單一實例所有權的服務開啟本機 log。"""
    directory = host_runtime_directory(create_dir=True)
    handler = RotatingFileHandler(
        directory / filename, maxBytes=10 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    return handler


def owner_lock_path() -> Path:
    """共享帳號 home 中的 daemon 所有權路徑。"""
    return home_for_uid(os.geteuid()) / ".terminux" / "run" / "owner.lock"


def read_owner() -> dict[str, Any]:
    """讀取完整 owner metadata；讀取／解析失敗由呼叫端決定處置。"""
    value = json.loads((owner_lock_path() / "owner.json").read_text())
    if not isinstance(value, dict):
        raise ValueError("TERM_DAEMON_OWNER_INVALID")
    return value


def owner_present() -> bool:
    """鎖仍存在就不宣稱 daemon 已停止，包含尚在啟動或需人工處理的鎖。"""
    return os.path.lexists(owner_lock_path())


def check_local_database_access() -> int:
    """阻擋在非 owner 主機直接開啟共享帳號的資料庫。"""
    if not owner_present():
        return 0
    try:
        owner = read_owner()
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    owner_nodename = owner.get("nodename")
    if not isinstance(owner_nodename, str) or owner_nodename == local_nodename():
        return 0
    owner_host = owner.get("host")
    display_host = owner_host if isinstance(owner_host, str) and owner_host else owner_nodename
    print(
        "TERM_DATABASE_REMOTE_OWNER: daemon owner 位於 "
        f"{display_host}；請到該主機執行此資料庫指令。",
        file=sys.stderr,
    )
    return 5


class DaemonOwnerExists(RuntimeError):
    """已有 owner，或既有所有權無法安全判定。"""


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _is_daemon_cmdline(cmdline: bytes) -> bool:
    args = [part.decode(errors="replace") for part in cmdline.split(b"\0") if part]
    return (any(Path(arg).name == "terminux-daemon" for arg in args)
            or any(args[i:i + 2] == ["-m", "terminux_engine"]
                   for i in range(len(args) - 1)))


def _local_owner_is_stale(owner: dict[str, Any]) -> bool:
    if owner.get("nodename") != local_nodename():
        return False
    pid = owner.get("pid")
    boot_id = owner.get("boot_id")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(boot_id, str):
        return False
    if boot_id != _boot_id():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not _is_daemon_cmdline(cmdline)


def _directory(path: Path, *, private: bool) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    metadata = path.lstat()
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & forbidden):
        raise RuntimeError("TERM_DAEMON_OWNER_DIRECTORY_UNSAFE")


class DaemonOwner:
    """持有一次 daemon instance 的目錄鎖與原子 owner metadata。"""

    def __init__(self, lock_path: Path, metadata: dict[str, Any]) -> None:
        self.lock_path = lock_path
        self.metadata = metadata

    @classmethod
    def acquire(cls) -> DaemonOwner:
        """取得目錄鎖；只回收確認位於本機的殘留 owner。"""
        home = home_for_uid(os.geteuid())
        terminux = home / ".terminux"
        _directory(terminux, private=False)
        run_dir = terminux / "run"
        _directory(run_dir, private=True)
        lock_path = run_dir / "owner.lock"
        for attempt in range(2):
            try:
                lock_path.mkdir(mode=0o700)
            except FileExistsError:
                try:
                    info = lock_path.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                        raise ValueError("unsafe lock")
                    existing = json.loads((lock_path / "owner.json").read_text())
                    if not isinstance(existing, dict):
                        raise ValueError("invalid owner")
                except (OSError, ValueError):
                    raise DaemonOwnerExists(
                        "TERM_DAEMON_OWNER_EXISTS owner.json 無法讀取或解析；請人工確認並刪除 lock") from None
                if attempt == 0 and _local_owner_is_stale(existing):
                    # 重新核對目錄身分，避免回收已被替換的鎖。
                    current = lock_path.lstat()
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise DaemonOwnerExists("TERM_DAEMON_OWNER_EXISTS lock 已被替換")
                    shutil.rmtree(lock_path)
                    continue
                raise DaemonOwnerExists(
                    f"TERM_DAEMON_OWNER_EXISTS host={existing.get('host')} pid={existing.get('pid')}")
            try:
                metadata = {
                    "schema_version": 1, "state": "starting",
                    "host": os.environ.get("TERMINUX_ADVERTISE_HOST") or socket.getfqdn(),
                    "nodename": local_nodename(), "boot_id": _boot_id(), "pid": os.getpid(),
                    "instance_id": str(uuid4()), "core_port": None, "lineage_port": None,
                    "token": None, "version": __version__,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                owner = cls(lock_path, metadata)
                owner._write()
            except BaseException:
                lock_path.rmdir()
                raise
            return owner
        raise DaemonOwnerExists("TERM_DAEMON_OWNER_EXISTS")

    def _write(self) -> None:
        fd, name = tempfile.mkstemp(prefix=".owner-", dir=self.lock_path)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(self.metadata, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.lock_path / "owner.json")
        finally:
            temporary.unlink(missing_ok=True)

    def mark_ready(self, core_port: int, lineage_port: int, token: str) -> None:
        """兩個 TCP listener 就緒後公開連線 metadata。"""
        self.metadata.update(state="ready", core_port=core_port, lineage_port=lineage_port, token=token)
        self._write()

    def mark_unix_ready(self) -> None:
        """只在兩個本機 Unix listener 就緒後公告服務。"""
        self.metadata.update(state="ready", unix_only_ipc=True)
        self._write()

    def clear_stale_socket(self, path: str | Path) -> None:
        """確認仍持有 owner 後，只移除預定 endpoint 上的同 UID socket。"""
        current = json.loads((self.lock_path / "owner.json").read_text())
        if current.get("instance_id") != self.metadata["instance_id"]:
            raise RuntimeError("TERM_DAEMON_OWNER_CHANGED")
        endpoint = Path(path)
        if endpoint.parent != self.lock_path.parent / local_nodename():
            return
        try:
            metadata = endpoint.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(metadata.st_mode):
            if metadata.st_uid != os.geteuid():
                raise RuntimeError("TERM_DAEMON_SOCKET_OWNER_MISMATCH")
            endpoint.unlink()

    def release(self) -> bool:
        """只釋放仍屬於本次 instance 的鎖。"""
        try:
            target = self.lock_path / "owner.json"
            current = json.loads(target.read_text())
            if current.get("instance_id") != self.metadata["instance_id"]:
                _log.error("TERM_DAEMON_OWNER_LOCK_RETAINED reason=instance_id_mismatch")
                return False
            target.unlink()
            self.lock_path.rmdir()
        except (OSError, ValueError, AttributeError):
            _log.error("TERM_DAEMON_OWNER_LOCK_RETAINED reason=owner_metadata_or_directory")
            return False
        return True
