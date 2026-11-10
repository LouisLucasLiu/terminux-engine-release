# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Formal pair 的 Package-owned DQE evaluator。

這不是 ORFS 14-row 或 setup-margin evaluator 的變形。它只讀 Kernel 已捕獲的
formal pair manifest，透過既有 DQE writer 寫入固定十列，並把 evidence
readiness 留給既有 Kernel barrier 判定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.loader import PackageLoader, RuntimePackage
from terminux_engine.package_runtime.result import PhaseResult, RecordRequest
from terminux_engine.packages._be_dqe_common import db_writer
from terminux_engine.packages.orfs_dqe.manifest_input import (
    ManifestInputError,
    ManifestNotApplicable,
    load_manifest_input,
    orchestration_metadata,
)

from terminux_engine.packages.orfs_run_intelligence.formal_pair import (
    PROOF_KIND,
    RAW_RESULT_NAME,
    TOOL_RESULT_SCHEMA,
    canonical_json,
    proof_descriptor,
    sha256_file,
)

EVALUATOR_ID = "orfs.formal_pair_dqe.v1"
POLICY_ID = "orfs.formal_pair_evidence_readiness.v1"
PACKAGE_ID = "orfs_formal_pair_dqe"
ROW_COUNT = 10
FP_RUN_RESULT_BINDING = "FP_RUN_RESULT_BINDING"
_RAW_ROLE = "orfs.formal_pair." + RAW_RESULT_NAME


def load_runtime_package() -> RuntimePackage:
    """供 descriptor-driven generic hook 載入本 Package，無 Common 分支。"""
    return PackageLoader().load(Path(__file__).resolve().parent / "formal_pair_dqe_package")


def run(ctx: PackageContext) -> PhaseResult:
    """評估一次 kernel-managed formal tool event，寫既有 DQE DB。"""
    if ctx.event.tool_name != PROOF_KIND:
        return PhaseResult(status="noop", message="Not a formal pair event.")
    try:
        manifest_input = load_manifest_input(ctx.event)
        raw, raw_sha = _load_raw_result(manifest_input)
        checks = _checks(raw, raw_sha, manifest_input, ctx)
    except (ManifestNotApplicable, ManifestInputError, OSError, ValueError) as exc:
        return PhaseResult(status="fail", message=f"formal pair DQE input unavailable: {exc}",
                           facts={"evaluator_id": EVALUATOR_ID, "error": str(exc)},
                           evidence={"evidence_ready": False, "error": str(exc)})

    orchestration = orchestration_metadata(ctx.event)
    receipt = db_writer.write_dqe_batch(
        ctx, checks, flow="formal_pair",
        dqe_meta={str(row["id"]): {"title": str(row["id"]),
                                    "description": str(row.get("detail", "")),
                                    "reference": manifest_input.manifest_reference}
                  for row in checks},
        report_path=manifest_input.manifest_reference,
        orchestration_run_id=orchestration.get("run_id"), step_id=orchestration.get("step_id"),
        attempt=orchestration.get("attempt"), dispatch_key=orchestration.get("dispatch_key"),
        source_event_id=ctx.event.event_id, package_id=PACKAGE_ID)
    ready = receipt.status == "written" and receipt.rows_written == ROW_COUNT
    evidence = {
        "dqe_write": db_writer.receipt_to_evidence(receipt),
        "evaluator_id": EVALUATOR_ID, "policy_id": POLICY_ID,
        "manifest_id": manifest_input.manifest.manifest_id,
        "manifest_sha256": manifest_input.manifest_sha256,
        "row_count": ROW_COUNT, "evidence_ready": ready,
        "input_binding": FP_RUN_RESULT_BINDING,
    }
    return PhaseResult(
        status="ok" if ready else "fail",
        message=f"formal pair DQE rows={receipt.rows_written}/{ROW_COUNT}",
        facts={"evaluator_id": EVALUATOR_ID, "policy_id": POLICY_ID,
               "classification": raw.get("status"), "row_count": ROW_COUNT},
        evidence=evidence,
        records=[RecordRequest(
            namespace="orfs.formal_pair", record_type="dqe.v1",
            key=f"formal-pair-dqe:{ctx.event.event_id}",
            payload={"schema_version": EVALUATOR_ID, "raw_result_sha256": raw_sha,
                     "checks": checks, "evidence": evidence},
            display_message=f"Formal pair DQE: {raw.get('status')}")])


def _load_raw_result(manifest_input: Any) -> tuple[dict[str, Any], str]:
    observations = manifest_input.observations_by_role().get(_RAW_ROLE, [])
    if len(observations) != 1:
        raise ValueError("raw formal tool result observation is missing or ambiguous")
    observation = observations[0]
    if (observation.resolved_path is None or observation.content_identity is None):
        raise ValueError("raw formal tool result is not stable")
    path = Path(observation.resolved_path)
    if not path.is_file() or sha256_file(path) != observation.content_identity.digest:
        raise ValueError("raw formal tool result hash differs")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("raw formal tool result is not an object")
    return raw, observation.content_identity.digest


def _checks(raw: dict[str, Any], raw_sha: str, manifest_input: Any,
            ctx: PackageContext) -> list[dict[str, Any]]:
    """十列固定 contract；所有行仍保留而不是用 exit code 取代 verdict。"""
    descriptor = proof_descriptor()
    status = raw.get("status")
    equivalent = (status == "EQUIVALENT" and raw.get("exit_code") == 0
                  and raw.get("unproven_count") == 0)
    # 「仍有未證明義務」與「已證出不等價」是兩件事，DQE 也必須分開核對：
    # 前者只要求計數與 assert 失敗，後者**另外**要求可核驗的反例證據。
    unresolved = (isinstance(raw.get("unproven_count"), int)
                  and raw["unproven_count"] > 0
                  and raw.get("exit_code") != 0
                  and raw.get("equiv_status_assert_failure") is True)
    unproven = (status == "UNPROVEN" and unresolved
                and raw.get("inequivalence_evidence") is not True)
    non_equivalent = (status == "NOT_EQUIVALENT" and unresolved
                      and raw.get("inequivalence_evidence") is True
                      and isinstance(raw.get("counterexample"), dict))
    rows = [
        ("schema", raw.get("schema_version") == TOOL_RESULT_SCHEMA),
        ("proof_kind", raw.get("proof_kind") == PROOF_KIND),
        ("manifest", manifest_input.capture_status == "complete"),
        ("raw_result", bool(raw_sha)),
        ("script", raw.get("script_sha256") is not None),
        ("argv", isinstance(raw.get("argv_sha256"), str)),
        ("timeout", raw.get("timeout_seconds") == descriptor["timeout_seconds"]),
        ("tool_identity", raw.get("tool") == descriptor["tool"]),
        ("input_identity", isinstance(raw.get("baseline"), dict)
         and isinstance(raw.get("candidate"), dict)),
        # 三種都是已完成判讀的工程結果；`UNPROVEN` 不是失敗，是明確的拒絕依據。
        ("verdict", equivalent or non_equivalent or unproven),
    ]
    return [{"id": f"formal_pair.{name}", "title": name, "value": str(ok),
             "status": "OK" if ok else "ERROR",
             "semantic_status": "pass" if ok else "fail",
             "detail": canonical_json({"event_id": ctx.event.event_id,
                                         "classification": status})}
            for name, ok in rows]


__all__ = ["EVALUATOR_ID", "FP_RUN_RESULT_BINDING", "POLICY_ID", "ROW_COUNT", "load_runtime_package", "run"]
