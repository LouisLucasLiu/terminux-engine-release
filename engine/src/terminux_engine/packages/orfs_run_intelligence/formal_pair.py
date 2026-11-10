# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS Package 所有的 formal pair-equivalence 定義與證據解碼。

這個模組刻意把方法、設計名稱與 netlist 檔名留在 descriptor／task 資料內。
Common Runtime 只傳遞已釘住的 task；只有本 Package 知道如何把兩個已驗證
artifact 轉為 Yosys 的 canonical script 與 argv。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.execution_contract import (
    ArtifactCandidate,
    ArtifactDiscoveryResult,
    ExecutionContractError,
    ExecutionRequest,
    PreparedExecution,
)
from terminux_engine.orchestration.legacy_docker_execution import (
    PATH_FORMAL_PAIR,
    LegacyDockerExecutionDisabled,
)


PROOF_KIND = "formal_equivalence"
TASK_SCHEMA = "orfs.eap.formal_pair_proof_task.v1"
TOOL_RESULT_SCHEMA = "orfs.eap.formal_pair_tool_result.v1"
ENVELOPE_SCHEMA = "orfs.eap.formal_pair_proof.v1"
RENDERER_VERSION = "orfs.formal_pair.yosys_renderer.v3"
#: gate-level netlist 只有元件**實例**；沒有標準元件的功能模型，`prep` 會在
#: hierarchy 階段就因為未解析的元件而失敗（曾造成 TOOL_ERROR）。
#:
#: 載入方式固定使用帶 `-ignore_miss_func` 的 `read_liberty`：它由 liberty 的
#: `function` 屬性建出**功能**模型。`-ignore_miss_func` 用於略過沒有布林 function
#: 的時鐘閘控 cell（如 CLKGATETST/CLKGATE 系列），但前提是待證電路中絕不使用
#: 這些被略過的 cell。`-lib` 僅建介面（blackbox）會使等價性失去意義因此不使用。
#: 實際使用的未解析元件仍然必須讓 proof 失敗，不得被吞掉。
CELL_LIBRARY_ROLE = "cell_library"
CELL_LIBRARY_LOAD_COMMAND = "read_liberty -ignore_miss_func"
CELL_LIBRARY_CONTAINER_DIR = "/inputs/lib"
TIMEOUT_SECONDS = 300
SCRIPT_NAME = "formal_pair.ys"
LOG_NAME = "formal_pair.log"
STATUS_NAME = "formal_pair_status.json"
RAW_RESULT_NAME = "formal_pair_raw_tool_result.json"
#: 不等價的**必要**證據：一份由 recipe 自己寫出的、可核驗的反例紀錄。
#:
#: `equiv_status -assert` 失敗只說「還有未證明的義務」，它**不證明**兩側不等價；
#: 把它讀成 NOT_EQUIVALENT 等於用「沒證出來」冒充「證出不一樣」。因此不等價必須
#: 由這份獨立的反例檔案支持：它要指出在哪一組輸入賦值下，哪一個輸出端點兩側不同。
#:
#: 目前釘住的 recipe（`equiv_make` ＋ `equiv_simple` ＋ `equiv_status -assert`）
#: **不會**產生這份檔案，因此現行 executor 在未證明時一律誠實回傳 `UNPROVEN`。
#: 這是刻意的：本包不新增更強的證明能力，只要求分類不得超過證據。
COUNTEREXAMPLE_NAME = "formal_pair_counterexample.json"
COUNTEREXAMPLE_SCHEMA = "orfs.eap.formal_pair_counterexample.v1"

# 這些是 Package qualification 已驗證的 image 與 image 內實際 executable，
# 不是由 host PATH 推得。production task 仍必須把完整 descriptor pin 進 digest。
PINNED_IMAGE = "sha256:8926a3cd2b269694cb4950a932d8a8b31c811cd5b819a85f326aed412d7ae28c"
YOSYS_REALPATH = "/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys"
YOSYS_SHA256 = "5cd52bc790d39b1e59a88112e9132ef8de338c2a26e4b0df8222d937e65bab92"
YOSYS_VERSION = (
    "Yosys 0.68+post (git sha1 UNKNOWN, Release, GNU "
    "/usr/local/bin/wrapped-cc/g++ 11.4.0)"
)

_TOP_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_HEX = re.compile(r"^[0-9a-f]{64}$")

#: 判讀 Yosys 證據的規則版本。與 `RENDERER_VERSION`（產生 script）分開：
#: script 一個位元都沒變，判讀規則仍可能改，兩者都必須各自可稽核。
DECODER_VERSION = "orfs.formal_pair.yosys_decoder.v3"
#: 分類規則本身的身分。v3 修正 v2 的越權判讀：v2 把「unproven > 0 ＋ assert
#: 失敗」直接讀成 `NOT_EQUIVALENT`，但那組事實只支持「仍有未證明義務」。
#: v3 的五個分類與各自的**必要**依據：
#:
#: - `EQUIVALENT`：宣告範圍內的必要證明全部成立（unproven == 0、exit 0、
#:   且有 Yosys 自己的成立宣告），模型與輸入檢查通過。
#: - `NOT_EQUIVALENT`：有足以支持不等價的可核驗證據（`COUNTEREXAMPLE_NAME`），
#:   不得只依未證明數量。
#: - `UNPROVEN`：尚有未證明義務，且沒有足夠的不等價證據。
#: - `TOOL_ERROR`：輸入、模型、工具或執行錯誤。
#: - `TIMEOUT`：工具執行逾時。
CLASSIFICATION_RULE_VERSION = "orfs.formal_pair.classification.v3"
#: decoder 可能產生的全部分類。`UNVERIFIABLE` 不在其中：它是「證據互相矛盾、
#: 連分類都不成立」的狀態，不是一種工程結果。
CLASSIFICATIONS = ("EQUIVALENT", "NOT_EQUIVALENT", "UNPROVEN", "TOOL_ERROR",
                   "TIMEOUT")
#: 已完成判讀、可以交給下游處置的工程結果。`UNPROVEN` 在內：它是**明確的
#: 拒絕依據**，不是失敗。能不能選用 candidate 由 eligibility 決定，不在這裡。
DECIDABLE_CLASSIFICATIONS = ("EQUIVALENT", "NOT_EQUIVALENT", "UNPROVEN")
#: 這一行必須真的在已驗 digest 的 script 裡；沒有它就沒有 assert，
#: 任何 unproven 敘述都不足以成為 verdict。
ASSERT_COMMAND = "equiv_status -assert"
#: `equiv_status` 的唯一權威計數。空白量與大小寫允許漂移，語意不允許。
_EQUIV_SUMMARY = re.compile(
    r"Of those cells\s+(\d+)\s+are proven and\s+(\d+)\s+are unproven", re.IGNORECASE)
#: assert 失敗的語意：一個「仍有 unproven $equiv cells」的 ERROR。刻意不綁
#: 句點、引號或 `in 'equiv_status -assert'` 這種 wording——這些是非語意差異，
#: 不得讓一個真正的 NOT_EQUIVALENT 退化成 TOOL_ERROR。
_ASSERT_ERROR = re.compile(
    r"^[^\n]*\bERROR\b[^\n]*?unproven\s+\$equiv\s+cells", re.IGNORECASE | re.MULTILINE)
_EQUIVALENCE_PROVEN = re.compile(r"Equivalence successfully proven", re.IGNORECASE)
#: Yosys 自報的版本橫幅；有橫幅且與釘住的版本不符即工具身分不符。
_YOSYS_BANNER = re.compile(r"^\s*(Yosys\s+[^\n]*)$", re.MULTILINE)


class FormalPairError(ValueError):
    """formal pair task 或證據不符合固定契約。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json(value: Any) -> str:
    """固定 JSON 表示，供所有 descriptor／argv／task digest 使用。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dqe_reference(*, rows: list[dict[str, Any]], run_id: str, step_id: str,
                  dispatch_key: str) -> dict[str, Any]:
    """由既有 DQE DB 的唯讀 rows 建立可重算的 formal evidence reference。"""
    if (not isinstance(run_id, str) or not isinstance(step_id, str)
            or not isinstance(dispatch_key, str)
            or dispatch_key != f"{run_id}:{step_id}:1" or len(rows) != 10):
        raise FormalPairError("FORMAL_PROOF_DQE_INVALID", "DQE row set identity is invalid")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("orchestration_run_id") != run_id \
                or row.get("step_id") != step_id or row.get("attempt") != 1 \
                or row.get("dispatch_key") != dispatch_key \
                or row.get("package_id") != proof_descriptor()["dqe_hook"]["package_id"] \
                or not str(row.get("DQE_ID", "")).startswith("formal_pair."):
            raise FormalPairError("FORMAL_PROOF_DQE_INVALID", "DQE row lineage differs")
        normalized.append({key: row.get(key) for key in (
            "DQE_ID", "VALUE", "UNIT", "STATUS", "semantic_status",
            "orchestration_run_id", "step_id", "attempt", "dispatch_key",
            "source_event_id", "package_id")})
    return {"dispatch_key": dispatch_key, "row_count": len(normalized),
            "row_set_sha256": digest_json(sorted(normalized, key=lambda item: item["DQE_ID"])),
            "evaluator_id": proof_descriptor()["dqe_hook"]["evaluator_id"],
            "policy_id": proof_descriptor()["dqe_hook"]["policy_id"],
            "input_binding": proof_descriptor()["dqe_hook"]["input_binding"]}


def proof_descriptor() -> dict[str, Any]:
    """回傳 Package 固定的 proof 定義；由 method extension pin 進 plan。"""
    return {
        "proof_kind": PROOF_KIND,
        "task_schema": TASK_SCHEMA,
        "tool_result_schema": TOOL_RESULT_SCHEMA,
        "envelope_schema": ENVELOPE_SCHEMA,
        "renderer_version": RENDERER_VERSION,
        # 判讀規則的身分。Package identity 只說「哪個 Package」，renderer 只說
        # 「script 怎麼產生」；verdict 是由 decoder 決定的，因此必須自己被釘住，
        # 否則同一份 log 在不同版本下可能得到不同 verdict 而無從辨識。
        "decoder_version": DECODER_VERSION,
        # 分類規則與「不等價要怎樣才算成立」一起釘住。沒有這兩項，同一份 log
        # 可以在不同版本下被讀成 UNPROVEN 或 NOT_EQUIVALENT 而無從辨識。
        "classification_rule_version": CLASSIFICATION_RULE_VERSION,
        "classifications": list(CLASSIFICATIONS),
        "decidable_classifications": list(DECIDABLE_CLASSIFICATIONS),
        # 不等價必須有可核驗的反例證據；未證明的數量永遠不是不等價的依據。
        "inequivalence_evidence_required": True,
        "counterexample_name": COUNTEREXAMPLE_NAME,
        "counterexample_schema": COUNTEREXAMPLE_SCHEMA,
        "assert_command": ASSERT_COMMAND,
        "timeout_seconds": TIMEOUT_SECONDS,
        "baseline_input_role": "baseline_netlist",
        "candidate_input_role": "candidate_netlist",
        # 元件庫是 proof 身分的一部分：載入方式改了 descriptor digest 就會變。
        "cell_library_input_role": CELL_LIBRARY_ROLE,
        "cell_library_load_command": CELL_LIBRARY_LOAD_COMMAND,
        "cell_library_container_dir": CELL_LIBRARY_CONTAINER_DIR,
        "script_name": SCRIPT_NAME,
        "status_name": STATUS_NAME,
        # DQE hook 是 Package descriptor 的一部分；Common Runtime 只依這個
        # 已釘住的宣告載入 after phase，不認識 formal 的 evaluator 名稱。
        "dqe_hook": {
            "schema_version": "package.single_tool_dqe_hook.v1",
            "registry_loader_spec": (
                "terminux_engine.packages.orfs_run_intelligence.formal_pair_dqe:"
                "load_runtime_package"),
            "package_id": "orfs_formal_pair_dqe",
            "policy_id": "orfs.formal_pair_evidence_readiness.v1",
            "evaluator_id": "orfs.formal_pair_dqe.v1",
            "required_rows": 10,
            # immutable receipt 在 dispatcher 之後才形成；DQE 只綁定已捕獲的
            # Kernel run result／manifest，絕不可要求未來的 receipt。
            "input_binding": "FP_RUN_RESULT_BINDING",
        },
        "tool": {
            "image_id": PINNED_IMAGE,
            "executable_realpath": YOSYS_REALPATH,
            "executable_sha256": YOSYS_SHA256,
            "version_output": YOSYS_VERSION,
            "version_sha256": hashlib.sha256(YOSYS_VERSION.encode("utf-8")).hexdigest(),
        },
    }


def descriptor_digest() -> str:
    return digest_json(proof_descriptor())


def _require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise FormalPairError("FORMAL_PROOF_DESCRIPTOR_INVALID", f"{field} must be sha256")
    return value


def _artifact(value: Any, *, role: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"role", "path", "sha256"}:
        raise FormalPairError("FORMAL_PROOF_ARTIFACT_INVALID", f"{role} artifact is invalid")
    if value["role"] != role or not isinstance(value["path"], str):
        raise FormalPairError("FORMAL_PROOF_ARTIFACT_INVALID", f"{role} artifact role differs")
    path = Path(value["path"])
    if not path.is_absolute() or not path.is_file():
        raise FormalPairError("FORMAL_PROOF_ARTIFACT_MISSING", f"{role} artifact is missing")
    expected = _require_hash(value["sha256"], f"{role}.sha256")
    if sha256_file(path) != expected:
        raise FormalPairError("FORMAL_PROOF_ARTIFACT_HASH_MISMATCH",
                              f"{role} artifact hash differs")
    return {"role": role, "path": str(path.resolve()), "sha256": expected}


def render_script(*, top_module: str,
                  cell_libraries: tuple[str, ...] = ()) -> str:
    """產生唯一 canonical Yosys script；不接受任意 Tcl／shell 片段。

    `cell_libraries` 是 container 內的 liberty 路徑，依序以
    `read_liberty -ignore_miss_func` 載入，讓 gate-level netlist 的元件有**功能**模型可解析。
    使用 `-ignore_miss_func` 是為了略過 liberty 中沒有布林 function 屬性的 8 個 CLKGATE
    系列 cell（如 CLKGATETST_X1 等），且驗證條件是待證 netlist 絕對不能使用任何被略過的 cell。
    """
    if not isinstance(top_module, str) or not _TOP_MODULE.fullmatch(top_module):
        raise FormalPairError("FORMAL_PROOF_TOP_INVALID", "top module name is invalid")
    for path in cell_libraries:
        if not isinstance(path, str) or not path.startswith(
                CELL_LIBRARY_CONTAINER_DIR + "/"):
            raise FormalPairError("FORMAL_PROOF_CELL_LIBRARY_INVALID",
                                  "cell library path is outside the pinned mount")
    # gold／gate 的 namespace 在 Yosys design 中分離，避免兩邊同名 top 互相覆寫。
    # 每次 stash 後目前 design 會被清空，因此在 baseline、candidate 與 equiv 前
    # 均須載入元件庫功能模型，確保所有 gate-level 元件均能解析。
    load_libs = tuple(f"{CELL_LIBRARY_LOAD_COMMAND} {path}" for path in cell_libraries)
    return "\n".join((
        "# " + RENDERER_VERSION,
        *load_libs,
        "read_verilog -formal /inputs/baseline.v",
        f"prep -top {top_module}",
        "design -stash gold",
        *load_libs,
        "read_verilog -formal /inputs/candidate.v",
        f"prep -top {top_module}",
        "design -stash gate",
        *load_libs,
        f"design -copy-from gold -as gold {top_module}",
        f"design -copy-from gate -as gate {top_module}",
        "equiv_make gold gate equiv",
        "prep -top equiv",
        "equiv_simple",
        "equiv_status -assert",
        "",
    ))



def canonical_argv(*, docker_executable: str, task_root: str | Path,
                   image_id: str = PINNED_IMAGE,
                   cell_library_names: tuple[str, ...] = ()) -> list[str]:
    """產生 host 的 Docker argv；container 內直接執行固定 realpath 的 Yosys。

    兩個 input 是檔案級唯讀 mount，輸出只允許 task-owned root；沒有 PATH 搜尋、
    shell 或 ``bash -c``。
    """
    docker = Path(docker_executable)
    root = Path(task_root).resolve()
    if (not docker.is_absolute() or not docker.is_file() or not root.is_dir()
            or image_id != PINNED_IMAGE):
        raise FormalPairError("FORMAL_PROOF_TOOL_IDENTITY_INVALID",
                              "Docker executable, root, or image is invalid")
    baseline = root / "inputs" / "baseline.v"
    candidate = root / "inputs" / "candidate.v"
    output = root / "output"
    if not baseline.is_file() or not candidate.is_file() or not output.is_dir():
        raise FormalPairError("FORMAL_PROOF_TASK_LAYOUT_INVALID", "task files are missing")
    library_mounts: list[str] = []
    for name in cell_library_names:
        source = (root / "inputs" / "lib" / name).resolve()
        if not source.is_file() or source.parent != (root / "inputs" / "lib").resolve():
            raise FormalPairError("FORMAL_PROOF_TASK_LAYOUT_INVALID",
                                  "a pinned cell library is missing")
        library_mounts += ["--mount", (f"type=bind,src={source},"
                                       f"dst={CELL_LIBRARY_CONTAINER_DIR}/{name},"
                                       "readonly")]
    return [
        str(docker.resolve()), "run", "--rm", "--network", "none", "--read-only",
        "--mount", f"type=bind,src={baseline.resolve()},dst=/inputs/baseline.v,readonly",
        "--mount", f"type=bind,src={candidate.resolve()},dst=/inputs/candidate.v,readonly",
        *library_mounts,
        "--mount", f"type=bind,src={output.resolve()},dst=/out",
        image_id, YOSYS_REALPATH, "-ql", f"/out/{LOG_NAME}", "-s", f"/out/{SCRIPT_NAME}",
    ]


def build_task(*, work_id: str, attempt_id: str, task_id: str, task_digest: str,
               grant_id: str, run_id: str, package_identity: dict[str, Any],
               method_extension: dict[str, Any], baseline: dict[str, Any],
               candidate: dict[str, Any], input_manifest: dict[str, Any],
               task_root: str | Path, top_module: str,
               cell_libraries: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """由 host 已釘住的 method extension 建立 proof task，並在落盤前重驗 bytes。"""
    required_ids = {"work_id": work_id, "attempt_id": attempt_id, "task_id": task_id,
                    "task_digest": task_digest, "grant_id": grant_id, "run_id": run_id}
    if any(not isinstance(value, str) or not value for value in required_ids.values()):
        raise FormalPairError("FORMAL_PROOF_IDENTITY_INVALID", "task identity is incomplete")
    if not isinstance(package_identity, dict) or not isinstance(method_extension, dict):
        raise FormalPairError("FORMAL_PROOF_DESCRIPTOR_INVALID", "package or extension is invalid")
    extension_descriptor = method_extension.get("descriptor")
    if (not isinstance(extension_descriptor, dict)
            or extension_descriptor.get("formal_pair_proof") != proof_descriptor()
            or method_extension.get("descriptor_sha256") != digest_json(extension_descriptor)):
        raise FormalPairError("FORMAL_PROOF_DESCRIPTOR_MISMATCH",
                              "method extension does not pin this proof descriptor")
    baseline_artifact = _artifact(baseline, role="baseline_netlist")
    candidate_artifact = _artifact(candidate, role="candidate_netlist")
    if (not isinstance(input_manifest, dict)
            or set(input_manifest) != {"manifest_sha256", "tree_hash"}):
        raise FormalPairError("FORMAL_PROOF_INPUT_MANIFEST_INVALID", "input manifest is invalid")
    manifest = {key: _require_hash(value, key) for key, value in input_manifest.items()}
    root = Path(task_root).resolve()
    if not root.is_absolute():
        raise FormalPairError("FORMAL_PROOF_TASK_ROOT_INVALID", "task root is invalid")
    # 元件庫逐檔重驗 bytes，並以 container 內的固定路徑進 script。
    libraries = tuple(_artifact(entry, role=CELL_LIBRARY_ROLE)
                      for entry in cell_libraries)
    names = tuple(Path(entry["path"]).name for entry in libraries)
    if len(set(names)) != len(names):
        raise FormalPairError("FORMAL_PROOF_CELL_LIBRARY_INVALID",
                              "cell library file names collide")
    script = render_script(
        top_module=top_module,
        cell_libraries=tuple(f"{CELL_LIBRARY_CONTAINER_DIR}/{name}"
                             for name in names))
    task = {
        "schema_version": TASK_SCHEMA, **required_ids,
        "proof_kind": PROOF_KIND, "package_identity": package_identity,
        "method_extension": method_extension, "proof_descriptor": proof_descriptor(),
        "proof_descriptor_sha256": descriptor_digest(), "baseline": baseline_artifact,
        "candidate": candidate_artifact, "input_manifest": manifest,
        # 元件庫內容與版本進 task，因此也進 proof_plan_digest。
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


def materialize_task(task: dict[str, Any]) -> dict[str, Any]:
    """把已釘住的兩份 input 與 script 寫進 task-owned root，重驗後才可 dispatch。"""
    if not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA:
        raise FormalPairError("FORMAL_PROOF_TASK_INVALID", "proof task schema differs")
    if task.get("proof_descriptor") != proof_descriptor() \
            or task.get("proof_descriptor_sha256") != descriptor_digest():
        raise FormalPairError("FORMAL_PROOF_DESCRIPTOR_MISMATCH", "proof descriptor differs")
    recompute = dict(task)
    plan_digest = recompute.pop("proof_plan_digest", None)
    if plan_digest != digest_json(recompute):
        raise FormalPairError("FORMAL_PROOF_PLAN_DIGEST_MISMATCH", "proof plan digest differs")
    root = Path(str(task["task_root"])).resolve()
    # daemon 會在 Popen 前建立 task-owned cwd；這不是 caller materialize 的
    # 證據，故只接受尚未含檔案的既有 root，避免重送覆寫先前的 child 輸出。
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise FormalPairError("FORMAL_PROOF_TASK_ROOT_EXISTS", "task root is not empty")
    root.mkdir(parents=True, exist_ok=True)
    inputs, output = root / "inputs", root / "output"
    inputs.mkdir()
    output.mkdir()
    for entry in task.get("cell_libraries") or []:
        library_dir = inputs / "lib"
        library_dir.mkdir(exist_ok=True)
        source = Path(entry["path"])
        destination = library_dir / source.name
        destination.write_bytes(source.read_bytes())
        if sha256_file(destination) != entry["sha256"]:
            raise FormalPairError("FORMAL_PROOF_ARTIFACT_HASH_MISMATCH",
                                  "a pinned cell library changed while copying")
    for name, source_key in (("baseline.v", "baseline"), ("candidate.v", "candidate")):
        source = Path(task[source_key]["path"])
        destination = inputs / name
        destination.write_bytes(source.read_bytes())
        if sha256_file(destination) != task[source_key]["sha256"]:
            raise FormalPairError("FORMAL_PROOF_ARTIFACT_HASH_MISMATCH",
                                  f"{source_key} changed during materialization")
    # 重算 script 必須用**同一份**釘住的元件庫，否則會寫出與 task digest
    # 不符的腳本：檔案先落盤、digest 檢查才失敗，proof child 因此死在這裡。
    script = render_script(
        top_module=task["top_module"],
        cell_libraries=tuple(
            f"{CELL_LIBRARY_CONTAINER_DIR}/{Path(entry['path']).name}"
            for entry in (task.get("cell_libraries") or [])))
    script_path = output / SCRIPT_NAME
    script_path.write_text(script, encoding="utf-8")
    if sha256_file(script_path) != task["script"]["sha256"]:
        raise FormalPairError("FORMAL_PROOF_SCRIPT_DIGEST_MISMATCH", "script differs")
    return {"task_root": str(root), "script": str(script_path),
            "script_sha256": task["script"]["sha256"]}


def decode_tool_result(*, task: dict[str, Any], argv: list[str], exit_code: int,
                       timed_out: bool) -> dict[str, Any]:
    """由 child 產物建立 raw tool result；exit code 從不是唯一判定依據。"""
    root = Path(str(task["task_root"])).resolve()
    output = root / "output"
    script = output / SCRIPT_NAME
    log = output / LOG_NAME
    # timeout／缺 log／script digest 不符時沒有可解析的 summary；這些分支同樣
    # 必須產出 raw result，不能因為沒有 proven／unproven 計數就中止。
    proven, unresolved, assert_failure = 0, None, False
    counterexample: dict[str, Any] | None = None
    if not script.is_file() or sha256_file(script) != task["script"]["sha256"]:
        status, reason = "UNVERIFIABLE", "script_digest_mismatch"
        text = ""
    elif timed_out:
        # timeout 永遠只是 timeout：它沒有 verdict，也不得被讀成工具錯誤。
        status, reason, text = "TIMEOUT", "process_timeout", ""
    elif exit_code < 0:
        # `SubprocessExecutor` 以 -1 表示 timeout（已在上面處理）；其餘負值是
        # signal termination，證據不完整，不可能是 proof verdict。
        status, reason, text = "TOOL_ERROR", "signal_termination", ""
    elif not log.is_file():
        status, reason, text = "TOOL_ERROR", "status_report_missing", ""
    else:
        text = log.read_text(encoding="utf-8", errors="replace")
        # 已驗過 digest 的 script 才是「這次真的下了 assert」的依據；
        # log 的字面訊息不能代替它。
        asserted = ASSERT_COMMAND in script.read_text(encoding="utf-8", errors="replace")
        banner = _YOSYS_BANNER.search(text)
        summary = _EQUIV_SUMMARY.search(text)
        assert_failure = bool(_ASSERT_ERROR.search(text))
        if summary is not None:
            proven, unresolved = int(summary.group(1)), int(summary.group(2))
        if banner is not None and banner.group(1).strip() != YOSYS_VERSION:
            # 工具身分不符：不管 log 說了什麼，都不是這個 descriptor 的證據。
            status, reason = "TOOL_ERROR", "tool_identity_mismatch"
        elif not asserted:
            status, reason = "TOOL_ERROR", "proof_command_missing_assert"
        elif summary is None:
            # status report 缺 equiv_status 計數 ⇒ 無法確認 unproven 結果。
            status, reason = "TOOL_ERROR", "status_report_unparseable"
        elif exit_code != 0 and unresolved > 0 and assert_failure:
            # 非 timeout、非 signal、script 真的下了 assert、計數可解析且
            # unproven>0、exit 非零、且 log 有相容的 assert failure。
            # wording／標點／引號差異不影響這個判定。
            #
            # 這組事實**只**支持「仍有未證明義務」。要讀成不等價，必須另外有
            # 可核驗的反例證據；沒有就是 `UNPROVEN`，不是 `NOT_EQUIVALENT`。
            counterexample = _read_counterexample(output)
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
        "proof_plan_digest": task["proof_plan_digest"],
        "script_sha256": task["script"]["sha256"], "argv": list(argv),
        "argv_sha256": digest_json(argv), "timeout_seconds": task["timeout_seconds"],
        "exit_code": exit_code, "timed_out": timed_out, "status": status,
        "reason_code": reason, "proven_count": proven,
        "unproven_count": unresolved,
        # 「assert 真的失敗了」只在證據齊備、已經完成判讀的分支成立；
        # tool error／signal／矛盾 exit code 一律不得宣稱這件事。
        "equiv_status_assert_failure": status in ("UNPROVEN", "NOT_EQUIVALENT"),
        # 未證明的義務數量。它是 `UNPROVEN` 的依據，**不是**不等價的依據。
        "unproven_obligations": unresolved if status in (
            "UNPROVEN", "NOT_EQUIVALENT", "EQUIVALENT") else None,
        # 不等價證據：`None` 代表這次執行沒有任何足以支持不等價的可核驗反例。
        "counterexample": counterexample,
        "inequivalence_evidence": counterexample is not None,
        "classification_rule_version": CLASSIFICATION_RULE_VERSION,
        "status_report": {"path": str(log), "sha256": sha256_file(log) if log.is_file() else None},
        "tool": proof_descriptor()["tool"],
        "baseline": task["baseline"], "candidate": task["candidate"],
        "input_manifest": task["input_manifest"],
    }
    status_path = output / STATUS_NAME
    # raw result 是 child 的非權威事實；receipt／manifest／DQE／result event
    # 一律仍由 host 建立，不能讓 child 偽填。
    raw_path = output / RAW_RESULT_NAME
    raw_path.write_text(canonical_json(result) + "\n", encoding="utf-8")
    result["raw_result"] = {"path": str(raw_path), "sha256": sha256_file(raw_path)}
    status_path.write_text(canonical_json(result) + "\n", encoding="utf-8")
    result["status_report"]["result_path"] = str(status_path)
    result["status_report"]["result_sha256"] = sha256_file(status_path)
    return result


def _read_counterexample(output: Path) -> dict[str, Any] | None:
    """讀取並核驗 recipe 寫出的反例紀錄；任何不完整一律視為沒有反例。

    這是不等價的**唯一**依據，因此檢查一律 fail closed：schema 不符、欄位缺
    失、或內容不指出「哪個輸出端點在哪一組賦值下不同」，都當作沒有證據，
    分類退回 `UNPROVEN`。刻意不接受由 log 文字推論出來的「疑似反例」。
    """
    path = output / COUNTEREXAMPLE_NAME
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") != COUNTEREXAMPLE_SCHEMA:
        return None
    endpoint = document.get("failing_endpoint")
    assignment = document.get("input_assignment")
    if not isinstance(endpoint, str) or not endpoint \
            or not isinstance(assignment, dict) or not assignment:
        return None
    return {"schema_version": COUNTEREXAMPLE_SCHEMA, "failing_endpoint": endpoint,
            "input_assignment": {str(key): assignment[key] for key in sorted(assignment)},
            "path": str(path), "sha256": sha256_file(path)}


def attest_result(*, task: dict[str, Any], tool_result: dict[str, Any],
                  receipt: dict[str, Any], manifest: dict[str, Any],
                  dqe: dict[str, Any]) -> dict[str, Any]:
    """在 immutable receipt 後由 host 建立 durable proof envelope。

    固定 authority 順序是 Kernel raw result → manifest → DQE → immutable
    receipt → envelope → result event。event 只能單向引用 envelope digest，
    因此不得反向塞進 envelope。
    """
    required = ("receipt_id", "content_hash")
    if (not isinstance(tool_result, dict) or tool_result.get("schema_version") != TOOL_RESULT_SCHEMA
            or any(tool_result.get(key) != task[key] for key in
                   ("work_id", "attempt_id", "task_id", "task_digest", "grant_id", "run_id"))
            or tool_result.get("proof_plan_digest") != task.get("proof_plan_digest")
            or tool_result.get("script_sha256") != task.get("script", {}).get("sha256")
            or tool_result.get("tool") != proof_descriptor()["tool"]
            or tool_result.get("timeout_seconds") != TIMEOUT_SECONDS):
        raise FormalPairError("FORMAL_PROOF_TOOL_RESULT_MISMATCH", "tool result lineage differs")
    if (not isinstance(receipt, dict) or any(not receipt.get(key) for key in required)
            or not isinstance(manifest, dict) or not manifest.get("manifest_id")
            or not isinstance(dqe, dict) or not dqe.get("dispatch_key")
            or dqe.get("row_count") != 10 or not isinstance(dqe.get("row_set_sha256"), str)):
        raise FormalPairError("FORMAL_PROOF_HOST_EVIDENCE_INCOMPLETE",
                              "host evidence lineage is incomplete")
    if (receipt.get("run_id") != task["run_id"]
            or (manifest.get("producer") or {}).get("run_id") != task["run_id"]
            or not str(dqe["dispatch_key"]).startswith(task["run_id"] + ":")):
        raise FormalPairError("FORMAL_PROOF_HOST_LINEAGE_MISMATCH",
                              "receipt, manifest, DQE, or result event differs")
    hook = proof_descriptor()["dqe_hook"]
    if any(dqe.get(key) != hook[key] for key in
           ("evaluator_id", "policy_id", "input_binding")):
        raise FormalPairError("FORMAL_PROOF_HOST_LINEAGE_MISMATCH",
                              "DQE evaluator or policy identity differs")
    envelope = {
        "schema_version": ENVELOPE_SCHEMA, "proof_kind": PROOF_KIND,
        "classification": tool_result["status"], "task": {
            key: task[key] for key in ("work_id", "attempt_id", "task_id", "task_digest",
                                       "grant_id", "run_id", "proof_plan_digest")},
        "package_identity": task["package_identity"], "method_extension": task["method_extension"],
        "proof_descriptor_sha256": task["proof_descriptor_sha256"],
        "tool_result": tool_result, "receipt": receipt, "artifact_manifest": manifest,
        "dqe": dqe,
    }
    envelope["durable_envelope_sha256"] = digest_json(envelope)
    return envelope


def read_durable_envelope(envelope: dict[str, Any], result_event: dict[str, Any],
                          dqe_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fresh reader：由 result event 單向驗證 envelope，不補欄位、更不重跑工具。"""
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
    if (envelope.get("proof_kind") != PROOF_KIND or raw.get("schema_version") != TOOL_RESULT_SCHEMA
            or raw.get("proof_kind") != PROOF_KIND
            or raw.get("tool") != proof_descriptor()["tool"]
            or raw.get("timeout_seconds") != TIMEOUT_SECONDS
            or not isinstance(raw.get("argv_sha256"), str)
            or raw.get("script_sha256") is None):
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
            or not isinstance(dqe, dict) or dqe.get("row_count") != 10
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
    status = raw.get("status")
    recomputed = _recompute_verdict(raw)
    if status != recomputed or envelope.get("classification") != recomputed:
        # lineage 全部對得上，只有分類對不上，而且這份 envelope 是在**另一個**
        # 已釘住的 descriptor 下產生的 ⇒ 這是判讀規則改版，不是證據被竄改。
        # 兩者的處置都一樣（不得當成已成立的 proof），但理由必須分得開：
        # 把規則改版報成 UNVERIFIABLE 等於誣指歷史證據有問題。
        if status == envelope.get("classification") and str(
                envelope.get("proof_descriptor_sha256") or "") != descriptor_digest():
            return {"state": "CLASSIFICATION_SUPERSEDED",
                    "reason": "decoder_rule_superseded",
                    "recorded_classification": status,
                    "current_classification": recomputed,
                    "recorded_descriptor_sha256": envelope.get(
                        "proof_descriptor_sha256"),
                    "current_descriptor_sha256": descriptor_digest(),
                    "durable_envelope_sha256": claimed_digest}
        return {"state": "UNVERIFIABLE", "reason": "proof_status_mismatch"}
    return {"state": "VERIFIED", "classification": recomputed,
            "durable_envelope_sha256": claimed_digest}


def _recompute_verdict(raw: dict[str, Any]) -> str:
    """依 raw facts 重算，不以 exit code 單獨決定。

    與 `decode_tool_result` 同一組規則：未證明的數量只支持 `UNPROVEN`；
    要重算出 `NOT_EQUIVALENT`，raw 必須自己帶著已核驗的反例證據。
    """
    if raw.get("timed_out") is True:
        return "TIMEOUT"
    unproven = raw.get("unproven_count")
    if (raw.get("exit_code") != 0 and isinstance(unproven, int) and unproven > 0
            and raw.get("equiv_status_assert_failure") is True):
        return ("NOT_EQUIVALENT" if _valid_counterexample(raw.get("counterexample"))
                else "UNPROVEN")
    if (raw.get("exit_code") == 0 and unproven == 0
            and raw.get("reason_code") == "equiv_status_assert_succeeded"):
        return "EQUIVALENT"
    if raw.get("exit_code") != 0:
        return "TOOL_ERROR"
    return "UNVERIFIABLE"


def _valid_counterexample(value: Any) -> bool:
    """envelope 內保存的反例紀錄是否仍然自洽。內容不足一律不算。"""
    if not isinstance(value, dict) or value.get("schema_version") != COUNTEREXAMPLE_SCHEMA:
        return False
    assignment = value.get("input_assignment")
    return bool(value.get("failing_endpoint")) and isinstance(assignment, dict) \
        and bool(assignment)


def validate_workflow_config(config: dict[str, Any]) -> None:
    """驗證 package_single_tool 的 formal-pair 分支；未知欄位不接受。"""
    required = {"kind", "proof_task", "docker_executable", "output_root", "artifact_root",
                "timeout_seconds", "step_id"}
    if not isinstance(config, dict) or set(config) != required or config.get("kind") != "formal_pair":
        raise ExecutionContractError("formal pair config has unexpected fields")
    if (not isinstance(config["docker_executable"], str)
            or not isinstance(config["output_root"], str)
            or not isinstance(config["artifact_root"], str)
            or not isinstance(config["step_id"], str)
            or config["timeout_seconds"] != TIMEOUT_SECONDS):
        raise ExecutionContractError("formal pair config identity or timeout is invalid")
    task = config["proof_task"]
    if (not isinstance(task, dict) or task.get("schema_version") != TASK_SCHEMA
            or task.get("task_root") != config["output_root"]
            or task.get("timeout_seconds") != TIMEOUT_SECONDS):
        raise ExecutionContractError("formal pair task differs from its host binding")


def workflow_config(*, task: dict[str, Any], docker_executable: str,
                    artifact_root: str, step_id: str) -> dict[str, Any]:
    """建立由 Package 簽入 binding 的 generic adapter config。"""
    config = {
        "schema_version": "package.adapter_binding.v1", "tool_id": "formal_equivalence",
        "adapter_spec": (
            "terminux_engine.packages.orfs_run_intelligence.formal_pair:"
            "FormalPairToolAdapter"),
        "adapter_kwargs": {"docker_executable": docker_executable},
        "parameters": {"proof_task": task}, "host_executable": docker_executable,
        "output_root": task.get("task_root"), "artifact_root": artifact_root,
        "timeout_seconds": TIMEOUT_SECONDS, "step_id": step_id,
        "dqe_hook": {key: proof_descriptor()["dqe_hook"][key] for key in
                     ("schema_version", "registry_loader_spec", "package_id", "input_binding")},
    }
    validate_workflow_config({"kind": "formal_pair", "proof_task": task,
                              "docker_executable": docker_executable,
                              "output_root": task.get("task_root"),
                              "artifact_root": artifact_root,
                              "timeout_seconds": TIMEOUT_SECONDS, "step_id": step_id})
    return config


class FormalPairToolAdapter:
    """container 版 formal pair 的 `ExecutionAdapter`。

    container 模式不再啟動新的 proof：`bind_kernel_identity()` 與 `prepare()`
    一律以 `LEGACY_DOCKER_EXECUTION_DISABLED` 拒絕（在 run identity 綁定、
    task 寫入與 docker argv 之前）。`discover_artifacts()` 保留，供既有 run 讀取。
    """

    adapter_id = "orfs_formal_pair"
    adapter_version = "orfs_formal_pair/1"

    def __init__(self, *, docker_executable: str) -> None:
        executable = Path(docker_executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ExecutionContractError("formal pair Docker executable is invalid")
        self._docker = str(executable.resolve())

    def bind_kernel_identity(self, config: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
        """container 模式不再綁定新的 proof run。"""
        del config, identity
        raise LegacyDockerExecutionDisabled(PATH_FORMAL_PAIR)

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        """container 模式不再派送：不 materialize task、不組 docker argv。"""
        del request
        raise LegacyDockerExecutionDisabled(PATH_FORMAL_PAIR)

    def discover_artifacts(self, request: ExecutionRequest, prepared: PreparedExecution, *,
                           process_exit_code: int) -> ArtifactDiscoveryResult:
        task = prepared.artifact_discovery_context.get("proof_task")
        if not isinstance(task, dict):
            raise ExecutionContractError("formal pair task context is missing")
        # `SubprocessExecutor` 用 -1 明確記錄 timeout；其他非零碼由 decoder
        # 依 Yosys status report 區分 NOT_EQUIVALENT 與 TOOL_ERROR。
        decode_tool_result(task=task, argv=prepared.argv, exit_code=process_exit_code,
                           timed_out=process_exit_code == -1)
        root = Path(task["task_root"]).resolve()
        output = root / "output"
        candidates = [ArtifactCandidate(adapter_role="orfs.formal_pair." + name,
                                        host_path=output / name,
                                        exists=(output / name).is_file())
                      for name in (SCRIPT_NAME, LOG_NAME, STATUS_NAME, RAW_RESULT_NAME)]
        return ArtifactDiscoveryResult(output_roots=[root], artifact_candidates=candidates)


__all__ = ["CLASSIFICATIONS", "CLASSIFICATION_RULE_VERSION", "COUNTEREXAMPLE_NAME",
           "COUNTEREXAMPLE_SCHEMA", "DECIDABLE_CLASSIFICATIONS", "DECODER_VERSION",
           "ENVELOPE_SCHEMA", "FormalPairError", "LOG_NAME", "PINNED_IMAGE",
           "PROOF_KIND", "RAW_RESULT_NAME", "RENDERER_VERSION", "STATUS_NAME", "TASK_SCHEMA",
           "TIMEOUT_SECONDS", "TOOL_RESULT_SCHEMA", "YOSYS_REALPATH", "attest_result",
           "FormalPairToolAdapter", "attest_result", "build_task", "canonical_argv",
           "dqe_reference", "bind_kernel_run",
           "decode_tool_result", "descriptor_digest", "materialize_task", "proof_descriptor",
           "read_durable_envelope", "render_script", "validate_workflow_config", "workflow_config"]
