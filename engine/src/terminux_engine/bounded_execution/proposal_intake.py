# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Manual proposal intake 契約（Profile 與 activation policy 共用）。

Phase A 只定義「誰可以手動送出 proposal、送哪一種 method」。這個宣告**兩側
都要有**：Profile 宣告它接受哪些 origin／method identity，activation policy
宣告這台 daemon 允許哪些。實際生效的是**交集**；任一側缺欄位、交集為空，
一律 fail closed（不是「沒宣告就全允許」）。

這裡不碰授權、不碰 grant、不碰執行——它只回答「這一筆手動 proposal 可不可以
被收下並保存」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 目前唯一支援的 intake schema。只接受 1。
PROPOSAL_INTAKE_SCHEMA_VERSION = 1

#: 目前唯一合法的手動 origin。未知 origin 不得被夾帶進來。
ORIGIN_USER_SUBMITTED = "USER_SUBMITTED"
SUPPORTED_ORIGINS = frozenset({ORIGIN_USER_SUBMITTED})

_INTAKE_FIELDS = frozenset({
    "schema_version", "allowed_origins", "allowed_method_identities",
})

# ── 宣告本身不合法的拒絕碼 ────────────────────────────────────────────

INTAKE_INVALID = "PROPOSAL_INTAKE_INVALID"
INTAKE_SCHEMA_UNSUPPORTED = "PROPOSAL_INTAKE_SCHEMA_UNSUPPORTED"

# ── 交集判定的拒絕碼 ──────────────────────────────────────────────────

NOT_DECLARED_BY_PROFILE = "MANUAL_PROPOSAL_NOT_DECLARED_BY_PROFILE"
NOT_DECLARED_BY_POLICY = "MANUAL_PROPOSAL_NOT_DECLARED_BY_POLICY"
EMPTY_INTERSECTION = "MANUAL_PROPOSAL_INTAKE_EMPTY_INTERSECTION"
ORIGIN_NOT_ALLOWED = "MANUAL_PROPOSAL_ORIGIN_NOT_ALLOWED"
METHOD_NOT_ALLOWED = "MANUAL_PROPOSAL_METHOD_NOT_ALLOWED"


class ProposalIntakeError(ValueError):
    """intake 宣告本身不合法。caller 轉成自己那一層的拒絕碼。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProposalIntake:
    """一側（Profile 或 policy）宣告的 manual proposal intake。"""

    schema_version: int
    allowed_origins: tuple[str, ...]
    allowed_method_identities: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "allowed_origins": list(self.allowed_origins),
            "allowed_method_identities": list(self.allowed_method_identities),
        }


@dataclass(frozen=True)
class ProposalIntakeDecision:
    """兩側交集後的結果。`allowed` 為 False 時兩個清單一律是空的。"""

    allowed: bool
    reason_code: str | None
    allowed_origins: tuple[str, ...] = ()
    allowed_method_identities: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "allowed_origins": list(self.allowed_origins),
            "allowed_method_identities": list(self.allowed_method_identities),
        }

    @classmethod
    def denied(cls, reason_code: str) -> "ProposalIntakeDecision":
        return cls(allowed=False, reason_code=reason_code)


def _require_identity_list(value: Any, *, where: str,
                           allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ProposalIntakeError(
            INTAKE_INVALID, f"{where} must be a non-empty array of strings")
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise ProposalIntakeError(
                INTAKE_INVALID, f"{where} entries must be non-empty trimmed strings")
        if item in seen:
            raise ProposalIntakeError(
                INTAKE_INVALID, f"{where} must not repeat {item!r}")
        if allowed is not None and item not in allowed:
            raise ProposalIntakeError(
                INTAKE_INVALID,
                f"{where} does not support {item!r}; supported: {sorted(allowed)}")
        seen.add(item)
        result.append(item)
    return tuple(result)


def build_proposal_intake(node: Any, *, where: str) -> ProposalIntake:
    """逐欄驗證一份 intake 宣告。未知欄位、缺欄位、版本不符一律拒絕。"""
    if not isinstance(node, dict):
        raise ProposalIntakeError(INTAKE_INVALID, f"{where} must be an object")
    unknown = sorted(set(node) - _INTAKE_FIELDS)
    if unknown:
        raise ProposalIntakeError(
            INTAKE_INVALID, f"unknown {where} field(s): {unknown}")
    missing = sorted(_INTAKE_FIELDS - set(node))
    if missing:
        raise ProposalIntakeError(
            INTAKE_INVALID, f"missing {where} field(s): {missing}")
    schema_version = node["schema_version"]
    if (isinstance(schema_version, bool)
            or schema_version != PROPOSAL_INTAKE_SCHEMA_VERSION):
        raise ProposalIntakeError(
            INTAKE_SCHEMA_UNSUPPORTED,
            f"{where}.schema_version must be {PROPOSAL_INTAKE_SCHEMA_VERSION}; "
            f"got {schema_version!r}")
    origins = _require_identity_list(
        node["allowed_origins"], where=f"{where}.allowed_origins",
        allowed=SUPPORTED_ORIGINS)
    methods = _require_identity_list(
        node["allowed_method_identities"],
        where=f"{where}.allowed_method_identities")
    return ProposalIntake(schema_version=PROPOSAL_INTAKE_SCHEMA_VERSION,
                          allowed_origins=origins,
                          allowed_method_identities=methods)


def intersect_proposal_intake(
    profile_intake: ProposalIntake | None,
    policy_intake: ProposalIntake | None,
) -> ProposalIntakeDecision:
    """兩側宣告的交集。任一側缺席或交集為空 ⇒ 不允許。"""
    if profile_intake is None:
        return ProposalIntakeDecision.denied(NOT_DECLARED_BY_PROFILE)
    if policy_intake is None:
        return ProposalIntakeDecision.denied(NOT_DECLARED_BY_POLICY)
    origins = tuple(sorted(set(profile_intake.allowed_origins)
                           & set(policy_intake.allowed_origins)))
    methods = tuple(sorted(set(profile_intake.allowed_method_identities)
                           & set(policy_intake.allowed_method_identities)))
    if not origins or not methods:
        return ProposalIntakeDecision.denied(EMPTY_INTERSECTION)
    return ProposalIntakeDecision(allowed=True, reason_code=None,
                                  allowed_origins=origins,
                                  allowed_method_identities=methods)


__all__ = [
    "EMPTY_INTERSECTION",
    "INTAKE_INVALID",
    "INTAKE_SCHEMA_UNSUPPORTED",
    "METHOD_NOT_ALLOWED",
    "NOT_DECLARED_BY_POLICY",
    "NOT_DECLARED_BY_PROFILE",
    "ORIGIN_NOT_ALLOWED",
    "ORIGIN_USER_SUBMITTED",
    "PROPOSAL_INTAKE_SCHEMA_VERSION",
    "SUPPORTED_ORIGINS",
    "ProposalIntake",
    "ProposalIntakeDecision",
    "ProposalIntakeError",
    "build_proposal_intake",
    "intersect_proposal_intake",
]
