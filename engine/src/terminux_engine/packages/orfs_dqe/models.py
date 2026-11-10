# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`orfs_dqe` 的資料模型。

只描述 DQE 結果的形狀；不含任何 parsing 或 policy 邏輯。

三個 dict 刻意分開，且每次都 `dict(...)` 複製，不共享 mutable object
（orfs_dqe「Facts、evidence、record payload 使用不同 dict」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DQE_SCHEMA_VERSION = 1
DQE_RECORD_TYPE = "orfs_dqe_result"
DQE_NAMESPACE = "orfs_dqe"

CheckStatus = Literal["pass", "warn", "fail", "unknown"]

STATUS_PASS: CheckStatus = "pass"
STATUS_WARN: CheckStatus = "warn"
STATUS_FAIL: CheckStatus = "fail"
STATUS_UNKNOWN: CheckStatus = "unknown"


@dataclass(frozen=True)
class CheckEvidence:
    """一個 check 的可重現證據位置。

    永遠指向 manifest 中的 artifact instance 與其 content SHA；`section`／
    `line_start`／`line_end` 讓人可以回到原檔對照，`excerpt_hash` 讓
    EventStore 不必保存整段文字也能證明當時讀到的是什麼。
    """

    artifact_instance_id: str | None = None
    adapter_role: str | None = None
    content_sha256: str | None = None
    section: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    excerpt_hash: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "artifact_instance_id": self.artifact_instance_id,
            "adapter_role": self.adapter_role,
            "content_sha256": self.content_sha256,
            "section": self.section,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "excerpt_hash": self.excerpt_hash,
        }


@dataclass(frozen=True)
class CheckResult:
    """單一 DQE check 的結果。

    `observed_value` 缺席時為 `None` ＋ `status="unknown"`——**不得填 0**
。數值保持 number type，單位另存 `unit`。
    """

    check_id: str
    category: str
    status: CheckStatus
    mandatory: bool
    observed_value: float | int | None = None
    unit: str | None = None
    comparator: str | None = None
    threshold: float | int | None = None
    message: str = ""
    evidence: CheckEvidence = field(default_factory=CheckEvidence)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "status": self.status,
            "mandatory": self.mandatory,
            "observed_value": self.observed_value,
            "unit": self.unit,
            "comparator": self.comparator,
            "threshold": self.threshold,
            "message": self.message,
            "evidence": self.evidence.to_json_dict(),
        }


@dataclass(frozen=True)
class DqeEvaluation:
    """一次完整的 `orfs_dqe` 評估結果。"""

    evaluation_id: str
    package_id: str
    package_version: str
    policy_id: str
    policy_version: str
    producer: dict[str, Any]
    artifact_manifest: dict[str, Any]
    overall_status: CheckStatus
    checks: list[CheckResult]
    unknowns: list[str]
    evaluated_at: str

    @property
    def counts(self) -> dict[str, int]:
        counts = {"pass": 0, "warn": 0, "fail": 0, "unknown": 0}
        for check in self.checks:
            counts[check.status] += 1
        return counts

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DQE_SCHEMA_VERSION,
            "record_type": DQE_RECORD_TYPE,
            "evaluation_id": self.evaluation_id,
            "package": {"id": self.package_id, "version": self.package_version},
            "policy": {"id": self.policy_id, "version": self.policy_version},
            "producer": dict(self.producer),
            "artifact_manifest": dict(self.artifact_manifest),
            "overall_status": self.overall_status,
            "counts": self.counts,
            "checks": [check.to_json_dict() for check in self.checks],
            "unknowns": list(self.unknowns),
            "evaluated_at": self.evaluated_at,
        }


__all__ = [
    "CheckEvidence",
    "CheckResult",
    "CheckStatus",
    "DQE_NAMESPACE",
    "DQE_RECORD_TYPE",
    "DQE_SCHEMA_VERSION",
    "DqeEvaluation",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_UNKNOWN",
    "STATUS_WARN",
]
