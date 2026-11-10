# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""AnalysisTask 專用的 canonical encoding 與 hash。

規則（固定，不可放寬）：

- JSON、字串 key、UTF-8、key 排序、無多餘空白；
- **拒絕** NaN／Infinity；
- hash 為完整 canonical bytes 的 SHA-256。

刻意**不重用** `bounded_execution.binding.canonical_json`：那一份用
`default=str` 靜默把不可序列化物件轉成字串，也允許 NaN／Infinity
（`json.dumps` 預設 `allow_nan=True`），不符合這裡對「完整結果」的
完整性要求。既有 proposal digest 演算法完全不動。

時點一律是 host 的 UTC epoch milliseconds 整數。活躍有效期另外
使用 monotonic deadline，防止牆上時鐘回撥延長授權；monotonic 值只在
process 內有意義，**不寫進任何持久化記錄**，也不拿來跨重啟續跑。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any


class CanonicalEncodingError(ValueError):
    """payload 不能以這裡的 canonical 規則表示。"""


def _check(value: Any, path: str) -> None:
    """遞迴檢查；不做任何靜默轉換。"""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalEncodingError(
                f"non-finite float at {path or '<root>'}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalEncodingError(
                    f"non-string key {key!r} at {path or '<root>'}")
            _check(item, f"{path}.{key}")
        return
    raise CanonicalEncodingError(
        f"unsupported type {type(value).__name__} at {path or '<root>'}")


def canonical_text(payload: Any) -> str:
    """canonical JSON 文字。不可序列化或含 NaN/Infinity ⇒ 拋出。"""
    _check(payload, "")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_bytes(payload: Any) -> bytes:
    return canonical_text(payload).encode("utf-8")


def canonical_size(payload: Any) -> int:
    """canonical bytes 長度。用於在保存前先驗大小上限。"""
    return len(canonical_bytes(payload))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_payload(payload: Any) -> str:
    return sha256_bytes(canonical_bytes(payload))


def hash_excluding(payload: dict[str, Any], excluded_key: str) -> str:
    """對「除了 `excluded_key` 以外的所有欄位」計算 SHA-256。

    task_digest 與 result_sha256 都用這個規則：hash 欄位本身不參與自己的
    hash 計算。
    """
    if not isinstance(payload, dict):
        raise CanonicalEncodingError("hash_excluding expects a JSON object")
    return sha256_payload({k: v for k, v in payload.items() if k != excluded_key})


def now_ms() -> int:
    """Host UTC epoch milliseconds。"""
    return int(time.time() * 1000)


def monotonic_s() -> float:
    """process-local monotonic 秒數；只用於活躍期限，不持久化。"""
    return time.monotonic()


__all__ = [
    "CanonicalEncodingError",
    "canonical_bytes",
    "canonical_size",
    "canonical_text",
    "hash_excluding",
    "monotonic_s",
    "now_ms",
    "sha256_bytes",
    "sha256_payload",
]
