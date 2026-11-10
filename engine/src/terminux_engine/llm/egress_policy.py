# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""外送 policy 檔的載入與形狀驗證（v1 digest 與 v2 structural）。

policy 檔只由本人 0600 普通檔提供；這裡只讀檔與驗證形狀，不做任何外送判定。
v1 形狀的檢查順序與錯誤字串沿用 `openai_compatible_provider._check_outbound_payload`
既有行為；v2 形狀新增 `policy_version`、`approval_mode`、`limits`、`non_agent_egress`
四鍵，全部必填，任何未知值一律視為 schema 無效。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import astuple, dataclass
from typing import Any
from urllib.parse import urlsplit

POLICY_VERSION_V1 = "terminux.egress_policy.v1"
POLICY_VERSION_V2 = "terminux.egress_policy.v2"
APPROVAL_EXACT_DIGEST = "exact_digest"
APPROVAL_STRUCTURAL_C2 = "structural_c2"
APPROVAL_MODES = frozenset({APPROVAL_EXACT_DIGEST, APPROVAL_STRUCTURAL_C2})
NON_AGENT_EGRESS_DENY = "deny"

#: v1 詞彙（digest 白名單時代的六個舊標籤）。只能搭配 `exact_digest`。
V1_DATA_CLASSES = frozenset({
    "public_gcd_nangate45", "sanitized_n1b_evidence", "bounded_ecc",
    "agent_history", "tool_result", "system_instructions",
})
#: 單一 setup path 證據（Evidence Bundle 的 `timing_path` section）的獨立類別。
#: 不在 `approved_data_classes` 內時該 section 不進任何請求；`evidence_bundle` 類別不涵蓋它。
TIMING_PATH_EVIDENCE_CLASS = "timing_path_evidence"
#: v2 詞彙：由 host 依來源指定的九個類別；內容不得自稱類別。
V2_DATA_CLASSES = frozenset({
    "system_instructions", "learner_text", "conversation_history", "tool_schema",
    "tool_call", "evidence_bundle", "model_output", "budget_summary",
    TIMING_PATH_EVIDENCE_CLASS,
})

V1_KEYS = frozenset({"endpoint", "model", "allowed_payload_sha256", "approved_data_classes"})
V2_KEYS = V1_KEYS | {"policy_version", "approval_mode", "limits", "non_agent_egress"}

#: `limits` 五鍵的合法範圍（含）。值本身由管理端的 policy 檔提供，程式不預設。
LIMIT_RANGES: dict[str, tuple[int, int]] = {
    "learner_text_max_chars": (1, 65536),
    "history_max_turns": (0, 200),
    "history_max_chars": (0, 262144),
    "model_output_max_chars": (1, 262144),
    "requests_per_turn": (1, 8),
}
MAX_POLICY_BYTES = 65536

REASON_UNAVAILABLE = "outbound payload policy unavailable or invalid"
REASON_NOT_REGULAR = "outbound payload policy must be a regular file"
REASON_OWNER_MODE = "outbound payload policy owner or mode invalid"
REASON_TOO_LARGE = "outbound payload policy too large"
REASON_SCHEMA = "outbound payload policy schema invalid"
REASON_CLASSES = "outbound data classes not approved"
REASON_DIGEST_LIST = "outbound payload digest list invalid"
REASON_PATH_ABSOLUTE = "outbound payload policy path must be absolute"


class EgressPolicyError(ValueError):
    """policy 檔無法讀取或形狀不合法；`reason` 是穩定的對外字串。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EgressLimits:
    learner_text_max_chars: int
    history_max_turns: int
    history_max_chars: int
    model_output_max_chars: int
    requests_per_turn: int


@dataclass(frozen=True)
class EgressPolicy:
    """一份已驗證形狀的 policy 檔；`sha256` 是檔案 bytes 的雜湊（＝policy revision）。"""

    sha256: str
    policy_version: str
    approval_mode: str
    endpoint: str
    model: str
    allowed_payload_sha256: tuple[str, ...]
    approved_data_classes: frozenset[str]
    limits: EgressLimits | None
    non_agent_egress: str | None

    @property
    def policy_id(self) -> str:
        return self.sha256[:16]

    @property
    def destination_host(self) -> str:
        try:
            return urlsplit(self.endpoint).hostname or ""
        except ValueError:
            return ""

    @property
    def destination(self) -> str:
        """可外送歷史與 audit 所用的目的地身分：host 與 model，不含 path 或 query。"""
        return f"{self.destination_host}/{self.model}"

    @property
    def structural(self) -> bool:
        return self.approval_mode == APPROVAL_STRUCTURAL_C2

    @property
    def context_scope(self) -> str:
        """digest 白名單（`allowed_payload_sha256`）以外全部欄位的雜湊。

        只增刪白名單時不變；版本、模式、endpoint、model、資料類別、limits 或 `non_agent_egress`
        任一改變即不同。本類別新增欄位時必須一併納入，否則該欄位的改變不會被視為授權範圍改變。
        """
        document = [self.policy_version, self.approval_mode, self.endpoint, self.model,
                    sorted(self.approved_data_classes),
                    None if self.limits is None else list(astuple(self.limits)), self.non_agent_egress]
        return hashlib.sha256(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def read_policy_bytes(path: str) -> bytes:
    """以與最終 Gate 相同的身分條件讀取 policy 檔：本人、0600、普通檔、不跟 symlink。"""
    if not os.path.isabs(path):
        raise EgressPolicyError(REASON_PATH_ABSOLUTE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise EgressPolicyError(REASON_UNAVAILABLE) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EgressPolicyError(REASON_NOT_REGULAR)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise EgressPolicyError(REASON_OWNER_MODE)
        if info.st_size > MAX_POLICY_BYTES:
            raise EgressPolicyError(REASON_TOO_LARGE)
        chunks: list[bytes] = []
        remaining = MAX_POLICY_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            raise EgressPolicyError(REASON_TOO_LARGE)
        return b"".join(chunks)
    except OSError as error:
        raise EgressPolicyError(REASON_UNAVAILABLE) from error
    finally:
        os.close(fd)


def _validate_digest_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and len(item) == 64 for item in value):
        raise EgressPolicyError(REASON_DIGEST_LIST)
    return tuple(value)


def _validate_classes(value: Any, vocabulary: frozenset[str]) -> frozenset[str]:
    if (not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value)
            or len(value) != len(set(value)) or not set(value) <= vocabulary):
        raise EgressPolicyError(REASON_CLASSES)
    return frozenset(value)


def _validate_limits(value: Any) -> EgressLimits:
    if not isinstance(value, dict) or set(value) != set(LIMIT_RANGES):
        raise EgressPolicyError(REASON_SCHEMA)
    resolved: dict[str, int] = {}
    for key, (low, high) in LIMIT_RANGES.items():
        number = value[key]
        if type(number) is not int or number < low or number > high:
            raise EgressPolicyError(REASON_SCHEMA)
        resolved[key] = number
    return EgressLimits(**resolved)


def parse_policy(raw: bytes) -> EgressPolicy:
    """驗證 policy bytes 的形狀；不比對 endpoint／model，也不比對 digest。

    v1：exact key set、classes ⊆ v1 詞彙、digest 清單為 64 字元字串的 list
    （與既有最終 Gate 相同的格式檢查，只是提前到 endpoint 比對之前）。
    v2：exact key set、固定版本字串、`approval_mode` 二選一、`limits` 五個正整數、
    `non_agent_egress="deny"`、classes ⊆ v2 詞彙；`structural_c2` 的 digest 清單必為空。
    """
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise EgressPolicyError(REASON_UNAVAILABLE) from error
    if not isinstance(document, dict):
        raise EgressPolicyError(REASON_SCHEMA)
    keys = set(document)
    digest = hashlib.sha256(raw).hexdigest()
    if keys == V1_KEYS:
        classes = _validate_classes(document["approved_data_classes"], V1_DATA_CLASSES)
        digests = _validate_digest_list(document["allowed_payload_sha256"])
        return EgressPolicy(
            sha256=digest, policy_version=POLICY_VERSION_V1, approval_mode=APPROVAL_EXACT_DIGEST,
            endpoint=document["endpoint"], model=document["model"],
            allowed_payload_sha256=digests,
            approved_data_classes=classes, limits=None, non_agent_egress=None,
        )
    if keys != V2_KEYS:
        raise EgressPolicyError(REASON_SCHEMA)
    if document["policy_version"] != POLICY_VERSION_V2:
        raise EgressPolicyError(REASON_SCHEMA)
    mode = document["approval_mode"]
    if not isinstance(mode, str) or mode not in APPROVAL_MODES:
        raise EgressPolicyError(REASON_SCHEMA)
    if document["non_agent_egress"] != NON_AGENT_EGRESS_DENY:
        raise EgressPolicyError(REASON_SCHEMA)
    if not isinstance(document["endpoint"], str) or not isinstance(document["model"], str):
        raise EgressPolicyError(REASON_SCHEMA)
    limits = _validate_limits(document["limits"])
    classes = _validate_classes(document["approved_data_classes"], V2_DATA_CLASSES)
    digests = _validate_digest_list(document["allowed_payload_sha256"])
    if mode == APPROVAL_STRUCTURAL_C2 and digests:
        raise EgressPolicyError(REASON_SCHEMA)
    return EgressPolicy(
        sha256=digest, policy_version=POLICY_VERSION_V2, approval_mode=mode,
        endpoint=document["endpoint"], model=document["model"],
        allowed_payload_sha256=digests, approved_data_classes=classes,
        limits=limits, non_agent_egress=NON_AGENT_EGRESS_DENY,
    )


def load_policy(path: str) -> EgressPolicy:
    """讀檔並驗證形狀；任何失敗都以 `EgressPolicyError` 回報，不回傳部分結果。"""
    return parse_policy(read_policy_bytes(path))


__all__ = [
    "APPROVAL_EXACT_DIGEST", "APPROVAL_MODES", "APPROVAL_STRUCTURAL_C2", "EgressLimits",
    "EgressPolicy", "EgressPolicyError", "LIMIT_RANGES", "MAX_POLICY_BYTES",
    "NON_AGENT_EGRESS_DENY", "POLICY_VERSION_V1", "POLICY_VERSION_V2", "TIMING_PATH_EVIDENCE_CLASS",
    "V1_DATA_CLASSES", "V1_KEYS", "V2_DATA_CLASSES", "V2_KEYS", "load_policy", "parse_policy",
    "read_policy_bytes",
]
