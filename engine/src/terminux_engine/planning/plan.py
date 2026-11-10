# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Bounded, non-executable Plan schema v1。

Plan 是**建議**，不是 Kernel state、不是 execution receipt、也不是完成宣告
。`execution_allowed` 永遠是 `False`——它不是一個可以被翻成
`True` 的旗標，而是這個 schema 的常數。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

PLAN_SCHEMA_VERSION = 1
PLAN_MODE = "plan_only"

MAX_STEPS = 5
MAX_GOAL_CHARS = 2000
MAX_TEXT_ITEM_CHARS = 400
MAX_LIST_ITEMS = 12
MAX_ARGUMENT_BYTES = 4000

STEP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: v1 不支援任何 control flow 或 dynamic binding。這些 key 出現在 step 裡
#: 就是 unsupported——不是「先忽略」，是明確拒絕。
UNSUPPORTED_STEP_KEYS: frozenset[str] = frozenset({
    "if", "condition", "when", "unless", "loop", "for_each", "foreach",
    "while", "repeat", "retry", "branch", "else", "on_failure", "on_success",
})

#: Arguments 裡不得出現的 dynamic binding 表示法。
UNSUPPORTED_BINDING_MARKERS: tuple[str, ...] = (
    "{{", "}}", "${", "$(", "<<", "steps.", "outputs.", "result.", "@step",
)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    risk: str = "dangerous"
    approval_required: bool = True
    side_effects: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "capability": self.capability,
            "arguments": dict(self.arguments),
            "depends_on": list(self.depends_on),
            "preconditions": list(self.preconditions),
            "expected_evidence": list(self.expected_evidence),
            "stop_conditions": list(self.stop_conditions),
            "risk": self.risk,
            "approval_required": self.approval_required,
            "side_effects": list(self.side_effects),
        }

    def identity(self) -> dict[str, Any]:
        """進 plan_id 的部分——不含 authority 重填前的值以外的東西。"""
        return {
            "step_id": self.step_id,
            "capability": self.capability,
            "arguments": dict(self.arguments),
            "depends_on": list(self.depends_on),
            "preconditions": list(self.preconditions),
            "expected_evidence": list(self.expected_evidence),
            "stop_conditions": list(self.stop_conditions),
        }


@dataclass(frozen=True)
class PlanValidationResult:
    status: str
    errors: tuple[dict[str, Any], ...] = ()

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def to_json_dict(self) -> dict[str, Any]:
        return {"status": self.status, "errors": [dict(e) for e in self.errors]}


@dataclass(frozen=True)
class Plan:
    schema_version: int
    plan_id: str
    mode: str
    goal: str
    catalog_snapshot_id: str
    created_from: dict[str, Any]
    observations: tuple[str, ...]
    unknowns: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    global_stop_conditions: tuple[str, ...]
    limitations: tuple[str, ...]
    validation: PlanValidationResult

    #: 常數。Plan-only 的輸出永遠不可執行；這裡刻意不是可設定欄位。
    execution_allowed: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "mode": self.mode,
            "goal": self.goal,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "created_from": dict(self.created_from),
            "observations": list(self.observations),
            "unknowns": list(self.unknowns),
            "steps": [s.to_json_dict() for s in self.steps],
            "global_stop_conditions": list(self.global_stop_conditions),
            "limitations": list(self.limitations),
            "execution_allowed": False,
            "validation": self.validation.to_json_dict(),
        }


def compute_plan_id(
    *,
    goal: str,
    catalog_snapshot_id: str,
    steps: list[PlanStep] | tuple[PlanStep, ...],
    observations: list[str] | tuple[str, ...],
    unknowns: list[str] | tuple[str, ...],
    global_stop_conditions: list[str] | tuple[str, ...],
) -> str:
    """由 canonical normalized content 計算。

    Timestamp、absolute path、provider latency 與 rendering 一律不參與
   。步驟順序是內容的一部分，因此**不**排序。
    """
    identity = canonical_json({
        "schema_version": PLAN_SCHEMA_VERSION,
        "mode": PLAN_MODE,
        "goal": normalize_text(goal),
        "catalog_snapshot_id": catalog_snapshot_id,
        "steps": [s.identity() for s in steps],
        "observations": [normalize_text(o) for o in observations],
        "unknowns": [normalize_text(u) for u in unknowns],
        "global_stop_conditions": [normalize_text(c) for c in global_stop_conditions],
    })
    return f"plan_{sha256_text(identity)[:32]}"


__all__ = [
    "MAX_ARGUMENT_BYTES",
    "MAX_GOAL_CHARS",
    "MAX_LIST_ITEMS",
    "MAX_STEPS",
    "MAX_TEXT_ITEM_CHARS",
    "PLAN_MODE",
    "PLAN_SCHEMA_VERSION",
    "Plan",
    "PlanStep",
    "PlanValidationResult",
    "STEP_ID_PATTERN",
    "UNSUPPORTED_BINDING_MARKERS",
    "UNSUPPORTED_STEP_KEYS",
    "canonical_json",
    "compute_plan_id",
    "normalize_text",
    "sha256_text",
]
