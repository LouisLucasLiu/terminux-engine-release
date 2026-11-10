# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Generic native／legacy adapter bridge。

把既有 `ToolDefinition`（`executables` 候選清單 + `command_builder`）包裝成
`ExecutionAdapter`，讓沒有指定自訂 adapter 的既有 tool（例如
`icc2_shell`／`mmmc_sum`／`pt_shell`）在不改變任何既有行為的前提下，透過
新的 `ToolRegistry.prepare_execution()` 路徑執行——argv 組裝與 executable
解析邏輯與既有 `ToolRegistry.build_argv()`／`resolve_executable()`
byte-for-byte 相同，只是包成 `PreparedExecution`。

`discover_artifacts()` 對這類既有工具回傳空清單——它們原本就沒有 artifact
discovery 這個概念（DQE package 自己用固定檔名讀 cwd），這裡不強加。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from terminux_engine.orchestration.execution_contract import (
    ArtifactDiscoveryResult,
    ExecutionRequest,
    PreparedExecution,
)

NATIVE_ADAPTER_ID = "native"
# `adapter_version` 必須是非空穩定值。這是 adapter 實作
# 本身的版本，不是被執行工具的版本（工具版本走 `tool_version_evidence`）。
NATIVE_ADAPTER_VERSION = "native/1"


class NativeArgvAdapter:
    """把 `executables` 候選清單 + `command_builder` 包成 `ExecutionAdapter`。"""

    adapter_id = NATIVE_ADAPTER_ID
    adapter_version = NATIVE_ADAPTER_VERSION

    def __init__(
        self,
        *,
        resolve_executable: Callable[[str, Path | None], Path],
        command_builder: Callable[[dict[str, Any]], list[str]],
    ) -> None:
        self._resolve_executable = resolve_executable
        self._command_builder = command_builder

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        executable = self._resolve_executable(request.tool_id, request.cwd)
        argv = [str(executable), *self._command_builder(request.parameters)]
        env = None
        if request.environment_overrides:
            env = dict(request.environment_overrides)
        return PreparedExecution(
            argv=argv,
            cwd=request.cwd,
            env=env,
            timeout_seconds=request.timeout_seconds,
            adapter_metadata={
                "executable": str(executable),
                "adapter_version": NATIVE_ADAPTER_VERSION,
            },
            artifact_discovery_context={},
        )

    def discover_artifacts(
        self,
        request: ExecutionRequest,
        prepared: PreparedExecution,
        *,
        process_exit_code: int,
    ) -> ArtifactDiscoveryResult:
        return ArtifactDiscoveryResult(output_roots=[request.cwd], artifact_candidates=[])
