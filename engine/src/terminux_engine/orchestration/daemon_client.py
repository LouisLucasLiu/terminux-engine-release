# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""CLI 專用的同步 daemon JSON-RPC transport adapter。

使用 newline-terminated JSON-RPC request／response。socket 路徑由
`ipc.agent_socket.client_lineage_socket` 集中解析，明確覆寫優先。
"""

from __future__ import annotations

import json
import socket
from typing import Any
from uuid import uuid4
from terminux_engine.ipc.agent_socket import AgentSocketError, client_lineage_socket
from terminux_engine.ipc.socket_lifecycle import (
    SocketPathTooLongError,
    validate_unix_socket_path,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_RECV_CHUNK_BYTES = 65536


class DaemonTransportError(RuntimeError):
    """socket unavailable、timeout、malformed response 或 id mismatch。"""


class DaemonRpcError(RuntimeError):
    """daemon 回傳的 JSON-RPC error envelope。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OrchestrationDaemonClient:
    """CLI 專用的同步 Unix-domain JSON-RPC client。"""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        # CLI 一次性 session.init bootstrap 取得的 Core-issued
        # session id（見 `session_bootstrap.resolve_cli_session_id()`）；
        # 8 個 orchestration.* method 現在是 context-required method，缺少
        # 有效 terminux_context 時 daemon 端會 fail-closed
        # （SESSION_CONTEXT_REQUIRED）。None 時完全不附加 terminux_context，
        # 行為與 session scope isolation 之前相同（daemon 端會拒絕該請求，屬本任務明確
        # 授權的 transport 行為變更，不是本 client 造成的相容性破壞）。
        session_id: str | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._session_id = session_id

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """送出一筆 JSON-RPC 2.0 request，只回傳成功時的 inner result object。"""

        try:
            socket_path = self._socket_path or str(client_lineage_socket().path)
        except AgentSocketError as error:
            raise DaemonTransportError(str(error)) from error

        request_id = uuid4().hex
        envelope: dict[str, Any] = {
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }
        if self._session_id:
            envelope["terminux_context"] = {"session_id": self._session_id}
        payload = json.dumps(envelope, ensure_ascii=False) + "\n"

        # 路徑過長在 connect **之前**就判定，並以明確訊息回報；
        # 否則 kernel 的 ENAMETOOLONG 會被下面的 `except OSError` 包成
        # 「daemon request failed」，看起來像 daemon 不存在。
        try:
            validate_unix_socket_path(socket_path)
        except SocketPathTooLongError as error:
            raise DaemonTransportError(str(error)) from None

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(_DEFAULT_TIMEOUT_SECONDS)
                connection.connect(socket_path)
                connection.sendall(payload.encode("utf-8"))
                data = b""
                while b"\n" not in data:
                    chunk = connection.recv(_RECV_CHUNK_BYTES)
                    if not chunk:
                        break
                    data += chunk
        except OSError as error:
            raise DaemonTransportError(
                f"orchestration daemon request failed: {error}"
            ) from error

        try:
            response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DaemonTransportError(
                "orchestration daemon returned invalid JSON"
            ) from error
        if not isinstance(response, dict):
            raise DaemonTransportError(
                "orchestration daemon returned invalid JSON-RPC envelope"
            )
        if response.get("id") != request_id:
            raise DaemonTransportError(
                "orchestration daemon response id does not match request"
            )

        error_payload = response.get("error")
        if isinstance(error_payload, dict):
            code = error_payload.get("code")
            message = error_payload.get("message")
            if type(code) is not int or type(message) is not str:
                raise DaemonTransportError(
                    "orchestration daemon returned invalid JSON-RPC error envelope"
                )
            raise DaemonRpcError(code, message)

        result = response.get("result")
        if type(result) is not dict:
            raise DaemonTransportError(
                "orchestration daemon success result must be an object"
            )
        return result
