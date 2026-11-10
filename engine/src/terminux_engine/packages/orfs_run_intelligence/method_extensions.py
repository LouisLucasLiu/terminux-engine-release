# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS Package 的 method-neutral execution/evidence extension registry。

Runtime 只保存／傳遞 ``method_identity`` 與 ``extension_version``；方法的
consumption、proof、comparison、acceptance 與 fresh-reader 解碼都由本 Package
registry 選擇。未知組合一律不可猜測。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from terminux_engine.packages.orfs_run_intelligence import formal_pair_native as _FPN
from terminux_engine.packages.orfs_run_intelligence import setup_margin_native_proof as _SMN
from terminux_engine.packages.orfs_run_intelligence.formal_pair import proof_descriptor
from terminux_engine.packages.orfs_run_intelligence.single_instance_eco import (
    METHOD_ID as SINGLE_INSTANCE_ECO_METHOD_ID,
    PARAMETER_NAME as SINGLE_INSTANCE_ECO_PARAMETER,
)


WORK_SCHEMA_UNSUPPORTED = "WORK_SCHEMA_UNSUPPORTED"
UNVERIFIABLE = "UNVERIFIABLE"

#: 固定進 ``flow_task_proposal.method_extension`` 的欄位；不多不少。
PINNED_FIELDS = frozenset({"method_identity", "extension_version", "descriptor",
                           "descriptor_sha256"})


class MethodExtensionError(ValueError):
    """extension 解析或證據歸屬不成立；``code`` 為穩定的 fail-closed 狀態。"""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason or code


@dataclass(frozen=True)
class MethodExtension:
    method_identity: str
    extension_version: str
    parameter: str
    consumption_contract: tuple[str, ...]
    proof_requirements: tuple[str, ...]
    comparison_policy_id: str
    acceptance_policy_id: str
    fresh_reader_schemas: tuple[str, ...]
    #: production 路徑是否需要 baseline 的功能驗證 reference（不是看 template
    #: 目前會不會產生它）。宣告需要但缺席 ⇒ fail closed。
    requires_baseline_functional_reference: bool = False
    #: 這個方法自己的流程是否會產生那份 reference。
    produces_baseline_functional_reference: bool = False
    #: 必須在證據裡出現的 proof 種類。
    required_proof_kinds: tuple[str, ...] = ()
    #: 可執行 formal pair proof 的 Package-owned descriptor。``None`` 代表此
    #: extension 沒有這條 execution path；Runtime 不得自行猜測工具或腳本。
    formal_pair_proof: dict[str, Any] | None = None
    #: host-native formal proof 契約（`formal_pair_native.proof_descriptor()`）。
    #: 與 `formal_pair_proof` 互斥；`None` 時 descriptor 不帶此鍵。
    formal_pair_native_proof: dict[str, Any] | None = None
    #: host-native setup-margin proof 契約（`setup_margin_native_proof.proof_descriptor()`）。
    #: `None` 時 descriptor 不帶此鍵。
    setup_margin_native_proof: dict[str, Any] | None = None
    #: descriptor 的 canonicalization 版本。`None` 是既有 extension 的格式
    #: （`descriptor_sha256()`）；有值時 descriptor 帶此鍵，digest 改用
    #: `extension_descriptor_sha256()` 對應的版本化算法。
    descriptor_canonicalization: str | None = None

    def descriptor(self) -> dict[str, Any]:
        return {"method_identity": self.method_identity,
                "extension_version": self.extension_version,
                "parameter": self.parameter,
                "consumption_contract": list(self.consumption_contract),
                "proof_requirements": list(self.proof_requirements),
                "comparison_policy_id": self.comparison_policy_id,
                "acceptance_policy_id": self.acceptance_policy_id,
                "fresh_reader_schemas": list(self.fresh_reader_schemas),
                "requires_baseline_functional_reference":
                    self.requires_baseline_functional_reference,
                "produces_baseline_functional_reference":
                    self.produces_baseline_functional_reference,
                "required_proof_kinds": list(self.required_proof_kinds),
                **({"formal_pair_proof": self.formal_pair_proof}
                   if self.formal_pair_proof is not None else {}),
                **({"formal_pair_native_proof": self.formal_pair_native_proof}
                   if self.formal_pair_native_proof is not None else {}),
                **({"setup_margin_native_proof": self.setup_margin_native_proof}
                   if self.setup_margin_native_proof is not None else {}),
                **({"descriptor_canonicalization": self.descriptor_canonicalization}
                   if self.descriptor_canonicalization is not None else {})}

    def pinned(self) -> dict[str, Any]:
        """compiler 固定進 plan 的完整身分：identity、version、descriptor 與 digest。"""
        descriptor = self.descriptor()
        return {"method_identity": self.method_identity,
                "extension_version": self.extension_version,
                "descriptor": descriptor,
                "descriptor_sha256": extension_descriptor_sha256(descriptor)}


def descriptor_sha256(descriptor: Any) -> str:
    """canonical JSON（排序鍵、無空白、保留 UTF-8）的 SHA-256。"""
    text = json.dumps(descriptor, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: host-native extension 的 descriptor canonicalization。與既有格式分開：digest
#: 材料前置版本字串（domain separation），因此不可能與既有 extension 的 digest
#: 相同，也不改變既有 extension 的持久化身分。
NATIVE_EXTENSION_CANONICALIZATION = "orfs.method_extension.native_canonical_json.v1"
_CANONICALIZATIONS = frozenset({NATIVE_EXTENSION_CANONICALIZATION})


def extension_descriptor_sha256(descriptor: Any) -> str:
    """依 descriptor 自己宣告的 canonicalization 計算 digest。

    沒有宣告 ⇒ 既有格式（`descriptor_sha256()`，位元不變）；宣告了未知版本
    ⇒ `MethodExtensionError(UNVERIFIABLE)`，不猜。
    """
    version = descriptor.get("descriptor_canonicalization") if isinstance(
        descriptor, dict) else None
    if version is None:
        return descriptor_sha256(descriptor)
    if version not in _CANONICALIZATIONS:
        raise MethodExtensionError(UNVERIFIABLE, "unknown descriptor canonicalization")
    text = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{version}\n{text}".encode("utf-8")).hexdigest()


SETUP_MARGIN = MethodExtension(
    "aes.setup_slack_margin.v1", "orfs.method_extension.v1", "SETUP_SLACK_MARGIN",
    ("task_snapshot", "materialized_config", "effective_settings", "repair_timing_argv"),
    ("post_cts_pair_proof", "final_pair_proof", "tool_identity"),
    "aes.same_requirements_setup_margin.v1", "aes.setup_slack_margin_acceptance.v1",
    ("orfs.setup_margin.reader.v1",),
    requires_baseline_functional_reference=True,
    produces_baseline_functional_reference=True,
    required_proof_kinds=("mapped_post_cts_equivalence", "mapped_final_equivalence"))
#: SETUP_MARGIN 的 host-native 版本：同一個 method、同一組 consumption 與工程條件，
#: proof 工具身分改由 Profile 授權的站點 Yosys（realpath ＋ SHA-256）建立，驗收用
#: 自己的 policy。自己的 extension version 與 canonicalization；不取代、不改寫上面
#: 那一筆，也不接受它的 container proof 身分。
SETUP_MARGIN_NATIVE_PROOF = MethodExtension(
    SETUP_MARGIN.method_identity, "orfs.method_extension.native_setup_margin.v1",
    SETUP_MARGIN.parameter, SETUP_MARGIN.consumption_contract,
    SETUP_MARGIN.proof_requirements, SETUP_MARGIN.comparison_policy_id,
    _SMN.ACCEPTANCE_POLICY_ID, SETUP_MARGIN.fresh_reader_schemas,
    requires_baseline_functional_reference=True,
    produces_baseline_functional_reference=True,
    required_proof_kinds=SETUP_MARGIN.required_proof_kinds,
    setup_margin_native_proof=_SMN.proof_descriptor(),
    descriptor_canonicalization=NATIVE_EXTENSION_CANONICALIZATION)
_SETUP_MARGIN_VARIANTS = (SETUP_MARGIN, SETUP_MARGIN_NATIVE_PROOF)
CORE_UTILIZATION = MethodExtension(
    "gcd.core_utilization.v1", "orfs.method_extension.v1", "CORE_UTILIZATION",
    ("validated_proposal", "task_snapshot", "repair_recipe", "execution_plan",
     "materialized_config", "effective_settings", "floorplan_invocation",
     "floorplan_die_area", "floorplan_core_area", "tool_image_script_run_identity"),
    ("post_cts_pair_proof", "final_pair_proof", "tool_identity", "proof_input_hashes",
     "baseline_candidate_change_digest", "task_run_identity"),
    "gcd.same_requirements_core_utilization.v1",
    "gcd.core_utilization_acceptance.v1",
    ("orfs.core_utilization.reader.v1",),
    # CORE 的 production 路徑需要 baseline 功能／formal reference，並由本方法
    # 自己的 baseline run 產生（Package 的 method-neutral component）。
    requires_baseline_functional_reference=True,
    produces_baseline_functional_reference=True,
    required_proof_kinds=("formal_equivalence",),
    formal_pair_proof=proof_descriptor())
#: CORE_UTILIZATION 的 host-native 版本：同一個 method、同一套 consumption／
#: acceptance，formal proof 改由 Profile 授權的站點 Yosys 在主機上執行。
#: 自己的 extension version 與 canonicalization；不取代、不改寫上面那一筆。
CORE_UTILIZATION_NATIVE_FORMAL = MethodExtension(
    CORE_UTILIZATION.method_identity, "orfs.method_extension.native_formal.v1",
    CORE_UTILIZATION.parameter, CORE_UTILIZATION.consumption_contract,
    CORE_UTILIZATION.proof_requirements, CORE_UTILIZATION.comparison_policy_id,
    CORE_UTILIZATION.acceptance_policy_id, CORE_UTILIZATION.fresh_reader_schemas,
    requires_baseline_functional_reference=True,
    produces_baseline_functional_reference=True,
    required_proof_kinds=CORE_UTILIZATION.required_proof_kinds,
    formal_pair_native_proof=_FPN.proof_descriptor(),
    descriptor_canonicalization=NATIVE_EXTENSION_CANONICALIZATION)
_CORE_VARIANTS = (CORE_UTILIZATION, CORE_UTILIZATION_NATIVE_FORMAL)

#: 單一非 clock buffer instance 換型。baseline 一律是「換型之前」的
#: 既有候選（透過既有 `REUSE_VALIDATED_EAP3_BASELINE` 機制重用，見
#: `eap_extension.py::compile_plan()`）；本方法自己**不**產生功能／formal
#: reference，只消費既有的——`produces_baseline_functional_reference=False`
#: 強制走重用路徑，不會意外變成「自己收集一次 baseline」。acceptance 直接
#: 沿用 setup repair 既有的 `gcd.setup_repair_acceptance.v1`（`repair_acceptance.
#: evaluate_repair()`）：evidence 形狀（eco_diff／eco_proof／final_pair_proof
#: ／setup_endpoints／core）刻意做成完全相同，不另外發明一套驗收規則。
SINGLE_INSTANCE_ECO = MethodExtension(
    SINGLE_INSTANCE_ECO_METHOD_ID, "orfs.method_extension.v1",
    SINGLE_INSTANCE_ECO_PARAMETER,
    ("task_snapshot", "materialized_config", "instance_snapshot", "eco_diff"),
    ("eco_proof", "final_pair_proof", "tool_identity"),
    "orfs_run_comparison",
    "gcd.setup_repair_acceptance.v1",
    ("orfs.single_instance_eco.reader.v1",),
    requires_baseline_functional_reference=True,
    produces_baseline_functional_reference=False,
    required_proof_kinds=("eco_local_equivalence", "final_pair_equivalence"))

_BY_METHOD = {row.method_identity: row
              for row in (SETUP_MARGIN, CORE_UTILIZATION, SINGLE_INSTANCE_ECO)}
#: (method, version) ⇒ extension。同一個 method 可有 host-native 版本；
#: `_BY_METHOD` 仍是未指定執行模式時的既有預設。
_BY_IDENTITY = {(row.method_identity, row.extension_version): row
                for row in (SETUP_MARGIN, CORE_UTILIZATION, SINGLE_INSTANCE_ECO,
                            CORE_UTILIZATION_NATIVE_FORMAL, SETUP_MARGIN_NATIVE_PROOF)}
_HOST_NATIVE_BY_METHOD = {CORE_UTILIZATION.method_identity: CORE_UTILIZATION_NATIVE_FORMAL,
                          SETUP_MARGIN.method_identity: SETUP_MARGIN_NATIVE_PROOF}


def resolve_extension(method_identity: Any, extension_version: Any) -> MethodExtension:
    extension = _BY_IDENTITY.get((method_identity, extension_version))
    if extension is None:
        raise ValueError(WORK_SCHEMA_UNSUPPORTED)
    return extension


def host_native_variant(extension: MethodExtension) -> MethodExtension:
    """host-native Profile 要用的 extension：有 native proof 版本就換成它。

    CORE_UTILIZATION 換成 native formal 版本、SETUP_MARGIN 換成 native proof 身分
    版本；沒有 proof 工具身分的 method 不受執行模式影響，原樣回傳。
    """
    if extension.formal_pair_proof is None and extension is not SETUP_MARGIN:
        return extension
    return _HOST_NATIVE_BY_METHOD.get(extension.method_identity, extension)


def is_setup_margin(extension: Any) -> bool:
    return any(extension is row for row in _SETUP_MARGIN_VARIANTS)


def setup_margin_native_proof_descriptor(extension: Any) -> dict[str, Any] | None:
    """extension 釘住的 native setup-margin proof 契約；container 版本為 `None`。"""
    descriptor = getattr(extension, "setup_margin_native_proof", None)
    return dict(descriptor) if descriptor else None


def is_core_utilization(extension: Any) -> bool:
    return any(extension is row for row in _CORE_VARIANTS)


def formal_proof_module(extension: Any) -> Any | None:
    """extension 釘住的 formal proof 契約模組；沒有 formal proof 則為 `None`。"""
    if getattr(extension, "formal_pair_native_proof", None):
        return _FPN
    if getattr(extension, "formal_pair_proof", None):
        from terminux_engine.packages.orfs_run_intelligence import formal_pair

        return formal_pair
    return None


def formal_proof_descriptor(extension: Any) -> dict[str, Any]:
    """extension 釘住的 formal proof descriptor（native 或 container 版本）。"""
    return dict(getattr(extension, "formal_pair_native_proof", None)
                or getattr(extension, "formal_pair_proof", None) or {})


def resolve_pinned(pinned: Any) -> MethodExtension:
    """由保存的 pinned descriptor 解析 extension；任何不一致都 fail closed。

    錯誤分類：identity／version 不在 registry ⇒ ``WORK_SCHEMA_UNSUPPORTED``；
    pin 形狀、identity 與 descriptor 不一致、descriptor 與 registry 不同或
    digest 重算不符 ⇒ ``UNVERIFIABLE``。
    """
    if not isinstance(pinned, dict) or set(pinned) != PINNED_FIELDS:
        raise MethodExtensionError(UNVERIFIABLE, "method extension pin is malformed")
    descriptor = pinned["descriptor"]
    if (not isinstance(descriptor, dict)
            or descriptor.get("method_identity") != pinned["method_identity"]
            or descriptor.get("extension_version") != pinned["extension_version"]):
        raise MethodExtensionError(UNVERIFIABLE,
                                   "method extension identity differs from its descriptor")
    try:
        extension = resolve_extension(pinned["method_identity"],
                                      pinned["extension_version"])
    except ValueError as exc:
        raise MethodExtensionError(WORK_SCHEMA_UNSUPPORTED,
                                   "unknown method extension") from exc
    if extension.descriptor() != descriptor:
        raise MethodExtensionError(UNVERIFIABLE,
                                   "method extension descriptor differs from the registry")
    if pinned["descriptor_sha256"] != extension_descriptor_sha256(descriptor):
        raise MethodExtensionError(UNVERIFIABLE,
                                   "method extension descriptor digest differs")
    return extension


def resolve_declared_identity(method_identity: Any, *,
                              host_native: bool = False) -> MethodExtension:
    """由**已核准 Profile 宣告的 method 身分**解析 extension。

    只給「proposal 還不存在」的 baseline 收集用；版本一律由 registry 決定，
    caller 不能指定。未知身分 fail closed。`host_native` 由 Profile 的執行
    模式決定，不是 caller 可選的值。
    """
    extension = _BY_METHOD.get(method_identity)
    if extension is None:
        raise MethodExtensionError(WORK_SCHEMA_UNSUPPORTED,
                                   "unknown declared method identity")
    return host_native_variant(extension) if host_native else extension


def decode_fresh_reader(envelope: dict[str, Any]) -> dict[str, Any]:
    """只依保存的 identity 解碼；不從檔名、design 或候選值推論方法。"""
    try:
        extension = resolve_extension(envelope.get("method_identity"),
                                      envelope.get("extension_version"))
    except ValueError:
        return {"status": WORK_SCHEMA_UNSUPPORTED, "reason": "unknown method extension"}
    schema = envelope.get("reader_schema")
    if schema not in extension.fresh_reader_schemas:
        return {"status": WORK_SCHEMA_UNSUPPORTED, "reason": "unsupported reader schema"}
    evidence = envelope.get("consumption")
    if not isinstance(evidence, dict) or any(key not in evidence
                                              for key in extension.consumption_contract):
        return {"status": UNVERIFIABLE, "reason": "consumption contract incomplete"}
    return {"status": "VERIFIED", "extension": extension.descriptor()}


#: bounded utilization search 定義的規則：
#: setup 三項都不得變差，且至少一項嚴格改善；缺資料或不可比一律不下判定。
#: 這裡沿用同一組指標與同一份 reason code（見 `selection.py`），只是把它套用到
#: 續作模型的單一 baseline／candidate 對，不是 bounded utilization search 的多候選排序。方向
#: （improved／degraded）由 `comparison_facts.py` 的 ORFS domain policy 決定，
#: 這裡不重算、不猜正負號。
_CORE_TIMING_UNVERIFIABLE = "UNVERIFIABLE"
_CORE_TIMING_NOT_IMPROVED = "NOT_IMPROVED"
_CORE_TIMING_IMPROVED = "IMPROVED"


def _setup_timing_verdict(metric_comparisons: Any) -> dict[str, Any]:
    """由正式 `metric_comparisons`（DQE 逐指標比較，setup_margin／bounded utilization search selection
    已在用的同一種資料）判定 setup timing 是否改善。三個指標任一缺失或不可比
    → `UNVERIFIABLE`；任一退步 → `NOT_IMPROVED`（不因另一項改善就合成整體改善）；
    都不退步且至少一項改善 → `IMPROVED`；都不退步也都沒改善 → `NOT_IMPROVED`。
    """
    from terminux_engine.packages.orfs_run_intelligence import selection as SEL

    index: dict[str, dict[str, Any]] = {}
    for entry in metric_comparisons or ():
        row = entry if isinstance(entry, dict) else entry.to_json_dict()
        metric_id = row.get("metric_id")
        if metric_id:
            index[str(metric_id)] = row

    metrics: dict[str, Any] = {}
    codes: list[str] = []
    reasons: list[str] = []
    improved: list[str] = []
    regressed: list[str] = []
    for metric_id in SEL.SETUP_METRICS:
        row = index.get(metric_id)
        metrics[metric_id] = {"baseline": (row or {}).get("baseline_value"),
                              "candidate": (row or {}).get("candidate_value"),
                              "comparison": (row or {}).get("comparison")}
        if row is None:
            codes.append(SEL.REASON_METRIC_MISSING)
            reasons.append(f"{metric_id} is missing from the comparison")
            continue
        outcome = row.get("comparison")
        if outcome == "not_comparable":
            codes.append(SEL.REASON_METRIC_NOT_COMPARABLE)
            reasons.append(f"{metric_id} is not comparable: {row.get('reason')}")
        elif outcome == "degraded":
            regressed.append(metric_id)
        elif outcome == "improved":
            improved.append(metric_id)
        # "unchanged" 既不算退步也不算改善，只是不列入 improved。

    if codes:
        status = _CORE_TIMING_UNVERIFIABLE
    elif regressed:
        codes.append(SEL.REASON_SETUP_REGRESSED)
        reasons.append(f"regressed metrics: {', '.join(regressed)}")
        status = _CORE_TIMING_NOT_IMPROVED
    elif improved:
        status = _CORE_TIMING_IMPROVED
    else:
        codes.append(SEL.REASON_NO_SETUP_IMPROVEMENT)
        reasons.append("no setup metric improved")
        status = _CORE_TIMING_NOT_IMPROVED
    return {"status": status, "reason_codes": codes, "reasons": reasons,
            "metrics": metrics, "improved_metrics": improved,
            "regressed_metrics": regressed}


#: `_area_verdict()` 的可能狀態；與 `_setup_timing_verdict()` 的狀態集合刻意
#: 分開命名，不共用同一組列舉——這是兩個獨立的工程事實，"完整驗收" 才把
#: 兩者合併判定（見 `_core_utilization_acceptance()`），續作用的
#: `engineering_verdict`／`continuation_decision()` 完全不受這裡影響。
_CORE_AREA_UNVERIFIABLE = "UNVERIFIABLE"
_CORE_AREA_WITHIN_LIMIT = "WITHIN_LIMIT"
_CORE_AREA_OVER_LIMIT = "OVER_LIMIT"
#: bounded utilization search（已核准）的面積上限：沿用「不超過 baseline 的
#: 1.75 倍」；續作模型的 Task/Profile 目前沒有欄位可以逐 Work 覆寫這個值
#: （bounded utilization search 8-step flow 的 `task.selection_policy.area_ratio_limit` 是另一個
#: Task 類型專用的欄位，續作模型的 `RepairRecipe` 沒有對等欄位）——這是明確
#: 記錄的後續缺口，不是本包自行發明的預設值。
CORE_UTILIZATION_AREA_RATIO_LIMIT = 1.75


def _as_area(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _slot_core_area(facts: Mapping[str, Any]) -> dict[str, Any]:
    """單側面積與其來源：只讀 per-slot 證據的 `floorplan_observed`。

    `floorplan_observed` 是 `flow_evidence.floorplan_consumption_evidence()`
    的原樣輸出（這次 run 由 `build_orfs_slot_evidence()` 寫入；重用的 baseline
    由 `build_reused_baseline_slot_evidence()` 從 durable reference 封存的
    `run_identity.details.parameter_consumption.observed` 帶出）。
    """
    observed = facts.get("floorplan_observed")
    log = facts.get("consuming_floorplan_log") or {}
    source = {"evidence_source": facts.get("evidence_source"),
              "reader": "flow_evidence.floorplan_consumption_evidence",
              "stage": "floorplan", "unit": "um^2",
              "baseline_reference_digest": facts.get("baseline_reference_digest"),
              "floorplan_log_sha256": log.get("sha256") if isinstance(log, dict) else None,
              "floorplan_log_relative_path": facts.get(
                  "consuming_floorplan_log_relative_path")}
    if not isinstance(observed, dict):
        return {"raw": None, "value": None, "utilization": None, "source": source,
                "observed": False}
    raw = observed.get("core_area_um2")
    return {"raw": raw, "value": _as_area(raw),
            "utilization": observed.get("tool_received_utilization"),
            "source": source, "observed": True}


def _area_basis_problems(base: Mapping[str, Any], cand: Mapping[str, Any], *,
                         base_facts: Mapping[str, Any], cand_facts: Mapping[str, Any],
                         host_observed: Mapping[str, Any]) -> list[str]:
    """兩側面積是否可比：同一 reader 的觀測、且就是該 slot 那次 floorplan。"""
    problems: list[str] = []
    for name, side, facts in (("baseline", base, base_facts),
                              ("candidate", cand, cand_facts)):
        if not side["observed"] or side["value"] is None:
            continue
        if _as_number(side["utilization"]) != _as_number(facts.get("core_utilization")):
            problems.append(
                f"the {name} floorplan observation was taken at utilization "
                f"{side['utilization']!r}, not at this slot's "
                f"{facts.get('core_utilization')!r}")
    host_area = host_observed.get("core_area_um2")
    if (cand["value"] is not None and host_area is not None
            and _as_area(host_area) != cand["value"]):
        problems.append(
            f"the host floorplan reader ({host_area!r}) and the candidate slot "
            f"evidence ({cand['raw']!r}) disagree on the candidate core area")
    return problems


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _area_verdict(*, baseline_um2: Any, candidate_um2: Any,
                  basis_problems: tuple[str, ...] | list[str] = (),
                  limit: float = CORE_UTILIZATION_AREA_RATIO_LIMIT) -> dict[str, Any]:
    """bounded utilization search 的面積上限判定：候選／baseline 的 `core_area_um2`（floorplan
    consumption 證據，與 `selection.py::assess_candidate()` 用的是同一個
    metric、同一個 stage、同一個 reader——不是拿 utilization 比例或其他階段的
    面積代替）。任一側缺失、非數值或非正值 → `UNVERIFIABLE`；比較基準不一致
    → `UNVERIFIABLE`（另一個 reason code）；比例超過上限 → `OVER_LIMIT`；
    否則 `WITHIN_LIMIT`。不從 utilization 推算，不以 candidate 補 baseline，
    不填佔位值。
    """
    from terminux_engine.packages.orfs_run_intelligence import selection as SEL

    base = _as_area(baseline_um2)
    cand = _as_area(candidate_um2)
    if base is None or cand is None:
        sides = [name for name, value in (("baseline", base), ("candidate", cand))
                 if value is None]
        return {"status": _CORE_AREA_UNVERIFIABLE, "baseline_um2": baseline_um2,
                "candidate_um2": candidate_um2, "ratio": None, "limit": limit,
                "resolution": R_AREA_NOT_MEASURED,
                "reason_codes": [SEL.REASON_AREA_MISSING],
                "reasons": [f"the floorplan core area is unavailable or invalid on "
                           f"the {' and '.join(sides)} side"]}
    if basis_problems:
        return {"status": _CORE_AREA_UNVERIFIABLE, "baseline_um2": base,
                "candidate_um2": cand, "ratio": None, "limit": limit,
                "resolution": R_AREA_NOT_COMPARABLE,
                "reason_codes": [R_AREA_NOT_COMPARABLE],
                "reasons": list(basis_problems)}
    ratio = cand / base
    if ratio > limit:
        return {"status": _CORE_AREA_OVER_LIMIT, "baseline_um2": base,
                "candidate_um2": cand, "ratio": ratio, "limit": limit,
                "resolution": "AREA_OVER_LIMIT",
                "reason_codes": [SEL.REASON_AREA_OVER_LIMIT],
                "reasons": [f"core area ratio {ratio:.4f} exceeds the limit {limit}"]}
    return {"status": _CORE_AREA_WITHIN_LIMIT, "baseline_um2": base,
            "candidate_um2": cand, "ratio": ratio, "limit": limit,
            "resolution": None, "reason_codes": [], "reasons": []}


def evaluate_core_utilization(*, baseline: dict[str, Any], candidate: dict[str, Any],
                              metric_comparisons: Any = ()) -> dict[str, Any]:
    """CORE 專用 declarative acceptance；formal 等價不被誤作 timing 成功。"""
    required = ("rtl_digest", "sdc_digest", "corner", "library", "scripts_digest",
                "image_digest", "tool_digest", "receipt_digest", "manifest_digest",
                "dqe_digest", "proof_status", "run_id", "result_event_id")
    comparable = all(baseline.get(key) == candidate.get(key) for key in required
                     if key not in {"receipt_digest", "manifest_digest", "dqe_digest",
                                    "run_id", "result_event_id"})
    protected = candidate.get("protected_metric_regressions") or []
    proof_ok = baseline.get("proof_status") == candidate.get("proof_status") == "equivalent"
    # 兩側引用同一份 envelope，因此 proof_status 一致時就是那份 proof 的結論。
    # 不成立時**不得**一律寫成 `unverifiable`：`unproven`（沒證出來）與
    # `not_equivalent`（證出不一樣）是不同的工程事實，拒絕的理由必須看得出來。
    observed_proof = (str(baseline.get("proof_status") or "")
                      if baseline.get("proof_status") == candidate.get("proof_status")
                      else "unverifiable")
    timing = _setup_timing_verdict(metric_comparisons)
    setup_improved = timing["status"] == _CORE_TIMING_IMPROVED
    setup_improvement_measured = timing["status"] != _CORE_TIMING_UNVERIFIABLE
    eligible = comparable and proof_ok and not protected
    return {"policy_id": CORE_UTILIZATION.acceptance_policy_id,
            "comparison_policy_id": CORE_UTILIZATION.comparison_policy_id,
            "comparable": comparable, "selection_eligible": eligible,
            "engineering_verdict": "IMPROVED" if eligible and setup_improved else "NOT_ACCEPTED",
            "setup_improvement_measured": setup_improvement_measured,
            "setup_timing": timing,
            "protected_metric_regressions": list(protected),
            "proof_status": observed_proof or "unverifiable",
            "remaining_objective_gap": candidate.get("remaining_objective_gap"),
            "formal_is_not_timing_acceptance": True}


# ── FlowController dispatch 介面 ─────────────────────────────────────
#
# host 只拿 resolve 出來的 extension 呼叫下列三個入口；方法的證據集合、
# consumption reader 與 proof／comparison／acceptance 都由本 Package 決定。

_REPAIRED = "REPAIRED"
_NOT_REPAIRED = "NOT_REPAIRED"
R_CROSS_METHOD_EVIDENCE = "method_extension_evidence_belongs_to_another_method"
R_LEGACY_SHAPE = "legacy_setup_margin_evidence_shape_not_historical"
R_CORE_EVIDENCE_UNAVAILABLE = "core_utilization_evidence_unavailable"
R_METHOD_NOT_CONSUMED = "method_not_consumed_by_the_tool"
R_BASELINE_REFERENCE_MISSING = "declared_baseline_functional_reference_is_missing"
R_PROOF_KIND_MISSING = "declared_proof_kind_is_missing_from_the_evidence"
#: 「量到了但沒改善」與「根本沒量」是不同的工程事實；後者不得寫成 `NOT_REPAIRED`。
R_SETUP_IMPROVEMENT_NOT_MEASURED = "setup_improvement_not_measured"
#: 面積證據缺失／不可比 vs. 面積超標，是兩個不同的工程事實，reason code 分開。
R_AREA_NOT_MEASURED = "core_area_not_measured"
#: 兩側都有面積，但不是同一 reader／同一次 floorplan 的觀測，不能相除。
R_AREA_NOT_COMPARABLE = "core_area_not_comparable"
#: fixture corpus replay：OpenROAD 沒有被執行過。
ORFS_NOT_RUN = "ORFS_NOT_RUN"
#: fixture 下的工程結論：只證明接線，不是工程判定。
FIXTURE_ONLY = "FIXTURE_ONLY"
R_FIXTURE_ONLY = "fixture_corpus_replay_is_not_an_engineering_result"

_SETUP_MARGIN_SLOT_EVIDENCE = ("setup_endpoints", "core", "post_cts_topology",
                               "method_consumption")
_SETUP_MARGIN_CANDIDATE_EVIDENCE = ("post_cts_pair_proof", "final_pair_proof")
_CORE_FACTS = "core_utilization"


@dataclass(frozen=True)
class ExtensionAcceptance:
    """extension 判定結果：可保存的 verdict payload 與是否選 candidate。"""

    payload: dict[str, Any]
    candidate_selected: bool


def host_evidence_set(extension: MethodExtension) -> str | None:
    """host 要收集的 repair evidence 檔案集合名稱；``None`` 表示不讀 host 檔案。"""
    if is_setup_margin(extension):
        return "setup_margin"
    if is_core_utilization(extension):
        return "core_utilization"
    if extension is SINGLE_INSTANCE_ECO:
        return "single_instance_eco"
    return None


def read_consumption_evidence(extension: MethodExtension, output_root: str, *,
                              platform: str, design: str, variant: str,
                              expected_value: str | None) -> Any:
    """依 extension 選 host consumption reader；不看 parameter、design 或檔名。"""
    from terminux_engine.packages.orfs_run_intelligence import flow_evidence as FE

    if is_setup_margin(extension):
        reader = FE.setup_margin_consumption_evidence
    elif is_core_utilization(extension):
        reader = FE.floorplan_consumption_evidence
    else:
        raise MethodExtensionError(WORK_SCHEMA_UNSUPPORTED, "no consumption reader")
    return reader(output_root, platform=platform, design=design, variant=variant,
                  expected_value=expected_value)


def evaluate_acceptance(
    extension: MethodExtension, *, baseline: Mapping[str, Any],
    candidate: Mapping[str, Any], host_consumption: Any, method: Mapping[str, Any],
    candidate_value: str, metric_comparisons: Any, execution_ok: bool,
    inputs_approved: bool, inputs_reason: str | None, legacy: bool = False,
    host_run_facts: Mapping[str, Any] | None = None,
    baseline_reference_available: bool = True,
    qualification_fixture: bool = False,
    target_slack_ns: float | None = None,
    native_proof: Mapping[str, Any] | None = None,
) -> ExtensionAcceptance:
    """呼叫 resolved extension 的 proof／comparison／acceptance。

    ``baseline``／``candidate`` 是各 slot 的證據 payload（名稱 → JSON）。證據
    必須屬於本 extension；跨 method 證據一律 ``MethodExtensionError``。
    ``native_proof`` 只給 host-native setup-margin：由 host 依 task-owned Profile
    提供授權的 proof binary pin、qualified identity digest 與 helper 雜湊。
    """
    if (extension.requires_baseline_functional_reference
            and not baseline_reference_available and not qualification_fixture):
        # 宣告需要 baseline 功能驗證 reference，實際卻沒有 ⇒ fail closed。
        raise MethodExtensionError(
            R_BASELINE_REFERENCE_MISSING,
            "the method extension declares a required baseline functional "
            "reference, but this task has none")
    if extension is SETUP_MARGIN:
        return _setup_margin_acceptance(
            baseline=baseline, candidate=candidate, host_consumption=host_consumption,
            method=method, candidate_value=candidate_value,
            metric_comparisons=metric_comparisons, execution_ok=execution_ok,
            inputs_approved=inputs_approved, inputs_reason=inputs_reason,
            legacy=legacy, target_slack_ns=target_slack_ns)
    if legacy:
        raise MethodExtensionError(WORK_SCHEMA_UNSUPPORTED,
                                   "only setup-margin has a legacy adapter")
    if extension is SETUP_MARGIN_NATIVE_PROOF:
        return _native_setup_margin_acceptance(
            baseline=baseline, candidate=candidate, host_consumption=host_consumption,
            method=method, candidate_value=candidate_value,
            metric_comparisons=metric_comparisons, execution_ok=execution_ok,
            inputs_approved=inputs_approved, inputs_reason=inputs_reason,
            target_slack_ns=target_slack_ns, native_proof=native_proof)
    if is_core_utilization(extension):
        return _core_utilization_acceptance(
            extension=extension, baseline=baseline, candidate=candidate, host_consumption=host_consumption,
            metric_comparisons=metric_comparisons,
            execution_ok=execution_ok, inputs_approved=inputs_approved,
            inputs_reason=inputs_reason, host_run_facts=host_run_facts or {},
            baseline_reference_available=baseline_reference_available)
    if extension is SINGLE_INSTANCE_ECO:
        return _single_instance_eco_acceptance(
            baseline=baseline, candidate=candidate,
            metric_comparisons=metric_comparisons, execution_ok=execution_ok,
            inputs_approved=inputs_approved, inputs_reason=inputs_reason)
    raise MethodExtensionError(WORK_SCHEMA_UNSUPPORTED, "no acceptance adapter")


def _setup_margin_acceptance(
    *, baseline: Mapping[str, Any], candidate: Mapping[str, Any],
    host_consumption: Any, method: Mapping[str, Any], candidate_value: str,
    metric_comparisons: Any, execution_ok: bool, inputs_approved: bool,
    inputs_reason: str | None, legacy: bool, target_slack_ns: float | None = None,
) -> ExtensionAcceptance:
    from terminux_engine.packages.orfs_run_intelligence import (
        setup_margin_acceptance as SMA,
    )

    _check_setup_margin_evidence_owner(baseline, candidate, method)
    if legacy:
        # 歷史 Work 不改寫；只接受與 parametric setup repair 相同的證據格式。
        required = ([(baseline, name) for name in _SETUP_MARGIN_SLOT_EVIDENCE]
                    + [(candidate, name) for name in _SETUP_MARGIN_SLOT_EVIDENCE
                       + _SETUP_MARGIN_CANDIDATE_EVIDENCE])
        if (any(not isinstance(slot.get(name), dict) for slot, name in required)
                or candidate["method_consumption"].get("variable")
                != SETUP_MARGIN.parameter):
            raise MethodExtensionError(R_LEGACY_SHAPE,
                                       "historical setup-margin evidence is incomplete")
    verdict = SMA.evaluate_setup_margin(
        baseline_setup=baseline.get("setup_endpoints"),
        candidate_setup=candidate.get("setup_endpoints"),
        baseline_core=baseline.get("core"),
        candidate_core=candidate.get("core"),
        baseline_post_cts_topology=baseline.get("post_cts_topology"),
        candidate_post_cts_topology=candidate.get("post_cts_topology"),
        post_cts_pair_proof=candidate.get("post_cts_pair_proof"),
        final_pair_proof=candidate.get("final_pair_proof"),
        method_consumption=candidate.get("method_consumption"),
        host_method_consumption=host_consumption,
        method_variable=str(method["variable"]),
        method_candidate_value=candidate_value,
        metric_comparisons=metric_comparisons,
        execution_ok=execution_ok,
        inputs_approved=inputs_approved, inputs_reason=inputs_reason,
        target_slack_ns=target_slack_ns)
    return ExtensionAcceptance(payload=verdict.to_json_dict(),
                               candidate_selected=verdict.repair_status == SMA.REPAIRED)


def _check_setup_margin_evidence_owner(baseline: Mapping[str, Any],
                                       candidate: Mapping[str, Any],
                                       method: Mapping[str, Any]) -> None:
    """兩種 setup-margin extension 共用：證據必須屬於 SETUP_SLACK_MARGIN。"""
    for slot in (baseline, candidate):
        if slot.get(_CORE_FACTS) is not None:
            raise MethodExtensionError(R_CROSS_METHOD_EVIDENCE,
                                       "CORE_UTILIZATION evidence was sent to setup-margin")
        consumption = slot.get("method_consumption")
        if (isinstance(consumption, dict) and "variable" in consumption
                and consumption["variable"] != SETUP_MARGIN.parameter):
            raise MethodExtensionError(R_CROSS_METHOD_EVIDENCE,
                                       "method consumption evidence names another parameter")
    if method.get("variable") != SETUP_MARGIN.parameter:
        raise MethodExtensionError(WORK_SCHEMA_UNSUPPORTED,
                                   "the task method does not match the setup-margin extension")


#: host-native setup-margin 驗收需要的 host 端身分；缺任一欄即不判定。
#: `host_observations` 只有歷史 replay 會提供（判定當時記錄的主機觀測）。
NATIVE_PROOF_CONTEXT_FIELDS = frozenset({"tool", "qualified_digest", "helper_sha256"})
_NATIVE_PROOF_CONTEXT_OPTIONAL = frozenset({"host_observations"})


def _native_setup_margin_acceptance(
    *, baseline: Mapping[str, Any], candidate: Mapping[str, Any],
    host_consumption: Any, method: Mapping[str, Any], candidate_value: str,
    metric_comparisons: Any, execution_ok: bool, inputs_approved: bool,
    inputs_reason: str | None, target_slack_ns: float | None,
    native_proof: Mapping[str, Any] | None,
) -> ExtensionAcceptance:
    """host-native setup-margin：工程條件同 container 版本，proof 身分走 native 契約。"""
    _check_setup_margin_evidence_owner(baseline, candidate, method)
    if (not isinstance(native_proof, Mapping)
            or not NATIVE_PROOF_CONTEXT_FIELDS <= set(native_proof)
            or set(native_proof) - NATIVE_PROOF_CONTEXT_FIELDS
            - _NATIVE_PROOF_CONTEXT_OPTIONAL):
        raise MethodExtensionError(
            UNVERIFIABLE, "the host-native setup-margin proof binding is not available")
    result = _SMN.evaluate_native_setup_margin(
        tool=native_proof["tool"], qualified_digest=native_proof["qualified_digest"],
        helper_sha256=frozenset(native_proof["helper_sha256"] or ()),
        host_observations=native_proof.get("host_observations"),
        baseline_setup=baseline.get("setup_endpoints"),
        candidate_setup=candidate.get("setup_endpoints"),
        baseline_core=baseline.get("core"),
        candidate_core=candidate.get("core"),
        baseline_post_cts_topology=baseline.get("post_cts_topology"),
        candidate_post_cts_topology=candidate.get("post_cts_topology"),
        post_cts_pair_proof=candidate.get("post_cts_pair_proof"),
        final_pair_proof=candidate.get("final_pair_proof"),
        method_consumption=candidate.get("method_consumption"),
        host_method_consumption=host_consumption,
        method_variable=str(method["variable"]),
        method_candidate_value=candidate_value,
        metric_comparisons=metric_comparisons,
        execution_ok=execution_ok,
        inputs_approved=inputs_approved, inputs_reason=inputs_reason,
        target_slack_ns=target_slack_ns)
    return ExtensionAcceptance(
        payload=result.to_json_dict(),
        candidate_selected=result.verdict.repair_status == _REPAIRED)


def _core_utilization_acceptance(
    *, extension: MethodExtension = CORE_UTILIZATION, baseline: Mapping[str, Any], candidate: Mapping[str, Any],
    host_consumption: Any, metric_comparisons: Any = (),
    execution_ok: bool, inputs_approved: bool,
    inputs_reason: str | None, host_run_facts: Mapping[str, Any],
    baseline_reference_available: bool = True,
) -> ExtensionAcceptance:
    from terminux_engine.packages.orfs_run_intelligence import (
        core_utilization_evidence as CU,
    )

    facts: dict[str, dict[str, Any]] = {}
    for name, slot in (("baseline", baseline), ("candidate", candidate)):
        if any(slot.get(key) is not None for key in
               ("post_cts_topology", "method_consumption", "post_cts_pair_proof")):
            raise MethodExtensionError(R_CROSS_METHOD_EVIDENCE,
                                       "setup-margin evidence was sent to CORE_UTILIZATION")
        payload = slot.get(_CORE_FACTS)
        if payload is None:
            continue
        if (not isinstance(payload, dict)
                or payload.get("method_identity") != extension.method_identity
                or payload.get("extension_version") != extension.extension_version):
            raise MethodExtensionError(R_CROSS_METHOD_EVIDENCE,
                                       "CORE evidence identity does not match the extension")
        if payload.get("slot") not in (None, name):
            raise MethodExtensionError(R_CROSS_METHOD_EVIDENCE,
                                       "CORE evidence belongs to another slot")
        merged = CU.slot_facts(payload, (host_run_facts or {}).get(name))
        if not merged:
            # 證據存在但與本次 run 的執行身分對不起來：不可信，不降級使用。
            raise MethodExtensionError(
                UNVERIFIABLE, "CORE evidence does not match this task run identity")
        facts[name] = merged
    common = {"policy_id": CORE_UTILIZATION.acceptance_policy_id,
              "policy_version": extension.extension_version,
              "method_identity": CORE_UTILIZATION.method_identity,
              "core_execution_path_ready": False}
    if len(facts) != 2:
        return ExtensionAcceptance(payload={
            **common, "repair_status": UNVERIFIABLE, "evidence_origin": ORFS_NOT_RUN,
            "engineering_resolution": FIXTURE_ONLY,
            "reason_codes": [R_CORE_EVIDENCE_UNAVAILABLE],
            "reasons": ["no CORE_UTILIZATION per-slot evidence is bound to this task run; "
                        "setup-margin evidence is never used in its place"]},
            candidate_selected=False)
    base_facts, cand_facts = facts["baseline"], facts["candidate"]
    fixture = bool(base_facts.get("fixture") or cand_facts.get("fixture"))
    # 宣告的 proof 種類必須真的出現在證據裡（fixture 也要）。
    missing_proofs = sorted(
        kind for kind in CORE_UTILIZATION.required_proof_kinds
        if any(kind not in (row.get("proof_kinds") or ())
               for row in (base_facts, cand_facts)))
    if missing_proofs:
        raise MethodExtensionError(
            R_PROOF_KIND_MISSING,
            f"the CORE evidence does not carry the declared proof kind(s): "
            f"{missing_proofs}")
    result = dict(evaluate_core_utilization(baseline=base_facts, candidate=cand_facts,
                                            metric_comparisons=metric_comparisons))
    codes: list[str] = []
    reasons: list[str] = []
    view = (host_consumption.to_json_dict() if hasattr(host_consumption, "to_json_dict")
            else dict(host_consumption or {}))
    # bounded utilization search（`gcd.setup_area_bounded.v1`，已核准）的 core area 上限。兩側面積
    # 都只取自 per-slot CORE 證據的 `floorplan_observed.core_area_um2`——與
    # `selection.py::assess_candidate()` 同一個 reader、同一個 stage、同一單位。
    # 重用的 baseline 由 durable reference 封存的 floorplan 觀測帶出；缺就是缺，
    # 不以 candidate 補、不從 utilization 推算、不填佔位值。
    base_area = _slot_core_area(base_facts)
    cand_area = _slot_core_area(cand_facts)
    area = _area_verdict(
        baseline_um2=base_area["raw"], candidate_um2=cand_area["raw"],
        basis_problems=_area_basis_problems(
            base_area, cand_area, base_facts=base_facts, cand_facts=cand_facts,
            host_observed=view.get("observed") or {}))
    core_area = {"baseline_um2": area["baseline_um2"], "candidate_um2": area["candidate_um2"],
                 "ratio": area["ratio"], "limit": area["limit"], "status": area["status"],
                 "reason_codes": list(area["reason_codes"]),
                 "sources": {"baseline": base_area["source"],
                             "candidate": cand_area["source"]}}
    if not view.get("consumed"):
        codes.append(R_METHOD_NOT_CONSUMED)
        reasons.append("CORE_UTILIZATION consumption was not proven by the floorplan reader")
    elif str(view.get("observed", {}).get("tool_received_utilization") or "") not in (
            "", str(cand_facts.get("core_utilization"))):
        codes.append(R_METHOD_NOT_CONSUMED)
        reasons.append("the floorplan reader and the per-slot evidence disagree on the value")
    if not execution_ok or not inputs_approved:
        codes.append("execution_or_inputs_not_approved")
        reasons.append(inputs_reason or "a required EDA step did not succeed")
    # platform wiring（routing／materialization／evidence shape／lineage）與工程
    # 判定分列：前者可以獨立 PASSED，後者在 fixture 下永遠不成立。
    wiring = {"status": "PASSED" if not codes and result["comparable"] else "FAILED",
              "evidence_shape": "COMPLETE", "reason_codes": list(codes)}
    # production readiness 與 platform wiring 分列：宣告需要的 baseline 功能
    # reference 不在時，不論 wiring 是否通過都不得宣稱 production ready。
    readiness = {
        "baseline_functional_reference": (
            "PRESENT" if baseline_reference_available else "MISSING"),
        "declared_required": CORE_UTILIZATION.requires_baseline_functional_reference,
        "produced_by_method": CORE_UTILIZATION.produces_baseline_functional_reference,
        "required_proof_kinds": list(CORE_UTILIZATION.required_proof_kinds),
        "status": ("NOT_ESTABLISHED"
                   if fixture or not baseline_reference_available else "ESTABLISHED")}
    if fixture:
        # fixture 只驗 routing／schema：不得宣稱 OpenROAD 執行過，也不得被選取。
        result["selection_eligible"] = False
        result["qualification_only"] = True
        status, resolution = UNVERIFIABLE, FIXTURE_ONLY
        reasons.append("fixture corpus replay: OpenROAD was not executed")
        codes = [*codes, R_FIXTURE_ONLY]
    elif codes:
        status, resolution = UNVERIFIABLE, "EVIDENCE_INCOMPLETE"
    elif not result.get("setup_improvement_measured"):
        # 其餘一切（comparable／proof／quality／consumption／execution）都成立，
        # 但沒有任何正式路徑量出「setup 是否改善」——這是缺資料，不是「量到了、
        # 結果是沒改善」。不得預設為 `NOT_REPAIRED`（那是對缺資料下了一個更具體
        # 的負面斷言，違反「缺失資料不得默認為零、改善或成功」——也不得默認為
        # 「確定沒改善」）。
        status, resolution = UNVERIFIABLE, R_SETUP_IMPROVEMENT_NOT_MEASURED
        codes = [*codes, R_SETUP_IMPROVEMENT_NOT_MEASURED]
        reasons.append(
            "no formal path currently captures a real setup-timing comparison "
            "for CORE_UTILIZATION (see repair_evidence.py's evidence file "
            "declaration for this method); engineering improvement can be "
            "neither claimed nor denied from placeholder evidence")
    elif result["engineering_verdict"] != "IMPROVED":
        # timing 本身已經確定不成立（未改善、退步、或 comparable／proof／品質
        # 任一既有 gate 不成立）——這是一個已經確知的具體負面事實，不該被
        # 「我們也不知道面積」這個**另一件**未知的事覆蓋掉。面積只在「除此
        # 之外都會判過」的情況下才是限制因素（見下一個分支）。
        status, resolution = _NOT_REPAIRED, result["engineering_verdict"]
    elif area["status"] == _CORE_AREA_UNVERIFIABLE:
        # 走到這裡代表 comparable／proof／品質／timing 都成立，本應是
        # REPAIRED——但面積這個 bounded utilization search 規則裡與其他三項同級的必要條件沒有
        # 量到，不能因為其餘都好看就放行。缺這項證據，整體只能 UNVERIFIABLE，
        # 即使 `setup_timing.status == IMPROVED`（見 `result["setup_timing"]`，
        # 這個事實原樣保留、不被整體判定蓋掉，滿足「timing 改善與完整驗收
        # 結果分開保留」）。
        status, resolution = UNVERIFIABLE, area["resolution"]
        codes = [*codes, *dict.fromkeys([area["resolution"], *area["reason_codes"]])]
        reasons.append(f"core area cannot be judged: {'; '.join(area['reasons'])}")
    elif area["status"] == _CORE_AREA_OVER_LIMIT:
        # 依既有「未達標」語意處理（`NOT_REPAIRED`），不新造一個狀態；
        # timing 改善同樣不能覆蓋面積超標。reason code 重用 `selection.py`
        # 既有的 `REASON_AREA_OVER_LIMIT`，不重新定義同義的第二個常數。
        from terminux_engine.packages.orfs_run_intelligence import selection as _SEL
        status, resolution = _NOT_REPAIRED, "AREA_OVER_LIMIT"
        codes = [*codes, _SEL.REASON_AREA_OVER_LIMIT]
        reasons.append(f"core area exceeds the approved limit: "
                       f"{'; '.join(area['reasons'])}")
    else:
        status, resolution = _REPAIRED, "IMPROVED"
    return ExtensionAcceptance(payload={
        **common, "repair_status": status, "reason_codes": codes or ["ok"],
        "reasons": reasons, "core_utilization_acceptance": result,
        "engineering_resolution": resolution, "platform_wiring": wiring,
        "production_readiness": readiness, "core_area": core_area,
        "core_utilization_value": {name: row.get("core_utilization")
                                   for name, row in facts.items()},
        **({"evidence_origin": ORFS_NOT_RUN} if fixture else {})},
        candidate_selected=status == _REPAIRED and not fixture)


def _single_instance_eco_acceptance(
    *, baseline: Mapping[str, Any], candidate: Mapping[str, Any],
    metric_comparisons: Any = (), execution_ok: bool, inputs_approved: bool,
    inputs_reason: str | None = None,
) -> ExtensionAcceptance:
    """直接沿用 setup repair 既有的 `repair_acceptance.evaluate_repair()`。

    這個方法的 evidence 形狀（`eco_diff`／`eco_proof`／`final_pair_proof`／
    `setup_endpoints`／`core`）刻意做成與 setup repair 完全相同，因此**不**重新實作
    一套驗收規則——「這個候選算不算修復」的判斷全部交給既有、已測過的
    `evaluate_repair()`；這裡只負責把本方法收集到的 per-slot 證據對應到它
    的參數，並補上 method identity 與（如果有）候選自己的 instance 快照
    （供 reader／稽核核對「換的是哪一個 instance」，不是驗收判斷的一部分）。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        repair_acceptance as RA,
    )

    for name, slot in (("baseline", baseline), ("candidate", candidate)):
        if any(slot.get(key) is not None for key in
               ("post_cts_topology", "method_consumption", "post_cts_pair_proof",
                _CORE_FACTS)):
            raise MethodExtensionError(
                R_CROSS_METHOD_EVIDENCE,
                f"setup-margin or CORE_UTILIZATION evidence was sent to "
                f"SINGLE_INSTANCE_ECO ({name} slot)")

    verdict = RA.evaluate_repair(
        baseline_setup=baseline.get("setup_endpoints"),
        candidate_setup=candidate.get("setup_endpoints"),
        baseline_core=baseline.get("core"),
        candidate_core=candidate.get("core"),
        eco_diff=candidate.get("eco_diff"),
        eco_proof=candidate.get("eco_proof"),
        final_pair_proof=candidate.get("final_pair_proof"),
        metric_comparisons=metric_comparisons,
        execution_ok=execution_ok,
        inputs_approved=inputs_approved, inputs_reason=inputs_reason)
    payload = verdict.to_json_dict()
    payload["method_identity"] = SINGLE_INSTANCE_ECO.method_identity
    instance_snapshot = candidate.get("instance_snapshot")
    if isinstance(instance_snapshot, dict):
        payload["instance_snapshot"] = instance_snapshot
    return ExtensionAcceptance(
        payload=payload, candidate_selected=verdict.repair_status == RA.REPAIRED)


__all__ = ["CORE_UTILIZATION", "CORE_UTILIZATION_NATIVE_FORMAL", "FIXTURE_ONLY",
           "NATIVE_EXTENSION_CANONICALIZATION", "NATIVE_PROOF_CONTEXT_FIELDS",
           "SETUP_MARGIN_NATIVE_PROOF", "extension_descriptor_sha256",
           "formal_proof_descriptor", "formal_proof_module", "host_native_variant",
           "is_core_utilization", "is_setup_margin", "setup_margin_native_proof_descriptor",
           "ORFS_NOT_RUN",
           "R_BASELINE_REFERENCE_MISSING", "R_FIXTURE_ONLY", "R_PROOF_KIND_MISSING", "PINNED_FIELDS", "SETUP_MARGIN",
           "SINGLE_INSTANCE_ECO",
           "ExtensionAcceptance", "MethodExtension", "MethodExtensionError",
           "R_CORE_EVIDENCE_UNAVAILABLE", "R_CROSS_METHOD_EVIDENCE", "R_LEGACY_SHAPE",
           "R_SETUP_IMPROVEMENT_NOT_MEASURED", "R_AREA_NOT_MEASURED",
           "R_AREA_NOT_COMPARABLE",
           "CORE_UTILIZATION_AREA_RATIO_LIMIT",
           "UNVERIFIABLE", "WORK_SCHEMA_UNSUPPORTED", "decode_fresh_reader",
           "descriptor_sha256", "evaluate_acceptance", "evaluate_core_utilization",
           "host_evidence_set", "read_consumption_evidence", "resolve_declared_identity",
           "resolve_extension", "resolve_pinned"]
