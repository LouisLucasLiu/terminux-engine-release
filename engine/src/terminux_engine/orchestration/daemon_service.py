# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Daemon 端的 orchestration child process registry。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, TypedDict

from terminux_engine.ipc.request_context import (
    CanonicalRequestContext,
    RequestContextMissingError,
    get_current_request_context,
)
from terminux_engine.orchestration.evidence_bundle import (
    EvidenceBundle,
    ReceiptIntegrity,
    caller_execution_evidence,
    load_published_eda_convergence_bundle,
    make_unavailable_evidence_bundle,
    receipt_integrity_of,
)
from terminux_engine.orchestration.checkpoint import (
    CheckpointValidationError,
    _validate_json_native,
    decode_checkpoint_v1,
)
from terminux_engine.orchestration.ledger import (
    ConditionalRunResult,
    OrchestrationLedger,
    RetryForkResult,
    RunRecord,
)
from terminux_engine.orchestration.process_group import (
    ProcessGroupSignalResult,
    signal_owned_process_group,
)
from terminux_engine.orchestration.status_cache import AgentStatusCache


_LOG = logging.getLogger(__name__)
_MAX_CONFIG_BYTES = 4096

# Evidence Set 的固定 v1 邊界。
_EVIDENCE_SET_SCHEMA_VERSION = "terminux.evidence_set.v1"
_EVIDENCE_SET_MIN_RUNS = 2
_EVIDENCE_SET_MAX_RUNS = 3
_EVIDENCE_SET_MAX_BYTES = 512 * 1024
_EVIDENCE_SET_COMPARISON_FOCUS = frozenset({"status", "steps", "dqe", "lineage", "all"})


class RunTimestamps(TypedDict):
    """Canonical RunView 的 durable timestamp 欄位。"""

    started_at: float | None
    finished_at: float | None
    last_heartbeat_at: float | None
    pause_requested_at: float | None
    cancel_requested_at: float | None


class RunView(TypedDict):
    """供 JSON-RPC、CLI 與 Agent 共用的 durable run projection。"""

    run_id: str
    workflow_type: str
    status: str
    current_step_id: str | None
    iteration: int | None
    checkpoint_safe_point: Literal["before_step", "after_step"] | None
    terminal_reason: str | None
    parent_run_id: str | None
    recovery_disposition: str | None
    child_pid: int | None
    timestamps: RunTimestamps


class StatusResponse(TypedDict):
    run: RunView


class ListRunsResponse(TypedDict):
    runs: list[RunView]


class EvidenceBundleResponse(TypedDict):
    evidence: EvidenceBundle


class EvidenceSet(TypedDict):
    """scope 內最多三個 run 的 canonical Evidence Bundle 集合。Core 只
    做 scope enforcement／組裝／bounded transport／audit，不判斷 EDA domain
    因果——跨 run 的工程判斷留在 Agent 層。"""

    schema_version: str
    requested_run_ids: list[str]
    run_ids_ordered: list[str]
    comparison_focus: str
    bundles: dict[str, EvidenceBundle]
    field_coverage: dict[str, dict[str, bool]]
    missing_evidence: list[str]
    conflicts: list[str]
    truncated: bool
    aggregate_sufficiency: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]


class EvidenceSetResponse(TypedDict):
    evidence_set: EvidenceSet


class StartResponse(TypedDict):
    ack: Literal[True]
    applied: bool
    run: RunView
    run_id: str


class PauseResponse(TypedDict):
    ack: Literal[True]
    applied: bool
    run: RunView
    effective_when: Literal["after_current_step"]


class ResumeResponse(TypedDict):
    ack: Literal[True]
    applied: bool
    run: RunView
    spawned_child_pid: int | None


class CancelResponse(TypedDict):
    ack: Literal[True]
    applied: bool
    run: RunView
    mode: Literal["soft", "immediate"]


class RetryResponse(TypedDict):
    ack: Literal[True]
    applied: bool
    run: RunView
    new_run_id: str | None


class AbandonResponse(TypedDict):
    ack: Literal[True]
    applied: bool
    run: RunView


class OrchestrationApiError(RuntimeError):
    """跨 surface 共用的 application error。"""

    def __init__(self, code: int, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class ResumeSpawnResult:
    """resume child 的派生結果；durable ownership 仍由 child claim 決定。"""

    accepted: bool
    run_id: str
    status: str
    child_pid: int | None


class OrchestrationService:
    """追蹤 daemon 派生的 orchestration children。"""

    def __init__(
        self,
        ledger: OrchestrationLedger,
        *,
        python_executable: str | None = None,
        child_module: str = "terminux_engine.orchestration.child_run",
        status_cache: AgentStatusCache | None = None,
        parent_daemon_socket: str | None = None,
        receipt_projection_reader: Any | None = None,
        execution_evidence_resolver: Any | None = None,
        timing_path_reader: Any | None = None,
        timing_path_reparsers: Any | None = None,
    ) -> None:
        self._ledger = ledger
        #: 可選的 `run_id -> list[TimingPathRow]` reader（`timing_evidence.record_reader`）。
        #: 只在 receipt 投影路徑上被查詢；未注入時 bundle 沒有 `timing_path` 鍵，行為不變。
        self._timing_path_reader = timing_path_reader
        #: `adapter_role -> reparser` 對應表：把 timing record 綁回已驗證 report bytes 時用；
        #: 沒有對應的 adapter 一律 `unavailable`。
        self._timing_path_reparsers = dict(timing_path_reparsers or {})
        #: 唯讀 Evidence Resolver。只用來在 evidence bundle 上附加四軸判定；
        #: 未注入時 bundle 的 `execution_evidence` 為 `None`，其餘欄位完全不變。
        self._execution_evidence_resolver = execution_evidence_resolver
        #: 可選的 `run_id -> list[dict]` reader，讀 immutable execution receipt
        #: projection。只在 ledger 不認識該 run 時被查詢；**不是**第二套
        #: lifecycle state，也不會被寫入。未注入時行為完全不變。
        self._receipt_projection_reader = receipt_projection_reader
        self._python_executable = python_executable or sys.executable
        self._child_module = child_module
        self._registered_runs: dict[str, int] = {}
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._lock = threading.Lock()
        self._status_cache = status_cache if status_cache is not None else AgentStatusCache()
        # B1：daemon 實際 bind 的 socket path，私有 process contract，
        # 不進 durable public workflow config，不新增 CLI option 或 JSON-RPC field。
        self._parent_daemon_socket = parent_daemon_socket

    def _child_argv_endpoint(self) -> list[str]:
        """回傳供 child argv 附加的 `--parent-daemon-socket <exact path>`；未設定時為空。"""
        if not self._parent_daemon_socket:
            return []
        return ["--parent-daemon-socket", self._parent_daemon_socket]

    @staticmethod
    def _invalid_params(message: str) -> None:
        raise OrchestrationApiError(-32602, message)

    @staticmethod
    def _require_string(value: object, *, name: str) -> str:
        if type(value) is not str:
            OrchestrationService._invalid_params(f"{name} 必須為 string")
        return value

    @staticmethod
    def _require_optional_string(value: object, *, name: str) -> str | None:
        if value is None:
            return None
        return OrchestrationService._require_string(value, name=name)

    @staticmethod
    def _require_bool(value: object, *, name: str) -> bool:
        if type(value) is not bool:
            OrchestrationService._invalid_params(f"{name} 必須為 boolean")
        return value

    @staticmethod
    def _require_object(value: object, *, name: str) -> dict[str, Any]:
        if type(value) is not dict:
            OrchestrationService._invalid_params(f"{name} 必須為 object")
        return value

    def _api_run_or_raise(self, run_id: str) -> RunRecord:
        try:
            return self._get_run_or_raise(run_id)
        except KeyError as exc:
            raise OrchestrationApiError(-32004, "unknown run") from exc

    @staticmethod
    def _caller_context_or_raise() -> CanonicalRequestContext:
        """取得目前 request 的 canonical caller；缺少時 fail-closed。"""
        try:
            return get_current_request_context()
        except RequestContextMissingError as exc:
            raise OrchestrationApiError(-32011, "invalid session context") from exc

    def _api_run_for_caller_or_raise(
        self,
        run_id: str,
        caller: CanonicalRequestContext,
    ) -> RunRecord:
        """只回傳目前 member/project namespace 可見的 run。"""
        run = self._ledger.get_run_for_scope(
            run_id,
            member_id=caller.member_id,
            project_id=caller.project_id,
        )
        if run is None:
            raise OrchestrationApiError(-32004, "unknown run")
        return run

    @staticmethod
    def _metadata_with_scope(
        config: dict[str, Any],
        caller: CanonicalRequestContext,
    ) -> dict[str, Any]:
        """建立 root run 固定 scope，caller config 不可覆寫此區塊。"""
        return {
            "orchestration": {
                "schema_version": 1,
                "workflow_config": config,
                "scope": {
                    "schema_version": 1,
                    "project_id": caller.project_id,
                    "created_session_id": caller.session_id,
                },
            }
        }

    def _fail_task_run_before_start(
        self, run_id: str, binding: Any, caller: CanonicalRequestContext, *,
        failure_phase: str, error: str, workflow_type: str = "orfs_single_run",
    ) -> None:
        """bounded utilization search 的 host-only 收尾。**不是**新的公開 IPC 入口。

        只給 `start_task_run()` 內、child 確定沒有被啟動的路徑呼叫；條件由
        ledger 端在同一個 transaction 內驗證，start/claim 搶先時會被拒絕。
        這裡不吞掉任何原始錯誤——收尾失敗只記 log。
        """
        try:
            self._ledger.fail_pending_run_before_start(
                run_id,
                expected_workflow_type=workflow_type,
                expected_owner=caller.member_id,
                expected_project_id=caller.project_id,
                expected_task_id=binding.task_id,
                expected_slot=binding.slot,
                expected_creator_instance=None,
                failure_phase=failure_phase,
                error=error,
            )
        except Exception as cleanup_exc:  # noqa: BLE001 - 收尾盡力而為
            _LOG.warning(
                "could not record the prelaunch failure for run %s: %s",
                run_id, cleanup_exc)

    def _project_run(self, run: RunRecord) -> RunView:
        """建立唯一 canonical RunView；checkpoint 一律採嚴格還原。"""

        current_step_id: str | None = None
        iteration: int | None = None
        checkpoint_safe_point: Literal["before_step", "after_step"] | None = None
        if run.kernel_checkpoint_json is not None:
            if run.domain_run_id is None:
                raise OrchestrationApiError(
                    -32603,
                    f"run lacks domain identity for checkpoint projection: {run.run_id}",
                )
            try:
                restored = decode_checkpoint_v1(
                    run.kernel_checkpoint_json,
                    expected_domain_run_id=run.domain_run_id,
                )
            except (CheckpointValidationError, ValueError) as exc:
                raise OrchestrationApiError(
                    -32603,
                    f"checkpoint projection failed: {run.run_id}",
                ) from exc
            current_step_id = restored.current_step_id
            iteration = restored.domain_state.iteration
            checkpoint_safe_point = restored.safe_point
        return {
            "run_id": run.run_id,
            "workflow_type": run.workflow_type,
            "status": run.status,
            "current_step_id": current_step_id,
            "iteration": iteration,
            "checkpoint_safe_point": checkpoint_safe_point,
            "terminal_reason": run.terminal_reason,
            "parent_run_id": run.parent_run_id,
            "recovery_disposition": run.recovery_disposition,
            "child_pid": run.child_pid,
            "timestamps": {
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "last_heartbeat_at": run.last_heartbeat_at,
                "pause_requested_at": run.pause_requested_at,
                "cancel_requested_at": run.cancel_requested_at,
            },
        }

    def start_request(self, workflow: str, config: dict[str, Any]) -> StartResponse:
        """以 shared contract 建立 normal run。"""

        workflow = self._require_string(workflow, name="workflow")
        config = self._require_object(config, name="config")
        caller = self._caller_context_or_raise()
        try:
            run_id = self.spawn_run(workflow, config)
        except OrchestrationApiError:
            raise
        except (KeyError, ValueError) as exc:
            raise OrchestrationApiError(-32603, "start request failed") from exc
        run = self._api_run_for_caller_or_raise(run_id, caller)
        return {"ack": True, "applied": True, "run": self._project_run(run), "run_id": run_id}

    def status_request(
        self,
        run_id: str,
        *,
        caller_kind: Literal["agent"] | None = None,
    ) -> StatusResponse:
        """讀取 durable RunView；`caller_kind="agent"` 時經 Agent-only cache
        包住完整成功結果，`None` 永遠 fresh。"""

        run_id = self._require_string(run_id, name="run_id")
        caller = self._caller_context_or_raise()
        if caller_kind not in {None, "agent"}:
            self._invalid_params("caller_kind 無效")

        def _load() -> RunView:
            return self._project_run(self._api_run_for_caller_or_raise(run_id, caller))

        if caller_kind == "agent":
            return {"run": self._status_cache.get_or_load(run_id, _load)}
        return {"run": _load()}

    def list_runs_request(
        self,
        *,
        workflow_type: str | None = None,
        status: str | None = None,
        pending_review: bool = False,
        limit: int = 20,
    ) -> ListRunsResponse:
        """以 canonical projection 列出 runs。"""

        workflow_type = self._require_optional_string(workflow_type, name="workflow_type")
        status = self._require_optional_string(status, name="status")
        pending_review = self._require_bool(pending_review, name="pending_review")
        if type(limit) is not int or not 1 <= limit <= 100:
            self._invalid_params("limit 必須為 1 至 100 的 integer")
        if pending_review and status not in {None, "abandoned"}:
            self._invalid_params("pending_review 只可與 abandoned status 併用")
        caller = self._caller_context_or_raise()
        runs = self._ledger.list_runs(
            workflow_type=workflow_type,
            status="abandoned" if pending_review else status,
            recovery_disposition="pending_review" if pending_review else None,
            member_id=caller.member_id,
            project_id=caller.project_id,
            limit=limit,
        )
        return {"runs": [self._project_run(run) for run in runs]}

    def evidence_bundle_request(self, run_id: str) -> EvidenceBundleResponse:
        """讀取目前 caller scope 內 run 的已發布 Evidence Bundle（caller 可見投影）。

        scope lookup 必須先於任何 metadata/workspace access；不支援 workflow、
        尚未建立 domain identity 或 metadata 不完整時也回固定 unavailable
        schema，避免把 run existence、workspace 或設定細節洩漏給 caller。
        回傳內容不含任何描述「其他 member 是否寫過同 run id 的列」的欄位；
        需要 integrity veto 的 LLM tool 層改用 `evidence_bundle_request_with_integrity`。
        """
        return self.evidence_bundle_request_with_integrity(run_id)[0]

    def evidence_bundle_request_with_integrity(
        self, run_id: str,
    ) -> tuple[EvidenceBundleResponse, ReceiptIntegrity]:
        """同 `evidence_bundle_request`，另附 daemon 內部的 receipt integrity 判定。

        第二個元素只能留在行程內（LLM tool 層以 `task_scope.veto_run_evidence` 轉成 Gate 拒送），
        不得序列化到 JSON-RPC 回應、transcript、request 或 log；第一個元素與
        `evidence_bundle_request` 完全相同。
        """
        run_id = self._require_string(run_id, name="run_id")
        caller = self._caller_context_or_raise()
        try:
            run = self._api_run_for_caller_or_raise(run_id, caller)
        except OrchestrationApiError:
            # ledger 不認識這個 run。改查 immutable receipt projection——
            # 只有**全部由 caller 本人寫入**的 receipt 列才能構成 bundle；外人列、混合
            # owner 或寫入者身分不可得時，一律維持 unknown run 行為（不洩漏存在性）。
            receipts = self._owned_receipt_projection(run_id, caller.member_id)
            if not receipts:
                raise
            from terminux_engine.orchestration.evidence_bundle import (
                build_timing_path_section,
                make_receipt_projection_evidence_bundle,
                read_bound_dqe_items,
            )
            bundle = make_receipt_projection_evidence_bundle(run_id, receipts=receipts)
            evidence = self._resolve_execution_evidence(run_id, ledger_run=None)
            # 只有 Resolver 判定 DQE 綁得回這次執行（Q=bound）時才補 DQE。
            if evidence is not None and evidence.dqe == "bound" and evidence.dqe_db_path:
                bundle["dqe"] = read_bound_dqe_items(Path(evidence.dqe_db_path), run_id)
            if self._timing_path_reader is not None:
                # 單一 setup path 證據：與 DQE 分開的 section，身分核對在 builder 內；
                # 讀不到 record 一律 unavailable，不影響其餘欄位。
                bundle["timing_path"] = build_timing_path_section(
                    self._read_timing_path_rows(run_id), orchestration_run_id=run_id,
                    latest_receipt=receipts[-1], member_id=caller.member_id,
                    execution_evidence=evidence, reparsers=self._timing_path_reparsers)
            return self._with_execution_evidence(bundle, evidence)
        if run.workflow_type == "orfs_single_run":
            # 本 workflow 的 bundle 由 ledger（lifecycle）加上
            # durable receipt projection（execution evidence）組成，
            # **不再**回 workflow_unsupported。
            from terminux_engine.orchestration.evidence_bundle import (
                make_flow_task_run_evidence_bundle,
            )

            metadata = run.metadata if isinstance(run.metadata, dict) else {}
            agent_task = metadata.get("agent_task")
            return self._ledger_bundle_response(make_flow_task_run_evidence_bundle(
                run_id, status=run.status, terminal_reason=run.terminal_reason,
                agent_task=agent_task if isinstance(agent_task, dict) else {},
                receipts=self._owned_receipt_projection(run_id, caller.member_id),
            ), run)
        if run.workflow_type != "eda_convergence":
            return self._ledger_bundle_response(
                make_unavailable_evidence_bundle(
                    run_id,
                    status=run.status,
                    terminal_reason=run.terminal_reason,
                    reason="workflow_unsupported",
                ),
                run,
            )
        if run.domain_run_id is None:
            return self._ledger_bundle_response(
                make_unavailable_evidence_bundle(
                    run_id,
                    status=run.status,
                    terminal_reason=run.terminal_reason,
                    reason="domain_identity_unavailable",
                ),
                run,
            )
        try:
            config = self._workflow_config_from_metadata(run)
            workspace = config.get("workspace")
            if type(workspace) is not str or not workspace:
                raise ValueError("invalid workspace")
            evidence = load_published_eda_convergence_bundle(
                workspace=Path(workspace),
                eco_run_id=run.domain_run_id,
                orchestration_run_id=run_id,
                status=run.status,
                terminal_reason=run.terminal_reason,
            )
        except (OSError, ValueError):
            evidence = make_unavailable_evidence_bundle(
                run_id,
                status=run.status,
                terminal_reason=run.terminal_reason,
                reason="bundle_unavailable",
            )
        return self._ledger_bundle_response(evidence, run)

    def _ledger_bundle_response(
        self, bundle: EvidenceBundle, run: RunRecord,
    ) -> tuple[EvidenceBundleResponse, ReceiptIntegrity]:
        """ledger 認得的 run：附上以同一筆（已套用 caller 範圍的）ledger run 判定的四軸。"""
        evidence = self._resolve_execution_evidence(run.run_id, ledger_run=run)
        return self._with_execution_evidence(bundle, evidence)

    def _resolve_execution_evidence(self, run_id: str, *, ledger_run: Any) -> Any:
        """以注入的唯讀 Evidence Resolver 判定；未注入或失敗時回 `None`，不猜。"""
        resolver = self._execution_evidence_resolver
        if resolver is None:
            return None
        try:
            return resolver.resolve(run_id, ledger_run=ledger_run)
        except Exception:  # noqa: BLE001 - 判定失敗不得影響既有 bundle 欄位
            _LOG.warning("execution evidence resolution failed for %s", run_id,
                         exc_info=True)
            return None

    @staticmethod
    def _with_execution_evidence(
        bundle: EvidenceBundle, evidence: Any,
    ) -> tuple[EvidenceBundleResponse, ReceiptIntegrity]:
        """欄位只增不減。沒有判定結果時為 `None`，既有欄位與語意不變。

        附加的是 Resolver 結果的 **caller 可見投影**（`caller_execution_evidence`）：其他
        member 寫入同 run id 的列的數量與由它派生的 reason 不進入 bundle；該訊號只以
        `ReceiptIntegrity` 回傳給行程內的呼叫端。
        """
        result = dict(bundle)
        result["execution_evidence"] = caller_execution_evidence(evidence)
        return {"evidence": result}, receipt_integrity_of(evidence)  # type: ignore[typeddict-item]

    def evidence_set_request(
        self,
        run_ids: list[Any],
        *,
        comparison_focus: str = "all",
    ) -> EvidenceSetResponse:
        """讀取目前 caller scope 內 2～3 個 run 的 Evidence Bundle 集合。

        逐一委派既有 `evidence_bundle_request()`（因此沿用同一 scope
        enforcement、同一 unavailable schema）；任何一個 run 的 scope lookup
        失敗（不存在或 foreign scope）一律收斂成同一種 unavailable bundle，
        呼叫端無法區分兩者。deterministic ordering 由 run_id 字串排序決定，
        與 request 給的順序無關。size 超過 512 KiB 上限時，只捨棄排不進去的
        完整 bundle 並標示 `truncated=True`，不靜默裁切單一 bundle 的內容。
        """
        if type(run_ids) is not list:
            self._invalid_params("run_ids 必須為 list")
        if not (_EVIDENCE_SET_MIN_RUNS <= len(run_ids) <= _EVIDENCE_SET_MAX_RUNS):
            self._invalid_params(
                f"run_ids 必須包含 {_EVIDENCE_SET_MIN_RUNS} 至 {_EVIDENCE_SET_MAX_RUNS} 個 run id"
            )
        normalized: list[str] = []
        for item in run_ids:
            normalized.append(self._require_string(item, name="run_ids[]"))
        if len(set(normalized)) != len(normalized):
            self._invalid_params("run_ids 不得重複")
        if comparison_focus not in _EVIDENCE_SET_COMPARISON_FOCUS:
            self._invalid_params(
                f"comparison_focus 必須是 {sorted(_EVIDENCE_SET_COMPARISON_FOCUS)} 其中之一"
            )
        # caller scope 由每一次 evidence_bundle_request() 各自驗證；此處不重複
        # 取用 request context，避免與底層 scope 檢查的取用時序產生分歧。
        ordered_run_ids = sorted(normalized)

        bundles: dict[str, EvidenceBundle] = {}
        for run_id in ordered_run_ids:
            try:
                bundles[run_id] = self.evidence_bundle_request(run_id)["evidence"]
            except OrchestrationApiError:
                bundles[run_id] = make_unavailable_evidence_bundle(
                    run_id, status="unknown", terminal_reason=None, reason="unavailable",
                )

        truncated = False
        included_run_ids: list[str] = []
        running_bytes = 0
        for run_id in ordered_run_ids:
            serialized = json.dumps(bundles[run_id], ensure_ascii=False, sort_keys=True)
            candidate_bytes = running_bytes + len(serialized.encode("utf-8"))
            if candidate_bytes > _EVIDENCE_SET_MAX_BYTES and included_run_ids:
                truncated = True
                break
            included_run_ids.append(run_id)
            running_bytes = candidate_bytes

        field_coverage: dict[str, dict[str, bool]] = {}
        missing_evidence: list[str] = []
        available_count = 0
        any_available = False
        for run_id in included_run_ids:
            bundle = bundles[run_id]
            field_coverage[run_id] = {
                "dqe": bundle["dqe"]["status"] == "available",
                "reports": bundle["reports"]["status"] == "available",
                "lineage": bundle["lineage"]["status"] == "available",
            }
            if bundle["availability"] == "unavailable":
                missing_evidence.append(run_id)
            else:
                any_available = True
            if bundle["availability"] == "available":
                available_count += 1

        if truncated or available_count < len(included_run_ids):
            aggregate: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"] = (
                "PARTIAL" if any_available else "INSUFFICIENT"
            )
        else:
            aggregate = "COMPLETE"

        return {
            "evidence_set": {
                "schema_version": _EVIDENCE_SET_SCHEMA_VERSION,
                "requested_run_ids": normalized,
                "run_ids_ordered": included_run_ids,
                "comparison_focus": comparison_focus,
                "bundles": {run_id: bundles[run_id] for run_id in included_run_ids},
                "field_coverage": field_coverage,
                "missing_evidence": missing_evidence,
                # Core 不判斷 EDA domain 因果；跨 run 語意衝突偵測留給 Agent 層，
                # 這裡只保留固定空陣列（結構性欄位，非 domain 判斷）。
                "conflicts": [],
                "truncated": truncated,
                "aggregate_sufficiency": aggregate,
            }
        }

    def _read_receipt_projection(self, run_id: str) -> list[Any]:
        reader = self._receipt_projection_reader
        if reader is None:
            return []
        try:
            return list(reader(run_id))
        except Exception:  # noqa: BLE001 - 讀不到就當作沒有，不猜
            return []

    @staticmethod
    def _owned_receipt_rows(rows: list[Any], member_id: str) -> list[dict]:
        """在組 bundle **之前**只留下 caller 本人寫入的 receipt 投影列。

        reader 回傳的列必須帶寫入者身分（`ReceiptRow`：`payload` ＋ `user_id`）；舊式只回 payload
        dict 的 reader 無法證明寫入者，其列一律不採用。其他 member 寫入的列不進入 bundle 的任何
        欄位：caller 不能因為知道 run id 就拿到別人的執行事實；只有外人列時 run 視為不存在。
        本人列與外人列並存時，Evidence Resolver 內部仍會計入外人列；該計數不進 caller 可見的
        bundle（`caller_execution_evidence`），只以 `ReceiptIntegrity` 交給 LLM tool 層在第一個
        可外送位元組前拒送。
        """
        owned: list[dict] = []
        for row in rows:
            payload = getattr(row, "payload", None)
            user_id = getattr(row, "user_id", None)
            if isinstance(payload, dict) and isinstance(user_id, str) and user_id == member_id:
                owned.append(payload)
        return owned

    def _owned_receipt_projection(self, run_id: str, member_id: str) -> list[dict]:
        return self._owned_receipt_rows(self._read_receipt_projection(run_id), member_id)

    def _read_timing_path_rows(self, run_id: str) -> list[Any]:
        reader = self._timing_path_reader
        if reader is None:
            return []
        try:
            return list(reader(run_id))
        except Exception:  # noqa: BLE001 - 讀不到就當作沒有 record，section 會是 unavailable
            return []

    def pause_request(self, run_id: str) -> PauseResponse:
        """以 shared contract 提出 pause boundary request。"""

        run_id = self._require_string(run_id, name="run_id")
        caller = self._caller_context_or_raise()
        self._api_run_for_caller_or_raise(run_id, caller)
        try:
            result = self.pause_run(run_id)
        except KeyError as exc:
            raise OrchestrationApiError(-32004, "unknown run") from exc
        return {
            "ack": True,
            "applied": result.applied,
            "run": self._project_run(result.run),
            "effective_when": "after_current_step",
        }

    def _reject_if_task_owned(self, run: Any, operation: str) -> None:
        """task-owned run 的 `recovery_policy=observe_only`。

        公開的 `resume`／`retry` 一律拒絕——既有 recovery 可以觀察／標示
        中斷，但不得自動發起新執行。合法的 scoped cancel 不受影響。

        `run` 是呼叫端已經做過 scope 檢查的 record，這裡不重新查詢。
        """
        if self.is_task_owned_run(run):
            raise OrchestrationApiError(
                -32602,
                f"{operation} is not available for task-owned runs "
                "(recovery_policy=observe_only); use the task controls instead")

    def resume_request(self, run_id: str) -> ResumeResponse:
        """以 shared contract 派生 resume child，false path 不洩漏 PID。"""

        run_id = self._require_string(run_id, name="run_id")
        caller = self._caller_context_or_raise()
        self._reject_if_task_owned(
            self._api_run_for_caller_or_raise(run_id, caller), "resume")
        try:
            result = self.resume_run(run_id)
        except KeyError as exc:
            raise OrchestrationApiError(-32004, "unknown run") from exc
        except ValueError as exc:
            raise OrchestrationApiError(-32603, "resume source data invalid") from exc
        run = self._api_run_for_caller_or_raise(run_id, caller)
        return {
            "ack": True,
            "applied": result.accepted,
            "run": self._project_run(run),
            "spawned_child_pid": result.child_pid if result.accepted else None,
        }

    def cancel_request(
        self,
        run_id: str,
        *,
        immediate: bool = False,
    ) -> CancelResponse:
        """以 durable 首個 accepted cancel mode 回應 request。"""

        run_id = self._require_string(run_id, name="run_id")
        immediate = self._require_bool(immediate, name="immediate")
        caller = self._caller_context_or_raise()
        self._api_run_for_caller_or_raise(run_id, caller)
        try:
            result = self.cancel_run(run_id, immediate=immediate)
        except KeyError as exc:
            raise OrchestrationApiError(-32004, "unknown run") from exc
        durable_mode = result.run.cancel_immediate
        mode: Literal["soft", "immediate"] = (
            "immediate" if durable_mode == 1 else "soft"
        )
        return {
            "ack": True,
            "applied": result.applied,
            "run": self._project_run(result.run),
            "mode": mode,
        }

    def retry_request(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
    ) -> RetryResponse:
        """以 parent RunView 與唯一 child identity 回應 retry fork。"""

        run_id = self._require_string(run_id, name="run_id")
        step_id = self._require_optional_string(step_id, name="step_id")
        caller = self._caller_context_or_raise()
        self._reject_if_task_owned(
            self._api_run_for_caller_or_raise(run_id, caller), "retry")
        try:
            result = self.retry_run(run_id, step_id=step_id)
        except KeyError as exc:
            raise OrchestrationApiError(-32004, "unknown run") from exc
        except ValueError as exc:
            raise OrchestrationApiError(-32603, "retry source data invalid") from exc
        return {
            "ack": True,
            "applied": result.applied,
            "run": self._project_run(result.parent),
            "new_run_id": None if result.child is None else result.child.run_id,
        }

    def abandon_request(self, run_id: str) -> AbandonResponse:
        """將 abandoned + pending_review 處置為 dismissed。"""

        run_id = self._require_string(run_id, name="run_id")
        caller = self._caller_context_or_raise()
        self._api_run_for_caller_or_raise(run_id, caller)
        try:
            result = self._ledger.dismiss_pending_review_if_abandoned(run_id)
        except KeyError as exc:
            raise OrchestrationApiError(-32004, "unknown run") from exc
        return {
            "ack": True,
            "applied": result.applied,
            "run": self._project_run(result.run),
        }

    #: 只能由 host 的 `start_task_run()` 啟動的 workflow。
    #: 公開入口（`orchestration.start`／Agent tool／CLI）缺 live task binding
    #: 時一律拒絕——否則 task-owned 的 EDA 能力就有一條旁路。
    TASK_ONLY_WORKFLOWS: ClassVar[frozenset[str]] = frozenset({
        "orfs_single_run", "package_single_tool"})

    def spawn_run(self, workflow: str, config: dict[str, Any]) -> str:
        """建立 ledger run、派生 child，並登記其 PID。

        這是公開 `start` 的下層。task-owned workflow 在這裡就 fail closed：
        它們沒有 live `TaskExecutionBinding`，因此不得從這條路徑啟動。
        """
        if workflow in self.TASK_ONLY_WORKFLOWS:
            raise OrchestrationApiError(
                -32602,
                f"workflow {workflow!r} can only be started by an approved flow "
                "task step with a live host binding; it is not available from "
                "the public start entry")
        # optional workflow 未安裝時，在建立 run 與派生 child 之前就拒絕。
        from terminux_engine.orchestration import runner_factory

        if not runner_factory.is_workflow_installed(workflow):
            _LOG.info("orchestration start rejected: workflow %s is not installed", workflow)
            raise OrchestrationApiError(
                -32602,
                f"workflow {workflow!r} is not installed",
                data={"reason": runner_factory.WORKFLOW_NOT_INSTALLED, "workflow": workflow},
            )
        self._validate_workflow_config(config)
        caller = self._caller_context_or_raise()
        config_json = json.dumps(config, allow_nan=False, sort_keys=True)
        if len(config_json.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise ValueError("orchestration config exceeds 4KB CLI argument limit")

        run_id = self._ledger.create_run(
            workflow_type=workflow,
            domain_run_id=None,
            owner=caller.member_id,
            metadata=self._metadata_with_scope(config, caller),
        )
        self._ledger.start_run(run_id)
        argv = [
            self._python_executable,
            "-m",
            self._child_module,
            "--run-id",
            run_id,
            "--ledger-path",
            str(self._ledger.db_path),
            "--workflow",
            workflow,
            "--config-json",
            config_json,
            *self._child_argv_endpoint(),
        ]
        try:
            process = subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=os.environ.copy(),
                cwd=os.getcwd(),
            )
        except OSError:
            self._ledger.complete_run(
                run_id,
                final_status="failed",
                terminal_reason="spawn_failed",
            )
            raise

        try:
            self._ledger.update_child_pid(run_id, process.pid)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _LOG.warning("Child did not exit after PID write failure: %s", run_id)
            self._ledger.complete_run(
                run_id,
                final_status="failed",
                terminal_reason="spawn_failed",
            )
            raise

        self._register_spawned_process(run_id, process)
        threading.Thread(
            target=self._watch_child,
            args=(run_id, process),
            daemon=True,
            name=f"orchestration-child-{run_id}",
        ).start()
        _LOG.info("Orchestration child spawned: run_id=%s pid=%s", run_id, process.pid)
        return run_id

    # ── host-only 的 task-owned run 啟動 ──────────────────

    def start_task_run(
        self,
        binding: Any,
        slot: str,
        on_run_created: Any,
    ) -> str:
        """為一個 live flow task binding 啟動一次受限的 ORFS run。

        **host-only**：`binding` 必須是 host 建立的 `TaskExecutionBinding`
        物件；序列化 dict 一律拒絕，provider／caller 無法自己組一份。

        固定順序（不可調換）：

        1. 驗 binding 型別與 slot；workflow config 完全由 binding 推導，
           caller 不提供任何一欄。
        2. 在正式 ledger 建立 run identity 與 scope，`metadata.agent_task`
           記 task_id／slot／grant_id／task_digest／profile_digest／
           input_manifest_hash——這個子樹由 host 組出，caller 不能覆寫。
        3. **child 啟動前**呼叫 `on_run_created(run_ref)`，讓 task service
           成功持久化 RunRef。寫入失敗就**不啟動 child**，保留可查的
           未啟動記錄。
        4. 只有 ledger 與 task 關聯都已提交才啟動 child，回 run_id。

        同 task/slot 只容許一次 start——額度預留與重複檢查由 task service
        在自己的鎖內完成；失敗不得透過新 proposal 重取同 slot。
        """
        from terminux_engine.bounded_execution.analysis_tasks.flow_models import (
            RECOVERY_POLICY_OBSERVE_ONLY,
            SLOT_BASELINE,
            SLOT_CANDIDATE,
            RunRef,
            TaskExecutionBinding,
        )
        from terminux_engine.bounded_execution.analysis_tasks.canonical import now_ms
        from terminux_engine.orchestration.orfs_single_run_runner import (
            WORKFLOW_TYPE as ORFS_SINGLE_RUN_WORKFLOW,
            validate_orfs_single_run_workflow_config,
        )
        from terminux_engine.orchestration.runner_factory import (
            PACKAGE_SINGLE_TOOL_WORKFLOW,
        )

        if not isinstance(binding, TaskExecutionBinding):
            raise OrchestrationApiError(
                -32602, "start_task_run requires a host-built TaskExecutionBinding")
        # 候選 slot 由 template 決定。slot 名稱本身**不是**授權——它必須
        # 與 host 建立的 live binding 完全相同，binding 才是唯一的執行依據。
        from terminux_engine.bounded_execution.analysis_tasks.flow_models import (
            SLOT_CANDIDATE_1,
            SLOT_CANDIDATE_2,
        )

        if (slot not in (SLOT_BASELINE, SLOT_CANDIDATE, SLOT_CANDIDATE_1,
                         SLOT_CANDIDATE_2)
                or slot != binding.slot):
            raise OrchestrationApiError(-32602, f"invalid slot: {slot!r}")
        if not callable(on_run_created):
            raise OrchestrationApiError(-32602, "on_run_created must be callable")

        caller = self._caller_context_or_raise()
        if (caller.member_id != binding.member_id
                or caller.project_id != binding.project_id):
            raise OrchestrationApiError(
                -32602, "the caller scope does not match the task binding")

        # 1. workflow config 完全由 binding 推導。
        workflow_type = (PACKAGE_SINGLE_TOOL_WORKFLOW
                         if binding.package_tool is not None else ORFS_SINGLE_RUN_WORKFLOW)
        if binding.package_tool is not None:
            from terminux_engine.package_runtime.approved_local_tool import (
                validate_package_single_tool_config,
            )

            tool = binding.package_tool
            adapter_config = tool.get("adapter_config") if isinstance(tool, dict) else None
            if isinstance(adapter_config, dict):
                # Package 已把 descriptor adapter 完整釘進 task digest；host 只
                # 驗 task-owned root／step，不依 module、method 或工具名稱分支。
                config = dict(adapter_config)
                if (config.get("output_root") != binding.output_root
                        or config.get("artifact_root") != binding.artifact_root
                        or config.get("step_id") != binding.step_id
                        or config.get("timeout_seconds") != float(binding.run_timeout_sec)):
                    raise OrchestrationApiError(
                        -32602, "package adapter config differs from the task binding")
            else:
                candidate_file = Path(binding.output_root) / tool["materialized_relative_path"]
                config = {
                    "tool_id": tool["tool_id"], "executable": tool["executable"],
                    "executable_sha256": tool["executable_sha256"],
                    "argument_template": tool["argument_template"],
                    "materialized_sha256": tool["materialized_sha256"],
                    "artifact_paths": tool["artifact_paths"],
                    "candidate_value": tool["candidate_value"],
                    "candidate_file": str(candidate_file),
                    "output_root": binding.output_root,
                    "artifact_root": binding.artifact_root,
                    "timeout_seconds": float(binding.run_timeout_sec),
                    "step_id": binding.step_id,
                }
            validate_package_single_tool_config(config)
        else:
            native_binding = binding.native_tool_binding_sha256
            from terminux_engine.orchestration import legacy_docker_execution as LDE

            denial = (LDE.flow_task_execution_denial(binding.execution)
                      if native_binding is None else None)
            if denial is not None:
                # 舊 container 模式或舊 host runner；ledger run、輸出目錄與 child
                # 都還沒有建立。
                raise OrchestrationApiError(-32602, denial.message,
                                            data=denial.to_json_dict())
            config = {
                "snapshot_flow_dir": str(Path(binding.input_root) / "flow"),
                "output_root": binding.output_root,
                # 舊 Profile 綁 image；host-native Profile 綁工具 binding digest。
                **({"native_tool_binding_sha256": native_binding} if native_binding
                   else {"docker_image_id": binding.docker_image_id}),
                "design": binding.design_id,
                "platform": binding.platform_id,
                "variant": binding.variant,
                "design_config_relpath": binding.design_config_relpath,
                "step_id": binding.step_id,
                "timeout_seconds": float(binding.run_timeout_sec),
            }
        # 證據 sinks 同樣是 task-owned binding 的一部分。這個 workflow 沒有
        # public start 入口，且值從已核准 Profile snapshot 取得，child 因此不會
        # 因隔離 daemon 的 HOME 而悄悄落到另一份 DQE／manifest storage。
        if binding.dqe_db_path:
            if binding.package_tool is None:
                config["dqe_db_path"] = binding.dqe_db_path
            else:
                # `event_metadata` 是 SingleToolRunner 的 opaque 通道；Common
                # Runtime 不解讀 DQE domain，只傳遞 task-owned sink path。
                config["event_metadata"] = {"dqe_db_path": binding.dqe_db_path}
        if binding.artifact_root:
            config["artifact_root"] = binding.artifact_root
        # 唯讀的功能驗證參考。路徑由 host 從 binding 取得，
        # 不接受 caller 提供。
        if binding.verification_reference_dir and binding.package_tool is None:
            config["reference_dir"] = binding.verification_reference_dir
        if binding.package_tool is None:
            validate_orfs_single_run_workflow_config(config)
        # run id 是 host 在 child 前配置的 Kernel identity。descriptor adapter
        # 若有可選 binding hook，僅能把自己的 proof draft 綁到此 id；Common
        # Runtime 不依 method／verdict 分支，其他 package config 位元不變。
        planned_run_id = f"run_{uuid.uuid4().hex}"
        # 只有 **descriptor** binding 有這個 hook。既有的單工具 config
        # （tool_id／executable／argument_template…）沒有 adapter descriptor，
        # 把它送進 descriptor 驗證只會在 child 之前就被判成「未知欄位」，
        # 於是那條路徑一個 child 都啟動不了。
        if binding.package_tool is not None and config.get("schema_version") == \
                "package.adapter_binding.v1":
            from terminux_engine.package_runtime.approved_local_tool import (
                bind_package_run_identity,
            )

            from terminux_engine.orchestration import legacy_docker_execution as LDE

            try:
                config = bind_package_run_identity(config, {
                    "run_id": planned_run_id, "task_id": binding.task_id,
                    "task_digest": binding.task_digest, "grant_id": binding.grant_id,
                })
            except LDE.LegacyDockerExecutionDisabled as exc:
                raise OrchestrationApiError(-32602, exc.message, data=exc.to_json_dict())
        self._validate_workflow_config(config)

        # 本 slot 的可寫輸出區由 **host** 準備：Adapter 以它當 cwd，且
        # container 把它掛成 WORK_HOME。沒有先建立的話 child 會在
        # `Popen(cwd=...)` 就死掉，看起來像沒有原因的 child_died。
        # inputs 是唯讀的，永遠不會被這裡碰到。
        try:
            Path(binding.output_root).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OrchestrationApiError(
                -32603, f"the slot output directory could not be created: {exc}")
        config_json = json.dumps(config, allow_nan=False, sort_keys=True)
        # task-owned descriptor plans 可以大於 argv 安全上限；完整 immutable
        # config 已在同一 ledger metadata 保存，child 因此可讀回它而非截斷。
        inline_config = config_json if len(config_json.encode("utf-8")) <= _MAX_CONFIG_BYTES else None

        # 2. ledger run identity ＋ scope ＋ 不可被覆寫的 agent_task 子樹。
        metadata = self._metadata_with_scope(config, caller)
        metadata["agent_task"] = binding.agent_task_metadata()
        metadata["agent_task"]["recovery_policy"] = RECOVERY_POLICY_OBSERVE_ONLY
        run_id = self._ledger.create_run(
            run_id=planned_run_id,
            workflow_type=workflow_type,
            domain_run_id=None,
            owner=caller.member_id,
            metadata=metadata,
        )

        # 3. child 啟動前先讓 task service 持久化 RunRef。
        run_ref = RunRef(run_id=run_id, slot=slot, task_id=binding.task_id,
                         workflow_type=workflow_type,
                         created_at_ms=now_ms())
        try:
            on_run_created(run_ref)
        except Exception as exc:
            # child **沒有**被啟動。bounded utilization search：用受限的 pending -> failed 如實
            # 終結，不送 `start_run()` 去記一個從未發生的接管事實。
            # 收尾本身失敗時只記 log，**絕不**覆蓋原始例外。
            self._fail_task_run_before_start(
                run_id, binding, caller, failure_phase="run_link_not_persisted",
                error=repr(exc), workflow_type=workflow_type)
            _LOG.warning(
                "task run link not persisted; child_started=false (run_id=%s): %s",
                run_id, exc)
            raise

        # 4. 兩邊都提交後才啟動 child。
        self._ledger.start_run(run_id)
        argv = [
            self._python_executable, "-m", self._child_module,
            "--run-id", run_id,
            "--ledger-path", str(self._ledger.db_path),
            "--workflow", workflow_type,
            *(["--config-json", inline_config] if inline_config is not None else []),
            *self._child_argv_endpoint(),
        ]
        try:
            process = subprocess.Popen(
                argv, start_new_session=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=os.environ.copy(), cwd=os.getcwd(),
            )
        except OSError:
            self._ledger.complete_run(
                run_id, final_status="failed", terminal_reason="spawn_failed")
            raise
        try:
            self._ledger.update_child_pid(run_id, process.pid)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _LOG.warning("Child did not exit after PID write failure: %s", run_id)
            self._ledger.complete_run(
                run_id, final_status="failed", terminal_reason="spawn_failed")
            raise
        self._register_spawned_process(run_id, process)
        threading.Thread(
            target=self._watch_child, args=(run_id, process), daemon=True,
            name=f"orchestration-child-{run_id}",
        ).start()
        _LOG.info("task-owned orchestration child spawned: run_id=%s slot=%s pid=%s",
                  run_id, slot, process.pid)
        return run_id

    @staticmethod
    def is_task_owned_run(run: Any) -> bool:
        """這筆 run 是否由 flow task 擁有。

        task-owned run 的 `recovery_policy=observe_only`——公開的
        `resume`／`retry` 一律拒絕，既有 recovery 只能觀察／標示中斷。
        """
        metadata = getattr(run, "metadata", None)
        if not isinstance(metadata, dict):
            return False
        agent_task = metadata.get("agent_task")
        return isinstance(agent_task, dict) and bool(agent_task.get("task_id"))

    def pause_run(self, run_id: str) -> ConditionalRunResult:
        """只接受 pause request；checkpoint 與 child 結束由 kernel boundary 處理。"""

        return self._ledger.request_pause_if_running(run_id)

    def resume_run(self, run_id: str) -> ResumeSpawnResult:
        """派生同一 run 的 resume child；child 的 conditional claim 決定唯一 owner。"""

        run = self._get_run_or_raise(run_id)
        if run.status != "paused":
            return ResumeSpawnResult(
                accepted=False,
                run_id=run_id,
                status=run.status,
                child_pid=self.get_child_pid(run_id),
            )
        if run.kernel_checkpoint_json is None:
            raise ValueError(f"paused run lacks checkpoint: {run_id}")
        self._workflow_config_from_metadata(run)

        with self._lock:
            registered_pid = self._registered_runs.get(run_id)
            if registered_pid is not None:
                return ResumeSpawnResult(
                    accepted=False,
                    run_id=run_id,
                    status=run.status,
                    child_pid=registered_pid,
                )
            argv = [
                self._python_executable,
                "-m",
                self._child_module,
                "--run-id",
                run_id,
                "--ledger-path",
                str(self._ledger.db_path),
                "--workflow",
                run.workflow_type,
                "--resume",
                *self._child_argv_endpoint(),
            ]
            process = subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=os.environ.copy(),
                cwd=os.getcwd(),
            )
            self._registered_runs[run_id] = process.pid
            self._processes[run_id] = process

        threading.Thread(
            target=self._watch_child,
            args=(run_id, process),
            daemon=True,
            name=f"orchestration-resume-{run_id}",
        ).start()
        _LOG.info("Orchestration resume child spawned: run_id=%s pid=%s", run_id, process.pid)
        return ResumeSpawnResult(
            accepted=True,
            run_id=run_id,
            status=run.status,
            child_pid=process.pid,
        )

    def retry_run(
        self,
        parent_run_id: str,
        *,
        step_id: str | None = None,
    ) -> RetryForkResult:
        """從 terminal parent fork 唯一 retry child，durable ownership 由 child claim。"""

        result = self._ledger.fork_retry_from_terminal(
            parent_run_id,
            new_run_id=str(uuid.uuid4()),
            expected_step_id=step_id,
        )
        if not result.applied:
            return result
        if result.child is None:
            raise RuntimeError("retry fork succeeded without child run")

        child = result.child
        self._workflow_config_from_metadata(child)
        argv = [
            self._python_executable,
            "-m",
            self._child_module,
            "--run-id",
            child.run_id,
            "--ledger-path",
            str(self._ledger.db_path),
            "--workflow",
            child.workflow_type,
            "--retry",
            *self._child_argv_endpoint(),
        ]
        try:
            process = subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=os.environ.copy(),
                cwd=os.getcwd(),
            )
        except OSError:
            self._ledger.fail_retry_spawn_if_unstarted(child.run_id)
            raise

        self._register_spawned_process(child.run_id, process)
        threading.Thread(
            target=self._watch_child,
            args=(child.run_id, process),
            daemon=True,
            name=f"orchestration-retry-{child.run_id}",
        ).start()
        _LOG.info(
            "Orchestration retry child spawned: parent_run_id=%s run_id=%s pid=%s",
            parent_run_id,
            child.run_id,
            process.pid,
        )
        return result

    def cancel_run(
        self,
        run_id: str,
        *,
        immediate: bool = False,
    ) -> ConditionalRunResult:
        """接受 cancel；硬 cancel 僅由 conditional winner 取得 group signal 權。"""

        request = self._ledger.request_cancel_if_active(run_id, immediate=immediate)
        if not immediate or not request.applied or request.run.status != "running":
            return request

        child_pid = request.run.child_pid
        if child_pid is None:
            self._release_package_resources(run_id)
            return self._finalize_missing_immediate_cancel(run_id, request)

        signal_result = signal_owned_process_group(child_pid)
        if signal_result is ProcessGroupSignalResult.SENT:
            self._wait_for_direct_child(run_id, expected_pid=child_pid)
            self.unregister_run(run_id, expected_pid=child_pid)
            # runner 已被 SIGKILL，它自己的 finally 不會執行；由仍存活的管理端
            # 依 child 派發前就寫進 ledger 的 run config 代為釋放外部資源。
            self._release_package_resources(run_id)
            return self._ledger.finalize_cancel_if_requested(
                run_id,
                immediate=True,
                checkpoint_json=None,
            )
        if signal_result is ProcessGroupSignalResult.MISSING:
            self.unregister_run(run_id, expected_pid=child_pid)
            self._release_package_resources(run_id)
            return self._finalize_missing_immediate_cancel(run_id, request)

        _LOG.error(
            "Immediate cancel accepted but process group is not owned: run_id=%s pid=%s",
            run_id,
            child_pid,
        )
        return request

    def _release_package_resources(self, run_id: str) -> None:
        """immediate cancel 後，代被終止的 runner 釋放本 run 的外部資源。

        身分只取自這筆 run 在 child 派發前已持久化的 workflow config
        （`package.adapter_binding.v1`，含已綁定 run id 的 task），不掃描其他
        run 或容器；沒有 descriptor binding 或 adapter 沒有 release hook 時不做
        任何事。結果（含失敗）以獨立的 `resource_cleanup` transition 記錄，
        不改寫 run 的 terminal 結果，也不重派工；任何例外都只記錄、不外拋，
        取消本身照常完成。
        """
        from terminux_engine.orchestration.runner_factory import (
            PACKAGE_SINGLE_TOOL_WORKFLOW,
        )

        try:
            run = self._get_run_or_raise(run_id)
            if run.workflow_type != PACKAGE_SINGLE_TOOL_WORKFLOW:
                return
            config = self._workflow_config_from_metadata(run)
            if config.get("schema_version") != "package.adapter_binding.v1":
                return
            from terminux_engine.package_runtime.approved_local_tool import (
                release_package_run_resources,
            )

            result = release_package_run_resources(config)
        except Exception as exc:  # noqa: BLE001 - 清理失敗獨立記錄，不影響取消
            result = {"attempted": True, "ok": False,
                      "detail": "resource release raised before completion",
                      "error_type": type(exc).__name__}
        if result is None:
            return
        try:
            self._ledger.record_resource_cleanup(
                run_id, ok=result.get("ok") is True,
                detail={"source": "manager_release_after_cancel", **result})
        except Exception:  # noqa: BLE001
            _LOG.exception("resource cleanup record not persisted: run_id=%s", run_id)
        if result.get("ok") is not True:
            _LOG.error("package resource release failed after cancel: run_id=%s", run_id)

    def get_child_pid(self, run_id: str) -> int | None:
        """由記憶體 registry 讀取 child PID。"""
        with self._lock:
            return self._registered_runs.get(run_id)

    def register_existing_run(self, run_id: str, child_pid: int) -> None:
        """將既有 child 加入記憶體 registry。"""
        with self._lock:
            self._registered_runs[run_id] = child_pid
        _LOG.info("Orchestration child registered: run_id=%s pid=%s", run_id, child_pid)

    def unregister_run(self, run_id: str, *, expected_pid: int | None = None) -> None:
        """從 registry 移除指定 child；舊 watcher 不得刪除新 PID。"""

        with self._lock:
            child_pid = self._registered_runs.get(run_id)
            if child_pid is None or (expected_pid is not None and child_pid != expected_pid):
                return
            self._registered_runs.pop(run_id)
            self._processes.pop(run_id, None)
        _LOG.info("Orchestration child unregistered: run_id=%s pid=%s", run_id, child_pid)

    def list_registered_runs(self) -> dict[str, int]:
        """回傳 registry 快照。"""
        with self._lock:
            return dict(self._registered_runs)

    def shutdown_registered_children(self) -> dict[str, str]:
        """daemon 結束前終止它實際派生的 child process groups。

        child 以新 session 執行，故只停止 daemon 本身會把 EDA child 留成 orphan。
        這裡只處理本 instance 記憶體 registry 的 direct children，不掃描 PID、
        不碰歷史/recovery process；ledger 不捏造 terminal receipt，後續讀回仍會
        如實判為未確認。
        """
        with self._lock:
            registered = dict(self._registered_runs)
        results: dict[str, str] = {}
        for run_id, pid in registered.items():
            try:
                outcome = signal_owned_process_group(pid)
                results[run_id] = outcome.value
            except (OSError, ValueError) as exc:
                results[run_id] = f"error:{type(exc).__name__}"
            self.unregister_run(run_id, expected_pid=pid)
        if results:
            _LOG.warning("Orchestration shutdown terminated registered children: %s", results)
        return results

    def _watch_child(self, run_id: str, process: subprocess.Popen[Any]) -> None:
        """等待 child 結束後清理 daemon registry。"""
        process.wait()
        self.unregister_run(run_id, expected_pid=process.pid)

    def _register_spawned_process(
        self,
        run_id: str,
        process: subprocess.Popen[Any],
    ) -> None:
        """登記 daemon 自己派生的 direct child，供硬 cancel／scanner wait-reap。"""

        with self._lock:
            self._registered_runs[run_id] = process.pid
            self._processes[run_id] = process
        _LOG.info("Orchestration child registered: run_id=%s pid=%s", run_id, process.pid)

    def wait_for_registered_child(self, run_id: str, *, expected_pid: int) -> None:
        """若 PID 是本 daemon 的 direct child，等待 watcher 可安全 reaping 的同一 process。"""

        with self._lock:
            process = self._processes.get(run_id)
            if process is None or process.pid != expected_pid:
                return
        process.wait()

    def _wait_for_direct_child(self, run_id: str, *, expected_pid: int) -> None:
        """硬 cancel 專用 wrapper，避免對 recovery re-attach PID 做 waitpid。"""

        self.wait_for_registered_child(run_id, expected_pid=expected_pid)

    def _finalize_missing_immediate_cancel(
        self,
        run_id: str,
        request: ConditionalRunResult,
    ) -> ConditionalRunResult:
        """MISSING 分支只在同一 immediate request 仍為 durable winner 時終結。"""

        current = self._get_run_or_raise(run_id)
        if current.status != "running":
            return ConditionalRunResult(applied=False, run=current)
        if current.cancel_requested_at is None or current.cancel_immediate != 1:
            return ConditionalRunResult(applied=False, run=current)
        return self._ledger.finalize_cancel_if_requested(
            run_id,
            immediate=True,
            checkpoint_json=None,
        )

    @staticmethod
    def _validate_workflow_config(config: dict[str, Any]) -> None:
        """拒絕不能安全持久化或傳給 child 的 workflow config。"""

        if type(config) is not dict:
            raise ValueError("workflow config 必須為 object")
        _validate_json_native(config, path="$.orchestration.workflow_config")

    def _get_run_or_raise(self, run_id: str) -> RunRecord:
        run = self._ledger.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        return run

    @classmethod
    def _workflow_config_from_metadata(cls, run: RunRecord) -> dict[str, Any]:
        """讀取正式 workflow metadata，不猜測舊 child argv 或記憶體狀態。"""

        metadata = run.metadata
        if type(metadata) is not dict:
            raise ValueError(f"run lacks orchestration metadata: {run.run_id}")
        orchestration = metadata.get("orchestration")
        if type(orchestration) is not dict:
            raise ValueError(f"run lacks orchestration metadata: {run.run_id}")
        schema_version = orchestration.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError(f"unsupported orchestration metadata schema: {run.run_id}")
        config = orchestration.get("workflow_config")
        cls._validate_workflow_config(config)
        return config
