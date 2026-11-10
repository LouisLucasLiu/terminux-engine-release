# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""外部執行 receipt 的 durable projection（Common、vendor-neutral）。

vendor-neutral execution contract／artifact lineage 已經產生完整、客觀的 `ExecutionReceipt`，但它只被寫到 caller 指定
的檔案路徑，而 CLI 的 orchestration ledger 是 `--ledger-db` 拋棄式的。結果是
daemon 端**查不到**任何 Kernel-managed run —— `query_orchestration_evidence`
一律回 `unknown run`，Agent 因此無法對真實 run 提案。

這裡把既有 receipt 投影成既有 EventStore 上的 **append-only projection**：

* **不是**第二套 lifecycle ledger。orchestration ledger 仍然是 process
  lifecycle 的 authority；這裡只保存 immutable 的事實副本。
* 沒有 update、沒有 delete、沒有 scheduler、沒有新 store。
* 完全 domain-blind：不認識任何 EDA 工具、檔名或 stage 語意。欄位全部來自
  vendor-neutral execution contract／artifact lineage receipt 本身。

**誠實邊界**：既有 EventStore 的 logical key 不是 unique（只有生成的
`event_id` 是 PRIMARY KEY），所以這裡的 idempotency 是「查詢 ＋ content hash
比對」，**不是** DB 保證。跨 process 併發仍可能寫進兩筆內容相同的紀錄，
因此**不得**宣稱 cross-process exactly-once。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

PROJECTION_SCHEMA_VERSION = 1

#: EventStore 上的 record type。與 orchestration ledger 的表分開，避免被誤讀成
#: lifecycle state。
RECEIPT_RECORD_TYPE = "external_execution_receipt"

#: 這個 projection 的 concurrency 上限，會寫進每一筆紀錄，避免下游過度宣稱。
CONCURRENCY_SEMANTICS = "same-process conflict-checked append"

OUTCOME_APPENDED = "appended"
OUTCOME_IDEMPOTENT = "idempotent"
OUTCOME_CONFLICT = "conflict"
OUTCOME_UNREADABLE = "unreadable"
OUTCOME_WRITE_FAILED = "write_failed"

CONFLICT_ERROR_CODE = "EXECUTION_RECEIPT_CONTENT_HASH_MISMATCH"
UNREADABLE_ERROR_CODE = "EXECUTION_RECEIPT_RECORD_UNREADABLE"
WRITE_FAILED_ERROR_CODE = "EXECUTION_RECEIPT_PERSIST_FAILED"

#: 進 content hash 的欄位。刻意固定並排序，讓 hash 可重算。
#: `persistence_status` 與 `projected_at` 是投影行為，不是 receipt 內容。
_CONTENT_KEYS: tuple[str, ...] = (
    "schema_version", "run_id", "step_id", "attempt",
    "parent_attempt_or_retry_reference", "tool_id", "event_tool_name",
    "adapter_id", "adapter_version", "executor_identity", "tool_version_evidence",
    "started_at", "finished_at", "process_exit_code", "timed_out",
    "termination_reason", "execution_lifecycle_status", "dispatch_status",
    "evidence_status", "output_roots", "artifact_manifest_reference",
    "artifact_manifest_id", "artifact_manifest_sha256", "artifact_capture_status",
    "argv_digest", "cwd_digest",
)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_or_none(value: Any) -> str | None:
    """argv／cwd 只保存安全摘要，不保存原始命令列。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        return sha256_text(canonical_json([str(v) for v in value]))
    text = str(value)
    return sha256_text(text) if text else None


@dataclass(frozen=True)
class ProjectionResult:
    outcome: str
    content_hash: str | None = None
    error_code: str | None = None
    message: str | None = None
    existing_hashes: tuple[str, ...] = ()

    @property
    def durable(self) -> bool:
        """只有真的 append 或確認 idempotent 才算 durable。"""
        return self.outcome in (OUTCOME_APPENDED, OUTCOME_IDEMPOTENT)

    def to_json_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "durable": self.durable,
                "content_hash": self.content_hash, "error_code": self.error_code,
                "message": self.message,
                "existing_hashes": list(self.existing_hashes),
                "concurrency_semantics": CONCURRENCY_SEMANTICS}


def build_projection(
    receipt: Any, *, source_receipt_sha256: str | None = None,
    argv: Iterable[str] | None = None, cwd: str | None = None,
) -> dict[str, Any]:
    """從既有 vendor-neutral execution contract／artifact lineage receipt 組出可投影的 payload。

    `receipt` 只需要有 `to_json_dict()`，或本身就是 dict——這一層不 import
    orchestration，也不知道任何 domain 語意。
    """
    raw = receipt.to_json_dict() if hasattr(receipt, "to_json_dict") else dict(receipt)
    content = {k: raw.get(k) for k in _CONTENT_KEYS}
    content["argv_digest"] = _digest_or_none(argv)
    content["cwd_digest"] = _digest_or_none(cwd)
    content_hash = sha256_text(canonical_json(content))
    payload = dict(content)
    payload.update({
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "record_type": RECEIPT_RECORD_TYPE,
        "receipt_id": f"xrcpt_{content_hash[:32]}",
        "content_hash": content_hash,
        "source_receipt_sha256": source_receipt_sha256,
        "concurrency_semantics": CONCURRENCY_SEMANTICS,
        "authority_note": ("this is an immutable projection of an execution "
                           "receipt; the orchestration ledger remains the process "
                           "lifecycle authority"),
    })
    return payload


def content_hash_of(payload: dict[str, Any]) -> str | None:
    """從**內容**重算 hash。讀不懂就回 None——不猜、也不信任存起來的宣稱值。"""
    if not isinstance(payload, dict):
        return None
    content = {k: payload.get(k) for k in _CONTENT_KEYS}
    if payload.get("run_id") is None or payload.get("schema_version") is None:
        return None
    return sha256_text(canonical_json(content))


def project_receipt(
    payload: dict[str, Any], *,
    read_existing: Callable[[], Iterable[dict[str, Any]]],
    write: Callable[[dict[str, Any]], bool],
) -> ProjectionResult:
    """Append-only 投影：查既有 → 比 content hash → 才寫。

    同 id 同內容 ⇒ idempotent（0 write）；同 id 不同內容 ⇒ fail closed。
    """
    content_hash = payload.get("content_hash")
    try:
        existing = list(read_existing())
    except Exception as exc:  # noqa: BLE001 - 讀不到就不寫
        return ProjectionResult(OUTCOME_UNREADABLE, content_hash,
                                UNREADABLE_ERROR_CODE, f"{exc!r}")
    hashes: list[str] = []
    for row in existing:
        recomputed = content_hash_of(row)
        if recomputed is None:
            return ProjectionResult(
                OUTCOME_UNREADABLE, content_hash, UNREADABLE_ERROR_CODE,
                "an existing projection record could not be canonicalized")
        hashes.append(recomputed)
    if hashes:
        if content_hash in hashes:
            return ProjectionResult(OUTCOME_IDEMPOTENT, content_hash,
                                    existing_hashes=tuple(sorted(set(hashes))))
        return ProjectionResult(
            OUTCOME_CONFLICT, content_hash, CONFLICT_ERROR_CODE,
            "a projection with this identity already exists with different content",
            tuple(sorted(set(hashes))))
    try:
        written = bool(write(payload))
    except Exception as exc:  # noqa: BLE001
        return ProjectionResult(OUTCOME_WRITE_FAILED, content_hash,
                                WRITE_FAILED_ERROR_CODE, f"{exc!r}")
    if not written:
        return ProjectionResult(OUTCOME_WRITE_FAILED, content_hash,
                                WRITE_FAILED_ERROR_CODE,
                                "the projection record was not persisted")
    return ProjectionResult(OUTCOME_APPENDED, content_hash)


__all__ = [
    "CONCURRENCY_SEMANTICS",
    "CONFLICT_ERROR_CODE",
    "OUTCOME_APPENDED",
    "OUTCOME_CONFLICT",
    "OUTCOME_IDEMPOTENT",
    "OUTCOME_UNREADABLE",
    "OUTCOME_WRITE_FAILED",
    "PROJECTION_SCHEMA_VERSION",
    "ProjectionResult",
    "RECEIPT_RECORD_TYPE",
    "UNREADABLE_ERROR_CODE",
    "WRITE_FAILED_ERROR_CODE",
    "build_projection",
    "canonical_json",
    "content_hash_of",
    "project_receipt",
    "sha256_text",
]
