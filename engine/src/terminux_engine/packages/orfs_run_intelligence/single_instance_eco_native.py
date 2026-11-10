# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`gcd.single_instance_buffer_swap.v1` 的 host-native `ExecutionAdapter`。

與 `single_instance_eco_adapter.py`（container 版本）並存、互不取代：task schema、
adapter 身分與 plan digest 都是自己的。Operation 1／2 的 Tcl、證據檔與判定沿用
同一套（`single_instance_eco.py` 的腳本產生器與 `evaluate_validated_swap()`）。

差異只在執行邊界：

- OpenROAD 與元件庫是主機 ECO 綁定（`host_binding.native.v1`）以 realpath ＋
  SHA-256 釘住的站點檔案；派送前與子行程內各核對一次，不符即零派發／不啟動。
- flow scripts、platform 與來源結果都是主機路徑，以唯讀方式讀取；唯一寫入
  位置是 task root。
- 子行程環境以 `env -i` 清空；兩個 OpenROAD 行程與 shell 同在一個 process
  group，取消時寫入取消標記並停止、確認整個 group 已不存在。
- 不提供 container 的網路與檔案系統隔離；這點記錄在 adapter metadata。
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from terminux_engine.expert_ai.eap3 import digest_json
from terminux_engine.orchestration.execution_contract import (
    ArtifactCandidate,
    ArtifactDiscoveryResult,
    ExecutionRequest,
    PreparedExecution,
    validate_within_boundary,
)
from terminux_engine.packages.orfs_run_intelligence import single_instance_eco as SIE
from terminux_engine.packages.orfs_run_intelligence import single_instance_eco_adapter as SIEA
from terminux_engine.packages.orfs_run_intelligence.single_instance_eco_adapter import (
    CANCEL_MARKER_FILENAME,
    CANDIDATE_ODB_FILENAME,
    OP1_SCRIPT_FILENAME,
    OP2_SCRIPT_FILENAME,
    SingleInstanceEcoAdapterError,
)

TASK_SCHEMA = "gcd.single_instance_buffer_swap.task.native.v1"
EXECUTION_MODE = "host_native"
ADAPTER_SPEC = ("terminux_engine.packages.orfs_run_intelligence.single_instance_eco_native:"
                "NativeSingleInstanceEcoSwapAdapter")
#: 子行程內工具核對失敗；與其他 host-native runner 相同。
EXIT_TOOL_MISMATCH = 92
#: task root 下的工作目錄；HOME／TMPDIR 指向這裡，讓 OpenROAD 的使用者狀態
#: 與暫存檔留在 task root 之內。
HOME_DIRNAME = "home"
TMP_DIRNAME = "tmp"

_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./+-]*$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_IDENTITY_KEYS = ("work_id", "attempt_id", "task_id", "task_digest", "grant_id", "run_id")


def _plain_path(value: Any, field: str) -> str:
    text = str(value or "")
    if not _SAFE_PATH_RE.fullmatch(text) or str(Path(text)) != text:
        raise SingleInstanceEcoAdapterError(
            f"{field} must be an absolute path made of plain characters")
    return text


def _pin(value: Any, field: str) -> dict[str, str]:
    from terminux_engine.orchestration.native_tool_identity import (
        NativeToolIdentityError,
        parse_pin,
    )

    try:
        pin = parse_pin(value, where=field)
    except NativeToolIdentityError as exc:
        raise SingleInstanceEcoAdapterError(exc.message) from exc
    _plain_path(pin.realpath, f"{field}.realpath")
    return pin.to_json_dict()


def native_tool_binding_sha256(*, openroad: dict[str, str], lib_files: dict[str, str]) -> str:
    """plan 材料內的工具身分（取代 container 版本的 `docker_image`）。"""
    return digest_json({"execution_mode": EXECUTION_MODE, "openroad": openroad,
                        "lib_files": lib_files})


def build_task(
    *, work_id: str, attempt_id: str, task_id: str, task_digest: str,
    grant_id: str, run_id: str, target_instance: str, expected_cell: str,
    replacement_cell: str, library: str, stage: str,
    source_candidate: dict[str, Any], verified_pre_swap_snapshot: dict[str, Any],
    task_root: str | Path, openroad: dict[str, str], lib_files: dict[str, str],
    flow_dir: str, platform_dir: str, source_results_dir: str,
    source_design_file: str, source_sdc_file: str,
    source_odb_host_path: str, source_odb_sha256: str, num_cores: int = 1,
) -> dict[str, Any]:
    """由 host 已核對的 recipe、來源快照與主機 ECO 綁定建立 native swap task。"""
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
            or any(not isinstance(item, str)
                   or not SIEA._PIN_NET_RE.fullmatch(item)  # noqa: SLF001
                   for item in pin_nets)):
        raise SingleInstanceEcoAdapterError(
            "verified_pre_swap_snapshot lacks usable host-parsed connectivity")
    if (not isinstance(provenance, dict)
            or provenance.get("netlist_sha256") != source_candidate.get("netlist_sha256")
            or provenance.get("candidate_reference")
            != source_candidate.get("candidate_reference")):
        raise SingleInstanceEcoAdapterError(
            "verified_pre_swap_snapshot provenance differs from the source candidate")
    for name, value in (("source_design_file", source_design_file),
                        ("source_sdc_file", source_sdc_file)):
        if not _SAFE_NAME_RE.fullmatch(str(value or "")):
            raise SingleInstanceEcoAdapterError(f"{name} must be a plain file name")
    results_dir = _plain_path(source_results_dir, "source_results_dir")
    odb_path = _plain_path(source_odb_host_path, "source_odb_host_path")
    if Path(odb_path).parent != Path(results_dir) or Path(odb_path).name != source_design_file:
        raise SingleInstanceEcoAdapterError(
            "the source ODB must be the design file inside the source results directory")
    if not isinstance(source_odb_sha256, str) or len(source_odb_sha256) != 64:
        raise SingleInstanceEcoAdapterError("source ODB identity is invalid")
    if not isinstance(num_cores, int) or isinstance(num_cores, bool) or not 1 <= num_cores <= 64:
        raise SingleInstanceEcoAdapterError("num_cores must be an integer in [1, 64]")
    root = _plain_path(str(Path(task_root).resolve()), "task_root")
    openroad_pin = _pin(openroad, "openroad")
    lib_pin = _pin(lib_files, "lib_files")
    task = {
        "schema_version": TASK_SCHEMA, **identity,
        "method_identity": SIE.METHOD_ID, "execution_mode": EXECUTION_MODE,
        "target_instance": target_instance,
        "expected_cell": expected_cell, "replacement_cell": replacement_cell,
        "library": library, "stage": stage, "source_candidate": dict(source_candidate),
        "verified_pre_swap_snapshot": verified_pre_swap_snapshot,
        "task_root": root,
        "tool": {"openroad": openroad_pin, "lib_files": lib_pin,
                 "binding_sha256": native_tool_binding_sha256(
                     openroad=openroad_pin, lib_files=lib_pin)},
        "flow_dir": _plain_path(flow_dir, "flow_dir"),
        "platform_dir": _plain_path(platform_dir, "platform_dir"),
        "source_results_dir": results_dir,
        "source_design_file": source_design_file, "source_sdc_file": source_sdc_file,
        "source_odb_host_path": odb_path, "source_odb_sha256": source_odb_sha256,
        "num_cores": num_cores,
        "timeout_seconds": SIE.EXECUTION_PHASE_TIMEOUT_SECONDS,
    }
    task["swap_plan_digest"] = digest_json(task)
    return task


def bind_kernel_run(task: dict[str, Any], run_id: str, *, task_digest: str | None = None,
                    grant_id: str | None = None) -> dict[str, Any]:
    """把已 grant 的 native swap draft 綁到 daemon 預先配置的 Kernel run。"""
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


def validate_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA:
        raise SingleInstanceEcoAdapterError("native swap task schema differs")
    recompute = dict(task)
    plan_digest = recompute.pop("swap_plan_digest", None)
    if plan_digest != digest_json(recompute):
        raise SingleInstanceEcoAdapterError("swap plan digest differs from task content")
    tool = task.get("tool") or {}
    if tool.get("binding_sha256") != native_tool_binding_sha256(
            openroad=_pin(tool.get("openroad"), "openroad"),
            lib_files=_pin(tool.get("lib_files"), "lib_files")):
        raise SingleInstanceEcoAdapterError("native swap tool binding digest differs")
    return task


def is_native_task(task: Any) -> bool:
    return isinstance(task, dict) and task.get("schema_version") == TASK_SCHEMA


def materialize_task(task: dict[str, Any]) -> dict[str, Any]:
    """把 Operation 1／2 Tcl（沿用既有產生器）以主機路徑寫進 task root。"""
    task = validate_task(task)
    root = Path(task["task_root"])
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise SingleInstanceEcoAdapterError("swap task root is not empty")
    root.mkdir(parents=True, exist_ok=True)
    for name in (HOME_DIRNAME, TMP_DIRNAME):
        (root / name).mkdir(mode=0o700)
    candidate_odb_path = str(root / CANDIDATE_ODB_FILENAME)
    op1_tcl = SIE.build_standalone_modify_and_save_tcl(
        target_instance=task["target_instance"], expected_cell=task["expected_cell"],
        replacement_cell=task["replacement_cell"], library=task["library"],
        candidate_odb_output_path=candidate_odb_path,
        source_design_file=task["source_design_file"],
        source_sdc_file=task["source_sdc_file"],
        expected_target_pin_net=list(task["verified_pre_swap_snapshot"]["target_pin_net"]))
    op2_tcl = SIE.build_standalone_readback_verification_tcl(
        target_instance=task["target_instance"],
        replacement_cell=task["replacement_cell"],
        candidate_odb_path=candidate_odb_path)
    (root / OP1_SCRIPT_FILENAME).write_text(op1_tcl, encoding="utf-8")
    (root / OP2_SCRIPT_FILENAME).write_text(op2_tcl, encoding="utf-8")
    return task


def build_argv(task: dict[str, Any]) -> list[str]:
    """`env -i ... bash -c '<pgid>; <pin check>; <op1>; <op2>'`（同一個 process group）。

    兩個 OpenROAD 行程依序執行；`set -e` 讓 Operation 1 失敗時 Operation 2 不會
    啟動。`timeout --foreground` 不另建 process group，取消時整個 group 一起停止。
    """
    from terminux_engine.orchestration.native_tool_identity import (
        BASH_EXECUTABLE,
        RUN_MARKER_ENV,
        FilePin,
        pgid_record_line,
        pin_check_script,
        strict_env_argv,
    )

    run_id = str(task.get("run_id") or "")
    if not run_id.startswith("run_"):
        raise SingleInstanceEcoAdapterError("swap task is not bound to a Kernel run")
    root = task["task_root"]
    tool = task["tool"]
    openroad = tool["openroad"]["realpath"]
    pins = {"OPENROAD": FilePin(**tool["openroad"]), "LIB_FILES": FilePin(**tool["lib_files"])}
    marker = shlex.quote(f"{root}/{CANCEL_MARKER_FILENAME}")
    guard = f"test ! -e {marker} || exit {SIE.CANCELLED_BEFORE_OPERATION_EXIT_CODE}"
    reports, readback = f"{root}/reports", f"{root}/reports_readback"
    command = (f"timeout --foreground {SIE.OPERATION_PROCESS_TIMEOUT_SECONDS} "
               f"{shlex.quote(openroad)} -exit -no_init -threads {int(task['num_cores'])} "
               "-no_splash")
    exports = {
        "SCRIPTS_DIR": f"{task['flow_dir']}/scripts",
        "RESULTS_DIR": task["source_results_dir"],
        "PLATFORM_DIR": task["platform_dir"],
        "PLATFORM": SIE.LIBRARY_NANGATE45,
        "LIB_FILES": tool["lib_files"]["realpath"],
        "OPENROAD_HIERARCHICAL": "0",
    }
    script = "\n".join((
        "set -eu",
        pgid_record_line(root),
        pin_check_script(pins, exit_code=EXIT_TOOL_MISMATCH),
        f"cd {shlex.quote(root)}",
        *(f"export {name}={shlex.quote(value)}" for name, value in exports.items()),
        f"mkdir -p {shlex.quote(reports)} {shlex.quote(readback)}",
        f"export REPORTS_DIR={shlex.quote(reports)}",
        guard,
        f"{command} {shlex.quote(f'{root}/{OP1_SCRIPT_FILENAME}')}",
        f"export REPORTS_DIR={shlex.quote(readback)}",
        guard,
        f"{command} {shlex.quote(f'{root}/{OP2_SCRIPT_FILENAME}')}",
    ))
    return strict_env_argv(
        [str(BASH_EXECUTABLE), "--noprofile", "--norc", "-c", script],
        extra_env={"HOME": f"{root}/{HOME_DIRNAME}", "TMPDIR": f"{root}/{TMP_DIRNAME}",
                   RUN_MARKER_ENV: run_id})


def _release(task: dict[str, Any]) -> dict[str, Any]:
    from terminux_engine.orchestration.native_tool_identity import release_native_process_group

    run_id = str(task.get("run_id") or "")
    if not run_id.startswith("run_"):
        return {"attempted": False, "ok": True, "resource_kind": "native_process_group",
                "reason": "swap task was never bound to a Kernel run"}
    return release_native_process_group(task["task_root"], run_id)


class NativeSingleInstanceEcoSwapAdapter:
    """把 native swap task 轉為主機上的兩個 OpenROAD 行程；沒有 container。"""

    adapter_id = "orfs_single_instance_eco_swap_native"
    adapter_version = "orfs_single_instance_eco_swap_native/1"

    def bind_kernel_identity(self, config: dict[str, Any],
                             identity: dict[str, str]) -> dict[str, Any]:
        task = (config.get("parameters") or {}).get("swap_task")
        if not isinstance(task, dict):
            raise SingleInstanceEcoAdapterError("swap draft is missing")
        if task.get("task_id") != identity["task_id"]:
            raise SingleInstanceEcoAdapterError("swap task identity differs")
        bound = dict(config)
        bound["parameters"] = {"swap_task": bind_kernel_run(
            task, identity["run_id"], task_digest=identity["task_digest"],
            grant_id=identity["grant_id"])}
        return bound

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        from terminux_engine.orchestration.native_tool_identity import (
            FilePin,
            NativeToolIdentityError,
            identity_layers,
            sha256_file,
            verify_pin,
        )

        if set(request.parameters) != {"swap_task"}:
            raise SingleInstanceEcoAdapterError("swap parameters differ from the fixed binding")
        task = validate_task(request.parameters["swap_task"])
        root = validate_within_boundary(task["task_root"], request.output_boundary)
        if root != request.cwd:
            raise SingleInstanceEcoAdapterError("swap cwd differs from task-owned root")
        if (request.timeout_seconds != task["timeout_seconds"]
                or task["timeout_seconds"] != SIE.EXECUTION_PHASE_TIMEOUT_SECONDS):
            raise SingleInstanceEcoAdapterError("swap timeout differs from task")
        ok, code, reason = SIE.check_source_drift(
            expected_cell=task["expected_cell"], target_instance=task["target_instance"],
            observed_instance_snapshot=task["verified_pre_swap_snapshot"])
        if not ok:
            raise SingleInstanceEcoAdapterError(
                f"swap rejected before dispatch ({code}): {reason}")
        if (root / CANCEL_MARKER_FILENAME).exists():
            raise SingleInstanceEcoAdapterError(
                "swap rejected before dispatch: this run was cancelled")
        tool = task["tool"]
        try:
            resolved = {
                "OPENROAD": verify_pin(FilePin(**tool["openroad"]), where="OPENROAD"),
                "LIB_FILES": verify_pin(FilePin(**tool["lib_files"]), where="LIB_FILES",
                                        executable=False)}
        except NativeToolIdentityError as exc:
            raise SingleInstanceEcoAdapterError(
                f"swap rejected before dispatch: {exc}") from exc
        source_odb = Path(task["source_odb_host_path"])
        if not source_odb.is_file() or sha256_file(source_odb) != task["source_odb_sha256"]:
            raise SingleInstanceEcoAdapterError(
                "swap rejected before dispatch: the source ODB differs from the "
                "host-verified identity")
        for name in ("flow_dir", "platform_dir", "source_results_dir"):
            if not Path(task[name]).is_dir():
                raise SingleInstanceEcoAdapterError(
                    f"swap rejected before dispatch: {name} is unavailable")
        argv = build_argv(task)
        if not (root / OP1_SCRIPT_FILENAME).is_file():
            materialize_task(task)
        return PreparedExecution(
            argv=argv, cwd=root, env=None, timeout_seconds=task["timeout_seconds"],
            adapter_metadata={
                "adapter_version": self.adapter_version,
                "method_identity": SIE.METHOD_ID, "runner": EXECUTION_MODE,
                "target_instance": task["target_instance"],
                "expected_cell": task["expected_cell"],
                "replacement_cell": task["replacement_cell"],
                "swap_plan_digest": task["swap_plan_digest"],
                "argv_sha256": digest_json(argv),
                "openroad_process_count": SIE.MAX_OPENROAD_PROCESSES,
                "native_tool_binding_sha256": tool["binding_sha256"],
                "tool_version_evidence": {
                    "tool_source": "single_instance_eco_host_binding",
                    "native_tool_binding_sha256": tool["binding_sha256"],
                    "identity": identity_layers(
                        {"OPENROAD": tool["openroad"], "LIB_FILES": tool["lib_files"]},
                        resolved)},
                "network_isolation": "not_enforced_host_policy",
                "filesystem_isolation": "not_enforced_host_policy",
                "environment_boundary": "env -i allowlist",
            },
            artifact_discovery_context={"swap_task": task, "batch_id": task["run_id"]},
        )

    def discover_artifacts(self, request: ExecutionRequest, prepared: PreparedExecution, *,
                           process_exit_code: int) -> ArtifactDiscoveryResult:
        """純粹的產物收集；與 container 版本相同的角色與 boundary 規則。"""
        task = prepared.artifact_discovery_context.get("swap_task")
        if not isinstance(task, dict):
            raise SingleInstanceEcoAdapterError("swap task context is missing")
        root = Path(str(task["task_root"])).resolve()
        reports, readback = root / "reports", root / "reports_readback"
        file_roles = (
            (reports / SIE.TOOL_VERSION_FILENAME, "tool_version"),
            (reports / SIE.ECO_DIFF_FILENAME, "eco_diff"),
            (reports / SIE.INSTANCE_SNAPSHOT_FILENAME, "instance_snapshot"),
            (reports / SIE.NEIGHBOR_SNAPSHOT_FILENAME, "neighbor_snapshot"),
            (readback / SIE.READBACK_INSTANCE_SNAPSHOT_FILENAME, "readback_instance_snapshot"),
            (readback / SIE.READBACK_NEIGHBOR_SNAPSHOT_FILENAME, "readback_neighbor_snapshot"),
            (root / CANDIDATE_ODB_FILENAME, "candidate_odb"),
        )
        candidates = [
            ArtifactCandidate(adapter_role=f"{SIE.METHOD_ID}.{role}",
                              host_path=validate_within_boundary(path, request.output_boundary),
                              exists=path.is_file())
            for path, role in file_roles]
        return ArtifactDiscoveryResult(output_roots=[root], artifact_candidates=candidates)

    def cleanup(self, request: ExecutionRequest, prepared: PreparedExecution) -> dict[str, Any]:
        """確認本 run 的 process group 已不存在；只處理帶本 run 標記的 group。"""
        task = prepared.artifact_discovery_context.get("swap_task")
        if not isinstance(task, dict):
            return {"attempted": False, "ok": True,
                    "detail": "no swap task in prepared execution context"}
        return _release(task)

    def release_external_resources(self, config: dict[str, Any]) -> dict[str, Any]:
        """daemon immediate cancel 之後：先寫取消標記，再停止並確認 process group。"""
        task = validate_task((config.get("parameters") or {}).get("swap_task"))
        if not str(task.get("run_id") or "").startswith("run_"):
            return {"attempted": False, "ok": True,
                    "detail": "swap task was never bound to a Kernel run"}
        root = Path(task["task_root"])
        marker: dict[str, Any] = {"path": str(root / CANCEL_MARKER_FILENAME)}
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / CANCEL_MARKER_FILENAME).write_text(task["run_id"] + "\n", encoding="utf-8")
            marker["written"] = True
        except OSError as exc:
            marker.update({"written": False, "error": type(exc).__name__})
        result = dict(_release(task))
        result["cancel_marker"] = marker
        if not marker.get("written"):
            result["ok"] = False
        return result


def workflow_config(*, task: dict[str, Any], artifact_root: str,
                    step_id: str) -> dict[str, Any]:
    """Package 簽入的 generic adapter binding；沒有 container 欄位。"""
    from terminux_engine.orchestration.native_tool_identity import ENV_EXECUTABLE

    task = validate_task(task)
    return {
        "schema_version": "package.adapter_binding.v1",
        "tool_id": "single_instance_eco_swap",
        "adapter_spec": ADAPTER_SPEC, "adapter_kwargs": {},
        "parameters": {"swap_task": task},
        "host_executable": str(ENV_EXECUTABLE),
        "output_root": task["task_root"], "artifact_root": artifact_root,
        "timeout_seconds": task["timeout_seconds"], "step_id": step_id,
    }


def evaluate_swap_evidence(
    *, task: dict[str, Any], process_exit_code: int | None, timed_out: bool,
    evidence: dict[str, Any], evidence_digests: dict[str, str],
    cleanup_result: dict[str, Any] | None, run_status: str = "completed",
) -> dict[str, Any]:
    """native task 的判定：與 container 版本相同規則，另記錄工具身分與執行模式。"""
    task = validate_task(task)
    verdict = SIEA.evaluate_validated_swap(
        task=task, process_exit_code=process_exit_code, timed_out=timed_out,
        evidence=evidence, evidence_digests=evidence_digests,
        cleanup_result=cleanup_result, run_status=run_status)
    verdict["execution_mode"] = EXECUTION_MODE
    verdict["native_tool_binding_sha256"] = task["tool"]["binding_sha256"]
    return verdict


__all__ = [
    "ADAPTER_SPEC", "EXECUTION_MODE", "EXIT_TOOL_MISMATCH", "TASK_SCHEMA",
    "NativeSingleInstanceEcoSwapAdapter", "bind_kernel_run", "build_argv", "build_task",
    "evaluate_swap_evidence", "is_native_task", "materialize_task",
    "native_tool_binding_sha256", "validate_task", "workflow_config",
]
