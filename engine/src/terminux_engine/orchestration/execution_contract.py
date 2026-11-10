# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Vendor-neutral tool execution contract。

定義 Kernel-managed tool execution 的三個共同型別：
`ExecutionRequest`（呼叫端要什麼）、`PreparedExecution`（adapter 把 request
轉成的、`SubprocessExecutor` 可直接執行的中性資料）、`ExecutionReceipt`
（一次執行完整、可 JSON serialize 的客觀事實紀錄）。

Domain-blind 邊界：本檔案與 `ExecutionAdapter` Protocol 完全不理解
任何 vendor-specific 概念（Docker、ORFS stage、EDA report 格式、timing／DRC
判斷）。這些概念只允許存在於個別 adapter 實作（如
`orfs_adapter.py::OrfsDockerAdapter`）與其回傳的 opaque
`adapter_metadata`／`artifact_discovery_context`／`ArtifactCandidate.adapter_role`
字串內容——Core 只原樣攜帶，不解讀。

不得 import 任何 ORFS-specific module、常數或 schema（Implementation
Constraint #3）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ExecutionContractError(ValueError):
    """Request／boundary 驗證失敗；adapter 或呼叫端應 fail-closed。"""


def canonicalize_boundary(path: str | Path) -> Path:
    """把一個路徑正規化為絕對、resolved path，供 boundary 比較使用。"""
    return Path(path).expanduser().resolve()


def validate_within_boundary(candidate: str | Path, boundary: str | Path) -> Path:
    """確認 `candidate` 落在 `boundary` 內；否則拋出 `ExecutionContractError`。

    純字串前綴比較之外，額外用 `Path.relative_to()` 做結構化驗證，避免
    `"/a/bc"` 誤判為在 `"/a/b"` boundary 內這類前綴陷阱。
    """
    resolved_boundary = canonicalize_boundary(boundary)
    resolved_candidate = canonicalize_boundary(candidate)
    try:
        resolved_candidate.relative_to(resolved_boundary)
    except ValueError as exc:
        raise ExecutionContractError(
            f"path outside output_boundary: {resolved_candidate} not under {resolved_boundary}"
        ) from exc
    return resolved_candidate


@dataclass(frozen=True)
class ExecutionRequest:
    """呼叫端（Kernel／CLI／RPC entry）對一次工具執行的最小共同描述。

    Core 不解讀 `parameters` 的 domain 意義；由被解析出的 adapter 自行驗證
    。
    """

    tool_id: str
    parameters: dict[str, Any]
    cwd: Path
    timeout_seconds: float
    output_boundary: Path
    environment_overrides: dict[str, str] | None = None
    adapter_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_id:
            raise ExecutionContractError("tool_id must not be empty")
        if self.timeout_seconds <= 0:
            raise ExecutionContractError("timeout_seconds must be positive")
        object.__setattr__(self, "cwd", canonicalize_boundary(self.cwd))
        object.__setattr__(
            self, "output_boundary", canonicalize_boundary(self.output_boundary)
        )


@dataclass(frozen=True)
class PreparedExecution:
    """Adapter 把 `ExecutionRequest` 轉成的、`SubprocessExecutor` 可直接執行的中性資料。

    共同層只執行 `argv`（`subprocess.Popen(argv, ...)`），不使用
    `shell=True`，不解析或組裝任何 shell 字串。
    """

    argv: list[str]
    cwd: Path
    env: dict[str, str] | None
    timeout_seconds: float
    adapter_metadata: dict[str, Any] = field(default_factory=dict)
    artifact_discovery_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.argv:
            raise ExecutionContractError("argv must not be empty")
        if self.timeout_seconds <= 0:
            raise ExecutionContractError("timeout_seconds must be positive")


@dataclass(frozen=True)
class ArtifactCandidate:
    """一個由 adapter 發現的 artifact 位置描述（中性、不含 domain 判斷）。

    `adapter_role` 是 adapter-owned 的 namespaced 字串（例如
    `"orfs.final_layout"`）；Core 不建立任何 GDS／timing／DRC taxonomy，只
    原樣攜帶。
    """

    adapter_role: str
    host_path: Path
    exists: bool


@dataclass(frozen=True)
class ArtifactDiscoveryResult:
    """一次執行後，adapter 回報的 artifact 揭露結果。"""

    output_roots: list[Path]
    artifact_candidates: list[ArtifactCandidate]
    artifact_manifest_reference: str | None = None


class ExecutionAdapter(Protocol):
    """Logical tool 與 physical execution 之間的最小 adapter 邊界。

    Adapter 擁有 request → argv 的組裝邏輯與 artifact 發現邏輯；Core／Kernel
    完全不理解這兩件事的內容。
    """

    adapter_id: str

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        """驗證 `request.parameters`（fail-closed，未知參數不得靜默忽略）並
        回傳可直接交給 `SubprocessExecutor` 的 `PreparedExecution`。"""
        ...

    def discover_artifacts(
        self,
        request: ExecutionRequest,
        prepared: PreparedExecution,
        *,
        process_exit_code: int,
    ) -> ArtifactDiscoveryResult:
        """執行完成後，回報這次執行揭露出的 artifact 候選清單。

        每個候選的 `host_path` 必須先由呼叫端以
        `validate_within_boundary(candidate, request.output_boundary)` 驗證；
        超出邊界的路徑不得被納入 receipt。
        """
        ...

    # `cleanup(request, prepared) -> dict[str, Any]`（**選擇性**，不是
    # Protocol 硬性要求，`ToolRegistry.cleanup_execution()` 用
    # `getattr(adapter, "cleanup", None)` 判斷有沒有實作）。與
    # `discover_artifacts()` 刻意分開：`prepare()` 成功之後——不管
    # `Kernel` 最終走正常完成、dispatch 失敗、還是 timeout 的哪一條既有
    # 路徑，甚至中途拋出未預期例外——都會被 Kernel 呼叫一次；用來處理
    # `prepare()` 可能留下的副作用（例如 Docker container），不是報告
    # 執行產出的內容。沒有需要清理的 adapter（`NativeArgvAdapter`／
    # `FormalPairToolAdapter` 等）不需要實作這個方法。


# ── ExecutionReceipt ─────────────────────────────────────────────────────

SCHEMA_VERSION = 1

_LIFECYCLE_STATES = frozenset(
    {"running", "completed", "failed", "timeout", "cancelled"}
)
_DISPATCH_STATES = frozenset(
    {"not_attempted", "running", "completed", "failed", "timeout"}
)
_EVIDENCE_STATES = frozenset({"not_attempted", "ready", "not_ready", "timeout"})


@dataclass(frozen=True)
class ExecutionReceipt:
    """一次執行的完整、客觀事實紀錄——不宣告 domain success。

    `execution_lifecycle_status`／`dispatch_status`／`evidence_status` 三者
    刻意分離：`process_exit_code == 0` 只能證明最外層 process
    正常返回，不能單獨代表 dispatch 已完成或 evidence 已就緒。
    """

    schema_version: int
    run_id: str
    step_id: str
    attempt: int
    parent_attempt_or_retry_reference: str | None
    tool_id: str
    event_tool_name: str
    adapter_id: str
    adapter_version: str | None
    executor_identity: str | None
    tool_version_evidence: dict[str, Any]
    started_at: float | None
    finished_at: float | None
    process_exit_code: int | None
    timed_out: bool
    termination_reason: str
    stdout_tail: list[str]
    stderr_tail: list[str]
    execution_lifecycle_status: str
    dispatch_status: str
    evidence_status: str
    output_roots: list[str]
    artifact_manifest_reference: str | None
    adapter_metadata: dict[str, Any]
    # ── artifact manifest 參照（additive、選填，尾端欄位）──────────
    # 舊 vendor-neutral execution contract receipt 不含這些欄位，反序列化後為 None——刻意保持
    # backward-compatible，且**不回寫**任何歷史 receipt。
    # artifact lineage 新產生的 receipt 三者必須非空。
    artifact_manifest_id: str | None = None
    artifact_manifest_sha256: str | None = None
    artifact_capture_status: str | None = None
    # adapter 選擇性 `cleanup()` 的結果（尾端、選填，backward-compatible，
    # 沿用 artifact lineage artifact capture 欄位同一種新增慣例）。與 `process_exit_code`／
    # `execution_lifecycle_status` 完全獨立——清理是不是成功，不影響、也不
    # 由「工具本身跑得怎樣」這幾個既有欄位代表。
    cleanup_result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.execution_lifecycle_status not in _LIFECYCLE_STATES:
            raise ExecutionContractError(
                f"invalid execution_lifecycle_status: {self.execution_lifecycle_status!r}"
            )
        if self.dispatch_status not in _DISPATCH_STATES:
            raise ExecutionContractError(
                f"invalid dispatch_status: {self.dispatch_status!r}"
            )
        if self.evidence_status not in _EVIDENCE_STATES:
            raise ExecutionContractError(
                f"invalid evidence_status: {self.evidence_status!r}"
            )

    def to_json_dict(self) -> dict[str, Any]:
        """回傳可直接 `json.dumps` 的 dict。不保存 secret environment values
        （由呼叫端在建構 receipt 前就不得把敏感 env 塞進 `adapter_metadata`；
        本方法不做二次過濾，信任呼叫端已遵守這項限制）。"""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "parent_attempt_or_retry_reference": self.parent_attempt_or_retry_reference,
            "tool_id": self.tool_id,
            "event_tool_name": self.event_tool_name,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "executor_identity": self.executor_identity,
            "tool_version_evidence": self.tool_version_evidence,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "process_exit_code": self.process_exit_code,
            "timed_out": self.timed_out,
            "termination_reason": self.termination_reason,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "execution_lifecycle_status": self.execution_lifecycle_status,
            "dispatch_status": self.dispatch_status,
            "evidence_status": self.evidence_status,
            "output_roots": self.output_roots,
            "artifact_manifest_reference": self.artifact_manifest_reference,
            "adapter_metadata": self.adapter_metadata,
            "artifact_manifest_id": self.artifact_manifest_id,
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "artifact_capture_status": self.artifact_capture_status,
            "cleanup_result": self.cleanup_result,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_json_dict(), indent=indent, sort_keys=True)


#: 前景 CLI 中斷分支的 step terminal reason（見 `kernel.py`）。
TERMINAL_REASON_INTERRUPTED = "interrupted"

_DISPATCH_BARRIER_TO_RECEIPT = {
    "completed": "completed",
    "not_ready": "not_attempted",
    "timeout": "timeout",
    "failed": "failed",
}
_EVIDENCE_BARRIER_TO_RECEIPT = {
    "ready": "ready",
    "not_ready": "not_ready",
    "timeout": "timeout",
    "failed": "not_ready",
    "completed": "ready",
}


def build_receipt(
    *,
    run_id: str,
    step_outcome: Any,
    event_tool_name: str,
    adapter_id: str,
    attempt_reference: str | None = None,
    adapter_version: str | None = None,
) -> ExecutionReceipt:
    """把一個 Kernel `StepOutcome`（見 `kernel.py`）組成 `ExecutionReceipt`。

    刻意接受 duck-typed `step_outcome`（而非 import `kernel.StepOutcome`），
    避免 `execution_contract.py` 反向依賴 `kernel.py`（Implementation
    Constraint #4：common contract 不得反向依賴任何單一 domain/adapter，
    這裡進一步避免與 Kernel 產生循環 import）。
    """
    subprocess_result = step_outcome.subprocess_result
    prepared = step_outcome.prepared_execution
    artifact_discovery = step_outcome.artifact_discovery

    dispatch_status = _DISPATCH_BARRIER_TO_RECEIPT.get(
        step_outcome.dispatch_barrier.state, "failed"
    )
    evidence_status = _EVIDENCE_BARRIER_TO_RECEIPT.get(
        step_outcome.evidence_barrier.state, "not_ready"
    )
    lifecycle_status = (
        "completed"
        if step_outcome.terminal_reason == "step_complete"
        else (
            "timeout"
            if subprocess_result is not None and subprocess_result.timed_out
            # 前景 CLI 已確認的中斷（Kernel 的 interrupted 分支）。
            else "cancelled" if step_outcome.terminal_reason == TERMINAL_REASON_INTERRUPTED
            else "failed"
        )
    )
    # 收到 signal 時工具尚未啟動：沒有 process，就沒有 exit code。
    process_started = getattr(subprocess_result, "process_started", True)

    adapter_metadata: dict[str, Any] = dict(prepared.adapter_metadata) if prepared else {}
    tool_version_evidence = adapter_metadata.pop("tool_version_evidence", {})
    # `adapter_version` 優先取呼叫端明示值，其次取 adapter 自報的
    # `adapter_metadata["adapter_version"]`（見 `native_adapter.py`／
    # `orfs_adapter.py`），都沒有才維持舊行為的 None（歷史 receipt 相容）。
    resolved_adapter_version = adapter_version or adapter_metadata.get("adapter_version")

    output_roots: list[str] = []
    artifact_manifest_reference = None
    if artifact_discovery is not None:
        output_roots = [str(root) for root in artifact_discovery.output_roots]
        artifact_manifest_reference = artifact_discovery.artifact_manifest_reference
        adapter_metadata["artifact_candidates"] = [
            {
                "adapter_role": candidate.adapter_role,
                "host_path": str(candidate.host_path),
                "exists": candidate.exists,
            }
            for candidate in artifact_discovery.artifact_candidates
        ]

    # artifact capture 結果（duck-typed）。vendor-neutral execution contract 的 `StepOutcome`
    # 沒有這個屬性時 `getattr` 回 None，receipt 行為與舊版完全相同。
    artifact_capture = getattr(step_outcome, "artifact_capture", None)
    artifact_manifest_id = None
    artifact_manifest_sha256 = None
    artifact_capture_status = None
    if artifact_capture is not None:
        artifact_manifest_id = artifact_capture.manifest_id
        artifact_manifest_sha256 = artifact_capture.manifest_sha256
        artifact_capture_status = artifact_capture.capture_status
        if artifact_capture.manifest_reference is not None:
            # Capture 產生的 durable manifest reference 優先於 adapter 自報值
            # （adapter 本來就回 None；artifact identity 不歸 adapter）。
            artifact_manifest_reference = artifact_capture.manifest_reference

    # 清理結果（duck-typed，同一種 backward-compatible 慣例）。舊
    # `StepOutcome`／沒有實作 `cleanup()` 的 adapter 一律是 None，receipt
    # 行為與新增前完全相同。
    cleanup_result = getattr(step_outcome, "cleanup_result", None)

    return ExecutionReceipt(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        step_id=step_outcome.step_id,
        attempt=step_outcome.attempt,
        parent_attempt_or_retry_reference=attempt_reference,
        tool_id=step_outcome.tool_id,
        event_tool_name=event_tool_name,
        adapter_id=adapter_id,
        adapter_version=resolved_adapter_version,
        executor_identity="terminux_engine.orchestration.subprocess_executor.SubprocessExecutor",
        tool_version_evidence=tool_version_evidence,
        started_at=subprocess_result.started_at if subprocess_result else None,
        finished_at=subprocess_result.finished_at if subprocess_result else None,
        process_exit_code=(subprocess_result.exit_code
                           if subprocess_result and process_started else None),
        timed_out=subprocess_result.timed_out if subprocess_result else False,
        termination_reason=step_outcome.terminal_reason,
        stdout_tail=list(subprocess_result.stdout_tail) if subprocess_result else [],
        stderr_tail=list(subprocess_result.stderr_tail) if subprocess_result else [],
        execution_lifecycle_status=lifecycle_status,
        dispatch_status=dispatch_status,
        evidence_status=evidence_status,
        output_roots=output_roots,
        artifact_manifest_reference=artifact_manifest_reference,
        adapter_metadata=adapter_metadata,
        artifact_manifest_id=artifact_manifest_id,
        artifact_manifest_sha256=artifact_manifest_sha256,
        artifact_capture_status=artifact_capture_status,
        cleanup_result=cleanup_result,
    )
