# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`PackageActionExecutionReceipt v1`。

Store ownership（source audit 結論）：

- **Orchestration Ledger** 是 *外部 tool／process* lifecycle 的 authority。
  approved package action 沒有 tool process，因此**不**寫 Ledger，也不冒充 Kernel receipt。
- **EventStore** 是既有的 append-only session record store，approved package action 的 plan
  snapshot 與 execution receipt 都寫在這裡。
- **不新增第三套 ledger。**

誠實揭露：`session_events` 只有 `event_id` 是 PRIMARY KEY，而那是每筆自動
產生的 UUID。record 的邏輯 key 在 payload 裡，**沒有** DB uniqueness。因此
「同一個 proposal 最多一筆 terminal receipt」是由 ProposalRegistry 的
one-shot claim 保證的，不是資料庫保證的。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

RECEIPT_SCHEMA_VERSION = 1
RECEIPT_RECORD_TYPE = "package_action_execution_receipt"

DISPATCH_COMPLETED = "completed"
DISPATCH_FAILED = "failed"
#: outer timeout 到期，但 action 可能仍在背景執行——**不是** `failed`。
#: `failed` 會讓讀者以為沒有副作用；這裡明確表示「結果未知」。
DISPATCH_OUTCOME_UNCONFIRMED = "outcome_unconfirmed"

PERSISTENCE_DURABLE = "durable"
PERSISTENCE_FAILED = "failed"
PERSISTENCE_UNKNOWN = "unknown"

#: Action result 超過這個大小就只存 digest ＋ reference，不存全文。
MAX_INLINE_RESULT_BYTES = 4096


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_result_digest(result: Any) -> tuple[str, int]:
    encoded = _canonical(result)
    return _sha256(encoded), len(encoded.encode("utf-8"))


@dataclass(frozen=True)
class ExecutionReceipt:
    schema_version: int
    record_type: str
    receipt_id: str
    execution_instance_id: str
    plan_id: str
    plan_sha256: str
    step_id: str
    proposal_id: str
    approval_binding_sha256: str
    capability_id: str
    arguments_sha256: str
    catalog_snapshot_id_at_plan: str
    catalog_snapshot_id_at_execution: str
    capability_contract_sha256: str
    risk: str
    member_id: str
    project_id: str
    session_id: str
    claimed_at: float | None
    started_at: float | None
    finished_at: float | None
    dispatch_status: str
    action_result_status: str | None
    action_result_digest: str | None
    action_result_reference: str | None
    dqe_status: str
    dqe_references: tuple[str, ...] = ()
    record_references: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    receipt_persistence_status: str = PERSISTENCE_UNKNOWN
    error: dict[str, Any] | None = None

    def content(self) -> dict[str, Any]:
        """進 `receipt_sha256` 的內容——不含 persistence status 本身
        （那是寫入結果，不是收據內容）。"""
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "receipt_id": self.receipt_id,
            "execution_instance_id": self.execution_instance_id,
            "plan_id": self.plan_id, "plan_sha256": self.plan_sha256,
            "step_id": self.step_id, "proposal_id": self.proposal_id,
            "approval_binding_sha256": self.approval_binding_sha256,
            "capability_id": self.capability_id,
            "arguments_sha256": self.arguments_sha256,
            "catalog_snapshot_id_at_plan": self.catalog_snapshot_id_at_plan,
            "catalog_snapshot_id_at_execution": self.catalog_snapshot_id_at_execution,
            "capability_contract_sha256": self.capability_contract_sha256,
            "risk": self.risk,
            "member_id": self.member_id, "project_id": self.project_id,
            "session_id": self.session_id,
            "claimed_at": self.claimed_at, "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dispatch_status": self.dispatch_status,
            "action_result_status": self.action_result_status,
            "action_result_digest": self.action_result_digest,
            "action_result_reference": self.action_result_reference,
            "dqe_status": self.dqe_status,
            "dqe_references": list(self.dqe_references),
            "record_references": list(self.record_references),
            "evidence_references": list(self.evidence_references),
            "error": self.error,
        }

    def to_json_dict(self) -> dict[str, Any]:
        payload = self.content()
        payload["receipt_sha256"] = compute_receipt_sha256(self)
        payload["receipt_persistence_status"] = self.receipt_persistence_status
        return payload

    def with_persistence(self, status: str) -> "ExecutionReceipt":
        from dataclasses import replace

        return replace(self, receipt_persistence_status=status)


def compute_receipt_sha256(receipt: ExecutionReceipt) -> str:
    return _sha256(_canonical(receipt.content()))


__all__ = [
    "DISPATCH_COMPLETED",
    "DISPATCH_FAILED",
    "DISPATCH_OUTCOME_UNCONFIRMED",
    "ExecutionReceipt",
    "MAX_INLINE_RESULT_BYTES",
    "PERSISTENCE_DURABLE",
    "PERSISTENCE_FAILED",
    "PERSISTENCE_UNKNOWN",
    "RECEIPT_RECORD_TYPE",
    "RECEIPT_SCHEMA_VERSION",
    "compute_receipt_sha256",
    "compute_result_digest",
]
