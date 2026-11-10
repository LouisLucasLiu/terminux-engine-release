# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""host-native formal pair 的 Package-owned DQE evaluator。

與 container 版本（`formal_pair_dqe.py`）分開：package id、policy、row 前綴與
row 數都不同，兩者的 DQE 紀錄不會互相混用。多出的一列核對執行後的完整性
（binary 與全部可信輸入在執行期間沒有改變）。
"""

from __future__ import annotations

import json
import re
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
    canonical_json,
    sha256_file,
)
from terminux_engine.packages.orfs_run_intelligence.formal_pair_native import (
    DQE_EVALUATOR_ID,
    DQE_ID_PREFIX,
    DQE_INPUT_BINDING,
    DQE_PACKAGE_ID,
    DQE_POLICY_ID,
    DQE_ROW_COUNT,
    EXECUTION_MODE,
    TOOL_RESULT_SCHEMA,
    descriptor_digest,
    proof_descriptor,
)

_RAW_ROLE = "orfs.formal_pair." + RAW_RESULT_NAME
_HEX = re.compile(r"^[0-9a-f]{64}$")


def load_runtime_package() -> RuntimePackage:
    """供 descriptor-driven generic hook 載入本 Package，無 Common 分支。"""
    return PackageLoader().load(Path(__file__).resolve().parent / "formal_pair_native_dqe_package")


def run(ctx: PackageContext) -> PhaseResult:
    """評估一次 kernel-managed native formal tool event，寫既有 DQE DB。"""
    if ctx.event.tool_name != PROOF_KIND:
        return PhaseResult(status="noop", message="Not a formal pair event.")
    try:
        manifest_input = load_manifest_input(ctx.event)
        raw, raw_sha = _load_raw_result(manifest_input)
        checks = evaluate_checks(raw, raw_sha, capture_status=manifest_input.capture_status,
                                 event_id=ctx.event.event_id)
    except (ManifestNotApplicable, ManifestInputError, OSError, ValueError) as exc:
        return PhaseResult(status="fail",
                           message=f"native formal pair DQE input unavailable: {exc}",
                           facts={"evaluator_id": DQE_EVALUATOR_ID, "error": str(exc)},
                           evidence={"evidence_ready": False, "error": str(exc)})

    orchestration = orchestration_metadata(ctx.event)
    receipt = db_writer.write_dqe_batch(
        ctx, checks, flow="formal_pair_native",
        dqe_meta={str(row["id"]): {"title": str(row["id"]),
                                    "description": str(row.get("detail", "")),
                                    "reference": manifest_input.manifest_reference}
                  for row in checks},
        report_path=manifest_input.manifest_reference,
        orchestration_run_id=orchestration.get("run_id"), step_id=orchestration.get("step_id"),
        attempt=orchestration.get("attempt"), dispatch_key=orchestration.get("dispatch_key"),
        source_event_id=ctx.event.event_id, package_id=DQE_PACKAGE_ID)
    ready = receipt.status == "written" and receipt.rows_written == DQE_ROW_COUNT
    evidence = {
        "dqe_write": db_writer.receipt_to_evidence(receipt),
        "evaluator_id": DQE_EVALUATOR_ID, "policy_id": DQE_POLICY_ID,
        "manifest_id": manifest_input.manifest.manifest_id,
        "manifest_sha256": manifest_input.manifest_sha256,
        "row_count": DQE_ROW_COUNT, "evidence_ready": ready,
        "input_binding": DQE_INPUT_BINDING,
    }
    return PhaseResult(
        status="ok" if ready else "fail",
        message=f"native formal pair DQE rows={receipt.rows_written}/{DQE_ROW_COUNT}",
        facts={"evaluator_id": DQE_EVALUATOR_ID, "policy_id": DQE_POLICY_ID,
               "classification": raw.get("status"), "row_count": DQE_ROW_COUNT},
        evidence=evidence,
        records=[RecordRequest(
            namespace="orfs.formal_pair_native", record_type="dqe.v1",
            key=f"formal-pair-native-dqe:{ctx.event.event_id}",
            payload={"schema_version": DQE_EVALUATOR_ID, "raw_result_sha256": raw_sha,
                     "checks": checks, "evidence": evidence},
            display_message=f"Native formal pair DQE: {raw.get('status')}")])


def _load_raw_result(manifest_input: Any) -> tuple[dict[str, Any], str]:
    observations = manifest_input.observations_by_role().get(_RAW_ROLE, [])
    if len(observations) != 1:
        raise ValueError("raw formal tool result observation is missing or ambiguous")
    observation = observations[0]
    if observation.resolved_path is None or observation.content_identity is None:
        raise ValueError("raw formal tool result is not stable")
    path = Path(observation.resolved_path)
    if not path.is_file() or sha256_file(path) != observation.content_identity.digest:
        raise ValueError("raw formal tool result hash differs")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("raw formal tool result is not an object")
    return raw, observation.content_identity.digest


def _tool_identity_ok(tool: Any) -> bool:
    return (isinstance(tool, dict)
            and set(tool) == {"execution_mode", "executable_realpath", "executable_sha256"}
            and tool.get("execution_mode") == EXECUTION_MODE
            and isinstance(tool.get("executable_realpath"), str)
            and Path(tool["executable_realpath"]).is_absolute()
            and isinstance(tool.get("executable_sha256"), str)
            and bool(_HEX.fullmatch(tool["executable_sha256"])))


def evaluate_checks(raw: dict[str, Any], raw_sha: str, *, capture_status: str,
                    event_id: str) -> list[dict[str, Any]]:
    """十一列固定 contract；verdict 由 raw facts 決定，不以 exit code 取代。"""
    status = raw.get("status")
    equivalent = (status == "EQUIVALENT" and raw.get("exit_code") == 0
                  and raw.get("unproven_count") == 0)
    unresolved = (isinstance(raw.get("unproven_count"), int)
                  and raw["unproven_count"] > 0
                  and raw.get("exit_code") != 0
                  and raw.get("equiv_status_assert_failure") is True)
    unproven = (status == "UNPROVEN" and unresolved
                and raw.get("inequivalence_evidence") is not True)
    non_equivalent = (status == "NOT_EQUIVALENT" and unresolved
                      and raw.get("inequivalence_evidence") is True
                      and isinstance(raw.get("counterexample"), dict))
    integrity = raw.get("post_run_integrity")
    rows = [
        ("schema", raw.get("schema_version") == TOOL_RESULT_SCHEMA),
        ("proof_kind", raw.get("proof_kind") == PROOF_KIND),
        ("manifest", capture_status == "complete"),
        ("raw_result", bool(raw_sha)),
        ("script", raw.get("script_sha256") is not None),
        ("argv", isinstance(raw.get("argv_sha256"), str)),
        ("timeout", raw.get("timeout_seconds") == proof_descriptor()["timeout_seconds"]),
        ("tool_identity", raw.get("proof_descriptor_sha256") == descriptor_digest()
         and _tool_identity_ok(raw.get("tool"))),
        ("input_identity", isinstance(raw.get("baseline"), dict)
         and isinstance(raw.get("candidate"), dict)),
        ("post_run_integrity", isinstance(integrity, dict) and integrity.get("ok") is True),
        ("verdict", equivalent or non_equivalent or unproven),
    ]
    return [{"id": f"{DQE_ID_PREFIX}{name}", "title": name, "value": str(ok),
             "status": "OK" if ok else "ERROR",
             "semantic_status": "pass" if ok else "fail",
             "detail": canonical_json({"event_id": event_id, "classification": status})}
            for name, ok in rows]


__all__ = ["evaluate_checks", "load_runtime_package", "run"]
