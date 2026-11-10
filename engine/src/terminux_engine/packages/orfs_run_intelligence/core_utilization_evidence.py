# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""CORE_UTILIZATION extension 的 per-slot 證據格式（Package 擁有）。

每個 slot 的 run 在自己的 task-owned output 區寫一份
``terminux_core_utilization_<slot>.json``：方法身分、實際生效的
``CORE_UTILIZATION``、materialized config 的 hash、消費它的 floorplan 證據、
input／scripts／image／工具身分與 proof 引用。

**執行身分**（receipt／artifact manifest／DQE／result event）不在這份檔案裡：
它們由 host 在判定時從本次 run 的 durable 證據補上（`host_run_facts`），因此
replay 的 corpus 不可能把舊 run 的執行身分冒充成本次的。

兩條入口，語意不同，**不以布林參數互換**
------------------------------------------

* `build_fixture_slot_evidence()`：corpus replay 專用。固定 `fixture=True`、
  `openroad_launched=False`、`evidence_origin=FIXTURE`，不宣稱工程結論。
* `build_orfs_slot_evidence()`：真實 ORFS 專用。**不接受呼叫端宣告的
  provenance**——它自己從本次 run 的權威資料推導：floorplan log 的實測
  `-utilization`、materialized config、effective settings、artifact manifest
  的 hash 與 task／run／slot lineage。任何一項缺失、歧義、hash 不符或路徑
  逸出，一律拋 `CoreEvidenceUnverifiable`，不以 config 補判。

為什麼要分成兩個函式而不是一個布林旗標：布林旗標會讓任何呼叫端都能自稱
`fixture=False`。真實路徑的可信度必須來自「它驗了什麼」，不是「它被告知什麼」。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "orfs.core_utilization.evidence.v1"
#: 逐 slot 的固定檔名（相對於 `reports/<platform>/<design>/<variant>/`）。
EVIDENCE_FILENAME_TEMPLATE = "terminux_core_utilization_{slot}.json"
#: 由 host 在判定時補上的執行身分欄位。
HOST_RUN_FIELDS = ("receipt_digest", "manifest_digest", "dqe_digest",
                   "result_event_id")
#: 兩個 slot 必須相同的共同條件（`evaluate_core_utilization` 用來判 comparable）。
COMMON_CONDITION_FIELDS = ("rtl_digest", "sdc_digest", "corner", "library",
                           "scripts_digest", "image_digest", "tool_digest")

#: 本方法的固定身分。真實路徑不接受呼叫端改寫。
METHOD_IDENTITY = "CORE_UTILIZATION"
#: `evidence_origin` 的兩個值。ORFS 只由真實路徑產生。
ORIGIN_ORFS = "ORFS"
ORIGIN_FIXTURE = "FIXTURE"
#: `evidence_source`：這份 slot 證據是這次新收的，還是重用既有 durable
#: reference。**不可省略**——重用的 baseline 不得被當成這次重新收集的證據。
SOURCE_THIS_RUN = "THIS_RUN"
SOURCE_REUSED_REFERENCE = "REUSED_DURABLE_REFERENCE"

#: pair-proof envelope 內兩側輸入的角色名稱。
PROOF_ROLE_BASELINE = "baseline_netlist"
PROOF_ROLE_CANDIDATE = "candidate_netlist"
#: envelope 的 classification → 本方法的 `proof_status`。
#: 只有 `EQUIVALENT` 能讓 `evaluate_core_utilization` 的 `proof_ok` 成立。
#: `NOT_EQUIVALENT` 與 `UNPROVEN` 都是**可信**的工程結果（證據鏈完整、分類
#: 成立），可以形成明確的拒絕判定，但一律不得進入可選結果。兩者必須分開保存：
#: 「沒證出來」不等於「證出不一樣」。
#: 其餘（TOOL_ERROR／TIMEOUT／未知）一律不可驗證，連比較都不放行。
PROOF_STATUS_BY_CLASSIFICATION = {
    "EQUIVALENT": "equivalent",
    "NOT_EQUIVALENT": "not_equivalent",
    "UNPROVEN": "unproven",
}
#: 真實路徑必須齊備的 durable 執行身分；缺任何一個就是 UNVERIFIABLE。
REQUIRED_RUN_IDENTITY = ("task_id", "run_id", "grant_id", "receipt_digest",
                         "manifest_digest", "dqe_digest", "result_event_id")
#: materialized config 內的參數賦值。只接受明確的 `NAME = VALUE` 形式。
_CONFIG_ASSIGNMENT = re.compile(
    r"^\s*export\s+CORE_UTILIZATION\s*[:?]?=\s*(?P<value>[^\s#]+)",
    re.MULTILINE)


class CoreEvidenceUnverifiable(RuntimeError):
    """真實 ORFS 的 CORE 證據無法從權威資料成立。

    刻意不提供「降級」路徑：呼叫端只能停下，不能改用 config 宣告值補判。
    """

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


def file_identity(path: str | Path) -> dict[str, str]:
    """檔案身分：路徑與 bytes 的 SHA-256。讀不到就是沒有這份證據。"""
    resolved = Path(path)
    return {"path": str(resolved),
            "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest()}


def build_fixture_slot_evidence(
    *, method_identity: str, extension_version: str, slot: str, task_id: str,
    run_id: str, core_utilization: str, materialized_config: str | Path,
    floorplan_log: str | Path, scripts_digest: str, image_digest: str,
    tool_digest: str, rtl_digest: str, sdc_digest: str, corner: str, library: str,
    proof_references: tuple[str | Path, ...] = (), proof_status: str = "unverifiable",
    proof_kinds: tuple[str, ...] = (),
    corpus: dict[str, Any] | None = None,
    remaining_objective_gap: str = "fixture replay: ORFS was not executed",
) -> dict[str, Any]:
    """組出一個 slot 的 **fixture** CORE 證據。一律標示且不宣稱工程改善。

    corpus replay 專用。真實 ORFS 請用 `build_orfs_slot_evidence()`——這支
    不會、也不能產生 `evidence_origin=ORFS` 的證據。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "method_identity": method_identity,
        "extension_version": extension_version,
        "slot": slot, "task_id": task_id, "run_id": run_id,
        "core_utilization": str(core_utilization),
        "materialized_config": file_identity(materialized_config),
        "consuming_floorplan_log": file_identity(floorplan_log),
        "scripts_digest": scripts_digest, "image_digest": image_digest,
        "tool_digest": tool_digest, "rtl_digest": rtl_digest,
        "sdc_digest": sdc_digest, "corner": corner, "library": library,
        "proof_references": [file_identity(row) for row in proof_references],
        "proof_kinds": list(proof_kinds),
        "proof_status": proof_status,
        # fixture replay 只證明 routing／materialization／evidence shape。
        "fixture": True, "openroad_launched": False,
        "qualification_only": True, "evidence_origin": ORIGIN_FIXTURE,
        "corpus": dict(corpus or {}),
        "setup_improved": False, "protected_metric_regressions": [],
        "remaining_objective_gap": remaining_objective_gap,
    }


@dataclass(frozen=True)
class OrfsRunProvenance:
    """一次真實 ORFS run 的**權威身分**。每個欄位都來自 durable 紀錄。

    這個 dataclass 不是「呼叫端說了算」的宣告：`build_orfs_slot_evidence()`
    會拿它去對本次 run 的磁碟產物與 artifact manifest，對不上就 UNVERIFIABLE。
    它存在的意義是把「host 已經驗過的身分」一次交進來，而不是讓 Package 自己
    去猜 run 是誰的。
    """

    task_id: str
    run_id: str
    grant_id: str
    slot: str
    receipt_digest: str
    manifest_digest: str
    dqe_digest: str
    result_event_id: str
    #: artifact manifest 的 `相對路徑 -> sha256`。floorplan log 必須在裡面。
    manifest_entries: dict[str, str] = field(default_factory=dict)

    def identity(self) -> dict[str, str]:
        return {name: str(getattr(self, name) or "")
                for name in REQUIRED_RUN_IDENTITY}


def _require_identity(provenance: OrfsRunProvenance) -> None:
    missing = sorted(name for name, value in provenance.identity().items() if not value)
    if missing:
        raise CoreEvidenceUnverifiable(
            "the ORFS run provenance is incomplete; CORE evidence cannot be "
            "derived from a partial run identity",
            missing=missing)


def _as_number(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _contained(root: Path, path: Path) -> bool:
    """`path` 必須嚴格位於 `root` 之內，且路徑上沒有 symlink 逸出。"""
    resolved = path.resolve()
    return root == resolved or root in resolved.parents


def _config_core_utilization(path: Path) -> str:
    matches = _CONFIG_ASSIGNMENT.findall(path.read_text(errors="replace"))
    if not matches:
        raise CoreEvidenceUnverifiable(
            "the materialized config does not assign CORE_UTILIZATION",
            materialized_config=str(path))
    distinct = {value.strip() for value in matches}
    if len(distinct) != 1:
        raise CoreEvidenceUnverifiable(
            "the materialized config assigns CORE_UTILIZATION more than once "
            "with different values; the effective value is ambiguous",
            materialized_config=str(path), values=sorted(distinct))
    return distinct.pop()


def build_orfs_slot_evidence(
    *, method_identity: str = METHOD_IDENTITY,
    provenance: OrfsRunProvenance, extension_version: str,
    output_root: str | Path, platform: str, design: str, variant: str,
    materialized_config: str | Path, expected_core_utilization: Any,
    scripts_digest: str, image_digest: str, tool_digest: str,
    rtl_digest: str, sdc_digest: str, corner: str, library: str,
    proof: PairProofBinding,
) -> dict[str, Any]:
    """由**真實 ORFS run 的權威產物**組出一個 slot 的 CORE 證據。

    這支函式不接受 `fixture` 或 `openroad_launched` 參數：那兩個值是它的結論，
    不是它的輸入。要成立，下列每一項都必須從本次 run 自己的資料推導出來：

    1. run 身分完整（task／run／grant／receipt／manifest／DQE／result event）。
    2. floorplan log 記錄了工具**實際收到**的 `-utilization`，且等於預期值。
       值從 log 讀，不從 config 讀——config 只用來做第三方交叉核對。
    3. 那份 log 位於本 slot 的 output root 之內、不是 symlink，且以**相同
       hash** 出現在本次 run 的 artifact manifest 裡。
    4. materialized config、effective settings 與 log 實測值三者一致
       （加上呼叫端給的預期值，共四者）。

    任何一項不成立就拋 `CoreEvidenceUnverifiable`。**沒有降級路徑**：不允許
    「log 讀不到就用 config 補」，那正是要禁止的形狀。

    `proof` 必須是已經過 `verify_pair_proof_envelope()` 的綁定：proof 結果只
    能來自 durable envelope 自己的 classification。這也是為什麼這份證據一次
    寫成——不允許先寫 `proof_status=pending` 之後再改寫 durable evidence。

    回傳的證據**不含** `selection_eligible` 與 `repair_status`——它們要等
    comparison／acceptance 決定，ORFS evidence builder 不得預先宣告。
    """
    from terminux_engine.packages.orfs_run_intelligence import flow_evidence as FE

    _require_identity(provenance)
    expected = str(expected_core_utilization).strip()
    if _as_number(expected) is None:
        raise CoreEvidenceUnverifiable(
            "the expected CORE_UTILIZATION is not numeric", expected=expected)

    root = Path(output_root).resolve()
    if not root.is_dir():
        raise CoreEvidenceUnverifiable(
            "the slot output root does not exist", output_root=str(root))

    # ── 2. 工具實際收到的值（唯一權威來源是 floorplan log） ──────────
    consumption = FE.floorplan_consumption_evidence(
        root, platform=platform, design=design, variant=variant,
        expected_value=expected)
    if not consumption.consumed:
        raise CoreEvidenceUnverifiable(
            f"the floorplan reader did not prove CORE_UTILIZATION consumption: "
            f"{consumption.reason}",
            observed=dict(consumption.observed))
    observed_value = str(consumption.observed.get("tool_received_utilization") or "")
    if _as_number(observed_value) != _as_number(expected):
        raise CoreEvidenceUnverifiable(
            "the floorplan log and the expected candidate disagree on the value",
            observed=observed_value, expected=expected)

    # ── 3. log 必須在 output root 內、非 symlink、且經 manifest attest ──
    logs = [Path(row) for row in consumption.evidence_files if row.endswith(".log")]
    if not logs:
        raise CoreEvidenceUnverifiable(
            "no floorplan log backs the consumption evidence",
            evidence_files=list(consumption.evidence_files))
    floorplan_log = logs[0]
    if os.path.islink(floorplan_log):
        raise CoreEvidenceUnverifiable(
            "the floorplan log is a symlink; its target can be repointed after "
            "the hash check", floorplan_log=str(floorplan_log))
    if not _contained(root, floorplan_log):
        raise CoreEvidenceUnverifiable(
            "the floorplan log resolves outside this slot output root",
            floorplan_log=str(floorplan_log), output_root=str(root))
    log_identity = file_identity(floorplan_log)
    resolved_log = floorplan_log.resolve()
    relative = str(resolved_log.relative_to(root))
    # manifest 的鍵是 Kernel 寫下的 `resolved_path`（絕對路徑），與
    # `_artifact_manifest_entries()` 同一個形狀。
    attested = dict(provenance.manifest_entries or {}).get(str(resolved_log))
    if attested is None:
        raise CoreEvidenceUnverifiable(
            "the floorplan log is not listed in this run's artifact manifest; "
            "the adapter must declare the orfs.floorplan_log role",
            floorplan_log=str(resolved_log))
    if attested != log_identity["sha256"]:
        raise CoreEvidenceUnverifiable(
            "the floorplan log hash does not match the artifact manifest",
            floorplan_log=str(resolved_log))

    # ── 4. 四者一致：預期值、materialized config、effective settings、log ──
    config_path = Path(materialized_config).resolve()
    config_value = _config_core_utilization(config_path)
    settings = FE.read_effective_settings(
        root, platform=platform, design=design, variant=variant,
        required=("CORE_UTILIZATION",))
    settings_value = settings.values.get("CORE_UTILIZATION")
    if settings_value is None:
        raise CoreEvidenceUnverifiable(
            "the effective settings do not report CORE_UTILIZATION; the run's "
            "effective configuration is unknown", source=settings.source)
    agreement = {
        "expected": expected, "materialized_config": config_value,
        "effective_settings": str(settings_value).strip(),
        "floorplan_log": observed_value,
    }
    numeric = {name: _as_number(value) for name, value in agreement.items()}
    if len({value for value in numeric.values()}) != 1 or None in numeric.values():
        raise CoreEvidenceUnverifiable(
            "the task snapshot, materialized config, effective settings and "
            "floorplan log do not agree on CORE_UTILIZATION",
            agreement=agreement)

    return {
        "schema_version": SCHEMA_VERSION,
        # 驗收端比對的是 method extension 宣告的身分
              # （例如 `gcd.core_utilization.v1`），不是參數名稱。
        "method_identity": method_identity,
        "extension_version": extension_version,
        "slot": provenance.slot, "task_id": provenance.task_id,
        "run_id": provenance.run_id, "grant_id": provenance.grant_id,
        "core_utilization": observed_value,
        # 實測值另外具名一次：讀的人不必猜 `core_utilization` 是宣告還是實測。
        "effective_core_utilization": observed_value,
        "core_utilization_agreement": agreement,
        "materialized_config": file_identity(config_path),
        "consuming_floorplan_log": log_identity,
        "consuming_floorplan_log_relative_path": relative,
        "artifact_manifest_attested": True,
        "floorplan_observed": dict(consumption.observed),
        "effective_settings": settings.to_json_dict(),
        "scripts_digest": scripts_digest, "image_digest": image_digest,
        "tool_digest": tool_digest, "rtl_digest": rtl_digest,
        "sdc_digest": sdc_digest, "corner": corner, "library": library,
        "pair_proof": proof.slot_view(provenance.slot),
        "proof_kinds": [proof.proof_kind],
        # proof 結果只能出自 envelope 自己的 classification。
        "proof_status": proof.proof_status,
        "fixture": False, "openroad_launched": True,
        "qualification_only": False, "evidence_origin": ORIGIN_ORFS,
        "evidence_source": SOURCE_THIS_RUN, "new_orfs_launch": True,
        # 工程結論一律留給 acceptance：ORFS 證據不得預先宣告可否選用或已修復。
        "setup_improved": False, "protected_metric_regressions": [],
        "remaining_objective_gap": (
            "the formal pair proof and the comparison have not been evaluated yet"),
    }


@dataclass(frozen=True)
class PairProofBinding:
    """一份 durable pair-proof envelope 對這次 CORE 比較的**已驗證**綁定。

    只有 `verify_pair_proof_envelope()` 會建立它。拿到這個物件就代表：
    envelope 屬於同一個 Work／attempt、descriptor 身分相符、兩側 netlist hash
    各自對得上權威來源、lineage 完整、且不是 qualification-only 的證據。
    """

    classification: str
    proof_status: str
    envelope_sha256: str
    proof_descriptor_sha256: str
    proof_kind: str
    work_id: str
    attempt_id: str
    task_id: str
    run_id: str
    grant_id: str
    baseline_sha256: str
    candidate_sha256: str
    receipt_digest: str
    manifest_digest: str
    dqe_digest: str
    tool: dict[str, Any] = field(default_factory=dict)

    @property
    def equivalent(self) -> bool:
        return self.classification == "EQUIVALENT"

    def slot_view(self, slot: str) -> dict[str, Any]:
        """這個 slot 在 proof 裡的角色與它被證明的那份輸入 hash。

        同一份 envelope 可以被兩側引用，但各自記錄自己的角色——讀的人不必
        從路徑或檔名反推誰是 baseline。
        """
        if slot == "baseline":
            role, digest = PROOF_ROLE_BASELINE, self.baseline_sha256
        elif slot == "candidate":
            role, digest = PROOF_ROLE_CANDIDATE, self.candidate_sha256
        else:
            raise CoreEvidenceUnverifiable(f"unknown proof slot: {slot!r}")
        return {
            "proof_kind": self.proof_kind, "role": role, "input_sha256": digest,
            "classification": self.classification,
            "durable_envelope_sha256": self.envelope_sha256,
            "proof_descriptor_sha256": self.proof_descriptor_sha256,
            "proof_task_id": self.task_id, "proof_run_id": self.run_id,
            "proof_grant_id": self.grant_id,
            "work_id": self.work_id, "attempt_id": self.attempt_id,
            "receipt_digest": self.receipt_digest,
            "manifest_digest": self.manifest_digest,
            "dqe_digest": self.dqe_digest,
            "tool": dict(self.tool),
        }


def verify_pair_proof_envelope(
    envelope: Any, *, work_id: str, attempt_id: str,
    expected_descriptor_sha256: str,
    baseline_netlist_sha256: str, candidate_netlist_sha256: str,
    expected_tool: dict[str, Any] | None = None,
    qualification_only: Any = None,
) -> PairProofBinding:
    """核對一份 durable pair-proof envelope，回傳可綁進 CORE 證據的結論。

    **結果只從 envelope 自己的 `classification` 取得**——不接受呼叫端傳入的
    `proof_status`，也不從路徑或檔名推測。

    核對項目（任一不符即 `CoreEvidenceUnverifiable`）：

    1. 同一個 Work／attempt。
    2. proof descriptor 身分與 digest 等於授權前已固定的值。
    3. baseline 輸入 hash 來自 durable baseline reference；candidate 輸入 hash
       來自這次 candidate artifact manifest。兩者**各自**比對，不可互換。
    4. proof task／grant／run／manifest／DQE／receipt lineage 完整且互相一致。
    5. envelope 不是 `qualification_only`；Phase B／C 的 qualification 證據
       不得被複製進 production 判定。
    6. Yosys 工具身分與 image digest 符合 descriptor 固定值。
    """
    if not isinstance(envelope, dict):
        raise CoreEvidenceUnverifiable("the proof envelope is not a document")
    if qualification_only:
        raise CoreEvidenceUnverifiable(
            "the proof envelope is qualification-only; a qualification proof is "
            "never a production engineering result")

    task = dict(envelope.get("task") or {})
    if str(task.get("work_id") or "") != str(work_id) or (
            str(task.get("attempt_id") or "") != str(attempt_id)):
        raise CoreEvidenceUnverifiable(
            "the proof envelope belongs to another Work or attempt",
            envelope_work=task.get("work_id"), envelope_attempt=task.get("attempt_id"))

    descriptor = str(envelope.get("proof_descriptor_sha256") or "")
    if descriptor != str(expected_descriptor_sha256):
        raise CoreEvidenceUnverifiable(
            "the proof descriptor digest is not the one fixed before the "
            "execution authorization", envelope_descriptor=descriptor)

    tool_result = dict(envelope.get("tool_result") or {})
    baseline = dict(tool_result.get("baseline") or {})
    candidate = dict(tool_result.get("candidate") or {})
    if baseline.get("role") != PROOF_ROLE_BASELINE or (
            candidate.get("role") != PROOF_ROLE_CANDIDATE):
        raise CoreEvidenceUnverifiable(
            "the proof inputs do not declare the expected baseline/candidate roles",
            baseline_role=baseline.get("role"), candidate_role=candidate.get("role"))
    if str(baseline.get("sha256") or "") != str(baseline_netlist_sha256):
        raise CoreEvidenceUnverifiable(
            "the proven baseline netlist is not the one from the durable "
            "baseline reference")
    if str(candidate.get("sha256") or "") != str(candidate_netlist_sha256):
        raise CoreEvidenceUnverifiable(
            "the proven candidate netlist is not the one from this run's "
            "artifact manifest")

    receipt = dict(envelope.get("receipt") or {})
    dqe = dict(envelope.get("dqe") or {})
    manifest = dict(envelope.get("artifact_manifest") or {})
    lineage = {
        "task_id": task.get("task_id"), "run_id": task.get("run_id"),
        "grant_id": task.get("grant_id"),
        "receipt_digest": receipt.get("content_hash"),
        "manifest_digest": receipt.get("artifact_manifest_sha256"),
        "dqe_digest": dqe.get("dispatch_key"),
        "envelope_sha256": envelope.get("durable_envelope_sha256"),
    }
    missing = sorted(name for name, value in lineage.items() if not value)
    if missing:
        raise CoreEvidenceUnverifiable(
            "the proof envelope lineage is incomplete", missing=missing)
    for label, value in (("receipt", receipt.get("run_id")),
                         ("tool_result", tool_result.get("run_id")),
                         ("manifest", (manifest.get("producer") or {}).get("run_id"))):
        if str(value or "") != str(task["run_id"]):
            raise CoreEvidenceUnverifiable(
                f"the proof {label} belongs to another run",
                expected=task["run_id"], found=value)
    if str(manifest.get("manifest_id") or "") != str(receipt.get("artifact_manifest_id") or ""):
        raise CoreEvidenceUnverifiable(
            "the proof receipt and artifact manifest disagree on the manifest id")

    tool = dict(tool_result.get("tool") or {})
    for name, expected_value in (dict(expected_tool or {})).items():
        if str(tool.get(name) or "") != str(expected_value):
            raise CoreEvidenceUnverifiable(
                f"the proof tool identity does not match the descriptor: {name}",
                found=tool.get(name), expected=expected_value)

    classification = str(envelope.get("classification") or "")
    status = PROOF_STATUS_BY_CLASSIFICATION.get(classification)
    if status is None:
        raise CoreEvidenceUnverifiable(
            "the proof classification is not a decidable proof result; "
            "a tool error, a timeout or an unknown status never releases the "
            "comparison", classification=classification or "(absent)")

    return PairProofBinding(
        classification=classification, proof_status=status,
        envelope_sha256=str(lineage["envelope_sha256"]),
        proof_descriptor_sha256=descriptor,
        proof_kind=str(envelope.get("proof_kind") or ""),
        work_id=str(work_id), attempt_id=str(attempt_id),
        task_id=str(lineage["task_id"]), run_id=str(lineage["run_id"]),
        grant_id=str(lineage["grant_id"]),
        baseline_sha256=str(baseline["sha256"]),
        candidate_sha256=str(candidate["sha256"]),
        receipt_digest=str(lineage["receipt_digest"]),
        manifest_digest=str(lineage["manifest_digest"]),
        dqe_digest=str(lineage["dqe_digest"]), tool=tool)


def build_reused_baseline_slot_evidence(
    *, reference: dict[str, Any], extension_version: str,
    method_identity: str = METHOD_IDENTITY,
    expected_core_utilization: Any, proof: PairProofBinding,
) -> dict[str, Any]:
    """由**已封存並驗證過的** durable baseline reference 推導 baseline slot 證據。

    這條路徑存在的理由：baseline 依授權唯讀重用、不重跑，所以這次不會有
    baseline 的新 run、新 manifest 或新 receipt。它的 CORE 事實早就封存在
    reference 裡（`details.parameter_consumption` 是當初真實 ORFS run 的
    floorplan 讀數），reference 本身又以 `reference_digest` 固定。

    **不是追溯補寫**：舊 manifest 沒有 `orfs.floorplan_log` 這件事不在這裡
    被「補上」。判定依據是 reference 內**已封存且當初已驗證**的 consumption
    facts，並且結果明確標成 `evidence_source=REUSED_DURABLE_REFERENCE`／
    `new_orfs_launch=False`，不偽裝成這次重新收集的 slot evidence。

    新的 candidate 不走這條路：它必須用新增的 `orfs.floorplan_log` manifest
    role 與實際 hash（`build_orfs_slot_evidence()`）。
    """
    if not isinstance(reference, dict):
        raise CoreEvidenceUnverifiable("the baseline reference is not a document")
    digest = str(reference.get("reference_digest") or "")
    if not digest:
        raise CoreEvidenceUnverifiable("the baseline reference carries no digest")

    provenance = dict(reference.get("provenance") or {})
    if (provenance.get("evidence_origin") != ORIGIN_ORFS
            or provenance.get("fixture") is not False
            or provenance.get("openroad_launched") is not True):
        raise CoreEvidenceUnverifiable(
            "the baseline reference does not record a real ORFS run",
            reference_digest=digest, provenance=provenance)

    identity = dict(reference.get("run_identity") or {})
    details = dict(identity.get("details") or {})
    baseline = dict(reference.get("baseline") or {})
    consumption = dict(details.get("parameter_consumption") or {})
    observed = dict(consumption.get("observed") or {})
    if not consumption.get("consumed"):
        raise CoreEvidenceUnverifiable(
            "the baseline reference does not record proven CORE_UTILIZATION "
            "consumption", reference_digest=digest)
    observed_value = str(observed.get("tool_received_utilization") or "")
    settings = dict((details.get("effective_settings") or {}).get("values") or {})
    settings_value = str(settings.get("CORE_UTILIZATION") or "")
    expected = str(expected_core_utilization).strip()
    agreement = {"expected": expected, "effective_settings": settings_value,
                 "floorplan_log": observed_value}
    numeric = {name: _as_number(value) for name, value in agreement.items()}
    if None in numeric.values() or len(set(numeric.values())) != 1:
        raise CoreEvidenceUnverifiable(
            "the baseline reference's sealed CORE_UTILIZATION facts do not agree "
            "with the expected baseline", agreement=agreement,
            reference_digest=digest)

    original = {
        "task_id": baseline.get("task_id"), "run_id": baseline.get("run_id"),
        "receipt_id": identity.get("receipt_id"),
        "receipt_digest": identity.get("receipt_content_hash"),
        "manifest_id": identity.get("artifact_manifest_id"),
        "manifest_digest": identity.get("artifact_manifest_sha256"),
        "dqe_digest": identity.get("dqe_dispatch_key"),
        "result_event_id": identity.get("result_event_id"),
    }
    missing = sorted(name for name, value in original.items() if not value)
    if missing:
        raise CoreEvidenceUnverifiable(
            "the baseline reference's original run identity is incomplete",
            missing=missing, reference_digest=digest)

    return {
        "schema_version": SCHEMA_VERSION,
        # 驗收端比對的是 method extension 宣告的身分
              # （例如 `gcd.core_utilization.v1`），不是參數名稱。
        "method_identity": method_identity,
        "extension_version": extension_version,
        "slot": "baseline",
        # slot_facts() 會拿 task_id／run_id 與本次 host 身分比對；重用的
        # baseline 沒有這次 run，因此沿用原 run 的身分並明確標示來源。
        "task_id": original["task_id"], "run_id": original["run_id"],
        "core_utilization": observed_value,
        "effective_core_utilization": observed_value,
        "core_utilization_agreement": agreement,
        "floorplan_observed": observed,
        "effective_settings": details.get("effective_settings") or {},
        "evidence_source": SOURCE_REUSED_REFERENCE,
        "new_orfs_launch": False,
        "baseline_reference_digest": digest,
        "original_run_identity": original,
        "scripts_digest": (reference.get("scripts") or {}).get("digest"),
        "image_digest": (reference.get("image") or {}).get("docker_image_id"),
        "tool_digest": ((reference.get("tools") or {}).get("yosys") or {}).get(
            "executable_sha256"),
        "rtl_digest": ((reference.get("input") or {}).get("rtl") or {}).get("sha256"),
        "sdc_digest": ((reference.get("input") or {}).get("sdc") or {}).get("sha256"),
        "corner": (reference.get("baseline") or {}).get("corner") or "",
        "library": (reference.get("baseline") or {}).get("library") or "",
        "pair_proof": proof.slot_view("baseline"),
        "proof_kinds": [proof.proof_kind],
        "proof_status": proof.proof_status,
        "fixture": False, "openroad_launched": True,
        "qualification_only": False, "evidence_origin": ORIGIN_ORFS,
        "setup_improved": False, "protected_metric_regressions": [],
        "remaining_objective_gap": (
            "the baseline side is a read-only reuse of the durable reference; "
            "no ORFS run was launched for it this round"),
    }


def slot_facts(payload: Any, host_run_facts: Any) -> dict[str, Any]:
    """合併 per-slot 證據與本次 run 的執行身分；不一致時回 `None`。

    ``None`` 代表這個 slot 沒有可用的 CORE 證據——呼叫端一律 fail closed，
    不得以其他方法的證據補足。
    """
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return {}
    facts = dict(payload)
    host = dict(host_run_facts or {})
    if host:
        if (host.get("run_id") != payload.get("run_id")
                or host.get("task_id") != payload.get("task_id")
                or host.get("slot") != payload.get("slot")):
            return {}
        facts.update({field: host.get(field) for field in HOST_RUN_FIELDS})
        facts["run_id"] = host.get("run_id")
    return facts


__all__ = ["COMMON_CONDITION_FIELDS", "EVIDENCE_FILENAME_TEMPLATE", "HOST_RUN_FIELDS",
           "METHOD_IDENTITY", "ORIGIN_FIXTURE", "ORIGIN_ORFS",
           "REQUIRED_RUN_IDENTITY", "SCHEMA_VERSION", "CoreEvidenceUnverifiable",
           "OrfsRunProvenance", "SOURCE_REUSED_REFERENCE", "SOURCE_THIS_RUN",
           "PROOF_ROLE_BASELINE", "PROOF_ROLE_CANDIDATE", "PairProofBinding",
           "build_fixture_slot_evidence", "build_reused_baseline_slot_evidence",
           "verify_pair_proof_envelope",
           "build_orfs_slot_evidence", "file_identity", "slot_facts"]
