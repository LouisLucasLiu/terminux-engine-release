# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Proactive trigger lifecycle records 與 dedup。

沿用既有 EventStore，以三種 append-only record 表示：

    proactive_trigger_claimed
    proactive_trigger_completed
    proactive_trigger_failed

**已宣告的語意上限**：EventStore 對邏輯 key 沒有 uniqueness，也沒有
query+insert 的單一 transaction，因此本模組只能提供

    same-process single-attempt with durable replay suppression

——同一 daemon process 內由 per-trigger-id lock 序列化；**跨 process 的
concurrent worker 仍可能各自 claim 同一個 trigger**。不得宣稱
cross-process exactly-once 或 database-enforced uniqueness。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

LIFECYCLE_SEMANTICS_VERSION = 1
LIFECYCLE_SEMANTICS = "same-process single-attempt with durable replay suppression"

RECORD_CLAIMED = "proactive_trigger_claimed"
RECORD_COMPLETED = "proactive_trigger_completed"
RECORD_FAILED = "proactive_trigger_failed"
RECORD_TYPES: tuple[str, ...] = (RECORD_CLAIMED, RECORD_COMPLETED, RECORD_FAILED)

#: 看到這些既有紀錄就不再處理同一個 trigger。`claimed` 也算——claim 之後
#: crash 會留下 incomplete claim，本版**不自動重跑**。
SUPPRESSING_RECORD_TYPES: frozenset[str] = frozenset(RECORD_TYPES)

CLAIM_OK = "claimed"
CLAIM_SUPPRESSED = "suppressed"
CLAIM_WRITE_FAILED = "claim_write_failed"

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(trigger_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(trigger_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[trigger_id] = lock
        return lock


@dataclass(frozen=True)
class LifecycleRecord:
    record_type: str
    trigger_id: str
    policy_id: str
    policy_version: str
    observation_identity: dict[str, Any]
    subject_identity: dict[str, Any]
    baseline_identity: dict[str, Any] | None
    recorded_at: str | None
    lifecycle_semantics_version: int = LIFECYCLE_SEMANTICS_VERSION
    snapshot_id: str | None = None
    notification_summary_sha256: str | None = None
    failure_code: str | None = None
    #: 被觀察對象的 analysis-input 與 lifecycle 三軸。record_type
    #: 的 `completed` 指的是 **proactive cycle** 完成，不是被觀察的 run 完成；
    #: 這個欄位讓兩者不會被混淆。
    observed_lifecycle: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "trigger_id": self.trigger_id,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "observation_identity": dict(self.observation_identity),
            "subject_identity": dict(self.subject_identity),
            "baseline_identity": dict(self.baseline_identity)
            if self.baseline_identity else None,
            "recorded_at": self.recorded_at,
            "lifecycle_semantics_version": self.lifecycle_semantics_version,
            "lifecycle_semantics": LIFECYCLE_SEMANTICS,
            "snapshot_id": self.snapshot_id,
            "notification_summary_sha256": self.notification_summary_sha256,
            "failure_code": self.failure_code,
            "observed_lifecycle": dict(self.observed_lifecycle),
        }


@dataclass(frozen=True)
class ClaimResult:
    status: str
    existing_record_types: tuple[str, ...] = ()
    message: str | None = None

    @property
    def may_proceed(self) -> bool:
        return self.status == CLAIM_OK

    def to_json_dict(self) -> dict[str, Any]:
        return {"status": self.status,
                "existing_record_types": list(self.existing_record_types),
                "message": self.message,
                "lifecycle_semantics": LIFECYCLE_SEMANTICS}


def claim_trigger(
    *,
    trigger_id: str,
    read_history: Callable[[], Iterable[dict[str, Any]]],
    write_claim: Callable[[], bool],
) -> ClaimResult:
    """在 per-trigger-id lock 內重查歷史再 claim。

    `write_claim` 必須把 claimed record **durable** 寫入才回 True；
    只有回 True 才允許呼叫 assessment。
    """
    with _lock_for(trigger_id):
        existing = [r.get("record_type") for r in read_history()
                    if r.get("record_type") in SUPPRESSING_RECORD_TYPES]
        if existing:
            return ClaimResult(
                CLAIM_SUPPRESSED, tuple(sorted(set(existing))),
                f"trigger {trigger_id} already has durable lifecycle records; "
                f"this version never re-runs an existing trigger automatically")
        if not write_claim():
            return ClaimResult(
                CLAIM_WRITE_FAILED, (),
                "the claim record could not be persisted; nothing was analysed, "
                "written or notified")
        return ClaimResult(CLAIM_OK)


__all__ = [
    "CLAIM_OK",
    "CLAIM_SUPPRESSED",
    "CLAIM_WRITE_FAILED",
    "LIFECYCLE_SEMANTICS",
    "LIFECYCLE_SEMANTICS_VERSION",
    "RECORD_CLAIMED",
    "RECORD_COMPLETED",
    "RECORD_FAILED",
    "RECORD_TYPES",
    "SUPPRESSING_RECORD_TYPES",
    "ClaimResult",
    "LifecycleRecord",
    "claim_trigger",
]
