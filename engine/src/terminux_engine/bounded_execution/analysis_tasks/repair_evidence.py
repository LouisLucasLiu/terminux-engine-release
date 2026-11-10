# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`VerifiedRepairEvidence` — host 端對 repair 證據的身分驗證。

分工固定：

* **host 只驗身分與關聯**：scope／task／run／slot／stage、producer、
  manifest／path／hash／reference。它**不解讀** netlist 差異、checker 結果
  或 timing——那是 Package 的事。
* 證據值**不得**從 provider JSON 取得；唯一來源是本 task 自己 slot 輸出區內、
  由正式 flow 在 stage 內寫出的檔案。
* 正式 producer 與注入入口才是信任依據；結構驗證只是補充，**不是**
  「有某幾個欄位就當可信」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks import errors as E

#: 每一份證據檔的固定身分：(邏輯名稱, 檔名樣板, 產生它的 stage)
REPAIR_EVIDENCE_FILES: tuple[tuple[str, str, str], ...] = (
    ("setup_endpoints", "terminux_r5_final_{slot}_setup_endpoints.json", "final"),
    ("core", "terminux_r5_final_{slot}_core.json", "final"),
    ("eco_diff", "terminux_r5_eco_diff.json", "place"),
    ("eco_proof", "terminux_r5_eco_proof.json", "place"),
    ("final_pair_proof", "terminux_r5_final_pair_proof.json", "final"),
)

#: 只有 candidate 才會有的證據。
CANDIDATE_ONLY: frozenset[str] = frozenset(
    {"eco_diff", "eco_proof", "final_pair_proof"})

#: 速度導向重合成的證據集合。沒有 ECO 產物；改用兩側 post-resize
#: 拓樸證明真實 netlist 變化，並各有一組指定 pair 的等價證明。
RESYNTHESIS_EVIDENCE_FILES: tuple[tuple[str, str, str], ...] = (
    ("setup_endpoints", "terminux_r5_final_{slot}_setup_endpoints.json", "final"),
    ("core", "terminux_r5_final_{slot}_core.json", "final"),
    ("post_resize_setup", "terminux_r5_post_resize_{slot}_setup_endpoints.json",
     "place"),
    ("post_resize_topology", "terminux_r5_post_resize_{slot}_topology.json",
     "place"),
    ("post_resize_pair_proof", "terminux_r5_post_resize_pair_proof.json", "place"),
    ("final_pair_proof", "terminux_r5_final_pair_proof.json", "final"),
)
RESYNTHESIS_CANDIDATE_ONLY: frozenset[str] = frozenset(
    {"post_resize_pair_proof", "final_pair_proof"})

#: SETUP_SLACK_MARGIN 受控修復的證據集合。作用點在 CTS（見
#: `scripts/cts.tcl` 的 `repair_timing_helper`），因此中間比較點是
#: **post-CTS** 而不是 post-resize；名稱如實反映階段，不沿用會誤導的名字。
SETUP_MARGIN_EVIDENCE_FILES: tuple[tuple[str, str, str], ...] = (
    ("setup_endpoints", "terminux_r8_final_{slot}_setup_endpoints.json", "final"),
    ("core", "terminux_r8_final_{slot}_core.json", "final"),
    ("post_cts_setup", "terminux_r8_post_cts_{slot}_setup_endpoints.json", "cts"),
    ("post_cts_topology", "terminux_r8_post_cts_{slot}_topology.json", "cts"),
    ("post_cts_pair_proof", "terminux_r8_post_cts_pair_proof.json", "cts"),
    ("final_pair_proof", "terminux_r8_final_pair_proof.json", "final"),
    ("method_consumption", "terminux_r8_method_consumption_{slot}.json", "cts"),
)
SETUP_MARGIN_CANDIDATE_ONLY: frozenset[str] = frozenset(
    {"post_cts_pair_proof", "final_pair_proof"})

#: CORE_UTILIZATION extension 的證據集合：每個 slot 一份 Package 定義的
#: per-slot 證據。執行身分（receipt／manifest／DQE／result event）不在檔案裡，
#: 由 host 在判定時從本次 run 的 durable 證據補上。
CORE_UTILIZATION_EVIDENCE_FILES: tuple[tuple[str, str, str], ...] = (
    ("core_utilization", "terminux_core_utilization_{slot}.json", "floorplan"),
)
CORE_UTILIZATION_CANDIDATE_ONLY: frozenset[str] = frozenset()

#: 單一非 clock buffer instance 換型 ECO。與 setup repair `REPAIR_EVIDENCE_FILES`
#: 同一種形狀（`eco_diff`／`eco_proof`／`final_pair_proof`／setup／core），
#: 只是檔名改用本方法自己的前綴，不與 setup repair 既有產物混用；`instance_snapshot`
#: 是本方法才有的候選專屬證據：host 已驗證的、置換**之前**的 instance cell
#: 快照，供 `single_instance_eco.check_source_drift()` 核對來源漂移。
SINGLE_INSTANCE_ECO_EVIDENCE_FILES: tuple[tuple[str, str, str], ...] = (
    ("setup_endpoints", "terminux_b3_final_{slot}_setup_endpoints.json", "final"),
    ("core", "terminux_b3_final_{slot}_core.json", "final"),
    ("eco_diff", "terminux_b3_eco_diff.json", "place"),
    ("eco_proof", "terminux_b3_eco_proof.json", "place"),
    ("final_pair_proof", "terminux_b3_final_pair_proof.json", "final"),
    ("instance_snapshot", "terminux_b3_instance_snapshot.json", "place"),
)
SINGLE_INSTANCE_ECO_CANDIDATE_ONLY: frozenset[str] = frozenset(
    {"eco_diff", "eco_proof", "final_pair_proof", "instance_snapshot"})

#: extension 宣告的 host evidence set 名稱 → (檔案集合, 只有 candidate 的名稱)。
EVIDENCE_SETS: dict[str, tuple[tuple[tuple[str, str, str], ...], frozenset[str]]] = {
    "setup_margin": (SETUP_MARGIN_EVIDENCE_FILES, SETUP_MARGIN_CANDIDATE_ONLY),
    "core_utilization": (CORE_UTILIZATION_EVIDENCE_FILES,
                         CORE_UTILIZATION_CANDIDATE_ONLY),
    "single_instance_eco": (SINGLE_INSTANCE_ECO_EVIDENCE_FILES,
                            SINGLE_INSTANCE_ECO_CANDIDATE_ONLY),
}


class RepairEvidenceError(E.TaskError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(E.TASK_EVIDENCE_INVALID, message, **details)


@dataclass(frozen=True)
class EvidenceRef:
    """一份證據檔的身分。內容由 Package 解讀，不在這裡判斷。"""

    name: str
    slot: str
    stage: str
    path: str
    sha256: str
    bytes: int
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_json_dict(self) -> dict[str, Any]:
        return {"name": self.name, "slot": self.slot, "stage": self.stage,
                "path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True)
class VerifiedRepairEvidence:
    """唯讀。只有身分與關聯全部通過才會被建立。"""

    member_id: str
    project_id: str
    task_id: str
    slot: str
    run_id: str
    output_root: str
    variant: str
    refs: dict[str, EvidenceRef] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    def payload(self, name: str) -> dict[str, Any] | None:
        ref = self.refs.get(name)
        return dict(ref.payload) if ref is not None else None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id, "project_id": self.project_id,
            "task_id": self.task_id, "slot": self.slot, "run_id": self.run_id,
            "output_root": self.output_root, "variant": self.variant,
            "refs": {k: v.to_json_dict() for k, v in sorted(self.refs.items())},
            "missing": list(self.missing),
        }


def host_run_facts(run_evidence: dict[str, Any] | None, *, task_id: str, slot: str,
                   result_event_id: str | None) -> dict[str, Any]:
    """本次 run 的執行身分：receipt／artifact manifest／DQE／result event。

    來源只有 host 自己的 durable run evidence 與 result event，因此 replay 的
    corpus 不可能把舊 run 的執行身分冒充成本次的。
    """
    evidence = dict(run_evidence or {})
    return {"task_id": task_id, "slot": slot,
            "run_id": evidence.get("run_id"),
            "receipt_digest": evidence.get("receipt_content_hash"),
            "manifest_digest": evidence.get("artifact_manifest_sha256"),
            # manifest 的**內容**要靠 id 才讀得回來（digest 只證明沒被改）。
            # CORE 的 production 證據要核對 floorplan log 的 attest，因此需要它。
            "artifact_manifest_id": evidence.get("artifact_manifest_id"),
            "dqe_digest": evidence.get("dqe_dispatch_key"),
            "result_event_id": result_event_id}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_repair_evidence(
    *, context: Any, task_id: str, slot: str, run_id: str,
    output_root: str, platform_id: str, design_id: str, variant: str,
    resynthesis: bool = False, setup_margin: bool = False,
    evidence_set: str | None = None,
) -> VerifiedRepairEvidence:
    """由**本 task 自己 slot 的輸出區**收集並驗證 repair 證據身分。

    路徑由 host 從 binding 組出——不是 caller 給的。任何一份檔案落在輸出區
    之外、讀不到或不是 JSON object，都直接拒絕。
    """
    member_id = getattr(context, "member_id", None)
    project_id = getattr(context, "project_id", None)
    if not member_id or not project_id:
        raise RepairEvidenceError("the request context carries no canonical scope")

    root = Path(output_root).resolve()
    reports = root / "reports" / platform_id / design_id / variant
    refs: dict[str, EvidenceRef] = {}
    missing: list[str] = []

    if evidence_set is not None:
        # extension 宣告的集合優先：host 不替方法猜證據檔。
        if evidence_set not in EVIDENCE_SETS:
            raise RepairEvidenceError(f"unknown evidence set: {evidence_set!r}",
                                      task_id=task_id, slot=slot)
        files, candidate_only = EVIDENCE_SETS[evidence_set]
    elif setup_margin:
        files = SETUP_MARGIN_EVIDENCE_FILES
        candidate_only = SETUP_MARGIN_CANDIDATE_ONLY
    elif resynthesis:
        files = RESYNTHESIS_EVIDENCE_FILES
        candidate_only = RESYNTHESIS_CANDIDATE_ONLY
    else:
        files = REPAIR_EVIDENCE_FILES
        candidate_only = CANDIDATE_ONLY
    for name, template, stage in files:
        if slot != "candidate" and name in candidate_only:
            continue
        path = (reports / template.format(slot=slot)).resolve()
        # 證據只能來自本 slot 的輸出區。
        if root not in path.parents:
            raise RepairEvidenceError(
                f"evidence {name!r} resolves outside this slot output area",
                task_id=task_id, slot=slot)
        if not path.is_file():
            missing.append(f"{name}: {path}")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RepairEvidenceError(
                f"evidence {name!r} is unreadable: {exc}",
                task_id=task_id, slot=slot) from exc
        if not isinstance(payload, dict):
            raise RepairEvidenceError(
                f"evidence {name!r} is not a JSON object",
                task_id=task_id, slot=slot)
        refs[name] = EvidenceRef(
            name=name, slot=slot, stage=stage, path=str(path),
            sha256=_sha256(path), bytes=path.stat().st_size, payload=payload)

    return VerifiedRepairEvidence(
        member_id=str(member_id), project_id=str(project_id), task_id=task_id,
        slot=slot, run_id=run_id, output_root=str(root), variant=variant,
        refs=refs, missing=tuple(missing))


__all__ = [
    "CANDIDATE_ONLY", "REPAIR_EVIDENCE_FILES",
    "SETUP_MARGIN_EVIDENCE_FILES", "SETUP_MARGIN_CANDIDATE_ONLY",
    "SINGLE_INSTANCE_ECO_EVIDENCE_FILES", "SINGLE_INSTANCE_ECO_CANDIDATE_ONLY",
    "EVIDENCE_SETS",
    "EvidenceRef", "RepairEvidenceError", "VerifiedRepairEvidence",
    "read_repair_evidence",
]
