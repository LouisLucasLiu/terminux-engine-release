# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Orchestration 專用的 in-memory tool registry。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from terminux_engine.orchestration.execution_contract import (
        ExecutionAdapter,
        ExecutionRequest,
        PreparedExecution,
    )


@dataclass(frozen=True)
class ToolDefinition:
    """單一 orchestration tool 的完整定義。

    `adapter`（vendor-neutral execution contract 新增，選填）：若提供，`ToolRegistry.prepare_execution()`
    會把 request 交給它處理，取代 `executables`/`command_builder` 的
    legacy 路徑（ORFS 等需要 vendor-specific 組裝的 tool 使用這條路徑）。
    未提供時（既有 `icc2_shell`／`mmmc_sum`／`pt_shell` 等），
    `prepare_execution()` 自動用 `NativeArgvAdapter` 包裝既有
    `executables`/`command_builder`，行為與既有 `build_argv()` 完全相同
    （backward-compatible，見 `native_adapter.py`）。
    """

    tool_id: str
    executables: list[str]
    event_tool_name: str
    expected_packages: list[str]
    default_timeout: float
    command_builder: Callable[[dict[str, Any]], list[str]]
    adapter: "ExecutionAdapter | None" = field(default=None)

    def __post_init__(self) -> None:
        if not self.executables:
            raise ValueError("ToolDefinition.executables must not be empty")


class ToolRegistry:
    """Orchestration path 使用的 in-memory tool registry。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool_def: ToolDefinition) -> None:
        """註冊 tool；重複 tool_id 會失敗。"""
        if tool_def.tool_id in self._tools:
            raise ValueError(f"tool already registered: {tool_def.tool_id}")
        self._tools[tool_def.tool_id] = tool_def

    def get(self, tool_id: str) -> ToolDefinition:
        """取得 tool 定義；未知 id 依 dict 語意拋 KeyError。"""
        return self._tools[tool_id]

    def has(self, tool_id: str) -> bool:
        """回傳指定 tool 是否已註冊。"""
        return tool_id in self._tools

    def list_tool_ids(self) -> list[str]:
        """回傳排序後的 tool id 清單。"""
        return sorted(self._tools)

    def build_argv(
        self,
        tool_id: str,
        parameters: dict[str, Any],
        *,
        cwd: Path | None = None,
    ) -> list[str]:
        """建構包含 executable 的完整 argv。"""
        executable = self.resolve_executable(tool_id, cwd=cwd)
        arguments = self.get(tool_id).command_builder(parameters)
        return [str(executable), *arguments]

    def resolve_executable(
        self,
        tool_id: str,
        *,
        cwd: Path | None = None,
    ) -> Path:
        """依定義順序找第一個可執行的 executable。"""
        for entry in self.get(tool_id).executables:
            path = Path(entry).expanduser()
            if path.is_absolute():
                candidate = path
            else:
                base = cwd if cwd is not None else Path.cwd()
                candidate = (base / path).resolve()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

            found = shutil.which(entry)
            if found is not None:
                return Path(found).resolve()
        raise FileNotFoundError(f"no executable found for tool: {tool_id}")

    def expected_packages(self, tool_id: str) -> list[str]:
        """回傳獨立副本，避免呼叫端改動 registry 定義。"""
        return list(self.get(tool_id).expected_packages)

    def event_tool_name(self, tool_id: str) -> str:
        """回傳 TimelineEvent 要使用的 canonical tool name。"""
        return self.get(tool_id).event_tool_name

    def default_timeout(self, tool_id: str) -> float:
        """回傳 tool 的預設 timeout 秒數。"""
        return self.get(tool_id).default_timeout

    def adapter_id(self, tool_id: str) -> str:
        """回傳這個 tool 實際會使用的 adapter id（未指定自訂 adapter 時為
        `native_adapter.NATIVE_ADAPTER_ID`）。"""
        from terminux_engine.orchestration.native_adapter import NATIVE_ADAPTER_ID

        definition = self.get(tool_id)
        return definition.adapter.adapter_id if definition.adapter is not None else NATIVE_ADAPTER_ID

    def adapter_version(self, tool_id: str) -> str:
        """回傳這個 tool 實際使用的 adapter 版本字串（artifact lineage：非空）。

        Adapter 未自報 `adapter_version` 時（第三方／測試用 adapter），
        以 `<adapter_id>/unversioned` 作為穩定、非空的 fallback——絕不
        回 None，避免 receipt 出現空 producer identity。
        """
        from terminux_engine.orchestration.native_adapter import NATIVE_ADAPTER_VERSION

        definition = self.get(tool_id)
        if definition.adapter is None:
            return NATIVE_ADAPTER_VERSION
        version = getattr(definition.adapter, "adapter_version", None)
        if isinstance(version, str) and version:
            return version
        return f"{definition.adapter.adapter_id}/unversioned"

    def prepare_execution(
        self,
        tool_id: str,
        parameters: dict[str, Any],
        *,
        cwd: Path,
        output_boundary: Path | None = None,
        timeout_seconds: float | None = None,
        environment_overrides: dict[str, str] | None = None,
    ) -> "PreparedExecution":
        """把一次工具呼叫請求交給對應 adapter，回傳 `PreparedExecution`。

        沒有自訂 `adapter` 的既有 tool（`icc2_shell` 等）走
        `NativeArgvAdapter`，行為與既有 `build_argv()`/`resolve_executable()`
        完全相同（backward-compatible）。
        """
        from terminux_engine.orchestration.execution_contract import ExecutionRequest
        from terminux_engine.orchestration.native_adapter import NativeArgvAdapter

        definition = self.get(tool_id)
        request = ExecutionRequest(
            tool_id=tool_id,
            parameters=parameters,
            cwd=cwd,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else definition.default_timeout,
            output_boundary=output_boundary if output_boundary is not None else cwd,
            environment_overrides=environment_overrides,
        )
        adapter = definition.adapter
        if adapter is None:
            adapter = NativeArgvAdapter(
                resolve_executable=lambda _tool_id, _cwd: self.resolve_executable(tool_id, cwd=_cwd),
                command_builder=definition.command_builder,
            )
        return adapter.prepare(request)

    def cleanup_execution(
        self,
        tool_id: str,
        parameters: dict[str, Any],
        prepared: "PreparedExecution",
        *,
        cwd: Path,
    ) -> dict[str, Any] | None:
        """把資源清理委派給對應 adapter 的**選擇性** `cleanup()` 方法。

        與 `discover_artifacts()` 刻意分開——那是產物收集，這是資源清理
        （例如孤兒 container）；existing adapter（`NativeArgvAdapter`／
        `FormalPairToolAdapter` 等）沒有實作 `cleanup()`，這裡直接回
        `None`，行為與新增前完全相同，backward-compatible。

        `cleanup()` 本身失敗時捕捉例外、如實回報 `{"ok": False, ...}`，
        不讓清理失敗變成整個 step 的例外，也不覆蓋既有的
        `subprocess_result`／`dispatch_barrier` 等欄位——清理失敗保留原始
        執行結果，只額外記錄一筆清理事實，不自動重跑工具。
        """
        definition = self.get(tool_id)
        adapter = definition.adapter
        cleanup = getattr(adapter, "cleanup", None) if adapter is not None else None
        if cleanup is None:
            return None
        from terminux_engine.orchestration.execution_contract import ExecutionRequest

        request = ExecutionRequest(
            tool_id=tool_id, parameters=parameters, cwd=cwd,
            timeout_seconds=definition.default_timeout, output_boundary=cwd)
        try:
            result = cleanup(request, prepared)
        except Exception as exc:  # noqa: BLE001 - 清理失敗如實記錄，不升級成例外
            return {"attempted": True, "ok": False, "detail": f"cleanup raised: {exc!r}"}
        return dict(result) if isinstance(result, dict) else {
            "attempted": True, "ok": False,
            "detail": f"cleanup() returned a non-dict result: {result!r}"}

    def discover_artifacts(
        self,
        tool_id: str,
        parameters: dict[str, Any],
        prepared: "PreparedExecution",
        *,
        cwd: Path,
        process_exit_code: int,
        output_boundary: Path | None = None,
    ):
        """把 artifact discovery 委派給對應 adapter；native tool 回傳空清單。

        內部重建一份與 `prepare_execution()` 語意相同的 `ExecutionRequest`
        （純 dataclass 建構＋驗證，無 side effect）供 adapter 使用，避免
        Kernel 需要另外保存並傳遞 request 物件。
        """
        from terminux_engine.orchestration.execution_contract import ExecutionRequest
        from terminux_engine.orchestration.native_adapter import NativeArgvAdapter

        definition = self.get(tool_id)
        request = ExecutionRequest(
            tool_id=tool_id,
            parameters=parameters,
            cwd=cwd,
            timeout_seconds=definition.default_timeout,
            output_boundary=output_boundary if output_boundary is not None else cwd,
        )
        adapter = definition.adapter
        if adapter is None:
            adapter = NativeArgvAdapter(
                resolve_executable=lambda _tool_id, _cwd: self.resolve_executable(tool_id, cwd=_cwd),
                command_builder=definition.command_builder,
            )
        return adapter.discover_artifacts(request, prepared, process_exit_code=process_exit_code)
