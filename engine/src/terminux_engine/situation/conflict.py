# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Immutable snapshot identity conflict handling（Workstream A）。

Vendor-neutral：這裡只認識 snapshot id 與 canonical content hash，不知道任何
metric、工具或 domain 語意。

**誠實的保證上限**：既有 EventStore 對 record 的邏輯 key **沒有** uniqueness
（`session_events` 只有生成的 `event_id` 是 PRIMARY KEY），也沒有可用的
「query + insert 同一 transaction」原語。因此本模組提供的是

    same-process conflict-checked append

——同一個 daemon process 內，query 與 commit 由 per-key lock 序列化，不會互相
穿越；**跨 process 的 concurrent writer 仍可能寫出同 id 不同內容的兩筆紀錄**。
這一點必須照實陳述，不得說成 globally unique、exactly-once 或
database-enforced uniqueness。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

CONFLICT_ERROR_CODE = "SITUATION_SNAPSHOT_HASH_MISMATCH"
INTEGRITY_ERROR_CODE = "SITUATION_SNAPSHOT_RECORD_UNREADABLE"

#: 這個字串會出現在 report 與 evidence 裡，避免各處自行改寫成更強的說法。
CONCURRENCY_SEMANTICS = "same-process conflict-checked append"

OUTCOME_NEW = "append"
OUTCOME_IDEMPOTENT = "idempotent"
OUTCOME_CONFLICT = "conflict"
OUTCOME_UNREADABLE = "unreadable"

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(snapshot_id: str) -> threading.Lock:
    """Per-snapshot-id lock。只序列化同一個 id，不阻塞無關的寫入。"""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(snapshot_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[snapshot_id] = lock
        return lock


@dataclass(frozen=True)
class ConflictDecision:
    outcome: str
    error_code: str | None = None
    message: str | None = None
    existing_hashes: tuple[str, ...] = ()

    @property
    def may_write(self) -> bool:
        return self.outcome == OUTCOME_NEW

    @property
    def ok(self) -> bool:
        return self.outcome in (OUTCOME_NEW, OUTCOME_IDEMPOTENT)

    def to_json_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "error_code": self.error_code,
                "message": self.message, "existing_hashes": list(self.existing_hashes),
                "concurrency_semantics": CONCURRENCY_SEMANTICS}


def decide(
    *,
    snapshot_id: str,
    content_hash: str,
    existing_records: Iterable[dict[str, Any]],
    hash_of: Callable[[dict[str, Any]], str | None],
) -> ConflictDecision:
    """比對既有紀錄。**不看最後一筆**——同 id 的任何一筆 hash 不同即衝突。

    `hash_of` 由呼叫端提供：它負責從既有 record payload 取出（或重算）
    canonical content hash。回 `None` 代表那筆紀錄無法 canonicalize——
    此時一律拒絕，**不猜測**。
    """
    hashes: list[str] = []
    for record in existing_records:
        actual = hash_of(record)
        if actual is None:
            return ConflictDecision(
                OUTCOME_UNREADABLE, INTEGRITY_ERROR_CODE,
                f"an existing snapshot record for {snapshot_id} could not be "
                f"canonicalized; refusing to write rather than guess",
                tuple(hashes))
        hashes.append(actual)

    if not hashes:
        return ConflictDecision(OUTCOME_NEW)
    mismatched = sorted({h for h in hashes if h != content_hash})
    if mismatched:
        return ConflictDecision(
            OUTCOME_CONFLICT, CONFLICT_ERROR_CODE,
            f"snapshot {snapshot_id} already has durable record(s) with a different "
            f"canonical content hash (existing {mismatched}, new {content_hash})",
            tuple(hashes))
    return ConflictDecision(OUTCOME_IDEMPOTENT, existing_hashes=tuple(hashes))


def guarded_append(
    *,
    snapshot_id: str,
    content_hash: str,
    read_existing: Callable[[], Iterable[dict[str, Any]]],
    hash_of: Callable[[dict[str, Any]], str | None],
    write: Callable[[], bool],
) -> ConflictDecision:
    """在 per-id lock 內完成 query → decide → write。

    保證上限是 `same-process conflict-checked append`：同 process 內的
    concurrent caller 不會互相穿越；跨 process 仍可能重複。
    """
    with _lock_for(snapshot_id):
        decision = decide(snapshot_id=snapshot_id, content_hash=content_hash,
                          existing_records=read_existing(), hash_of=hash_of)
        if not decision.may_write:
            return decision
        if not write():
            return ConflictDecision(
                OUTCOME_UNREADABLE, INTEGRITY_ERROR_CODE,
                f"snapshot {snapshot_id} could not be persisted")
        return decision


__all__ = [
    "CONCURRENCY_SEMANTICS",
    "CONFLICT_ERROR_CODE",
    "INTEGRITY_ERROR_CODE",
    "OUTCOME_CONFLICT",
    "OUTCOME_IDEMPOTENT",
    "OUTCOME_NEW",
    "OUTCOME_UNREADABLE",
    "ConflictDecision",
    "decide",
    "guarded_append",
]
