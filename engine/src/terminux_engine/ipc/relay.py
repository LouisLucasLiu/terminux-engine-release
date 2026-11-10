# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""將本機 Unix endpoint 連線轉送至目前的遠端 owner。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import stat
from pathlib import Path

from terminux_engine.ipc.agent_socket import (
    daemon_agent_socket, daemon_lineage_socket, host_runtime_directory, local_nodename,
)
from terminux_engine.ipc.daemon_owner import _runtime_log_handler, read_owner
from terminux_engine.ipc.socket_lifecycle import (
    BoundUnixSocket, bind_owned_unix_socket, unlink_owned_unix_socket,
)
from terminux_engine.ipc.tcp_bridge import StreamConnections, forward_streams
from terminux_engine.version_cli import _check_help_arg, check_version_arg

_log = logging.getLogger(__name__)


def pid_exists(pid: int) -> bool:
    """只檢查本機有效的正整數 pid，不以權限錯誤宣稱程序消失。"""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_relay_cmdline(raw: bytes) -> bool:
    args = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
    return (any(Path(arg).name == "terminux-relay" for arg in args)
            or any(args[index:index + 2] == ["-m", "terminux_engine.ipc.relay"]
                   for index in range(len(args) - 1)))


def running_relay(pid_path: Path) -> int | None:
    """回傳已確認為 relay 的本機 pid；不對未確認的程序送 signal。"""
    try:
        pid = int(pid_path.read_text())
        if pid_exists(pid) and _is_relay_cmdline(Path(f"/proc/{pid}/cmdline").read_bytes()):
            return pid
    except (OSError, ValueError):
        pass
    return None


def _remove_stale_sockets(directory: Path) -> None:
    for name in ("terminux.sock", "lineage.sock"):
        path = directory / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
            path.unlink()


class Relay(StreamConnections):
    """雙 socket relay；每條連線重新讀取 owner，不快取 token。"""

    def __init__(self) -> None:
        super().__init__()
        self._identities: list[BoundUnixSocket] = []

    async def start(self) -> None:
        """綁定本機 Core 與 lineage，保留任何未確認的既有 endpoint。"""
        for path, port in ((daemon_agent_socket().path, "core_port"),
                           (daemon_lineage_socket().path, "lineage_port")):
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                identity = bind_owned_unix_socket(raw, path)
                self._identities.append(identity)
                os.chmod(path, 0o600)
                raw.setblocking(False)
                server = await asyncio.start_unix_server(
                    lambda reader, writer, key=port: self._handle(reader, writer, key), sock=raw)
                self._servers.append(server)
            except BaseException:
                raw.close()
                raise

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      port_key: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        remote: asyncio.StreamWriter | None = None
        try:
            if self._closing:
                return
            owner = read_owner()
            if owner.get("state") != "ready" or owner.get("nodename") == local_nodename():
                return
            host, port, token = owner["host"], owner[port_key], owner["token"]
            if (not isinstance(host, str) or not isinstance(port, int)
                    or not isinstance(token, str)):
                return
            upstream, remote = await asyncio.wait_for(asyncio.open_connection(host, port), 3.0)
            self._writers.add(remote)
            remote.write((json.dumps({"token": token}) + "\n").encode())
            await remote.drain()
            await forward_streams(reader, writer, upstream, remote)
        except (OSError, ValueError, KeyError, TimeoutError):
            _log.info("TERM_RELAY_CONNECTION_UNAVAILABLE TERM_DAEMON_OWNER_UNAVAILABLE")
        finally:
            for connection in (writer, remote):
                if connection is not None:
                    connection.close()
                    try:
                        await connection.wait_closed()
                    except OSError:
                        pass
                    self._writers.discard(connection)
            if task is not None:
                self._tasks.discard(task)

    async def close(self) -> None:
        """先停止所有連線，再只刪除自己建立的 socket。"""
        await super().close()
        for identity in self._identities:
            unlink_owned_unix_socket(identity)
        self._identities.clear()


async def run() -> int:
    """若 owner 已 ready 且不在本機，持有 relay.pid 並服務至結束條件。"""
    try:
        owner = read_owner()
    except (OSError, ValueError):
        _log.info("TERM_RELAY_NOT_STARTED TERM_DAEMON_OWNER_UNAVAILABLE reason=owner_unavailable")
        return 0
    if owner.get("state") != "ready" or owner.get("nodename") == local_nodename():
        _log.info("TERM_RELAY_NOT_STARTED reason=owner_not_remote_ready")
        return 0
    directory = host_runtime_directory(create_dir=True)
    pid_path = directory / "relay.pid"
    stale = False
    try:
        fd = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            pid = int(pid_path.read_text())
        except (OSError, ValueError):
            _log.info("TERM_RELAY_NOT_STARTED reason=pid_unconfirmed")
            return 0
        if pid_exists(pid):
            reason = "already_running" if running_relay(pid_path) else "pid_not_relay"
            _log.info("TERM_RELAY_NOT_STARTED reason=%s", reason)
            return 0
        pid_path.unlink()
        stale = True
        try:
            fd = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _log.info("TERM_RELAY_NOT_STARTED reason=concurrent_start")
            return 0
    relay = Relay()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    log_handler: logging.Handler | None = None
    console_handlers = logging.getLogger().handlers[:]
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        log_handler = _runtime_log_handler("relay.log")
        logging.getLogger().handlers = [log_handler]
        if stale:
            _remove_stale_sockets(directory)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await relay.start()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), 5.0)
            except TimeoutError:
                try:
                    owner = read_owner()
                except FileNotFoundError:
                    _log.info("TERM_DAEMON_OWNER_UNAVAILABLE reason=owner_missing")
                    break
                except (OSError, ValueError):
                    _log.info("TERM_DAEMON_OWNER_UNAVAILABLE reason=owner_unreadable")
                    continue
                if owner.get("nodename") == local_nodename():
                    break
        return 0
    finally:
        try:
            await relay.close()
        finally:
            # 先關閉檔案 handler，再釋放 relay.pid，讓下一個實例可安全輪替。
            if log_handler is not None:
                logging.getLogger().handlers = console_handlers
                log_handler.close()
        try:
            if int(pid_path.read_text()) == os.getpid():
                pid_path.unlink()
        except (OSError, ValueError):
            _log.error("TERM_RELAY_PID_RETAINED reason=pid_unconfirmed")


def main() -> None:
    _check_help_arg("usage: terminux-relay [--help] [--version]")
    check_version_arg()
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
