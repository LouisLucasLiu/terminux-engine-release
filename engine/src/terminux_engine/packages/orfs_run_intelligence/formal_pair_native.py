# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""host-native formal pair-equivalence：以站點授權的 Yosys 在主機上執行。

與 `formal_pair.py`（container 版本）是兩個獨立的契約：descriptor、task、tool
result、envelope 與 DQE 都有自己的 schema 與 digest。container 版本的位元、
digest 與讀取語意一個都不變；本模組不重用、也不重新標記它的 descriptor。

工具身分：Profile 的 `execution.native_tool_binding.tools.FORMAL_YOSYS`
（realpath ＋ SHA-256）。它進 proof task（因此進 `proof_plan_digest`），在派送前
以 `verify_pin` 核對，並在子行程內、exec Yosys 之前再核對一次；任何不符都在
執行前結束。Yosys 版本字串只記錄、不作為身分。

proof 結果受影響的可信輸入（依 Yosys 原始碼確認，見 descriptor 的
`trusted_inputs`）：binary、canonical script、兩份 netlist、元件庫、argv 與
環境。script 只由本模組產生，不含 `plugin` 指令；argv 不含 `-m`，因此不會載入
plugin。Yosys 的 share 目錄不被本 script 用到的指令讀取。

隔離：host-native 執行**不**提供 container 的 `--network none --read-only`。
信任前提、允許寫入的位置與實際落實的檢查都寫在 descriptor 的 `trust_model`；
執行後會重算全部可信輸入與工具，任何一項改變都使結果 `UNVERIFIABLE`。
"""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.execution_contract import (
    ArtifactCandidate,
    ArtifactDiscoveryResult,
    ExecutionContractError,
    ExecutionRequest,
    PreparedExecution,
    validate_within_boundary,
)
from terminux_engine.packages.orfs_run_intelligence import formal_pair as _FP

# 判讀規則與 container 版本相同（v3 五分類）；正規式直接共用同一份定義，避免
# 兩條路徑的判讀悄悄分歧。
from terminux_engine.packages.orfs_run_intelligence.formal_pair import (
    _ASSERT_ERROR,
    _EQUIV_SUMMARY,
    _EQUIVALENCE_PROVEN,
    _YOSYS_BANNER,
    ASSERT_COMMAND,
    CELL_LIBRARY_LOAD_COMMAND,
    CELL_LIBRARY_ROLE,
    CLASSIFICATION_RULE_VERSION,
    CLASSIFICATIONS,
    COUNTEREXAMPLE_NAME,
    COUNTEREXAMPLE_SCHEMA,
    DECIDABLE_CLASSIFICATIONS,
    LOG_NAME,
    PROOF_KIND,
    RAW_RESULT_NAME,
    SCRIPT_NAME,
    STATUS_NAME,
    TIMEOUT_SECONDS,
    FormalPairError,
    canonical_json,
    digest_json,
    sha256_file,
)

DESCRIPTOR_SCHEMA = "orfs.formal_pair.native_descriptor.v1"
TASK_SCHEMA = "orfs.eap.formal_pair_proof_task.native.v1"
TOOL_RESULT_SCHEMA = "orfs.eap.formal_pair_tool_result.native.v1"
ENVELOPE_SCHEMA = "orfs.eap.formal_pair_proof.native.v1"
RENDERER_VERSION = "orfs.formal_pair.yosys_renderer.native.v1"
DECODER_VERSION = "orfs.formal_pair.yosys_decoder.native.v1"
EXECUTION_MODE = "host_native"
#: Profile native tool binding 內 formal Yosys 的名稱。
BINDING_TOOL_NAME = "FORMAL_YOSYS"
#: script 內元件庫相對於 task root 的位置；cwd 固定為 task root。
CELL_LIBRARY_TASK_DIR = "inputs/lib"
#: in-process 工具核對失敗的 exit code；與 flow task 的 native runner 相同。
EXIT_TOOL_MISMATCH = 92
#: task root 下允許 Yosys 寫入的目錄。HOME 與 TMPDIR 指向這裡，讓 Yosys 的
#: history 目錄與暫存檔留在 task root 之內。
WRITABLE_DIRS = ("output", "home", "tmp")

DQE_PACKAGE_ID = "orfs_formal_pair_native_dqe"
DQE_POLICY_ID = "orfs.formal_pair_native_evidence_readiness.v1"
DQE_EVALUATOR_ID = "orfs.formal_pair_native_dqe.v1"
DQE_ROW_COUNT = 11
DQE_ID_PREFIX = "formal_pair_native."
DQE_INPUT_BINDING = "FP_RUN_RESULT_BINDING"

ADAPTER_SPEC = ("terminux_engine.packages.orfs_run_intelligence.formal_pair_native:"
                "FormalPairNativeToolAdapter")


def proof_descriptor() -> dict[str, Any]:
    """Package 固定的 host-native proof 契約；由 native method extension pin 進 plan。

    不含站點路徑或雜湊：binary 身分屬於 Profile 授權，並逐 task 進
    `proof_plan_digest`。
    """
    return {
        "descriptor_schema": DESCRIPTOR_SCHEMA,
        "execution_mode": EXECUTION_MODE,
        "proof_kind": PROOF_KIND,
        "task_schema": TASK_SCHEMA,
        "tool_result_schema": TOOL_RESULT_SCHEMA,
        "envelope_schema": ENVELOPE_SCHEMA,
        "renderer_version": RENDERER_VERSION,
        "decoder_version": DECODER_VERSION,
        "classification_rule_version": CLASSIFICATION_RULE_VERSION,
        "classifications": list(CLASSIFICATIONS),
        "decidable_classifications": list(DECIDABLE_CLASSIFICATIONS),
        "inequivalence_evidence_required": True,
        "counterexample_name": COUNTEREXAMPLE_NAME,
        "counterexample_schema": COUNTEREXAMPLE_SCHEMA,
        "assert_command": ASSERT_COMMAND,
        "timeout_seconds": TIMEOUT_SECONDS,
        "baseline_input_role": "baseline_netlist",
        "candidate_input_role": "candidate_netlist",
        "cell_library_input_role": CELL_LIBRARY_ROLE,
        "cell_library_load_command": CELL_LIBRARY_LOAD_COMMAND,
        "cell_library_task_dir": CELL_LIBRARY_TASK_DIR,
        "script_name": SCRIPT_NAME,
        "log_name": LOG_NAME,
        "status_name": STATUS_NAME,
        "raw_result_name": RAW_RESULT_NAME,
        "tool_binding": {
            "source": "profile.execution.native_tool_binding.tools." + BINDING_TOOL_NAME,
            "identity": ["realpath", "sha256"],
            "verified": ["before_dispatch", "in_process_before_exec", "after_exit"],
            "version_string_is_identity": False,
        },
        "invocation": {
            "cwd": "task_root",
            "argv": ["<tool.executable_realpath>", "-q", "-l", f"output/{LOG_NAME}",
                     "-s", f"output/{SCRIPT_NAME}"],
            "plugins": "none",
            "environment": "cleared; allowlist plus HOME, TMPDIR and run marker",
            "home": "task_root/home", "tmpdir": "task_root/tmp",
        },
        # 依 Yosys 原始碼（ORFS tools/yosys a5af9d69）確認：本 script 用到的指令
        # 不讀 share 目錄；plugin 只能經 `-m` 或 `plugin` 載入，兩者皆不使用；
        # 會改變行為的環境變數（YOSYS_*、ABC、TMPDIR、HOME／XDG_STATE_HOME）
        # 由清空環境並重新指定 HOME／TMPDIR 排除。
        "trusted_inputs": ["tool.executable_sha256", "script.sha256", "baseline.sha256",
                           "candidate.sha256", "cell_libraries[].sha256", "argv_sha256",
                           "invocation.environment"],
        "trust_model": {
            "container_equivalent": False,
            "network_isolation": "NOT_ENFORCED",
            "filesystem_isolation": "NOT_ENFORCED",
            "process_identity": "the operating system account that runs the daemon",
            "assumed_trusted": [
                "the site-administered Yosys installation pinned by realpath and SHA-256",
                "shared libraries loaded by that binary (not content-pinned)",
                "the operating system account and host kernel",
            ],
            "enforced": [
                "environment cleared to an allowlist before the shell and Yosys start",
                "binary realpath and SHA-256 rechecked before dispatch and before exec",
                "no plugin flag in argv and no plugin command in the canonical script",
                "inputs, script and cell libraries written read-only into the task root",
                "binary, inputs, script and cell libraries re-hashed after exit",
                "HOME and TMPDIR redirected inside the task root",
            ],
            "allowed_write_paths": [f"task_root/{name}" for name in WRITABLE_DIRS],
            "not_enforced": [
                "network access is not blocked",
                "writes outside the task root by the tool are not prevented",
            ],
        },
        "dqe_hook": {
            "schema_version": "package.single_tool_dqe_hook.v1",
            "registry_loader_spec": (
                "terminux_engine.packages.orfs_run_intelligence.formal_pair_native_dqe:"
                "load_runtime_package"),
            "package_id": DQE_PACKAGE_ID,
            "policy_id": DQE_POLICY_ID,
            "evaluator_id": DQE_EVALUATOR_ID,
            "required_rows": DQE_ROW_COUNT,
            "input_binding": DQE_INPUT_BINDING,
        },
    }


def descriptor_digest() -> str:
    return digest_json(proof_descriptor())


def tool_identity(pin: Any) -> dict[str, str]:
    """把 Profile binding 的 `{realpath, sha256}` 轉成 task 內的工具身分。"""
    from terminux_engine.orchestration.native_tool_identity import (
        NativeToolIdentityError,
        parse_pin,
    )

    try:
        parsed = parse_pin(pin, where=BINDING_TOOL_NAME)
    except NativeToolIdentityError as exc:
        raise FormalPairError("FORMAL_PROOF_TOOL_IDENTITY_INVALID", exc.message) from exc
    return {"execution_mode": EXECUTION_MODE, "executable_realpath": parsed.realpath,
            "executable_sha256": parsed.sha256}


def _tool_pin(tool: Any) -> Any:
    from terminux_engine.orchestration.native_tool_identity import FilePin

    if (not isinstance(tool, dict) or set(tool) != {
            "execution_mode", "executable_realpath", "executable_sha256"}
            or tool.get("execution_mode") != EXECUTION_MODE):
        raise FormalPairError("FORMAL_PROOF_TOOL_IDENTITY_INVALID",
                              "the proof task does not carry a host-native tool identity")
    tool_identity({"realpath": tool["executable_realpath"],
                   "sha256": tool["executable_sha256"]})
    return FilePin(realpath=tool["executable_realpath"], sha256=tool["executable_sha256"])


def render_script(*, top_module: str, cell_library_names: tuple[str, ...] = ()) -> str:
    """canonical Yosys script；路徑一律相對於 task root，與主機位置無關。"""
    for name in cell_library_names:
        if (not isinstance(name, str) or not name or "/" in name or name in (".", "..")
                or "\n" in name or " " in name):
            raise FormalPairError("FORMAL_PROOF_CELL_LIBRARY_INVALID",
                                  "cell library file name is invalid")
    container_style = _FP.render_script(
        top_module=top_module,
        cell_libraries=tuple(f"{_FP.CELL_LIBRARY_CONTAINER_DIR}/{name}"
                             for name in cell_library_names))
    # 指令序列與 container 版本完全相同，只有版本行與輸入位置不同。
    lines = container_style.split("\n")
    lines[0] = "# " + RENDERER_VERSION
    replaced = []
    for line in lines:
        line = line.replace(f" {_FP.CELL_LIBRARY_CONTAINER_DIR}/", f" {CELL_LIBRARY_TASK_DIR}/")
        line = line.replace(" /inputs/baseline.v", " inputs/baseline.v")
        line = line.replace(" /inputs/candidate.v", " inputs/candidate.v")
        replaced.append(line)
    script = "\n".join(replaced)
    if (any(line.split(" ", 1)[0] == "plugin" for line in replaced)
            or "/inputs/" in script):
        raise FormalPairError("FORMAL_PROOF_SCRIPT_INVALID",
                              "the canonical script references an unexpected resource")
    return script


def _verify_extension(method_extension: Any) -> None:
    from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME

    descriptor = method_extension.get("descriptor") if isinstance(
        method_extension, dict) else None
    if (not isinstance(descriptor, dict)
            or descriptor.get("formal_pair_native_proof") != proof_descriptor()
            or method_extension.get("descriptor_sha256")
            != ME.extension_descriptor_sha256(descriptor)):
        raise FormalPairError("FORMAL_PROOF_DESCRIPTOR_MISMATCH",
                              "method extension does not pin the host-native proof descriptor")


def build_task(*, work_id: str, attempt_id: str, task_id: str, task_digest: str,
               grant_id: str, run_id: str, package_identity: dict[str, Any],
               method_extension: dict[str, Any], baseline: dict[str, Any],
               candidate: dict[str, Any], input_manifest: dict[str, Any],
               task_root: str | Path, top_module: str, tool: dict[str, Any],
               cell_libraries: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """由已釘住的 native method extension 與 Profile 授權的工具建立 proof task。"""
    required_ids = {"work_id": work_id, "attempt_id": attempt_id, "task_id": task_id,
                    "task_digest": task_digest, "grant_id": grant_id, "run_id": run_id}
    if any(not isinstance(value, str) or not value for value in required_ids.values()):
        raise FormalPairError("FORMAL_PROOF_IDENTITY_INVALID", "task identity is incomplete")
    if not isinstance(package_identity, dict):
        raise FormalPairError("FORMAL_PROOF_DESCRIPTOR_INVALID", "package identity is invalid")
    _verify_extension(method_extension)
    tool_entry = tool_identity(tool)
    baseline_artifact = _FP._artifact(baseline, role="baseline_netlist")  # noqa: SLF001
    candidate_artifact = _FP._artifact(candidate, role="candidate_netlist")  # noqa: SLF001
    if (not isinstance(input_manifest, dict)
            or set(input_manifest) != {"manifest_sha256", "tree_hash"}):
        raise FormalPairError("FORMAL_PROOF_INPUT_MANIFEST_INVALID", "input manifest is invalid")
    manifest = {key: _FP._require_hash(value, key)  # noqa: SLF001
                for key, value in input_manifest.items()}
    root = Path(task_root).resolve()
    libraries = tuple(_FP._artifact(entry, role=CELL_LIBRARY_ROLE)  # noqa: SLF001
                      for entry in cell_libraries)
    names = tuple(Path(entry["path"]).name for entry in libraries)
    if len(set(names)) != len(names):
        raise FormalPairError("FORMAL_PROOF_CELL_LIBRARY_INVALID",
                              "cell library file names collide")
    script = render_script(top_module=top_module, cell_library_names=names)
    task = {
        "schema_version": TASK_SCHEMA, **required_ids,
        "proof_kind": PROOF_KIND, "package_identity": package_identity,
        "method_extension": method_extension, "proof_descriptor": proof_descriptor(),
        "proof_descriptor_sha256": descriptor_digest(), "tool": tool_entry,
        "baseline": baseline_artifact, "candidate": candidate_artifact,
        "input_manifest": manifest,
        "cell_libraries": [dict(entry) for entry in libraries],
        "task_root": str(root), "top_module": top_module,
        "script": {"relative_path": f"output/{SCRIPT_NAME}",
                   "sha256": hashlib.sha256(script.encode("utf-8")).hexdigest()},
        "timeout_seconds": TIMEOUT_SECONDS,
    }
    task["proof_plan_digest"] = digest_json(task)
    return task


def bind_kernel_run(task: dict[str, Any], run_id: str, *, task_digest: str | None = None,
                    grant_id: str | None = None) -> dict[str, Any]:
    """把已 grant 的 proof draft 綁到 daemon 預先配置的 Kernel run。"""
    if (not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA
            or task.get("run_id") != "PENDING" or not isinstance(run_id, str)
            or not run_id.startswith("run_")):
        raise FormalPairError("FORMAL_PROOF_RUN_BINDING_INVALID", "proof draft cannot bind run")
    if task_digest is not None and task.get("task_digest") != "PENDING":
        raise FormalPairError("FORMAL_PROOF_RUN_BINDING_INVALID", "proof draft task is bound")
    if grant_id is not None and task.get("grant_id") != "PENDING":
        raise FormalPairError("FORMAL_PROOF_RUN_BINDING_INVALID", "proof draft grant is bound")
    bound = dict(task)
    bound["run_id"] = run_id
    if task_digest is not None:
        bound["task_digest"] = task_digest
    if grant_id is not None:
        bound["grant_id"] = grant_id
    bound.pop("proof_plan_digest", None)
    bound["proof_plan_digest"] = digest_json(bound)
    return bound


def _verify_task(task: Any) -> None:
    if not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA:
        raise FormalPairError("FORMAL_PROOF_TASK_INVALID", "proof task schema differs")
    if (task.get("proof_descriptor") != proof_descriptor()
            or task.get("proof_descriptor_sha256") != descriptor_digest()):
        raise FormalPairError("FORMAL_PROOF_DESCRIPTOR_MISMATCH", "proof descriptor differs")
    recompute = dict(task)
    plan_digest = recompute.pop("proof_plan_digest", None)
    if plan_digest != digest_json(recompute):
        raise FormalPairError("FORMAL_PROOF_PLAN_DIGEST_MISMATCH", "proof plan digest differs")
    _tool_pin(task.get("tool"))


def _library_names(task: dict[str, Any]) -> tuple[str, ...]:
    return tuple(Path(entry["path"]).name for entry in (task.get("cell_libraries") or []))


def materialize_task(task: dict[str, Any]) -> dict[str, Any]:
    """把已釘住的輸入與 script 寫進 task-owned root（唯讀），重驗後才可 dispatch。"""
    _verify_task(task)
    root = Path(str(task["task_root"])).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise FormalPairError("FORMAL_PROOF_TASK_ROOT_EXISTS", "task root is not empty")
    root.mkdir(parents=True, exist_ok=True)
    inputs = root / "inputs"
    inputs.mkdir()
    for name in WRITABLE_DIRS:
        (root / name).mkdir(mode=0o700)
    copies: list[tuple[Path, Path, str]] = []
    for entry in task.get("cell_libraries") or []:
        (inputs / "lib").mkdir(exist_ok=True)
        source = Path(entry["path"])
        copies.append((source, inputs / "lib" / source.name, entry["sha256"]))
    for name, key in (("baseline.v", "baseline"), ("candidate.v", "candidate")):
        copies.append((Path(task[key]["path"]), inputs / name, task[key]["sha256"]))
    for source, destination, expected in copies:
        destination.write_bytes(source.read_bytes())
        destination.chmod(stat.S_IRUSR | stat.S_IRGRP)
        if sha256_file(destination) != expected:
            raise FormalPairError("FORMAL_PROOF_ARTIFACT_HASH_MISMATCH",
                                  f"{destination.name} changed while it was copied")
    script = render_script(top_module=task["top_module"],
                           cell_library_names=_library_names(task))
    script_path = root / "output" / SCRIPT_NAME
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(stat.S_IRUSR | stat.S_IRGRP)
    if sha256_file(script_path) != task["script"]["sha256"]:
        raise FormalPairError("FORMAL_PROOF_SCRIPT_DIGEST_MISMATCH", "script differs")
    return {"task_root": str(root), "script": str(script_path),
            "script_sha256": task["script"]["sha256"]}


def build_argv(task: dict[str, Any]) -> list[str]:
    """`env -i <allowlist> bash -c '<pgid>; <pin check>; exec <yosys> ...'`。

    shell 只做三件事：記錄 process group、在 exec 前重新核對 binary、exec
    Yosys（同一個 pid）。Yosys 的 argv 固定，不含 `-m`。
    """
    from terminux_engine.orchestration.native_tool_identity import (
        BASH_EXECUTABLE,
        RUN_MARKER_ENV,
        pgid_record_line,
        pin_check_script,
        strict_env_argv,
    )

    pin = _tool_pin(task.get("tool"))
    run_id = task.get("run_id")
    if not isinstance(run_id, str) or not run_id.startswith("run_"):
        raise FormalPairError("FORMAL_PROOF_RUN_BINDING_INVALID",
                              "the proof task is not bound to a Kernel run")
    root = Path(str(task["task_root"])).resolve()
    yosys = [pin.realpath, "-q", "-l", f"output/{LOG_NAME}", "-s", f"output/{SCRIPT_NAME}"]
    script = "\n".join((
        "set -eu",
        pgid_record_line(root),
        pin_check_script({BINDING_TOOL_NAME: pin}, exit_code=EXIT_TOOL_MISMATCH),
        f"cd {shlex.quote(str(root))}",
        "exec " + " ".join(shlex.quote(part) for part in yosys),
    ))
    return strict_env_argv(
        [str(BASH_EXECUTABLE), "--noprofile", "--norc", "-c", script],
        extra_env={"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
                   RUN_MARKER_ENV: run_id})


def _post_run_integrity(task: dict[str, Any]) -> dict[str, Any]:
    """執行後重算 binary 與全部可信輸入；回傳每一項的觀測與是否全部相符。"""
    root = Path(str(task["task_root"])).resolve()
    expected: list[tuple[str, Path, str]] = [
        ("baseline", root / "inputs" / "baseline.v", task["baseline"]["sha256"]),
        ("candidate", root / "inputs" / "candidate.v", task["candidate"]["sha256"]),
        ("script", root / "output" / SCRIPT_NAME, task["script"]["sha256"]),
        *((f"cell_library:{Path(entry['path']).name}",
           root / "inputs" / "lib" / Path(entry["path"]).name, entry["sha256"])
          for entry in (task.get("cell_libraries") or [])),
    ]
    tool = task["tool"]
    expected.append(("tool", Path(tool["executable_realpath"]), tool["executable_sha256"]))
    changed: list[str] = []
    for name, path, digest in expected:
        try:
            observed = sha256_file(path) if path.is_file() else None
        except OSError:
            observed = None
        if name == "tool" and observed is not None and os.path.realpath(path) != str(path):
            observed = None
        if observed != digest:
            changed.append(name)
    return {"ok": not changed, "changed": changed}


def decode_tool_result(*, task: dict[str, Any], argv: list[str], exit_code: int,
                       timed_out: bool) -> dict[str, Any]:
    """由 child 產物建立 raw tool result；規則與 container 版本相同，另加完整性核對。"""
    root = Path(str(task["task_root"])).resolve()
    output = root / "output"
    script = output / SCRIPT_NAME
    log = output / LOG_NAME
    proven, unresolved = 0, None
    counterexample: dict[str, Any] | None = None
    banner_text: str | None = None
    integrity = _post_run_integrity(task)
    if not script.is_file() or sha256_file(script) != task["script"]["sha256"]:
        status, reason, text = "UNVERIFIABLE", "script_digest_mismatch", ""
    elif "tool" in integrity["changed"]:
        status, reason, text = "UNVERIFIABLE", "tool_changed_during_proof", ""
    elif not integrity["ok"]:
        status, reason, text = "UNVERIFIABLE", "trusted_input_changed_during_proof", ""
    elif timed_out:
        status, reason, text = "TIMEOUT", "process_timeout", ""
    elif exit_code < 0:
        status, reason, text = "TOOL_ERROR", "signal_termination", ""
    elif exit_code == EXIT_TOOL_MISMATCH and not log.is_file():
        status, reason, text = "TOOL_ERROR", "tool_identity_mismatch", ""
    elif not log.is_file():
        status, reason, text = "TOOL_ERROR", "status_report_missing", ""
    else:
        text = log.read_text(encoding="utf-8", errors="replace")
        asserted = ASSERT_COMMAND in script.read_text(encoding="utf-8", errors="replace")
        banner = _YOSYS_BANNER.search(text)
        banner_text = banner.group(1).strip() if banner is not None else None
        summary = _EQUIV_SUMMARY.search(text)
        assert_failure = bool(_ASSERT_ERROR.search(text))
        if summary is not None:
            proven, unresolved = int(summary.group(1)), int(summary.group(2))
        if not asserted:
            status, reason = "TOOL_ERROR", "proof_command_missing_assert"
        elif summary is None:
            status, reason = "TOOL_ERROR", "status_report_unparseable"
        elif exit_code != 0 and unresolved > 0 and assert_failure:
            counterexample = _FP._read_counterexample(output)  # noqa: SLF001
            if counterexample is not None:
                status, reason = "NOT_EQUIVALENT", "counterexample_evidence_present"
            else:
                status, reason = "UNPROVEN", "equiv_status_unproven_obligations"
        elif exit_code == 0 and unresolved == 0 and _EQUIVALENCE_PROVEN.search(text):
            status, reason = "EQUIVALENT", "equiv_status_assert_succeeded"
        elif exit_code != 0:
            status, reason = "TOOL_ERROR", "yosys_nonzero_exit"
        else:
            status, reason = "UNVERIFIABLE", "status_report_inconsistent_with_exit_code"
    result = {
        "schema_version": TOOL_RESULT_SCHEMA, "proof_kind": PROOF_KIND,
        "work_id": task["work_id"], "attempt_id": task["attempt_id"],
        "task_id": task["task_id"], "task_digest": task["task_digest"],
        "grant_id": task["grant_id"], "run_id": task["run_id"],
        "package_identity": task["package_identity"],
        "method_extension": task["method_extension"],
        "proof_descriptor_sha256": task["proof_descriptor_sha256"],
        "proof_plan_digest": task["proof_plan_digest"],
        "script_sha256": task["script"]["sha256"], "argv": list(argv),
        "argv_sha256": digest_json(argv), "timeout_seconds": task["timeout_seconds"],
        "exit_code": exit_code, "timed_out": timed_out, "status": status,
        "reason_code": reason, "proven_count": proven, "unproven_count": unresolved,
        "equiv_status_assert_failure": status in ("UNPROVEN", "NOT_EQUIVALENT"),
        "unproven_obligations": unresolved if status in (
            "UNPROVEN", "NOT_EQUIVALENT", "EQUIVALENT") else None,
        "counterexample": counterexample,
        "inequivalence_evidence": counterexample is not None,
        "classification_rule_version": CLASSIFICATION_RULE_VERSION,
        "status_report": {"path": str(log),
                          "sha256": sha256_file(log) if log.is_file() else None},
        "tool": dict(task["tool"]),
        # 版本字串只是觀測紀錄，不參與身分判定。
        "observed_version_banner": banner_text,
        "executed_identity": "NOT_VERIFIED",
        "post_run_integrity": integrity,
        "baseline": task["baseline"], "candidate": task["candidate"],
        "cell_libraries": list(task.get("cell_libraries") or []),
        "input_manifest": task["input_manifest"],
    }
    raw_path = output / RAW_RESULT_NAME
    raw_path.write_text(canonical_json(result) + "\n", encoding="utf-8")
    result["raw_result"] = {"path": str(raw_path), "sha256": sha256_file(raw_path)}
    status_path = output / STATUS_NAME
    status_path.write_text(canonical_json(result) + "\n", encoding="utf-8")
    result["status_report"]["result_path"] = str(status_path)
    result["status_report"]["result_sha256"] = sha256_file(status_path)
    return result


def dqe_reference(*, rows: list[dict[str, Any]], run_id: str, step_id: str,
                  dispatch_key: str) -> dict[str, Any]:
    """由 native DQE rows 建立可重算的 formal evidence reference。"""
    hook = proof_descriptor()["dqe_hook"]
    if (not isinstance(run_id, str) or not isinstance(step_id, str)
            or not isinstance(dispatch_key, str)
            or dispatch_key != f"{run_id}:{step_id}:1" or len(rows) != DQE_ROW_COUNT):
        raise FormalPairError("FORMAL_PROOF_DQE_INVALID", "DQE row set identity is invalid")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if (not isinstance(row, dict) or row.get("orchestration_run_id") != run_id
                or row.get("step_id") != step_id or row.get("attempt") != 1
                or row.get("dispatch_key") != dispatch_key
                or row.get("package_id") != hook["package_id"]
                or not str(row.get("DQE_ID", "")).startswith(DQE_ID_PREFIX)):
            raise FormalPairError("FORMAL_PROOF_DQE_INVALID", "DQE row lineage differs")
        normalized.append({key: row.get(key) for key in (
            "DQE_ID", "VALUE", "UNIT", "STATUS", "semantic_status",
            "orchestration_run_id", "step_id", "attempt", "dispatch_key",
            "source_event_id", "package_id")})
    return {"dispatch_key": dispatch_key, "row_count": len(normalized),
            "row_set_sha256": digest_json(sorted(normalized, key=lambda item: item["DQE_ID"])),
            "evaluator_id": hook["evaluator_id"], "policy_id": hook["policy_id"],
            "input_binding": hook["input_binding"]}


def attest_result(*, task: dict[str, Any], tool_result: dict[str, Any],
                  receipt: dict[str, Any], manifest: dict[str, Any],
                  dqe: dict[str, Any]) -> dict[str, Any]:
    """在 immutable receipt 之後由 host 建立 durable native proof envelope。"""
    if (not isinstance(tool_result, dict)
            or tool_result.get("schema_version") != TOOL_RESULT_SCHEMA
            or any(tool_result.get(key) != task[key] for key in
                   ("work_id", "attempt_id", "task_id", "task_digest", "grant_id", "run_id"))
            or tool_result.get("proof_plan_digest") != task.get("proof_plan_digest")
            or tool_result.get("proof_descriptor_sha256") != descriptor_digest()
            or task.get("proof_descriptor_sha256") != descriptor_digest()
            or tool_result.get("script_sha256") != task.get("script", {}).get("sha256")
            or tool_result.get("tool") != task.get("tool")
            or tool_result.get("timeout_seconds") != TIMEOUT_SECONDS):
        raise FormalPairError("FORMAL_PROOF_TOOL_RESULT_MISMATCH", "tool result lineage differs")
    _tool_pin(task.get("tool"))
    if (not isinstance(receipt, dict)
            or any(not receipt.get(key) for key in ("receipt_id", "content_hash"))
            or not isinstance(manifest, dict) or not manifest.get("manifest_id")
            or not isinstance(dqe, dict) or not dqe.get("dispatch_key")
            or dqe.get("row_count") != DQE_ROW_COUNT
            or not isinstance(dqe.get("row_set_sha256"), str)):
        raise FormalPairError("FORMAL_PROOF_HOST_EVIDENCE_INCOMPLETE",
                              "host evidence lineage is incomplete")
    if (receipt.get("run_id") != task["run_id"]
            or (manifest.get("producer") or {}).get("run_id") != task["run_id"]
            or not str(dqe["dispatch_key"]).startswith(task["run_id"] + ":")):
        raise FormalPairError("FORMAL_PROOF_HOST_LINEAGE_MISMATCH",
                              "receipt, manifest, DQE, or result event differs")
    hook = proof_descriptor()["dqe_hook"]
    if any(dqe.get(key) != hook[key] for key in ("evaluator_id", "policy_id", "input_binding")):
        raise FormalPairError("FORMAL_PROOF_HOST_LINEAGE_MISMATCH",
                              "DQE evaluator or policy identity differs")
    envelope = {
        "schema_version": ENVELOPE_SCHEMA, "proof_kind": PROOF_KIND,
        "execution_mode": EXECUTION_MODE,
        "classification": tool_result["status"], "task": {
            **{key: task[key] for key in ("work_id", "attempt_id", "task_id", "task_digest",
                                          "grant_id", "run_id", "proof_plan_digest")},
            "tool": dict(task["tool"])},
        "package_identity": task["package_identity"], "method_extension": task["method_extension"],
        "proof_descriptor_sha256": task["proof_descriptor_sha256"],
        "tool_result": tool_result, "receipt": receipt, "artifact_manifest": manifest,
        "dqe": dqe,
    }
    envelope["durable_envelope_sha256"] = digest_json(envelope)
    return envelope


def _recompute_verdict(raw: dict[str, Any]) -> str:
    integrity = raw.get("post_run_integrity")
    if not isinstance(integrity, dict) or integrity.get("ok") is not True:
        return "UNVERIFIABLE"
    return _FP._recompute_verdict(raw)  # noqa: SLF001


def read_durable_envelope(envelope: dict[str, Any], result_event: dict[str, Any],
                          dqe_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fresh reader：由 result event 單向驗證 native envelope，不重跑工具。"""
    if not isinstance(envelope, dict) or envelope.get("schema_version") != ENVELOPE_SCHEMA:
        return {"state": "WORK_SCHEMA_UNSUPPORTED", "reason": "envelope_schema"}
    claimed_digest = envelope.get("durable_envelope_sha256")
    material = dict(envelope)
    material.pop("durable_envelope_sha256", None)
    if claimed_digest != digest_json(material):
        return {"state": "UNVERIFIABLE", "reason": "envelope_digest_mismatch"}
    task_data = envelope.get("task")
    raw = envelope.get("tool_result")
    if not isinstance(task_data, dict) or not isinstance(raw, dict):
        return {"state": "UNVERIFIABLE", "reason": "task_or_raw_missing"}
    required = ("work_id", "attempt_id", "task_id", "task_digest", "grant_id", "run_id",
                "proof_plan_digest")
    if any(raw.get(key) != task_data.get(key) for key in required):
        return {"state": "UNVERIFIABLE", "reason": "task_run_lineage_mismatch"}
    if (envelope.get("proof_kind") != PROOF_KIND
            or envelope.get("execution_mode") != EXECUTION_MODE
            or raw.get("schema_version") != TOOL_RESULT_SCHEMA
            or raw.get("proof_kind") != PROOF_KIND
            or envelope.get("proof_descriptor_sha256") != descriptor_digest()
            or raw.get("proof_descriptor_sha256") != descriptor_digest()
            or raw.get("tool") != task_data.get("tool")
            or raw.get("timeout_seconds") != TIMEOUT_SECONDS
            or not isinstance(raw.get("argv_sha256"), str)
            or raw.get("script_sha256") is None):
        return {"state": "UNVERIFIABLE", "reason": "descriptor_or_tool_mismatch"}
    try:
        _tool_pin(task_data.get("tool"))
    except FormalPairError:
        return {"state": "UNVERIFIABLE", "reason": "descriptor_or_tool_mismatch"}
    if envelope.get("package_identity") != raw.get("package_identity") \
            or envelope.get("method_extension") != raw.get("method_extension"):
        return {"state": "UNVERIFIABLE", "reason": "cross_method_evidence"}
    receipt = envelope.get("receipt")
    manifest = envelope.get("artifact_manifest")
    dqe = envelope.get("dqe")
    if (not isinstance(receipt, dict) or receipt.get("run_id") != task_data["run_id"]
            or not receipt.get("receipt_id") or not receipt.get("content_hash")
            or not isinstance(manifest, dict)
            or (manifest.get("producer") or {}).get("run_id") != task_data["run_id"]
            or not isinstance(dqe, dict) or dqe.get("row_count") != DQE_ROW_COUNT
            or not str(dqe.get("dispatch_key", "")).startswith(task_data["run_id"] + ":")
            or not isinstance(dqe.get("row_set_sha256"), str)):
        return {"state": "UNVERIFIABLE", "reason": "host_lineage_mismatch"}
    if (not isinstance(result_event, dict)
            or result_event.get("task_id") != task_data["task_id"]
            or result_event.get("proof_envelope_sha256") != claimed_digest
            or result_event.get("dqe_dispatch_key") != dqe.get("dispatch_key")):
        return {"state": "UNVERIFIABLE", "reason": "result_event_lineage_mismatch"}
    try:
        recomputed_dqe = dqe_reference(
            rows=dqe_rows, run_id=str(task_data["run_id"]), step_id="s1",
            dispatch_key=str(dqe["dispatch_key"]))
    except FormalPairError:
        return {"state": "UNVERIFIABLE", "reason": "dqe_row_set_mismatch"}
    if recomputed_dqe != dqe:
        return {"state": "UNVERIFIABLE", "reason": "dqe_row_set_digest_mismatch"}
    recomputed = _recompute_verdict(raw)
    if raw.get("status") != recomputed or envelope.get("classification") != recomputed:
        return {"state": "UNVERIFIABLE", "reason": "proof_status_mismatch"}
    return {"state": "VERIFIED", "classification": recomputed,
            "durable_envelope_sha256": claimed_digest}


def raw_to_task(raw: dict[str, Any]) -> dict[str, Any]:
    """從 native raw 的 host-bound facts 還原 attestation 所需的 task view。"""
    keys = ("work_id", "attempt_id", "task_id", "task_digest", "grant_id", "run_id",
            "proof_plan_digest", "package_identity", "method_extension", "baseline",
            "candidate", "input_manifest", "tool")
    task = {key: raw[key] for key in keys}
    task["script"] = {"sha256": raw["script_sha256"]}
    task["proof_descriptor_sha256"] = descriptor_digest()
    return task


def validate_workflow_config(config: dict[str, Any]) -> None:
    """驗證 package_single_tool 的 native formal-pair 分支；未知欄位不接受。"""
    required = {"kind", "proof_task", "output_root", "artifact_root", "timeout_seconds",
                "step_id"}
    if (not isinstance(config, dict) or set(config) != required
            or config.get("kind") != "formal_pair_native"):
        raise ExecutionContractError("native formal pair config has unexpected fields")
    if (not isinstance(config["output_root"], str)
            or not isinstance(config["artifact_root"], str)
            or not isinstance(config["step_id"], str)
            or config["timeout_seconds"] != TIMEOUT_SECONDS):
        raise ExecutionContractError("native formal pair config identity or timeout is invalid")
    task = config["proof_task"]
    if (not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA
            or task.get("task_root") != config["output_root"]
            or task.get("timeout_seconds") != TIMEOUT_SECONDS):
        raise ExecutionContractError("native formal pair task differs from its host binding")
    try:
        _tool_pin(task.get("tool"))
    except FormalPairError as exc:
        raise ExecutionContractError(exc.message) from exc


def workflow_config(*, task: dict[str, Any], artifact_root: str,
                    step_id: str) -> dict[str, Any]:
    """建立由 Package 簽入 binding 的 generic adapter config（沒有 container）。"""
    from terminux_engine.orchestration.native_tool_identity import ENV_EXECUTABLE

    validate_workflow_config({"kind": "formal_pair_native", "proof_task": task,
                              "output_root": task.get("task_root"),
                              "artifact_root": artifact_root,
                              "timeout_seconds": TIMEOUT_SECONDS, "step_id": step_id})
    hook = proof_descriptor()["dqe_hook"]
    return {
        "schema_version": "package.adapter_binding.v1", "tool_id": PROOF_KIND,
        "adapter_spec": ADAPTER_SPEC, "adapter_kwargs": {},
        "parameters": {"proof_task": task}, "host_executable": str(ENV_EXECUTABLE),
        "output_root": task.get("task_root"), "artifact_root": artifact_root,
        "timeout_seconds": TIMEOUT_SECONDS, "step_id": step_id,
        "dqe_hook": {key: hook[key] for key in
                     ("schema_version", "registry_loader_spec", "package_id", "input_binding")},
    }


def _release(task: Any) -> dict[str, Any]:
    from terminux_engine.orchestration.native_tool_identity import release_native_process_group

    if (not isinstance(task, dict) or not isinstance(task.get("task_root"), str)
            or not str(task.get("run_id") or "").startswith("run_")):
        return {"attempted": False, "ok": False, "resource_kind": "native_process_group",
                "reason": "proof_task_unbound"}
    return release_native_process_group(task["task_root"], str(task["run_id"]))


class FormalPairNativeToolAdapter:
    """把 native descriptor 轉為 Kernel 可執行的主機 argv；沒有 container。"""

    adapter_id = "orfs_formal_pair_native"
    adapter_version = "orfs_formal_pair_native/1"

    def bind_kernel_identity(self, config: dict[str, Any],
                             identity: dict[str, str]) -> dict[str, Any]:
        """只接受 host identity；client／child 不可自行補填 authoritative 欄位。"""
        task = (config.get("parameters") or {}).get("proof_task")
        if not isinstance(task, dict):
            raise ExecutionContractError("native formal pair proof draft is missing")
        if task.get("task_id") != identity["task_id"]:
            raise ExecutionContractError("native formal pair proof task identity differs")
        bound = dict(config)
        bound["parameters"] = {"proof_task": bind_kernel_run(
            task, identity["run_id"], task_digest=identity["task_digest"],
            grant_id=identity["grant_id"])}
        return bound

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        from terminux_engine.orchestration.native_tool_identity import (
            NativeToolIdentityError,
            identity_layers,
            verify_pin,
        )

        if set(request.parameters) != {"proof_task"}:
            raise ExecutionContractError(
                "native formal pair parameters differ from the fixed binding")
        task = request.parameters["proof_task"]
        try:
            _verify_task(task)
        except FormalPairError as exc:
            raise ExecutionContractError(exc.message) from exc
        root = validate_within_boundary(task.get("task_root", ""), request.output_boundary)
        if root != request.cwd:
            raise ExecutionContractError("native formal pair cwd differs from task-owned root")
        if (request.timeout_seconds != TIMEOUT_SECONDS
                or task.get("timeout_seconds") != TIMEOUT_SECONDS):
            raise ExecutionContractError("native formal pair timeout differs from descriptor")
        pin = _tool_pin(task["tool"])
        try:
            resolved = verify_pin(pin, where=BINDING_TOOL_NAME)
        except NativeToolIdentityError as exc:
            # 站點工具與授權不符：零派發。
            raise ExecutionContractError(str(exc)) from exc
        if not (root / "inputs" / "baseline.v").is_file():
            try:
                materialize_task(task)
            except FormalPairError as exc:
                raise ExecutionContractError(exc.message) from exc
        try:
            argv = build_argv(task)
        except FormalPairError as exc:
            raise ExecutionContractError(exc.message) from exc
        return PreparedExecution(
            argv=argv, cwd=root, env=None, timeout_seconds=TIMEOUT_SECONDS,
            adapter_metadata={
                "adapter_version": self.adapter_version, "proof_kind": PROOF_KIND,
                "runner": EXECUTION_MODE,
                "proof_plan_digest": task["proof_plan_digest"],
                "proof_descriptor_sha256": descriptor_digest(),
                "tool_identity": dict(task["tool"]),
                "tool_version_evidence": {
                    "tool_source": "native_tool_binding",
                    "identity": identity_layers(
                        {BINDING_TOOL_NAME: pin.to_json_dict()},
                        {BINDING_TOOL_NAME: resolved})},
                "network_isolation": "not_enforced_host_policy",
                "filesystem_isolation": "not_enforced_host_policy",
                "environment_boundary": "env -i allowlist",
                "argv_sha256": digest_json(argv)},
            artifact_discovery_context={"proof_task": task},
        )

    def discover_artifacts(self, request: ExecutionRequest, prepared: PreparedExecution, *,
                           process_exit_code: int) -> ArtifactDiscoveryResult:
        task = prepared.artifact_discovery_context.get("proof_task")
        if not isinstance(task, dict):
            raise ExecutionContractError("native formal pair task context is missing")
        decode_tool_result(task=task, argv=prepared.argv, exit_code=process_exit_code,
                           timed_out=process_exit_code == -1)
        root = Path(task["task_root"]).resolve()
        output = root / "output"
        candidates = [ArtifactCandidate(adapter_role="orfs.formal_pair." + name,
                                        host_path=output / name,
                                        exists=(output / name).is_file())
                      for name in (SCRIPT_NAME, LOG_NAME, STATUS_NAME, RAW_RESULT_NAME)]
        return ArtifactDiscoveryResult(output_roots=[root], artifact_candidates=candidates)

    def cleanup(self, request: ExecutionRequest, prepared: PreparedExecution) -> dict[str, Any]:
        """停止並確認本 proof 的 process group 已不存在。"""
        return _release(prepared.artifact_discovery_context.get("proof_task"))

    def release_external_resources(self, config: dict[str, Any]) -> dict[str, Any]:
        """daemon immediate cancel 之後，確認本 proof 沒有殘留子行程。"""
        return _release((config.get("parameters") or {}).get("proof_task"))


def is_native_task(task: Any) -> bool:
    return isinstance(task, dict) and task.get("schema_version") == TASK_SCHEMA


def is_native_raw(raw: Any) -> bool:
    return isinstance(raw, dict) and raw.get("schema_version") == TOOL_RESULT_SCHEMA


__all__ = [
    "ADAPTER_SPEC", "BINDING_TOOL_NAME", "CELL_LIBRARY_TASK_DIR", "DECODER_VERSION",
    "DESCRIPTOR_SCHEMA", "DQE_EVALUATOR_ID", "DQE_ID_PREFIX", "DQE_PACKAGE_ID",
    "DQE_POLICY_ID", "DQE_ROW_COUNT", "ENVELOPE_SCHEMA", "EXECUTION_MODE",
    "EXIT_TOOL_MISMATCH", "FormalPairNativeToolAdapter", "RENDERER_VERSION", "TASK_SCHEMA",
    "TIMEOUT_SECONDS", "TOOL_RESULT_SCHEMA", "WRITABLE_DIRS", "attest_result",
    "bind_kernel_run", "build_argv", "build_task", "decode_tool_result",
    "descriptor_digest", "dqe_reference", "is_native_raw", "is_native_task",
    "materialize_task", "proof_descriptor", "raw_to_task", "read_durable_envelope",
    "render_script", "tool_identity", "validate_workflow_config", "workflow_config",
]
