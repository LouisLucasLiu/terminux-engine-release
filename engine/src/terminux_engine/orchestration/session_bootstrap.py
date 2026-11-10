# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""session scope isolation：orchestration CLI 的 persistent IPC session lifecycle。

`orchestration.*` 8 個 JSON-RPC 方法是 context-required method（見
`ipc/jsonrpc_server.py::_CONTEXT_REQUIRED_METHODS`），daemon 端要求每個請求
都帶有效的 `terminux_context.session_id`。

`session.init` 的 session id 有效期綁定核發它的那條 core-socket
（`<home>/.terminux/terminux.sock`）連線：`ipc/server.py::_handle_connection` 在該連線
關閉（無論是否呼叫 `session.end`）時，會立即在共用的
`CanonicalSessionContextRegistry` 撤銷該 session（見 `server.py` 的
`_handle_connection` finally block）。因此呼叫端必須在整個使用 session id
期間持續持有這條連線——bootstrap 完就關閉連線會讓 session 在真正送出
`orchestration.*` request 之前就已被撤銷。

`persistent_cli_session()` 是唯一入口：以 context manager 形式在整個
`with` scope 內保持 core-socket 連線存活（正常結束、exception、
`KeyboardInterrupt`、逾時都會在 `finally` 關閉連線並讓 daemon revoke），
取代舊有「bootstrap 完立即關閉再回傳裸 session id 字串」的作法。

production 拓樸下（`main.py`）同一 daemon process 一定同時啟動
`IPCServer`（`<home>/.terminux/terminux.sock`）與 lineage JSON-RPC server，所以這個
bootstrap 在正常部署下必定可用。若呼叫環境沒有 `IPCServer`（例如只啟動
lineage socket 的測試 harness、或操作者刻意隔離），也保留
`TERMINUX_ORCHESTRATION_SESSION_ID` 環境變數直接指定既有 session id 的
既有旁路，跳過即時 bootstrap（session id 本身仍會在 daemon 端經 registry
驗證 same-peer，不構成繞過 server-side 驗證；此路徑沒有本地連線可持有，
生命週期由外部管理者負責）。
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from uuid import uuid4
import logging

_log = logging.getLogger(__name__)

# 不再有 client 端的 "default" fallback——未設定
# project 環境變數 ⇒ session.init **不送** project_id（session 未宣告 project）。
_BOOTSTRAP_TIMEOUT_SECONDS = 3.0
_RECV_CHUNK_BYTES = 65536


@dataclass
class CliSession:
    """一次 CLI 呼叫的 session 狀態。

    `session_id` 為 ``None`` 時代表 bootstrap 失敗（daemon 不可達、
    connection refused、timeout、daemon 端拒絕 assertion 等）——呼叫端
    之後送 orchestration.* 請求時，daemon 會因缺少 context 回傳明確的
    `SESSION_CONTEXT_REQUIRED` 錯誤，操作者可由此判斷原因，不是本模組
    要吞掉的內部細節。
    """

    session_id: str | None
    _connection: socket.socket | None


def _resolve_client_socket_path() -> str | None:
    """per-user agent socket。解析不到（例如 daemon 沒起、沒有
    logind session）就回 ``None``——沒有 ``/tmp`` fallback。"""
    from terminux_engine.ipc.agent_socket import AgentSocketError, client_agent_socket

    try:
        return os.fspath(client_agent_socket().path)
    except AgentSocketError:
        return None


@contextlib.contextmanager
def persistent_cli_session(
    *, core_socket_path: str | None = None
) -> Iterator[CliSession]:
    """開啟 core-socket 連線、送一次 `session.init`，並在整個 `with` scope
    內保持該連線存活，離開 scope 時（正常、exception、Ctrl-C、逾時皆同）
    於 `finally` 關閉連線——daemon 端會在連線關閉時立即撤銷 session。

    優先順序：
    1. ``TERMINUX_ORCHESTRATION_SESSION_ID`` 環境變數（直接指定，供無
       ``IPCServer`` 的受控環境使用；此路徑沒有本地連線可持有）。
    2. 對 Core socket（``core_socket_path``，否則由
       ``ipc.agent_socket.client_agent_socket()`` 依本 process UID 查帳號資料
       解析成 ``<home>/.terminux/terminux.sock``；``TERMINUX_CORE_SOCK`` 只是
       明確的隔離部署覆寫，不是 fallback）送一次 ``session.init``，並持有
       該連線。
    """
    env_session_id = os.environ.get("TERMINUX_ORCHESTRATION_SESSION_ID")
    if env_session_id:
        yield CliSession(session_id=env_session_id, _connection=None)
        return

    sock_path = core_socket_path or _resolve_client_socket_path()
    if sock_path is None or not Path(sock_path).exists():
        yield CliSession(session_id=None, _connection=None)
        return

    member_id = getpass.getuser()
    # TERMINUX_PROJECT_ID（舊名 TERMINUX_PROJECT 相容）；兩者衝突或值不合法
    # 一律明確報錯，不猜一個。未設定 ⇒ 不宣告 project。
    from terminux_engine.ipc.project_env import (
        ProjectEnvError, resolve_project_declaration, session_init_project_params,
    )

    try:
        declaration = resolve_project_declaration()
    except ProjectEnvError as exc:
        raise RuntimeError(f"{exc.code}: {exc}") from exc
    if declaration.warning:
        _log.warning("TERM_PROJECT_ENV_LEGACY_NAME %s", declaration.warning)
    request_id = uuid4().hex
    payload = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session.init",
                "params": {
                    "member_id": member_id,
                    **session_init_project_params(declaration),
                    "shell": os.environ.get("SHELL", "bash"),
                    "work_directory": os.getcwd(),
                },
            },
            ensure_ascii=False,
        )
        + "\n"
    )

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        session_id = _bootstrap_over_connection(
            connection, sock_path=sock_path, payload=payload, request_id=request_id
        )
        yield CliSession(session_id=session_id, _connection=connection)
    finally:
        connection.close()


def _bootstrap_over_connection(
    connection: socket.socket, *, sock_path: str, payload: str, request_id: str
) -> str | None:
    """送出 session.init 並解析 session_id；任何失敗回傳 None，不拋出例外。"""
    try:
        connection.settimeout(_BOOTSTRAP_TIMEOUT_SECONDS)
        connection.connect(sock_path)
        connection.sendall(payload.encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(_RECV_CHUNK_BYTES)
            if not chunk:
                break
            data += chunk
    except OSError:
        return None

    try:
        response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(response, dict) or response.get("id") != request_id:
        return None

    result = response.get("result")
    if not isinstance(result, dict):
        return None
    session_id = result.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None
