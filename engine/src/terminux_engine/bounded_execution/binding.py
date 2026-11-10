# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`PlanStepBinding v1`。

這是**疊在既有 proposal binding 之上**的一層，不是取代它：

- `ProposalRegistry` 仍是 proposal／approval／one-shot claim 的權威，並且
  已經綁定 exact action name ＋ canonical arguments ＋ member／project／
  session（見 `tool_safety.ProposalExecutionBinding`）。
- approved package action 額外綁 plan／step／catalog snapshot／capability contract，讓
  「這筆 proposal 是哪份 plan 的哪一步」也不可竄改。

因此 approved package action **沒有**建立第二套 approval authority，也沒有修改
`proposal_registry.py`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

BINDING_SCHEMA_VERSION = 1
BINDING_RECORD_TYPE = "agent_plan_step_binding"
PROPOSAL_KIND_PLAN_STEP = "plan_step"


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_arguments_sha256(arguments: dict[str, Any]) -> str:
    return sha256_text(canonical_json(arguments))


def compute_capability_contract_sha256(capability: dict[str, Any]) -> str:
    """只取影響「能不能安全執行」的欄位。

    description 也納入——approved package action 採最保守策略：capability 的任何實質改變都
    應該讓既有 proposal 失效（不做 semantic diff）。"""
    material = {
        "capability_id": capability.get("capability_id"),
        "description": capability.get("description"),
        "input_schema": capability.get("input_schema"),
        "risk": capability.get("risk"),
        "approval_required": capability.get("approval_required"),
        "side_effects": capability.get("side_effects"),
        "availability": capability.get("availability"),
    }
    return sha256_text(canonical_json(material))


def revalidate_binding_material(
    *,
    capability: dict[str, Any],
    catalog_snapshot_id: str,
    arguments: dict[str, Any],
    member_id: str,
    project_id: str,
    session_id: str,
    expected_capability_id: str,
    expected_catalog_snapshot_id: str,
    expected_capability_contract_sha256: str,
    expected_arguments_sha256: str,
    expected_risk: str,
    expected_identity: tuple[str, str, str],
) -> str | None:
    """派送前的六項重驗。回傳 denial code；`None` 代表全部相符。

    這是**唯一**一份規則：`PlanStepBinding.matches_runtime()`（approved package action plan
    step）與 `analysis_tasks` 的 task step 都委派到這裡，不各寫一套
。六項分別是 identity、catalog snapshot、capability
    identity、capability contract hash、risk/approval_required、
    arguments hash。
    """
    from terminux_engine.bounded_execution.errors import (
        BOUNDED_EXECUTION_IDENTITY_MISMATCH,
        PLAN_ARGUMENTS_REVALIDATION_FAILED,
        PLAN_CATALOG_DRIFT_REPLAN_REQUIRED,
        PLAN_PROPOSAL_BINDING_MISMATCH,
        PLAN_RISK_REVALIDATION_FAILED,
    )

    if (member_id, project_id, session_id) != expected_identity:
        return BOUNDED_EXECUTION_IDENTITY_MISMATCH
    if catalog_snapshot_id != expected_catalog_snapshot_id:
        return PLAN_CATALOG_DRIFT_REPLAN_REQUIRED
    if capability.get("capability_id") != expected_capability_id:
        return PLAN_PROPOSAL_BINDING_MISMATCH
    if compute_capability_contract_sha256(capability) != expected_capability_contract_sha256:
        return PLAN_CATALOG_DRIFT_REPLAN_REQUIRED
    if str(capability.get("risk")) != expected_risk or not capability.get("approval_required"):
        return PLAN_RISK_REVALIDATION_FAILED
    if compute_arguments_sha256(arguments) != expected_arguments_sha256:
        return PLAN_ARGUMENTS_REVALIDATION_FAILED
    return None


@dataclass(frozen=True)
class PlanStepBinding:
    schema_version: int
    record_type: str
    proposal_id: str
    proposal_kind: str
    plan_id: str
    plan_sha256: str
    step_id: str
    capability_id: str
    canonical_arguments: str
    arguments_sha256: str
    catalog_snapshot_id: str
    capability_contract_sha256: str
    risk: str
    member_id: str
    project_id: str
    session_id: str
    side_effects: tuple[str, ...]
    preconditions: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    created_at: str
    expires_at: float | None = None

    #: 常數。approved package action 只執行 DANGEROUS package action，一律需要明確核准。
    approval_required: bool = True

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "proposal_id": self.proposal_id,
            "proposal_kind": self.proposal_kind,
            "plan_id": self.plan_id,
            "plan_sha256": self.plan_sha256,
            "step_id": self.step_id,
            "capability_id": self.capability_id,
            "canonical_arguments": self.canonical_arguments,
            "arguments_sha256": self.arguments_sha256,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "capability_contract_sha256": self.capability_contract_sha256,
            "risk": self.risk,
            "approval_required": True,
            "member_id": self.member_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "side_effects": list(self.side_effects),
            "preconditions": list(self.preconditions),
            "expected_evidence": list(self.expected_evidence),
            "stop_conditions": list(self.stop_conditions),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def binding_sha256(self) -> str:
        payload = self.to_json_dict()
        payload.pop("created_at", None)
        payload.pop("expires_at", None)
        return sha256_text(canonical_json(payload))

    def matches_runtime(
        self, *, capability: dict[str, Any], catalog_snapshot_id: str,
        arguments: dict[str, Any], member_id: str, project_id: str, session_id: str,
    ) -> str | None:
        """執行前重驗。回傳 denial code；`None` 代表全部相符。

        規則本身在 `revalidate_binding_material()`，與 analysis task 共用
        同一份程式碼；這裡只負責把自己的欄位交出去。
        """
        return revalidate_binding_material(
            capability=capability, catalog_snapshot_id=catalog_snapshot_id,
            arguments=arguments, member_id=member_id, project_id=project_id,
            session_id=session_id,
            expected_capability_id=self.capability_id,
            expected_catalog_snapshot_id=self.catalog_snapshot_id,
            expected_capability_contract_sha256=self.capability_contract_sha256,
            expected_arguments_sha256=self.arguments_sha256,
            expected_risk=self.risk,
            expected_identity=(self.member_id, self.project_id, self.session_id),
        )


__all__ = [
    "BINDING_RECORD_TYPE",
    "BINDING_SCHEMA_VERSION",
    "PROPOSAL_KIND_PLAN_STEP",
    "PlanStepBinding",
    "canonical_json",
    "compute_arguments_sha256",
    "compute_capability_contract_sha256",
    "revalidate_binding_material",
    "sha256_text",
]
