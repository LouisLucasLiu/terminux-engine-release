# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""以 **additive invalidation record** 標記失效證據，並讓正式讀取端預設排除。

為什麼不刪除
------------

EventStore、DQE `Version_Dqe_Tab` 與 artifact manifest store 都是 append-only
稽核紀錄。實體刪除會破壞「已寫入的事實不會消失」這個前提，也讓 forensic 無法
再回答「當時到底寫了什麼」。所以這裡走**加寫**：另存一筆 invalidation record，
精確指名被作廢的 event id、DQE row identity 與內容 hash。

三條不可放寬的邊界
------------------

1. **原始資料仍在**。invalidation record 不改寫、不刪除任何既有 row/檔案；
   forensic 查詢（直接 SQL、bundle 的 raw dump）仍看得到原值。
2. **排除必須是程式行為**。正式 reader、latest-run selection 與 DQE evidence
   一律預設排除；文件註記不算排除。
3. **authority 只有一個**：既有 EventStore 上的 `evidence_invalidation` event。
   不另立第二個 store，避免兩份清單漂移。路徑解析與其他元件同一條規則
   （`TERMINUX_EVENT_STORE_PATH`，否則 `~/.terminux/ecc/event_store.db`），
   因此隔離 harness 自然看到空集合。

「讀不到」的兩種情況要分開
--------------------------

* **檔案不存在** ⇒ 空集合。一份不存在的 append-only log 誠實地表示
  「零筆作廢」，這不是降級。
* **檔案存在但讀不到／內容不合法** ⇒ 拋 `InvalidationRecordError`。這時我們
  無法知道該排除什麼，繼續讀就是宣稱未經驗證的事情，所以 fail closed。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INVALIDATION_SCHEMA_VERSION = 1

#: EventStore 上的 record type。與任何 lifecycle 表分開，不是 state。
INVALIDATION_RECORD_TYPE = "evidence_invalidation"

#: 只讀 daemon／process 啟動環境，與 EventStore 本身同一個變數。
EVENT_STORE_ENV = "TERMINUX_EVENT_STORE_PATH"
DEFAULT_EVENT_STORE = "~/.terminux/ecc/event_store.db"

_TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "record_type", "invalidation_id", "reason_code",
    "declared_at", "authorized_by", "authorization_reference", "statement",
    "targets", "record_digest",
})
_TARGET_SECTIONS = frozenset({
    "orchestration_run_ids", "event_store_events", "dqe_rows",
    "artifact_manifests",
})

#: 進 digest 的欄位順序固定，讓 record 可重算。
_DIGEST_FIELDS: tuple[str, ...] = (
    "schema_version", "record_type", "invalidation_id", "reason_code",
    "declared_at", "authorized_by", "authorization_reference", "statement",
    "targets",
)


class InvalidationRecordError(RuntimeError):
    """invalidation record 不合法，或 authority 存在卻讀不到。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dqe_row_identity(*, db_identity: str, row_id: int) -> str:
    """DQE row 的 canonical identity：`<db 身分>#<Version_Dqe_Tab.id>`。

    `db_identity` 是 DB 檔案的 realpath——同一個 row id 在不同 DQE DB 是不同
    的 row，不可混用。
    """
    return f"{db_identity}#{int(row_id)}"


# ── record ──────────────────────────────────────────────────────────


def build_invalidation_record(
    *, invalidation_id: str, reason_code: str, declared_at: float,
    authorized_by: str, authorization_reference: str, statement: str,
    orchestration_run_ids: list[str] | tuple[str, ...] = (),
    event_store_events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    dqe_rows: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    artifact_manifests: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """組出一筆 record payload（含 digest）。組完即驗證，不合法就拋。"""
    payload: dict[str, Any] = {
        "schema_version": INVALIDATION_SCHEMA_VERSION,
        "record_type": INVALIDATION_RECORD_TYPE,
        "invalidation_id": invalidation_id,
        "reason_code": reason_code,
        "declared_at": float(declared_at),
        "authorized_by": authorized_by,
        "authorization_reference": authorization_reference,
        "statement": statement,
        "targets": {
            "orchestration_run_ids": sorted(set(orchestration_run_ids)),
            "event_store_events": [dict(e) for e in event_store_events],
            "dqe_rows": [dict(r) for r in dqe_rows],
            "artifact_manifests": [dict(m) for m in artifact_manifests],
        },
    }
    payload["record_digest"] = sha256_text(canonical_json(
        {k: payload[k] for k in _DIGEST_FIELDS}))
    validate_invalidation_record(payload)
    return payload


def validate_invalidation_record(payload: Any) -> dict[str, Any]:
    """完整驗證一筆 record。未知欄位一律拒絕（不猜、不忽略）。"""
    if not isinstance(payload, dict):
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", "the invalidation record must be an object")
    unknown = sorted(set(payload) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", f"unknown field(s): {unknown}")
    missing = sorted(_TOP_LEVEL_FIELDS - set(payload))
    if missing:
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", f"missing field(s): {missing}")
    if payload["schema_version"] != INVALIDATION_SCHEMA_VERSION:
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID",
            f"unsupported schema_version: {payload['schema_version']!r}")
    if payload["record_type"] != INVALIDATION_RECORD_TYPE:
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID",
            f"record_type must be {INVALIDATION_RECORD_TYPE!r}")
    for field_name in ("invalidation_id", "reason_code", "authorized_by",
                       "authorization_reference", "statement"):
        value = payload[field_name]
        if not isinstance(value, str) or not value.strip():
            raise InvalidationRecordError(
                "INVALIDATION_RECORD_INVALID",
                f"{field_name} must be a non-empty string")
    if not isinstance(payload["declared_at"], (int, float)):
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", "declared_at must be a number")

    targets = payload["targets"]
    if not isinstance(targets, dict):
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", "targets must be an object")
    extra = sorted(set(targets) - _TARGET_SECTIONS)
    if extra:
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", f"unknown targets section(s): {extra}")
    absent = sorted(_TARGET_SECTIONS - set(targets))
    if absent:
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", f"missing targets section(s): {absent}")

    _require_str_list(targets["orchestration_run_ids"], "orchestration_run_ids")
    _require_entries(targets["event_store_events"], "event_store_events",
                     required=("event_id", "event_type", "payload_sha256"))
    _require_entries(targets["dqe_rows"], "dqe_rows",
                     required=("db_identity", "table", "row_id", "row_sha256"))
    _require_entries(targets["artifact_manifests"], "artifact_manifests",
                     required=("path", "file_sha256"))

    if not any(targets[section] for section in sorted(_TARGET_SECTIONS)):
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID",
            "an invalidation record must name at least one target")

    expected = sha256_text(canonical_json({k: payload[k] for k in _DIGEST_FIELDS}))
    if payload["record_digest"] != expected:
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_DIGEST_MISMATCH",
            "the invalidation record digest does not recompute")
    return payload


def _require_str_list(value: Any, name: str) -> None:
    if not isinstance(value, list) or any(
            not isinstance(v, str) or not v for v in value):
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", f"{name} must be a list of non-empty strings")


def _require_entries(value: Any, name: str, *, required: tuple[str, ...]) -> None:
    if not isinstance(value, list):
        raise InvalidationRecordError(
            "INVALIDATION_RECORD_INVALID", f"{name} must be a list")
    for entry in value:
        if not isinstance(entry, dict):
            raise InvalidationRecordError(
                "INVALIDATION_RECORD_INVALID", f"each {name} entry must be an object")
        for key in required:
            if key not in entry:
                raise InvalidationRecordError(
                    "INVALIDATION_RECORD_INVALID",
                    f"{name} entry is missing {key!r}")
        for key in ("payload_sha256", "row_sha256", "file_sha256"):
            digest = entry.get(key)
            if digest is not None and (
                    not isinstance(digest, str) or len(digest) != 64):
                raise InvalidationRecordError(
                    "INVALIDATION_RECORD_INVALID",
                    f"{name}.{key} must be 64 hex chars")


# ── 已載入的集合 ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class InvalidationSet:
    """所有 invalidation record 的合併結果。空集合表示「零筆作廢」。"""

    event_ids: frozenset[str] = frozenset()
    run_ids: frozenset[str] = frozenset()
    dqe_row_ids: frozenset[str] = frozenset()
    manifest_paths: frozenset[str] = frozenset()
    records: tuple[dict[str, Any], ...] = field(default=())

    @property
    def empty(self) -> bool:
        return not (self.event_ids or self.run_ids or self.dqe_row_ids
                    or self.manifest_paths)

    def event_invalidated(self, event_id: Any) -> bool:
        return isinstance(event_id, str) and event_id in self.event_ids

    def run_invalidated(self, run_id: Any) -> bool:
        return isinstance(run_id, str) and run_id in self.run_ids

    def dqe_row_invalidated(self, *, db_identity: str, row_id: Any) -> bool:
        if row_id is None:
            return False
        try:
            identity = dqe_row_identity(db_identity=db_identity, row_id=row_id)
        except (TypeError, ValueError):
            return False
        return identity in self.dqe_row_ids

    def manifest_invalidated(self, path: Any) -> bool:
        if not isinstance(path, str) or not path:
            return False
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            resolved = path
        return resolved in self.manifest_paths or path in self.manifest_paths

    def public_summary(self) -> dict[str, Any]:
        return {
            "record_count": len(self.records),
            "invalidated_event_count": len(self.event_ids),
            "invalidated_run_count": len(self.run_ids),
            "invalidated_dqe_row_count": len(self.dqe_row_ids),
            "invalidated_manifest_count": len(self.manifest_paths),
            "record_digests": [r.get("record_digest") for r in self.records],
        }


EMPTY_INVALIDATION_SET = InvalidationSet()


# ── 載入 ─────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
#: key = (realpath, st_mtime_ns, st_size)。append 會改變 mtime/size，因此
#: 新寫入的 record 不需要重啟就會被看到。
_cache: dict[tuple[str, int, int], InvalidationSet] = {}


def configured_event_store_path() -> Path:
    raw = os.environ.get(EVENT_STORE_ENV, "").strip() or DEFAULT_EVENT_STORE
    return Path(raw).expanduser()


def load_invalidation_set(path: str | Path | None = None) -> InvalidationSet:
    """讀出目前有效的 invalidation 集合。

    檔案不存在 ⇒ 空集合（誠實地表示零筆作廢）。存在但讀不到或內容不合法
    ⇒ 拋 `InvalidationRecordError`（fail closed）。
    """
    target = Path(path).expanduser() if path is not None else configured_event_store_path()
    try:
        stat = target.stat()
    except OSError:
        return EMPTY_INVALIDATION_SET
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    loaded = _read_records(target)
    with _cache_lock:
        _cache[key] = loaded
    return loaded


def _read_records(path: Path) -> InvalidationSet:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise InvalidationRecordError(
            "INVALIDATION_AUTHORITY_UNREADABLE",
            f"the invalidation authority could not be opened: {exc}") from exc
    try:
        try:
            rows = conn.execute(
                "SELECT payload FROM session_events WHERE event_type = ? "
                "ORDER BY rowid ASC",
                (INVALIDATION_RECORD_TYPE,),
            ).fetchall()
        except sqlite3.Error as exc:
            # 表不存在（例如全新的隔離 store 尚未建表）不是損壞——沒有紀錄
            # 就是零筆作廢。其他 SQL 錯誤仍然 fail closed。
            if "no such table" in str(exc):
                return EMPTY_INVALIDATION_SET
            raise InvalidationRecordError(
                "INVALIDATION_AUTHORITY_UNREADABLE",
                f"the invalidation authority could not be read: {exc}") from exc
    finally:
        conn.close()

    events: set[str] = set()
    runs: set[str] = set()
    dqe_rows: set[str] = set()
    manifests: set[str] = set()
    records: list[dict[str, Any]] = []
    for (raw,) in rows:
        try:
            payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except ValueError as exc:
            raise InvalidationRecordError(
                "INVALIDATION_RECORD_INVALID",
                f"an invalidation record is not valid JSON: {exc}") from exc
        validate_invalidation_record(payload)
        records.append(payload)
        targets = payload["targets"]
        runs.update(targets["orchestration_run_ids"])
        events.update(str(e["event_id"]) for e in targets["event_store_events"])
        for entry in targets["dqe_rows"]:
            dqe_rows.add(dqe_row_identity(
                db_identity=str(entry["db_identity"]), row_id=entry["row_id"]))
        for entry in targets["artifact_manifests"]:
            manifests.add(str(entry["path"]))
    return InvalidationSet(
        event_ids=frozenset(events), run_ids=frozenset(runs),
        dqe_row_ids=frozenset(dqe_rows), manifest_paths=frozenset(manifests),
        records=tuple(records))


def invalidation_set_or_empty(path: str | Path | None = None) -> InvalidationSet:
    """給**非**判定路徑（例如診斷輸出）用的寬鬆版本。

    正式 reader **不得**使用它——排除失敗必須是可見的錯誤，不是靜默放行。
    """
    try:
        return load_invalidation_set(path)
    except InvalidationRecordError:
        return EMPTY_INVALIDATION_SET


def reset_cache() -> None:
    """測試用：清掉 mtime 快取。"""
    with _cache_lock:
        _cache.clear()


__all__ = [
    "DEFAULT_EVENT_STORE",
    "EMPTY_INVALIDATION_SET",
    "EVENT_STORE_ENV",
    "INVALIDATION_RECORD_TYPE",
    "INVALIDATION_SCHEMA_VERSION",
    "InvalidationRecordError",
    "InvalidationSet",
    "build_invalidation_record",
    "canonical_json",
    "configured_event_store_path",
    "dqe_row_identity",
    "invalidation_set_or_empty",
    "load_invalidation_set",
    "reset_cache",
    "sha256_text",
    "validate_invalidation_record",
]
