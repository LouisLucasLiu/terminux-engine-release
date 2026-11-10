# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Immutable `PlanSnapshot v1`（approved package action、G-3）。

Snapshot 是 approved package action 的 plan 權威來源：proposal 只能從**已 durable 保存的**
snapshot 取 step，不能從記憶體 draft 直接取。

Identity 規則：

- `plan_sha256` 由 canonical plan bytes 決定；
- 同 plan id ＋ 同 bytes ⇒ idempotent（可重複引用同一筆）；
- 同 plan id ＋ 不同 bytes ⇒ **fail closed**（`PLAN_SNAPSHOT_HASH_MISMATCH`）；
- append-only，不更新。

EventStore 的 record key **不是** DB unique 欄位（`session_events` 只有
`event_id` 是 PRIMARY KEY，而那是每筆自動產生的 UUID）。因此衝突偵測必須
靠「以既有 query 讀回同 plan id 的 snapshot 再比 content hash」，
**不得**宣稱具備 database uniqueness。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_RECORD_TYPE = "agent_plan_snapshot"
CANONICALIZATION_VERSION = 1

#: Snapshot 只保留 plan 的內容欄位。`validation.errors`、`created_from`、
#: renderer 產物與任何 provider transcript 都不進來。
_PLAN_CONTENT_FIELDS = (
    "schema_version", "mode", "goal", "catalog_snapshot_id",
    "observations", "unknowns", "steps", "global_stop_conditions", "limitations",
)


def canonical_plan_bytes(plan: dict[str, Any]) -> bytes:
    """Canonical serialization：排序 key、無多餘空白、UTF-8。

    只取 `_PLAN_CONTENT_FIELDS`——`plan_id` 本身、`validation` 結果與
    `execution_allowed` 都不參與，因為它們由內容決定或是常數。"""
    content = {k: plan.get(k) for k in _PLAN_CONTENT_FIELDS if k in plan}
    return json.dumps(content, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def compute_plan_sha256(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


@dataclass(frozen=True)
class PlanSnapshot:
    schema_version: int
    record_type: str
    plan_id: str
    plan_sha256: str
    canonicalization_version: int
    mode: str
    goal: str
    catalog_snapshot_id: str
    steps: tuple[dict[str, Any], ...]
    unknowns: tuple[str, ...]
    global_stop_conditions: tuple[str, ...]
    created_at: str
    created_by: dict[str, str]
    source_request_id: str | None
    planner_version: str
    validation: dict[str, Any]
    availability_state: str = "validation_deferred"

    #: 常數。Snapshot 保存的是 plan-only 的內容；approved package action 的執行授權存在
    #: proposal，不回寫 plan。
    execution_allowed: bool = False

    def step(self, step_id: str) -> dict[str, Any] | None:
        for s in self.steps:
            if s.get("step_id") == step_id:
                return dict(s)
        return None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "plan_id": self.plan_id,
            "plan_sha256": self.plan_sha256,
            "canonicalization_version": self.canonicalization_version,
            "mode": self.mode,
            "goal": self.goal,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "steps": [dict(s) for s in self.steps],
            "unknowns": list(self.unknowns),
            "global_stop_conditions": list(self.global_stop_conditions),
            "execution_allowed": False,
            "created_at": self.created_at,
            "created_by": dict(self.created_by),
            "source_request_id": self.source_request_id,
            "planner_version": self.planner_version,
            "validation": dict(self.validation),
            "availability_state": self.availability_state,
        }


def build_plan_snapshot(
    plan: dict[str, Any],
    *,
    created_at: str,
    member_id: str,
    project_id: str,
    session_id: str,
    planner_version: str,
    validator_version: str,
    source_request_id: str | None = None,
) -> PlanSnapshot:
    """從一份 **已驗證通過** 的 plan 建立 immutable snapshot。

    呼叫端必須先確認 `plan["validation"]["status"] == "valid"`；本函式不
    重新驗證 plan 的內容（那是 plan-only planner validator 的職責），只負責 canonical
    化與 identity。
    """
    return PlanSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        record_type=SNAPSHOT_RECORD_TYPE,
        plan_id=str(plan.get("plan_id")),
        plan_sha256=compute_plan_sha256(plan),
        canonicalization_version=CANONICALIZATION_VERSION,
        mode=str(plan.get("mode")),
        goal=str(plan.get("goal")),
        catalog_snapshot_id=str(plan.get("catalog_snapshot_id")),
        steps=tuple(dict(s) for s in (plan.get("steps") or [])),
        unknowns=tuple(plan.get("unknowns") or []),
        global_stop_conditions=tuple(plan.get("global_stop_conditions") or []),
        created_at=created_at,
        created_by={"member_id": member_id, "project_id": project_id,
                    "session_id": session_id},
        source_request_id=source_request_id,
        planner_version=planner_version,
        validation={"status": "valid", "validator_version": validator_version},
    )


class SnapshotConflictError(RuntimeError):
    """同 plan id 已有 durable snapshot，但 content hash 不同——fail closed。"""

    code = "PLAN_SNAPSHOT_HASH_MISMATCH"


def reconcile_existing(
    snapshot: PlanSnapshot, existing: list[dict[str, Any]]
) -> tuple[bool, dict[str, Any] | None]:
    """比對既有 durable snapshot。

    回傳 `(already_persisted, existing_payload)`：

    - 沒有同 plan id 的既有紀錄 → `(False, None)`，呼叫端應寫入；
    - 有且 hash 相同 → `(True, payload)`，idempotent 引用，不重複寫；
    - 有但 hash 不同 → 拋 `SnapshotConflictError`。
    """
    for payload in existing:
        if payload.get("plan_id") != snapshot.plan_id:
            continue
        if payload.get("plan_sha256") == snapshot.plan_sha256:
            return True, payload
        raise SnapshotConflictError(
            f"plan {snapshot.plan_id} already has a durable snapshot with a different "
            f"content hash (existing {payload.get('plan_sha256')!r}, "
            f"new {snapshot.plan_sha256!r})"
        )
    return False, None


__all__ = [
    "CANONICALIZATION_VERSION",
    "SNAPSHOT_RECORD_TYPE",
    "SNAPSHOT_SCHEMA_VERSION",
    "PlanSnapshot",
    "SnapshotConflictError",
    "build_plan_snapshot",
    "canonical_plan_bytes",
    "compute_plan_sha256",
    "reconcile_existing",
]
