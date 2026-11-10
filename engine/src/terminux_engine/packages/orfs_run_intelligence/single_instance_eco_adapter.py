# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`gcd.single_instance_buffer_swap.v1` 的正式 `ExecutionAdapter`。

把 Package 已核准、已釘住的 swap task 轉成 Kernel 可直接執行的 Docker
argv（沿用 single-instance ECO 已驗證的 `single_instance_eco.build_standalone_modify_and_save_tcl()`／
`build_standalone_readback_verification_tcl()`／`build_two_operation_batch_argv()`），
再把執行完的產物揭露給既有 `OrchestrationKernel`。

架構完全沿用既有 vendor-neutral execution contract／vendor-neutral execution contract vendor-neutral execution contract
（`orchestration/execution_contract.py`）與既有 `formal_pair.py::FormalPairToolAdapter`
的既定寫法——同一種 `ExecutionAdapter` Protocol、同一種
`package.adapter_binding.v1` 綁定機制、同一個
`OrchestrationKernel`／`SubprocessExecutor`／`ToolRegistry`。本檔案**不**
新建第二套 dispatch／lifecycle／cancel 機制。

工具操作成功（`process_exit_code`）與工程驗收合格（`evaluate_acceptance()`）
刻意分開保存：`prepare()`／`discover_artifacts()` 只負責客觀事實（沿用
`ExecutionReceipt` 的「不宣告 domain success」），`collect_and_verify_swap_result()`
是另一個、在 receipt 產生之後才呼叫的獨立步驟，讀已發現的證據檔、呼叫既有
`evaluate_acceptance()`，缺 proof 就是 `UNVERIFIABLE`，不因為 place 階段
成功就放寬。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from terminux_engine.expert_ai.eap3 import digest_json
from terminux_engine.orchestration.execution_contract import (
    ArtifactCandidate,
    ArtifactDiscoveryResult,
    ExecutionContractError,
    ExecutionRequest,
    PreparedExecution,
    validate_within_boundary,
)
from terminux_engine.orchestration.legacy_docker_execution import (
    PATH_SINGLE_INSTANCE_ECO,
    LegacyDockerExecutionDisabled,
)
from terminux_engine.packages.orfs_run_intelligence import (
    method_extensions as ME,
    repair_acceptance as RA,
    single_instance_eco as SIE,
)

#: 沿用 `formal_pair.py::TASK_SCHEMA` 同一種命名慣例，換成本方法自己的
#: schema 字串——不是同一個常數（proof task 與 swap task 是不同形狀）。
TASK_SCHEMA = "gcd.single_instance_buffer_swap.task.v1"

#: 容器內固定路徑（沿用 `single_instance_eco.py::build_two_operation_batch_argv()`
#: 測過的形狀——`/candidate` 唯一可寫掛載）。
CONTAINER_CANDIDATE_PATH = "/candidate"
CANDIDATE_ODB_FILENAME = "3_4_place_resized.odb"
OP1_SCRIPT_FILENAME = "op1_modify_and_save.tcl"
OP2_SCRIPT_FILENAME = "op2_readback_verify.tcl"
CIDFILE_FILENAME = "container.cid"
#: 管理端取消時寫在 task-owned root（容器唯一可寫掛載）裡的標記；容器內每個
#: OpenROAD 行程啟動前都會檢查它（見 `build_two_operation_batch_argv()`）。
CANCEL_MARKER_FILENAME = "terminux_cancel_requested"
#: host 解析出的 pin→net 會原樣放進 Tcl `list`；只接受不需要跳脫的字元，
#: 其他（空白、大括號、反斜線、引號）一律在建立 task 時拒絕，不猜跳脫規則。
_PIN_NET_RE = re.compile(r"^[A-Za-z_][\w$]*=[\w$\[\]./]*$")

_REQUIRED_IDENTITY_KEYS = (
    "work_id", "attempt_id", "task_id", "task_digest", "grant_id", "run_id")


class SingleInstanceEcoAdapterError(ExecutionContractError):
    """本 adapter 專屬的 fail-closed 例外；仍是 `ExecutionContractError`，
    `ExecutionAdapter` Protocol 的呼叫端（Kernel）不需要另外 catch 新型別。"""


def build_task(
    *, work_id: str, attempt_id: str, task_id: str, task_digest: str,
    grant_id: str, run_id: str, target_instance: str, expected_cell: str,
    replacement_cell: str, library: str, stage: str,
    source_candidate: dict[str, Any], verified_pre_swap_snapshot: dict[str, Any],
    task_root: str | Path, docker_image: str,
    flow_scripts_mount: str, source_results_dir_container: str,
    platform_dir_container: str, lib_files_container: str,
    source_design_file: str = "3_4_place_resized.odb",
    source_sdc_file: str = "2_floorplan.sdc", num_cores: int = 1,
    source_odb_host_path: str, source_odb_sha256: str,
) -> dict[str, Any]:
    """由 host 已核對過的 `repair_recipe`（`eap_extension.py::compile_plan()`）
    與已驗證的來源快照建立 swap task，落盤前重驗一次格式。

    `verified_pre_swap_snapshot` **必須**是 host 已驗證過的 instance 快照
    （例如靜態核對既有真實輸出得到的 `{"instances": {"input1": {"cell":
    "BUF_X1"}}}`）——不是 proposal 自稱的內容；`prepare()` 會用
    它呼叫既有 `check_source_drift()`，在**任何** dispatch 之前拒絕漂移，
    不是靠 Tcl 腳本自己的 `error` 事後失敗。

    single-instance ECO 正式鏈路：snapshot 必須帶 host 解析的 `target_pin_net` 與
    `provenance`，且 provenance 的 netlist digest 必須等於 proposal 宣告的
    `source_candidate.netlist_sha256`；`source_odb_host_path`／
    `source_odb_sha256` 是 host 在 proposal 階段核對過的來源 ODB，`prepare()`
    在派發前會重算一次。
    """
    identity = {"work_id": work_id, "attempt_id": attempt_id, "task_id": task_id,
                "task_digest": task_digest, "grant_id": grant_id, "run_id": run_id}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise SingleInstanceEcoAdapterError("swap task identity is incomplete")
    check = SIE.validate_single_instance_swap_change({
        "kind": SIE.CHANGE_KIND, "stage": stage, "library": library,
        "source_candidate": source_candidate, "target_instance": target_instance,
        "expected_cell": expected_cell, "replacement_cell": replacement_cell,
        "reason": "adapter task build", "evidence_reference": task_id,
    })
    if not check.passed:
        raise SingleInstanceEcoAdapterError(
            f"swap task change is not well formed: {check.reason_codes}")
    if (not isinstance(verified_pre_swap_snapshot, dict)
            or "instances" not in verified_pre_swap_snapshot):
        raise SingleInstanceEcoAdapterError(
            "verified_pre_swap_snapshot must be a host-verified instance snapshot")
    pin_nets = verified_pre_swap_snapshot.get("target_pin_net")
    provenance = verified_pre_swap_snapshot.get("provenance")
    if (not isinstance(pin_nets, list) or not pin_nets
            or any(not isinstance(item, str) or not _PIN_NET_RE.fullmatch(item)
                   for item in pin_nets)):
        raise SingleInstanceEcoAdapterError(
            "verified_pre_swap_snapshot lacks usable host-parsed connectivity")
    if (not isinstance(provenance, dict)
            or provenance.get("netlist_sha256") != source_candidate.get("netlist_sha256")
            or provenance.get("candidate_reference")
            != source_candidate.get("candidate_reference")):
        raise SingleInstanceEcoAdapterError(
            "verified_pre_swap_snapshot provenance differs from the source candidate")
    odb_path = Path(source_odb_host_path)
    if (not odb_path.is_absolute() or not isinstance(source_odb_sha256, str)
            or len(source_odb_sha256) != 64):
        raise SingleInstanceEcoAdapterError("source ODB identity is invalid")
    root = Path(task_root).resolve()
    if not root.is_absolute():
        raise SingleInstanceEcoAdapterError("swap task root is invalid")
    if not SIE.is_digest_pinned_image(docker_image):
        raise SIE.DockerImageNotDigestPinnedError(
            f"docker_image must be repo@sha256:<64 hex>, not a mutable tag: {docker_image!r}")
    task = {
        "schema_version": TASK_SCHEMA, **identity,
        "method_identity": SIE.METHOD_ID, "target_instance": target_instance,
        "expected_cell": expected_cell, "replacement_cell": replacement_cell,
        "library": library, "stage": stage, "source_candidate": dict(source_candidate),
        "verified_pre_swap_snapshot": verified_pre_swap_snapshot,
        "task_root": str(root), "docker_image": docker_image,
        "flow_scripts_mount": flow_scripts_mount,
        "source_results_dir_container": source_results_dir_container,
        "platform_dir_container": platform_dir_container,
        "lib_files_container": lib_files_container,
        "source_design_file": source_design_file, "source_sdc_file": source_sdc_file,
        "source_odb_host_path": str(odb_path), "source_odb_sha256": source_odb_sha256,
        "num_cores": num_cores,
        "timeout_seconds": SIE.EXECUTION_PHASE_TIMEOUT_SECONDS,
    }
    task["swap_plan_digest"] = digest_json(task)
    return task


def bind_kernel_run(
    task: dict[str, Any], run_id: str, *, task_digest: str | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """把已 grant 的 swap draft 綁到 daemon 預先配置的 Kernel run——與
    `formal_pair.py::bind_kernel_run()` 同一種 PENDING→實際值替換慣例。"""
    if (not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA
            or task.get("run_id") != "PENDING" or not isinstance(run_id, str)
            or not run_id.startswith("run_")):
        raise SingleInstanceEcoAdapterError("swap draft cannot bind run")
    if task_digest is not None and task.get("task_digest") != "PENDING":
        raise SingleInstanceEcoAdapterError("swap draft task is already bound")
    if grant_id is not None and task.get("grant_id") != "PENDING":
        raise SingleInstanceEcoAdapterError("swap draft grant is already bound")
    bound = dict(task)
    bound["run_id"] = run_id
    if task_digest is not None:
        bound["task_digest"] = task_digest
    if grant_id is not None:
        bound["grant_id"] = grant_id
    bound.pop("swap_plan_digest", None)
    bound["swap_plan_digest"] = digest_json(bound)
    return bound


def _validate_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA:
        raise SingleInstanceEcoAdapterError("swap task schema differs")
    recompute = dict(task)
    plan_digest = recompute.pop("swap_plan_digest", None)
    if plan_digest != digest_json(recompute):
        raise SingleInstanceEcoAdapterError("swap plan digest differs from task content")
    return task


def materialize_task(task: dict[str, Any]) -> dict[str, Any]:
    """把 single-instance ECO 已驗證的 Operation 1／2 Tcl 腳本寫進 task-owned root。

    只在這裡呼叫既有 `build_standalone_modify_and_save_tcl()`／
    `build_standalone_readback_verification_tcl()`——沿用 single-instance ECO 已驗證的
    腳本生成機制，不另寫一份。"""
    task = _validate_task(task)
    root = Path(str(task["task_root"])).resolve()
    # daemon 會在 Popen 前建立 task-owned cwd；只接受尚未含檔案的既有
    # root，避免重送覆寫先前 child 的輸出（沿用 formal_pair 的同一個規則）。
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise SingleInstanceEcoAdapterError("swap task root is not empty")
    root.mkdir(parents=True, exist_ok=True)
    candidate_odb_path = f"{CONTAINER_CANDIDATE_PATH}/{CANDIDATE_ODB_FILENAME}"
    op1_tcl = SIE.build_standalone_modify_and_save_tcl(
        target_instance=task["target_instance"], expected_cell=task["expected_cell"],
        replacement_cell=task["replacement_cell"], library=task["library"],
        candidate_odb_output_path=candidate_odb_path,
        source_design_file=task["source_design_file"],
        source_sdc_file=task["source_sdc_file"],
        expected_target_pin_net=list(
            task["verified_pre_swap_snapshot"]["target_pin_net"]))
    op2_tcl = SIE.build_standalone_readback_verification_tcl(
        target_instance=task["target_instance"],
        replacement_cell=task["replacement_cell"],
        candidate_odb_path=candidate_odb_path)
    (root / OP1_SCRIPT_FILENAME).write_text(op1_tcl, encoding="utf-8")
    (root / OP2_SCRIPT_FILENAME).write_text(op2_tcl, encoding="utf-8")
    return task


class SingleInstanceEcoSwapAdapter:
    """container 版 single-instance ECO 的 `ExecutionAdapter`。

    container 模式不再啟動新的 swap：`bind_kernel_identity()` 與 `prepare()`
    一律以 `LEGACY_DOCKER_EXECUTION_DISABLED` 拒絕（在 run identity 綁定、
    task 寫入與 docker argv 之前）。產物讀取與既有 container 的清理
    （`discover_artifacts()`、`cleanup()`、`release_external_resources()`）保留，
    只用來停止與收尾已在執行中的舊 run。
    """

    adapter_id = "orfs_single_instance_eco_swap"
    adapter_version = "orfs_single_instance_eco_swap/1"

    def __init__(
        self, *, docker_executable: str, docker_ops: "SIE.DockerOps | None" = None,
    ) -> None:
        executable = Path(docker_executable)
        if not executable.is_absolute() or not executable.is_file():
            raise SingleInstanceEcoAdapterError("swap Docker executable is invalid")
        self._docker = str(executable.resolve())
        # `docker_ops` 可替換（沿用 single-instance ECO 既有的 `DockerOps` 介面／
        # `SubprocessDockerOps` 真實實作，見 `single_instance_eco.py`）——
        # 受控測試用替身涵蓋清理成功／容器已不存在／清理失敗三種情況，不
        # 需要真的呼叫 `docker`；未指定時走真實實作，這裡不改變預設的
        # 正式行為。
        self._docker_ops: "SIE.DockerOps" = docker_ops or SIE.SubprocessDockerOps(
            self._docker)

    def bind_kernel_identity(
        self, config: dict[str, Any], identity: dict[str, str],
    ) -> dict[str, Any]:
        """container 模式不再綁定新的 swap run。"""
        del config, identity
        raise LegacyDockerExecutionDisabled(PATH_SINGLE_INSTANCE_ECO)

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        """container 模式不再派送：不 materialize task、不組 docker argv。"""
        del request
        raise LegacyDockerExecutionDisabled(PATH_SINGLE_INSTANCE_ECO)

    def discover_artifacts(
        self, request: ExecutionRequest, prepared: PreparedExecution, *,
        process_exit_code: int,
    ) -> ArtifactDiscoveryResult:
        """純粹的產物收集——**不做任何清理**（single-instance ECO：與 `cleanup()` 刻意分開，
        見該方法與 `ExecutionAdapter` Protocol 的說明）。沿用既有 Kernel
        行為：這個方法只在正常完成／dispatch 失敗兩條既有路徑被呼叫，
        timeout 路徑本來就不會呼叫它（`kernel.py::_execute_kernel_step_body()`
        沒有改這件事）——逾時不會因此被誤標成有產物、更不會被誤標成成功。
        """
        task = prepared.artifact_discovery_context.get("swap_task")
        if not isinstance(task, dict):
            raise SingleInstanceEcoAdapterError("swap task context is missing")
        root = Path(str(task["task_root"])).resolve()
        role_prefix = f"{SIE.METHOD_ID}."
        reports = root / "reports"
        reports_readback = root / "reports_readback"
        file_roles = (
            (reports / SIE.TOOL_VERSION_FILENAME, "tool_version"),
            (reports / SIE.ECO_DIFF_FILENAME, "eco_diff"),
            (reports / SIE.INSTANCE_SNAPSHOT_FILENAME, "instance_snapshot"),
            (reports / SIE.NEIGHBOR_SNAPSHOT_FILENAME, "neighbor_snapshot"),
            (reports_readback / SIE.READBACK_INSTANCE_SNAPSHOT_FILENAME,
             "readback_instance_snapshot"),
            (reports_readback / SIE.READBACK_NEIGHBOR_SNAPSHOT_FILENAME,
             "readback_neighbor_snapshot"),
            (root / CANDIDATE_ODB_FILENAME, "candidate_odb"),
        )
        candidates = [
            ArtifactCandidate(
                adapter_role=role_prefix + role,
                host_path=validate_within_boundary(path, request.output_boundary),
                exists=path.is_file())
            for path, role in file_roles
        ]
        return ArtifactDiscoveryResult(output_roots=[root], artifact_candidates=candidates)

    def cleanup(
        self, request: ExecutionRequest, prepared: PreparedExecution,
    ) -> dict[str, Any]:
        """資源清理——與 `discover_artifacts()` 刻意分開，由
        `ToolRegistry.cleanup_execution()`／`kernel.py::_execute_kernel_step()`
        的 `finally` 呼叫，覆蓋正常完成、dispatch 失敗、timeout、以及
        `prepare()` 成功之後任何未預期例外這四種情況（見該函式 docstring）；
        `prepare()` 本身失敗（zero dispatch）時完全不會被呼叫，因為那時
        還沒有任何副作用需要清理。

        只處理這個 run 自己的 container 身分，不掃描、不動任何其他
        container。清理結果由呼叫端（`ToolRegistry.cleanup_execution()`）寫進
        `StepOutcome.cleanup_result`／`ExecutionReceipt.cleanup_result`
        （見 `execution_contract.py`）durable 保存，這裡不需要自己另外
        寫檔——避免同一份清理結果同時活在兩個不同地方，之後容易漂移。

        single-instance ECO 正式鏈路：改以 run id 推導的容器名稱／label 清理
        （`SIE.release_batch_container()`），與管理端在 immediate cancel 後
        使用的 `release_external_resources()` 是同一套邏輯；cidfile 只是
        docker client 事後寫出的輔助檔，這裡順手移除。
        """
        context = prepared.artifact_discovery_context
        batch_id = context.get("batch_id")
        if not batch_id:
            return {"attempted": False, "ok": True,
                    "detail": "no batch identity in prepared execution context"}
        cidfile_path = context.get("cidfile_path")
        if cidfile_path:
            self._docker_ops.remove_file(str(cidfile_path))
        return SIE.release_batch_container(self._docker_ops, batch_id=str(batch_id))

    def release_external_resources(self, config: dict[str, Any]) -> dict[str, Any]:
        """管理端（daemon）在 runner 已被強制終止後呼叫的清理。

        只用 daemon 在 child 派發前寫進 ledger 的 bound config（本 run 的
        swap task）推導身分，不掃描其他容器：

        1. 先在 task-owned root 寫取消標記——之後才被 dockerd 建出來的遲到
           容器看到標記就不會啟動 OpenROAD（見 `build_two_operation_batch_argv()`）；
           尚未 `prepare()` 的 runner 也會因此零派發。
        2. 再依 `run_id` 推導的名稱／label 反覆移除並確認不存在。

        回傳清理結果；失敗如實回報，不拋例外、不重派工。"""
        task = _validate_task((config.get("parameters") or {}).get("swap_task"))
        run_id = str(task.get("run_id") or "")
        if not run_id.startswith("run_"):
            return {"attempted": False, "ok": True,
                    "detail": "swap task was never bound to a Kernel run"}
        root = Path(str(task["task_root"])).resolve()
        marker: dict[str, Any] = {"path": str(root / CANCEL_MARKER_FILENAME)}
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / CANCEL_MARKER_FILENAME).write_text(run_id + "\n", encoding="utf-8")
            marker["written"] = True
        except OSError as exc:
            marker.update({"written": False, "error": type(exc).__name__})
        result = SIE.release_batch_container(self._docker_ops, batch_id=run_id)
        result["cancel_marker"] = marker
        if not marker.get("written"):
            result["ok"] = False
        return result


def workflow_config(
    *, task: dict[str, Any], docker_executable: str, artifact_root: str, step_id: str,
) -> dict[str, Any]:
    """建立由 Package 簽入 binding 的 generic adapter config——與
    `formal_pair.py::workflow_config()` 同一種 `package.adapter_binding.v1`
    形狀（含 `artifact_root`，`package_runtime/approved_local_tool.py::
    _validate_descriptor_config()` 的必要欄位，之前漏掉這個欄位）。"""
    task = _validate_task(task)
    return {
        "schema_version": "package.adapter_binding.v1",
        "tool_id": "single_instance_eco_swap",
        "adapter_spec": (
            "terminux_engine.packages.orfs_run_intelligence."
            "single_instance_eco_adapter:SingleInstanceEcoSwapAdapter"),
        "adapter_kwargs": {"docker_executable": docker_executable},
        "parameters": {"swap_task": task},
        "host_executable": docker_executable,
        "output_root": task["task_root"],
        "artifact_root": artifact_root,
        "timeout_seconds": task["timeout_seconds"],
        "step_id": step_id,
    }


def _read_json_evidence(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    import json as _json

    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


#: 執行端從 artifact manifest 讀回、交給 `evaluate_swap_evidence()` 的證據
#: 角色（不含前綴 `SIE.METHOD_ID + "."`）。
EVIDENCE_ROLES = ("tool_version", "eco_diff", "instance_snapshot", "neighbor_snapshot",
                  "readback_instance_snapshot", "readback_neighbor_snapshot",
                  "candidate_odb")
VERDICT_SCHEMA = "gcd.single_instance_buffer_swap.verdict.v1"
#: single-instance ECO 只完成到 place 階段的有界操作；之後既定的流程步驟本 task 從不執行。
PENDING_REQUIRED_STEPS = ("legalization", "cts", "route", "final")


def evaluate_swap_evidence(
    *, task: dict[str, Any], process_exit_code: int | None, timed_out: bool,
    evidence: dict[str, Any], evidence_digests: dict[str, str],
    cleanup_result: dict[str, Any] | None, run_status: str = "completed",
) -> dict[str, Any]:
    """由已核對身分的證據算出這一次 swap 的工程判定（純函式）。

    工具操作是否完成（exit code／逾時／讀回一致）與工程驗收（`evaluate_acceptance()`）
    分開保存。驗收沿用既有 `ME.evaluate_acceptance(ME.SINGLE_INSTANCE_ECO, ...)`；
    目前沒有 baseline 功能 reference、也沒有執行任何 proof，因此該方法宣告的
    前置條件不成立時如實記成 `UNVERIFIABLE`，不因操作成功就放寬。
    """
    return evaluate_validated_swap(
        task=_validate_task(task), process_exit_code=process_exit_code,
        timed_out=timed_out, evidence=evidence, evidence_digests=evidence_digests,
        cleanup_result=cleanup_result, run_status=run_status)


def evaluate_validated_swap(
    *, task: dict[str, Any], process_exit_code: int | None, timed_out: bool,
    evidence: dict[str, Any], evidence_digests: dict[str, str],
    cleanup_result: dict[str, Any] | None, run_status: str = "completed",
) -> dict[str, Any]:
    """`evaluate_swap_evidence()` 的判定本體；呼叫端必須先依自己的 task schema
    核對過 `task`（container 與 host-native 兩種 task 共用同一套判定）。"""
    eco_diff = evidence.get("eco_diff")
    instance_snapshot = evidence.get("instance_snapshot")
    neighbor_snapshot = evidence.get("neighbor_snapshot")
    readback_instance = evidence.get("readback_instance_snapshot")
    readback_neighbor = evidence.get("readback_neighbor_snapshot")
    operation_completed = process_exit_code == 0 and not timed_out
    readback_verified = False
    mismatches: tuple[str, ...] = ()
    if operation_completed and all(isinstance(item, dict) for item in (
            eco_diff, instance_snapshot, neighbor_snapshot, readback_instance,
            readback_neighbor)):
        readback_verified, mismatches = SIE.compare_operation1_and_readback_snapshots(
            op1_neighbor_snapshot=neighbor_snapshot,
            readback_neighbor_snapshot=readback_neighbor,
            op1_instance_snapshot=instance_snapshot, op1_eco_diff=eco_diff,
            readback_instance_snapshot=readback_instance,
            target_instance=task["target_instance"])
    reason_codes: list[str] = []
    reasons: list[str] = []
    try:
        result = ME.evaluate_acceptance(
            ME.SINGLE_INSTANCE_ECO, baseline={},
            candidate={"setup_endpoints": None, "core": None, "eco_diff": eco_diff,
                       "eco_proof": None, "final_pair_proof": None,
                       "instance_snapshot": instance_snapshot},
            host_consumption=None, method={}, candidate_value="",
            metric_comparisons=[], execution_ok=bool(operation_completed
                                                     and readback_verified),
            inputs_approved=True, inputs_reason=None,
            baseline_reference_available=False)
        candidate_selected = result.candidate_selected
        repair_status = result.payload["repair_status"]
        reason_codes.extend(result.payload.get("reason_codes") or ())
        reasons.extend(result.payload.get("reasons") or ())
    except ME.MethodExtensionError as exc:
        candidate_selected = False
        repair_status = RA.UNVERIFIABLE
        reason_codes.append(exc.code)
        reasons.append(str(exc))
    required_proofs = list(ME.SINGLE_INSTANCE_ECO.required_proof_kinds or ())
    reason_codes.append("REQUIRED_PROOFS_NOT_RUN")
    reasons.append("required proofs were not executed: " + ", ".join(required_proofs))
    reason_codes.append("DOWNSTREAM_STEPS_NOT_RUN")
    reasons.append("pending required steps were not executed: "
                   + ", ".join(PENDING_REQUIRED_STEPS))
    if run_status == "cancelled":
        reason_codes.append("RUN_CANCELLED")
    elif process_exit_code is None and not timed_out:
        # run 已終結但沒有可核對的 receipt：不知道工具結果，不猜成失敗或成功。
        reason_codes.append("TOOL_RESULT_UNAVAILABLE")
    elif not operation_completed:
        reason_codes.append("TOOL_OPERATION_FAILED" if not timed_out
                            else "TOOL_OPERATION_TIMED_OUT")
    elif not readback_verified:
        reason_codes.append("READBACK_NOT_VERIFIED")
    if isinstance(cleanup_result, dict) and cleanup_result.get("ok") is False:
        reason_codes.append("RESOURCE_CLEANUP_FAILED")
    return {
        "schema_version": VERDICT_SCHEMA, "method_identity": SIE.METHOD_ID,
        "task_id": task["task_id"], "run_id": task["run_id"],
        "target_instance": task["target_instance"],
        "expected_cell": task["expected_cell"],
        "replacement_cell": task["replacement_cell"],
        "source_candidate": task["source_candidate"],
        "source_provenance": task["verified_pre_swap_snapshot"].get("provenance"),
        "source_odb_sha256": task["source_odb_sha256"],
        "tool_operation": {
            "run_status": run_status,
            "process_exit_code": process_exit_code, "timed_out": timed_out,
            "operation_completed": operation_completed,
            "cancelled_before_operation": (
                process_exit_code == SIE.CANCELLED_BEFORE_OPERATION_EXIT_CODE)},
        "readback_verified": readback_verified,
        "readback_mismatches": list(mismatches),
        "eco_diff": eco_diff, "instance_snapshot": instance_snapshot,
        "neighbor_snapshot": neighbor_snapshot,
        "evidence_digests": dict(sorted(evidence_digests.items())),
        "repair_status": repair_status,
        "claim_scope": "BOUNDED_TOOL_OPERATION_ONLY",
        "reason_codes": reason_codes, "reasons": reasons,
        "proof_status": "NOT_RUN",
        "production_readiness": {"ready": False,
                                 "required_proof_kinds": required_proofs},
        "selection_eligible": False, "qualification_only": True,
        "completed_steps": [task["stage"]] if operation_completed else [],
        "pending_required_steps": list(PENDING_REQUIRED_STEPS),
        "cleanup_result": cleanup_result,
        "acceptance": SIE.acceptance_to_attempt_outcome(
            candidate_selected=candidate_selected),
    }


def collect_and_verify_swap_result(
    *, task: dict[str, Any], process_exit_code: int,
    baseline_evidence: dict[str, Any],
) -> dict[str, Any]:
    """工具操作結束後，讀已發現的產物、核對讀回一致性、呼叫既有
    `evaluate_acceptance()`——與 `process_exit_code` 本身是分開保存的兩件事
：這個函式只在 `process_exit_code == 0`（Operation 1／2 都真的
    跑完）時才有意義去核對讀回；exit code 非零時，`repair_acceptance` 既有
    的「沒有真實變化」／缺 proof 規則本身就會把結果判成不合格，這裡不用
    另外攔一次。

    `baseline_evidence` 由呼叫端提供（REUSED_VALIDATED baseline 的既有
    證據，不屬於這個 task 自己的輸出範圍）——本函式不解析「哪個 baseline
    適用」，那是 Work／EAP3 層的事。
    """
    task = _validate_task(task)
    root = Path(str(task["task_root"])).resolve()
    reports = root / "reports"
    reports_readback = root / "reports_readback"
    eco_diff = _read_json_evidence(reports / SIE.ECO_DIFF_FILENAME)
    instance_snapshot = _read_json_evidence(reports / SIE.INSTANCE_SNAPSHOT_FILENAME)
    neighbor_snapshot = _read_json_evidence(reports / SIE.NEIGHBOR_SNAPSHOT_FILENAME)
    readback_instance_snapshot = _read_json_evidence(
        reports_readback / SIE.READBACK_INSTANCE_SNAPSHOT_FILENAME)
    readback_neighbor_snapshot = _read_json_evidence(
        reports_readback / SIE.READBACK_NEIGHBOR_SNAPSHOT_FILENAME)

    readback_verified = False
    readback_mismatches: tuple[str, ...] = ()
    if (eco_diff is not None and instance_snapshot is not None
            and neighbor_snapshot is not None
            and readback_instance_snapshot is not None
            and readback_neighbor_snapshot is not None):
        readback_verified, readback_mismatches = SIE.compare_operation1_and_readback_snapshots(
            op1_neighbor_snapshot=neighbor_snapshot,
            readback_neighbor_snapshot=readback_neighbor_snapshot,
            op1_instance_snapshot=instance_snapshot, op1_eco_diff=eco_diff,
            readback_instance_snapshot=readback_instance_snapshot,
            target_instance=task["target_instance"])

    candidate_evidence = {
        "setup_endpoints": None, "core": None,
        "eco_diff": eco_diff, "eco_proof": None, "final_pair_proof": None,
        "instance_snapshot": instance_snapshot,
    }
    try:
        result = ME.evaluate_acceptance(
            ME.SINGLE_INSTANCE_ECO, baseline=baseline_evidence,
            candidate=candidate_evidence, host_consumption=None, method={},
            candidate_value="", metric_comparisons=[],
            execution_ok=(process_exit_code == 0 and readback_verified),
            inputs_approved=True, inputs_reason=None,
            baseline_reference_available=bool(baseline_evidence))
        candidate_selected = result.candidate_selected
        repair_status = result.payload["repair_status"]
    except ME.MethodExtensionError as exc:
        candidate_selected = False
        repair_status = RA.UNVERIFIABLE
        readback_mismatches = readback_mismatches + (exc.code,)

    return {
        "method_identity": SIE.METHOD_ID,
        "task_id": task["task_id"], "run_id": task["run_id"],
        "target_instance": task["target_instance"],
        "expected_cell": task["expected_cell"],
        "replacement_cell": task["replacement_cell"],
        "source_candidate": task["source_candidate"],
        "process_exit_code": process_exit_code,
        "readback_verified": readback_verified,
        "readback_mismatches": list(readback_mismatches),
        "repair_status": repair_status,
        "eco_diff": eco_diff, "instance_snapshot": instance_snapshot,
        "neighbor_snapshot": neighbor_snapshot,
        # place 之後既定的後續執行需求：本 task 從不執行它們，因此永遠
        # 明確列成未完成——不把「排入計畫」記成「執行成功」。
        "completed_steps": [task["stage"]] if process_exit_code == 0 else [],
        "pending_required_steps": ["legalization", "cts", "route", "final"],
        "outcome": SIE.acceptance_to_attempt_outcome(
            candidate_selected=candidate_selected),
    }


__all__ = [
    "CANDIDATE_ODB_FILENAME", "CIDFILE_FILENAME",
    "CONTAINER_CANDIDATE_PATH", "OP1_SCRIPT_FILENAME", "OP2_SCRIPT_FILENAME",
    "CANCEL_MARKER_FILENAME", "EVIDENCE_ROLES", "PENDING_REQUIRED_STEPS",
    "TASK_SCHEMA", "VERDICT_SCHEMA",
    "SingleInstanceEcoAdapterError", "SingleInstanceEcoSwapAdapter",
    "bind_kernel_run", "build_task", "collect_and_verify_swap_result",
    "evaluate_swap_evidence", "evaluate_validated_swap",
    "materialize_task", "workflow_config",
]
