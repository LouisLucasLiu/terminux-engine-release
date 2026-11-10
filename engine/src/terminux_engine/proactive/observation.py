# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Proactive observation 與 trigger identity。

Common 只認識**結構性**欄位：誰產生的、哪一個 run/step/attempt、artifact
manifest identity、analysis-input 是否具備、lifecycle 是否被觀察到，以及一包
不解讀的 `opaque_refs`。它**不知道** readiness、DQE、WNS／TNS、timing、
utilization 或任何 report 語意。

proactive intelligence 的核心修正：**analysis-ready 不等於 execution-complete。**
兩者是彼此獨立的座標軸：

* `analysis_input_status` —— situation assessment 需要的分析輸入是否備妥（可由 durable store
  直接證明）。
* `execution_completion_status` / `dispatch_status` / `barrier_evidence_status`
  —— 執行、dispatch、Barrier 是否**被觀察到**完成。沒有 durable receipt 時
  一律是 `unverified` / `not_observed`，**不是**由 analysis input 推得。

Provenance 只能放在 `*_basis` 欄位，不得寫進 status。

Trigger identity 完全 deterministic：不含 wall-clock、random UUID、LLM 輸出或
notification 文字。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: 欄位語意由「lifecycle observation」改為「analysis-input ＋ 獨立
#: lifecycle 軸」，屬於 semantic change，故 bump。
OBSERVATION_SCHEMA_VERSION = 2
TRIGGER_ID_SCHEMA_VERSION = 2

DECISION_TRIGGER = "trigger"
DECISION_SUPPRESS = "suppress"
DECISION_INSUFFICIENT = "insufficient_evidence"
DECISIONS: tuple[str, ...] = (DECISION_TRIGGER, DECISION_SUPPRESS, DECISION_INSUFFICIENT)

#: Observation source 的正式名稱。刻意**不**叫
#: `execution_completion_event`——那樣的 durable authority 並不存在。
OBSERVATION_SOURCE = "package_owned_durable_discovery"

# ── analysis-input 軸（可由 durable store 直接證明）────────────────────
ANALYSIS_INPUT_READY = "ready"
ANALYSIS_INPUT_INCOMPLETE = "incomplete"
ANALYSIS_INPUT_UNVERIFIABLE = "unverifiable"
ANALYSIS_INPUT_STATUSES: tuple[str, ...] = (
    ANALYSIS_INPUT_READY, ANALYSIS_INPUT_INCOMPLETE, ANALYSIS_INPUT_UNVERIFIABLE)

# ── lifecycle 軸（只有 durable receipt 才能升級）──────────────────────
EXECUTION_VERIFIED = "verified"
EXECUTION_UNVERIFIED = "unverified"
EXECUTION_NOT_OBSERVED = "not_observed"
EXECUTION_COMPLETION_STATUSES: tuple[str, ...] = (
    EXECUTION_VERIFIED, EXECUTION_UNVERIFIED, EXECUTION_NOT_OBSERVED)

DISPATCH_COMPLETED = "completed"
DISPATCH_FAILED = "failed"
DISPATCH_NOT_OBSERVED = "not_observed"
DISPATCH_STATUSES: tuple[str, ...] = (
    DISPATCH_COMPLETED, DISPATCH_FAILED, DISPATCH_NOT_OBSERVED)

BARRIER_READY = "ready"
BARRIER_NOT_READY = "not_ready"
BARRIER_NOT_OBSERVED = "not_observed"
BARRIER_STATUSES: tuple[str, ...] = (
    BARRIER_READY, BARRIER_NOT_READY, BARRIER_NOT_OBSERVED)

RECEIPT_MISSING = "missing"
RECEIPT_UNAVAILABLE = "unavailable"
RECEIPT_VERIFIED = "verified"
RECEIPT_STATUSES: tuple[str, ...] = (
    RECEIPT_MISSING, RECEIPT_UNAVAILABLE, RECEIPT_VERIFIED)

#: G-1 未解時 production 唯一合法的 lifecycle evidence basis。
NO_DURABLE_RECEIPTS: dict[str, str] = {
    "execution_receipt": RECEIPT_UNAVAILABLE,
    "dispatch_receipt": RECEIPT_UNAVAILABLE,
    "barrier_receipt": RECEIPT_UNAVAILABLE,
}

#: optional lower bound：source audit 已證明 artifact manifest 只在
#: subprocess return **之後**建立（`orchestration/kernel.py`：
#: `subprocess_executor.execute()` → `discover_artifacts()` →
#: `_capture_artifacts()`）。它只代表「已到達 artifact-capture boundary」，
#: **不**代表 exit code 為 0、dispatch completed、Barrier passed 或存在
#: durable ExecutionReceipt，且不得成為 trigger 的必要依賴。
PRODUCER_EXECUTION_OBSERVATION_POST_RETURN = "post_process_return_artifact_capture"


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AnalysisInputBasis:
    """analysis input 各來源的可驗證狀態。

    刻意用 **泛用** 命名：`evaluation_kind` 由 Package 提供（ORFS 給的是
    `dqe_evaluation`），Common 只知道「有一份 manifest ＋ 一份 evaluation，
    各自有 status、id 與 row count」。這是設計要求——ORFS 的
    YAML 只是 serialization 實例，不是 Common 的欄位名。
    """

    artifact_manifest_status: str
    evaluation_status: str
    evaluation_kind: str = "evaluation"
    manifest_id: str | None = None
    manifest_sha256: str | None = None
    evaluation_id: str | None = None
    row_count: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "artifact_manifest": {
                "status": self.artifact_manifest_status,
                "manifest_id": self.manifest_id,
                "manifest_sha256": self.manifest_sha256,
            },
            self.evaluation_kind: {
                "status": self.evaluation_status,
                "evaluation_id": self.evaluation_id,
                "row_count": self.row_count,
            },
        }


@dataclass(frozen=True)
class ProactiveObservation:
    """一筆 **analysis-input** 觀察。

    它斷言的是「situation assessment 分析所需的輸入已持久化且可驗證」，**不是**「執行已完成」。
    lifecycle 三軸各自獨立，且在沒有 durable receipt 時只能是
    `unverified`／`not_observed`。

    `analysis_input_provenance` 說明 analysis input 是怎麼證明的；它**不得**
    被寫進任何 status 欄位。
    """

    schema_version: int
    observation_id: str
    observed_at: str | None
    observation_source: str
    source_kind: str
    producer_run_id: str
    producer_step_id: str | None
    producer_attempt: int | None
    artifact_manifest_id: str | None
    artifact_manifest_sha256: str | None
    analysis_input_status: str
    analysis_input_basis: AnalysisInputBasis
    analysis_input_provenance: tuple[str, ...] = ()
    execution_completion_status: str = EXECUTION_UNVERIFIED
    dispatch_status: str = DISPATCH_NOT_OBSERVED
    barrier_evidence_status: str = BARRIER_NOT_OBSERVED
    lifecycle_evidence_basis: dict[str, str] = field(
        default_factory=lambda: dict(NO_DURABLE_RECEIPTS))
    producer_execution_observation: str | None = None
    opaque_refs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Status 欄位只能取列舉值——這是擋住「把 provenance 當 status」的
        # 最後一道結構性防線。
        for value, allowed, name in (
            (self.analysis_input_status, ANALYSIS_INPUT_STATUSES, "analysis_input_status"),
            (self.execution_completion_status, EXECUTION_COMPLETION_STATUSES,
             "execution_completion_status"),
            (self.dispatch_status, DISPATCH_STATUSES, "dispatch_status"),
            (self.barrier_evidence_status, BARRIER_STATUSES, "barrier_evidence_status"),
        ):
            if value not in allowed:
                raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
        for key, value in self.lifecycle_evidence_basis.items():
            if value not in RECEIPT_STATUSES:
                raise ValueError(
                    f"lifecycle_evidence_basis[{key}] must be one of "
                    f"{RECEIPT_STATUSES}, got {value!r}")

    @property
    def analysis_input_ready(self) -> bool:
        return self.analysis_input_status == ANALYSIS_INPUT_READY

    @property
    def lifecycle_completion_observed(self) -> bool:
        """是否真的看到過 lifecycle completion。沒有 receipt 就是 False。"""
        return (self.execution_completion_status == EXECUTION_VERIFIED
                and self.dispatch_status == DISPATCH_COMPLETED
                and self.barrier_evidence_status == BARRIER_READY)

    def identity(self) -> dict[str, Any]:
        """進 trigger id 的部分。`observed_at` 與 `opaque_refs` 不參與——
        前者是 wall-clock，後者是 Package 私有內容。"""
        return {
            "schema_version": self.schema_version,
            "observation_source": self.observation_source,
            "source_kind": self.source_kind,
            "producer_run_id": self.producer_run_id,
            "producer_step_id": self.producer_step_id,
            "producer_attempt": self.producer_attempt,
            "artifact_manifest_id": self.artifact_manifest_id,
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
        }

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "observed_at": self.observed_at,
            "observation_source": self.observation_source,
            "source_kind": self.source_kind,
            "producer_run_id": self.producer_run_id,
            "producer_step_id": self.producer_step_id,
            "producer_attempt": self.producer_attempt,
            "artifact_manifest_id": self.artifact_manifest_id,
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "analysis_input_status": self.analysis_input_status,
            "analysis_input_basis": self.analysis_input_basis.to_json_dict(),
            "analysis_input_provenance": list(self.analysis_input_provenance),
            "execution_completion_status": self.execution_completion_status,
            "dispatch_status": self.dispatch_status,
            "barrier_evidence_status": self.barrier_evidence_status,
            "lifecycle_evidence_basis": dict(self.lifecycle_evidence_basis),
            "producer_execution_observation": self.producer_execution_observation,
            "opaque_refs": dict(self.opaque_refs),
        }


@dataclass(frozen=True)
class TriggerDecision:
    """Package provider 的決定。Common 只搬運，不重新判斷。"""

    decision: str
    policy_id: str
    policy_version: str
    subject_identity: dict[str, Any] = field(default_factory=dict)
    baseline_identity: dict[str, Any] | None = None
    reason_codes: tuple[str, ...] = ()
    missing_evidence: tuple[dict[str, Any], ...] = ()

    @property
    def should_trigger(self) -> bool:
        return self.decision == DECISION_TRIGGER

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "subject_identity": dict(self.subject_identity),
            "baseline_identity": dict(self.baseline_identity)
            if self.baseline_identity else None,
            "reason_codes": list(self.reason_codes),
            "missing_evidence": [dict(m) for m in self.missing_evidence],
        }


def compute_trigger_id(
    observation: ProactiveObservation, decision: TriggerDecision
) -> str:
    """相同 observation identity ＋ 相同 policy ＋ 相同 subject ⇒ 相同 id。

    Policy version 改變會產生新 id；是否因此重新分析是未來的設計
    decision，本版不自動處理。
    """
    identity = canonical_json({
        "trigger_id_schema_version": TRIGGER_ID_SCHEMA_VERSION,
        "observation": observation.identity(),
        "policy_id": decision.policy_id,
        "policy_version": decision.policy_version,
        "subject_identity": dict(decision.subject_identity),
    })
    return f"trg_{sha256_text(identity)[:32]}"


__all__ = [
    "ANALYSIS_INPUT_INCOMPLETE",
    "ANALYSIS_INPUT_READY",
    "ANALYSIS_INPUT_STATUSES",
    "ANALYSIS_INPUT_UNVERIFIABLE",
    "BARRIER_NOT_OBSERVED",
    "BARRIER_NOT_READY",
    "BARRIER_READY",
    "BARRIER_STATUSES",
    "DECISIONS",
    "DECISION_INSUFFICIENT",
    "DECISION_SUPPRESS",
    "DECISION_TRIGGER",
    "DISPATCH_COMPLETED",
    "DISPATCH_FAILED",
    "DISPATCH_NOT_OBSERVED",
    "DISPATCH_STATUSES",
    "EXECUTION_COMPLETION_STATUSES",
    "EXECUTION_NOT_OBSERVED",
    "EXECUTION_UNVERIFIED",
    "EXECUTION_VERIFIED",
    "NO_DURABLE_RECEIPTS",
    "OBSERVATION_SCHEMA_VERSION",
    "OBSERVATION_SOURCE",
    "PRODUCER_EXECUTION_OBSERVATION_POST_RETURN",
    "RECEIPT_MISSING",
    "RECEIPT_STATUSES",
    "RECEIPT_UNAVAILABLE",
    "RECEIPT_VERIFIED",
    "TRIGGER_ID_SCHEMA_VERSION",
    "AnalysisInputBasis",
    "ProactiveObservation",
    "TriggerDecision",
    "canonical_json",
    "compute_trigger_id",
    "sha256_text",
]
