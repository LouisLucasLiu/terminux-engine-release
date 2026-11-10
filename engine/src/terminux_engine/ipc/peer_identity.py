# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Linux UDS peer credential 解析（root of trust）。

Root of trust 是 kernel 驗證過的 ``SO_PEERCRED``（accepted Unix Domain
Socket 連線），不是 client 自稱的欄位、不是 ``getpass.getuser()``、不是
``$USER``／``$LOGNAME``、也不是 daemon 啟動時的 process owner。

兩個既有 listener 取得底層 socket 的方式不同：

- ``jsonrpc_server.py`` 的 ``serve_forever``／``_handle_connection``：
  ``socket.accept()`` 直接回傳原生 ``socket.socket``，可直接查
  ``SO_PEERCRED``。
- ``ipc/server.py`` 的 ``IPCServer``（asyncio）：``_handle_connection`` 收到
  的是 ``asyncio.StreamWriter``，底層 socket 經
  ``writer.get_extra_info("socket")`` 取得。

兩者最終都收斂到同一個 ``resolve_peer_uid_from_socket()``。
"""

from __future__ import annotations

import os
import pwd
import socket
import struct
from typing import Any

# Linux struct ucred { pid_t pid; uid_t uid; gid_t gid; } → 3 個 int。
_PEERCRED_STRUCT_FORMAT = "3i"
_PEERCRED_STRUCT_SIZE = struct.calcsize(_PEERCRED_STRUCT_FORMAT)


def resolve_peer_uid_from_socket(sock: socket.socket) -> int | None:
    """從已 accept 的原生 UDS socket 取得 peer UID。

    取不到（非 UDS、平台不支援、socket 已關閉等）時回傳 ``None``，不拋出
    exception——由呼叫端依 fail-closed 規則轉換成
    ``PEER_IDENTITY_UNAVAILABLE``。
    """
    try:
        raw = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED_STRUCT_SIZE
        )
    except (OSError, AttributeError):
        return None
    try:
        _pid, uid, _gid = struct.unpack(_PEERCRED_STRUCT_FORMAT, raw)
    except struct.error:
        return None
    # 非 AF_UNIX socket（例如 AF_INET）呼叫 SO_PEERCRED 不一定拋出例外，
    # 但 kernel 未填入 credential 時 uid 會是 -1（unset）；一併視為不可用。
    if uid < 0:
        return None
    return uid


def resolve_peer_uid_from_asyncio_writer(writer: Any) -> int | None:
    """從 asyncio ``StreamWriter`` 取得底層 socket 後解析 peer UID。

    取不到時回傳 ``None``（同上，fail-closed 由呼叫端負責）。
    """
    get_extra_info = getattr(writer, "get_extra_info", None)
    if get_extra_info is None:
        return None
    sock = get_extra_info("socket")
    if sock is None:
        return None
    return resolve_peer_uid_from_socket(sock)


def resolve_account_name(uid: int) -> str | None:
    """UID → canonical Linux account name（``pwd`` / NSS）。

    查不到時回傳 ``None``；呼叫端不得 fallback 到
    ``getpass.getuser()``／環境變數／daemon owner。
    """
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError, ValueError):
        return None


def capture_daemon_euid() -> int:
    """daemon process 啟動時的 effective UID（root of trust,
    per-member daemon 假設的錨點）。

    session scope isolation 只支援 per-member daemon——
    一個 daemon process 只能接受 ``peer_uid == daemon startup EUID`` 的
    client。呼叫端必須在 daemon bootstrap 時呼叫一次本函式並把結果傳給
    兩個 listener（``IPCServer`` 與 lineage ``serve_forever``／
    ``serve_once``），不得在每次 request 時重新呼叫 ``os.geteuid()``——
    daemon 的 EUID 理論上不會在 process 生命週期內改變，但錨點應該固定於
    啟動當下，而非每次比對時才詢問 OS（避免與未來任何 in-process
    privilege 變化耦合）。
    """
    return os.geteuid()


def is_peer_authorized_for_daemon(peer_uid: int | None, daemon_euid: int | None) -> bool:
    """per-member daemon guard 的比對本身。

    ``peer_uid`` 或 ``daemon_euid`` 任一為 ``None``（peer credential 無法
    取得、或呼叫端未設定 daemon EUID 錨點）一律視為未授權，fail-closed。
    """
    if peer_uid is None or daemon_euid is None:
        return False
    return peer_uid == daemon_euid
