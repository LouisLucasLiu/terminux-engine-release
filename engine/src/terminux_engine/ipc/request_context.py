# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""canonical request context 資料模型與 request-scoped 傳遞。

只提供三件事：

1. ``CanonicalRequestContext``：consumer 看到的 immutable identity（frozen
   dataclass），欄位固定為 ``session_id``／``member_id``／``project_id``。
2. request-scoped ``ContextVar`` 與 set/reset/get helper：讓
   ``IPCServer``、lineage JSON-RPC server、``AgentRunner``、
   ``ToolRegistry`` worker thread 都能取得同一份 context，而不需要在每個
   函式簽名多加一個 identity 參數，也不需要任何 process-global mutable
   「目前呼叫者」欄位。

本模組不含 peer credential 解析（見 ``peer_identity.py``）、不含 session
生命週期管理（見 ``session_registry.py``），刻意保持零依賴，避免循環 import。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


#: session 未宣告 project 時，`project_id` 仍保持
#: non-empty（既有 ECC／ShellEvent 消費端不變），由 `project_source` 表示來源。
UNDECLARED_PROJECT_ID = "default"

#: `project_source` 的合法值。
#: - ``session``：client 在 session.init 明確宣告（含使用者真的叫 "default"）。
#: - ``unset``：client 沒有宣告；`project_id` 是程式補的 `UNDECLARED_PROJECT_ID`。
#: - ``profile``：由 task 固定的 Profile project 推導出的**該 task 專用**
#:   context（worker thread／task 內 gate 用）；不會出現在 session 本身。
PROJECT_SOURCE_SESSION = "session"
PROJECT_SOURCE_UNSET = "unset"
PROJECT_SOURCE_PROFILE = "profile"
PROJECT_SOURCES: frozenset[str] = frozenset({
    PROJECT_SOURCE_SESSION, PROJECT_SOURCE_UNSET, PROJECT_SOURCE_PROFILE,
})


@dataclass(frozen=True)
class CanonicalRequestContext:
    """單一 request／Agent turn 的 immutable canonical identity。

    - ``session_id``：Core 產生的 UUID4 字串，non-empty。
    - ``member_id``：由 peer UID 解析出的 Linux account name，non-empty。
    - ``project_id``：session 建立時選定、session 內 immutable，non-empty。
    - ``project_source``：``project_id`` 的來源（見 ``PROJECT_SOURCES``）。
      **只由 daemon 決定**：client 不能在 session.init 自稱來源。舊建構點
      不帶此欄位 ⇒ 視為明確宣告（``session``），與過去行為一致。
    """

    session_id: str
    member_id: str
    project_id: str
    project_source: str = PROJECT_SOURCE_SESSION

    def __post_init__(self) -> None:
        if self.project_source not in PROJECT_SOURCES:
            raise ValueError(f"invalid project_source: {self.project_source!r}")

    @property
    def project_declared(self) -> bool:
        """session 是否**明確**宣告了 project（``unset`` 以外皆為 True）。"""
        return self.project_source != PROJECT_SOURCE_UNSET


class RequestContextError(RuntimeError):
    """本模組 internal error 的共同基底類別。"""


class RequestContextMissingError(RequestContextError):
    """``get_current_request_context()`` 在沒有 active context 時拋出。

    對應 ``REQUEST_CONTEXT_MISSING`` reason code；只在
    internal 呼叫鏈使用，不直接序列化成 public JSON-RPC error（public error
    由呼叫端依情境轉換）。
    """


# request-scoped context：每個 request boundary 明確 set，finally reset。
# 不使用任何 module-level 可變的「目前呼叫者」欄位。
_current_request_context: "contextvars.ContextVar[CanonicalRequestContext]" = (
    contextvars.ContextVar("terminux_canonical_request_context")
)


def set_current_request_context(
    context: CanonicalRequestContext,
) -> "contextvars.Token[CanonicalRequestContext]":
    """在目前 request boundary 設定 context，回傳 reset 用的 token。"""
    return _current_request_context.set(context)


def reset_current_request_context(
    token: "contextvars.Token[CanonicalRequestContext]",
) -> None:
    """以 ``set_current_request_context`` 回傳的 token reset。"""
    _current_request_context.reset(token)


def get_current_request_context() -> CanonicalRequestContext:
    """取得目前 request 的 canonical context。

    沒有 active context 時 fail-closed：拋出 ``RequestContextMissingError``，
    不回傳任何 daemon-startup-time global 或猜測值。
    """
    try:
        return _current_request_context.get()
    except LookupError as exc:
        raise RequestContextMissingError(
            "no active CanonicalRequestContext for this request"
        ) from exc


def try_get_current_request_context() -> CanonicalRequestContext | None:
    """非 fail-closed 版本：沒有 active context 時回傳 ``None``。

    供不需要強制 fail-closed 的內部診斷／測試 spy 使用；production
    context-required 路徑一律使用 ``get_current_request_context()``。
    """
    return _current_request_context.get(None)
