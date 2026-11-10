# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""daemon 派生的 child 透過 Unix socket 呼叫 daemon JSON-RPC 的最小 client。

`JsonRpcIpcClient` 供 child 的 language preflight 等 RPC 使用；`IpcLlmService` 讓
workflow runner 以與 LlmService 相同的 ask 介面借用 daemon 的 LLM。
"""

from __future__ import annotations

import json
import socket
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from uuid import uuid4

from terminux_engine.ipc.socket_lifecycle import (
    SocketPathTooLongError,
    validate_unix_socket_path,
)


class IpcRequestError(RuntimeError):
    """JSON-RPC transport 或 daemon 回應不合法。"""


class JsonRpcIpcClient:
    """最小同步 Unix-domain JSON-RPC client。"""

    def __init__(self, endpoint: str | Path, *, timeout: float = 30.0) -> None:
        self._endpoint = str(Path(endpoint).expanduser())
        self._timeout = timeout

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """送出一筆 JSON-RPC 請求並回傳 result object。"""
        request_id = uuid4().hex
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            ensure_ascii=False,
        ) + "\n"
        effective_timeout = self._timeout if timeout is None else timeout
        # 先驗長度，錯誤明確；不讓它變成一般的 "IPC request failed"。
        try:
            validate_unix_socket_path(self._endpoint)
        except SocketPathTooLongError as error:
            raise IpcRequestError(str(error)) from None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(effective_timeout)
                connection.connect(self._endpoint)
                connection.sendall(payload.encode("utf-8"))
                chunks: list[bytes] = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except OSError as error:
            raise IpcRequestError(f"IPC request failed: {error}") from error
        try:
            response = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IpcRequestError("IPC returned invalid JSON-RPC response") from error
        if not isinstance(response, dict):
            raise IpcRequestError("IPC returned invalid JSON-RPC response")
        if response.get("id") != request_id:
            raise IpcRequestError("IPC response id does not match request")
        error_payload = response.get("error")
        if isinstance(error_payload, dict):
            raise IpcRequestError(str(error_payload.get("message", "IPC request failed")))
        result = response.get("result")
        if not isinstance(result, dict):
            raise IpcRequestError("IPC response result must be an object")
        return result


class IpcLlmService:
    """以與 LlmService 相同的 ask 介面呼叫 daemon LLM。

    `transport`／`llm_attempts`／`nonempty_responses` 為既有定義的
    最小 run-level evidence counters；非 public contract，僅供
    `BeEcoRunner` 讀取以彙整 evidence，不保存 prompt 或 raw response。
    """

    transport = "daemon_ipc"

    def __init__(self, ipc_client: JsonRpcIpcClient) -> None:
        self._ipc = ipc_client
        self.llm_attempts = 0
        self.nonempty_responses = 0
        self._provider_called: ContextVar[bool] = ContextVar("ipc_provider_called", default=False)

    def ask(self, prompt: str, *, timeout: float | None = 30.0) -> str | None:
        """IPC 不可用或 LLM 無回覆時回傳 None，保留 package fallback 語意。"""
        self.llm_attempts += 1
        self._provider_called.set(False)
        try:
            result = self._ipc.call("llm.ask", {"prompt": prompt, "timeout": timeout}, timeout=timeout)
        except (IpcRequestError, OSError, ValueError):
            return None
        response = result.get("result")
        # 舊 daemon 沒有診斷欄位時，只能由非空回覆證明 provider 已呼叫。
        self._provider_called.set(result.get(
            "provider_called", isinstance(response, str) and bool(response.strip())
        ) is True)
        if isinstance(response, str) and response.strip():
            self.nonempty_responses += 1
            return response
        return None
