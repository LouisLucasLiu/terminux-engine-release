# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""由 durable 紀錄重建整個 CORE 比較決定（跨 FlowTask 的 fresh reader）。

驗收句：

    A fresh process reconstructed the complete comparison decision solely
    from durable identities, events, and manifests across FlowTasks.

因此這支模組**只**吃三種東西：

* EventStore 裡已持久化的事件；
* artifact manifest store 裡的 manifest（以 `artifact_manifest_id` 定位，
  讀回後重算 digest）；
* 由上述兩者指出的檔案（slot 證據檔的位置與 hash 都來自 durable verdict）。

它不吃：呼叫端傳進來的路徑、記憶體裡的 live 物件、process-local cache，
以及任何 fixture fallback。少任何一項 durable 紀錄就拋，不補值、不降級。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terminux_engine.packages.orfs_run_intelligence import (
    core_proof_resolution as PR,
    core_utilization_evidence as CU,
)

#: 這條流程涉及的 FlowTask 角色。每一個都必須有自己的 durable 身分。
FLOW_TASK_ROLES: tuple[str, ...] = (
    "candidate_orfs", "formal_pair", "core_comparison")


class ComparisonReconstructionError(RuntimeError):
    """無法只由 durable 紀錄重建。比較決定不成立。"""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


@dataclass(frozen=True)
class ReconstructedComparison:
    """重建結果。每個欄位都可以指回它的 durable 來源。"""

    work_id: str
    attempt_id: str
    flow_tasks: dict[str, str]
    baseline_evidence: dict[str, Any]
    candidate_evidence: dict[str, Any]
    envelope: dict[str, Any]
    decision: dict[str, Any]
    manifest_digests: dict[str, str] = field(default_factory=dict)

    @property
    def proof_ok(self) -> bool:
        return self.decision.get("proof_status") == "equivalent"

    @property
    def selection_eligible(self) -> bool:
        return bool(self.decision.get("selection_eligible"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(value: Any, reason: str, **details: Any) -> Any:
    if not value:
        raise ComparisonReconstructionError(reason, **details)
    return value


def _contained(roots: tuple[Path, ...], path: Path) -> bool:
    resolved = path.resolve()
    return any(root == resolved or root in resolved.parents for root in roots)


def _read_slot_evidence(identity: Any, *, slot: str,
                        authorized_roots: tuple[Path, ...]) -> dict[str, Any]:
    """由 durable verdict 記下的位置與 hash 讀回一份 slot 證據。

    路徑必須**解析後**仍落在授權根目錄之內：verdict 裡的字串看起來在界內，
    經 symlink 解析後卻可能指到別處，那正是要擋的形狀。
    """
    if not isinstance(identity, dict):
        raise ComparisonReconstructionError(
            f"the durable verdict records no {slot} slot evidence")
    path = Path(str(identity.get("path") or ""))
    if not path.is_absolute():
        raise ComparisonReconstructionError(
            f"the {slot} slot evidence path is not absolute", path=str(path))
    if path.is_symlink():
        raise ComparisonReconstructionError(
            f"the {slot} slot evidence is a symlink; its target can be "
            f"repointed after the hash check", path=str(path))
    if authorized_roots and not _contained(authorized_roots, path):
        raise ComparisonReconstructionError(
            f"the {slot} slot evidence resolves outside the authorized roots",
            path=str(path.resolve()))
    if not path.is_file():
        raise ComparisonReconstructionError(
            f"the {slot} slot evidence is not readable", path=str(path))
    digest = _sha256(path)
    if digest != identity.get("sha256"):
        raise ComparisonReconstructionError(
            f"the {slot} slot evidence hash differs from the durable record",
            path=str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != CU.SCHEMA_VERSION:
        raise ComparisonReconstructionError(
            f"the {slot} slot evidence is not a CORE evidence document")
    if payload.get("evidence_origin") != CU.ORIGIN_ORFS or payload.get("fixture"):
        raise ComparisonReconstructionError(
            f"the {slot} slot evidence is not a real ORFS record; fixture "
            f"evidence never enters the production reader")
    return payload


def reconstruct_comparison(
    *, event_store, artifact_store, member_id: str, work_id: str, attempt_id: str,
    descriptor_sha256: str, expected_tool: dict[str, Any],
    step_result_event_type: str, verdict_event_type: str | None = None,
    authorized_roots: tuple[str | Path, ...] = (),
) -> ReconstructedComparison:
    """只由 durable 紀錄重建兩側 slot 證據、formal envelope 與比較決定。

    `authorized_roots` 是允許放置 slot 證據的根目錄（execution root／artifact
    root）。給了就強制路徑包含檢查；空 tuple 只在單元測試的窄範圍使用。
    """
    payloads = PR.step_result_payloads(
        event_store, member_id=member_id, event_type=step_result_event_type)
    if verdict_event_type:
        payloads += PR.step_result_payloads(
            event_store, member_id=member_id, event_type=verdict_event_type)

    verdicts = [row for row in payloads if isinstance(row.get("core_slot_evidence"), dict)]
    if not verdicts:
        raise ComparisonReconstructionError(
            "no durable verdict records the CORE slot evidence identities")
    if len({json.dumps(row["core_slot_evidence"], sort_keys=True)
            for row in verdicts}) != 1:
        raise ComparisonReconstructionError(
            "the durable verdicts disagree on the CORE slot evidence identities")
    slot_files = verdicts[-1]["core_slot_evidence"]

    roots = tuple(Path(row).resolve() for row in authorized_roots)
    baseline = _read_slot_evidence(slot_files.get("baseline"), slot="baseline",
                                   authorized_roots=roots)
    candidate = _read_slot_evidence(slot_files.get("candidate"), slot="candidate",
                                    authorized_roots=roots)

    # ── 角色不得互換 ─────────────────────────────────────────────
    base_proof = dict(baseline.get("pair_proof") or {})
    cand_proof = dict(candidate.get("pair_proof") or {})
    if base_proof.get("role") != CU.PROOF_ROLE_BASELINE:
        raise ComparisonReconstructionError(
            "the baseline slot does not carry the baseline proof role",
            role=base_proof.get("role"))
    if cand_proof.get("role") != CU.PROOF_ROLE_CANDIDATE:
        raise ComparisonReconstructionError(
            "the candidate slot does not carry the candidate proof role",
            role=cand_proof.get("role"))

    # ── candidate 的 manifest：以 id 定位、讀回後重算 digest ──────
    manifest_id = _require(
        candidate.get("original_run_identity", {}).get("manifest_id")
        or cand_proof.get("manifest_id") or _candidate_manifest_id(payloads, candidate),
        "the candidate run's artifact manifest id is not recorded")
    manifest_digests: dict[str, str] = {}
    try:
        recomputed = artifact_store.sha256_of(str(manifest_id))
        manifest = artifact_store.load(str(manifest_id))
    except (OSError, ValueError) as exc:
        raise ComparisonReconstructionError(
            "the candidate artifact manifest cannot be read back",
            manifest_id=str(manifest_id), error=str(exc)) from None
    manifest_digests[str(manifest_id)] = recomputed
    if recomputed != candidate.get("manifest_digest_expected", recomputed):
        raise ComparisonReconstructionError(
            "the artifact manifest digest differs from the durable record")

    attested = {str(row.resolved_path): row.content_identity.digest
                for row in manifest.observations
                if row.resolved_path and row.content_identity is not None}
    log_identity = dict(candidate.get("consuming_floorplan_log") or {})
    if attested.get(str(log_identity.get("path"))) != log_identity.get("sha256"):
        raise ComparisonReconstructionError(
            "the floorplan log is no longer attested by the reloaded manifest",
            path=str(log_identity.get("path")))

    # ── formal envelope：由 durable identity 找回並重驗 ───────────
    baseline_netlist = str(base_proof.get("input_sha256") or "")
    candidate_netlist = str(cand_proof.get("input_sha256") or "")
    try:
        envelope, qualification_only = PR.resolve_pair_proof(
            payloads, work_id=work_id, attempt_id=attempt_id,
            descriptor_sha256=descriptor_sha256,
            baseline_netlist_sha256=baseline_netlist,
            candidate_netlist_sha256=candidate_netlist)
    except PR.PairProofResolutionError as exc:
        raise ComparisonReconstructionError(exc.reason, **exc.details) from None
    try:
        binding = CU.verify_pair_proof_envelope(
            envelope, work_id=work_id, attempt_id=attempt_id,
            expected_descriptor_sha256=descriptor_sha256,
            baseline_netlist_sha256=baseline_netlist,
            candidate_netlist_sha256=candidate_netlist,
            expected_tool=expected_tool, qualification_only=qualification_only)
    except CU.CoreEvidenceUnverifiable as exc:
        raise ComparisonReconstructionError(exc.reason, **exc.details) from None

    for slot, proof in (("baseline", base_proof), ("candidate", cand_proof)):
        if proof.get("durable_envelope_sha256") != binding.envelope_sha256:
            raise ComparisonReconstructionError(
                f"the {slot} slot references another proof envelope")
        if proof.get("work_id") != work_id or proof.get("attempt_id") != attempt_id:
            raise ComparisonReconstructionError(
                f"the {slot} slot proof belongs to another Work or attempt")

    # ── FlowTask 身分：三個角色各自成立 ──────────────────────────
    flow_tasks = {
        "candidate_orfs": str(candidate.get("task_id") or ""),
        "formal_pair": binding.task_id,
        "core_comparison": str(cand_proof.get("proof_task_id") or binding.task_id),
    }
    if flow_tasks["candidate_orfs"] == flow_tasks["formal_pair"]:
        raise ComparisonReconstructionError(
            "the ORFS run and the formal proof claim the same FlowTask identity")
    for role, value in flow_tasks.items():
        _require(value, f"the {role} FlowTask identity is missing")

    # ── 比較決定：以重建出來的兩側事實重算 ──────────────────────
    from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME

    merged_baseline = {**baseline, **_identity_fields(baseline)}
    merged_candidate = {**candidate, **_identity_fields(candidate)}
    decision = dict(ME.evaluate_core_utilization(
        baseline=merged_baseline, candidate=merged_candidate))
    return ReconstructedComparison(
        work_id=work_id, attempt_id=attempt_id, flow_tasks=flow_tasks,
        baseline_evidence=baseline, candidate_evidence=candidate,
        envelope=envelope, decision=decision, manifest_digests=manifest_digests)


def _identity_fields(evidence: dict[str, Any]) -> dict[str, Any]:
    """把 slot 證據內記錄的執行身分攤平，供 `evaluate_core_utilization` 使用。"""
    original = dict(evidence.get("original_run_identity") or {})
    return {
        "receipt_digest": evidence.get("receipt_digest") or original.get("receipt_digest"),
        "manifest_digest": evidence.get("manifest_digest") or original.get("manifest_digest"),
        "dqe_digest": evidence.get("dqe_digest") or original.get("dqe_digest"),
        "result_event_id": evidence.get("result_event_id") or original.get("result_event_id"),
    }


def _candidate_manifest_id(payloads: list[dict[str, Any]],
                           candidate: dict[str, Any]) -> str | None:
    """由 durable run evidence 找回這個 candidate run 的 manifest id。"""
    run_id = candidate.get("run_id")
    for row in payloads:
        evidence = row.get("completed_run_evidence")
        if isinstance(evidence, dict) and evidence.get("run_id") == run_id:
            return evidence.get("artifact_manifest_id")
    return None


__all__ = [
    "FLOW_TASK_ROLES", "ComparisonReconstructionError", "ReconstructedComparison",
    "reconstruct_comparison",
]
