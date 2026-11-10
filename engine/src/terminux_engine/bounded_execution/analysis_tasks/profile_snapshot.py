# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""FlowTask Profile snapshot v2 的可驗證持久化格式。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from terminux_engine.bounded_execution.analysis_tasks.canonical import sha256_payload
from terminux_engine.bounded_execution.analysis_tasks import flow_profile as FP


PROFILE_SNAPSHOT_SCHEMA_VERSION = 2
PROFILE_BUILDER_IDENTITY = "terminux.flow_task_profile_builder"
PROFILE_BUILDER_VERSION = 1


class ProfileSnapshotError(ValueError):
    """v2 Profile snapshot 不能安全重建。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProfileBuilder:
    """一個已登錄、可由 durable snapshot 重建的 Profile builder。"""

    identity: str
    version: int
    build: Callable[[dict[str, Any]], Any]


_PROFILE_BUILDERS: dict[tuple[str, int], ProfileBuilder] = {
    (PROFILE_BUILDER_IDENTITY, PROFILE_BUILDER_VERSION): ProfileBuilder(
        identity=PROFILE_BUILDER_IDENTITY,
        version=PROFILE_BUILDER_VERSION,
        build=FP.build_profile,
    ),
}


def profile_builder(identity: Any, version: Any) -> ProfileBuilder:
    """以顯式 versioned registry 取 builder；絕不依方法名稱猜測。"""
    builder = _PROFILE_BUILDERS.get((identity, version))
    if builder is None:
        raise ProfileSnapshotError("WORK_SCHEMA_UNSUPPORTED",
                                   "unknown profile snapshot builder")
    return builder


def _canonical_json(value: Any) -> Any:
    """拒絕非 JSON 值，並回傳獨立的 canonical JSON tree。"""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ProfileSnapshotError("UNVERIFIABLE", "profile snapshot is not JSON") from exc


def builder_contract_digest() -> str:
    """固定 v1 builder 的輸入與 canonicalization 合約身分。"""
    return sha256_payload({
        "identity": PROFILE_BUILDER_IDENTITY,
        "version": PROFILE_BUILDER_VERSION,
        "accepted_input": "validated_normalized_flow_profile_document",
        "canonical_profile": "FlowTaskProfile.to_json_dict",
        "normalization": "json_tree_sorted_keys_utf8",
        "supported_profile_schema_versions": sorted(FP.SUPPORTED_SCHEMA_VERSIONS),
    })


def _reject_secrets(value: Any) -> None:
    """snapshot 不得保存 secret value；Profile credential 僅能表達 reference。"""
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("api_key", "password", "token", "secret")):
                raise ProfileSnapshotError(
                    "UNVERIFIABLE", "accepted profile input contains a secret-like field")
            if "credential" in lowered:
                if not isinstance(child, dict) or set(child) - {
                        "binding", "binding_id", "identity", "reference", "ref"}:
                    raise ProfileSnapshotError(
                        "UNVERIFIABLE",
                        "credential data must contain reference identity only")
            _reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child)


def accepted_input(document: Any) -> dict[str, Any]:
    """產生可重餵 v1 builder 的完整 normalized document。"""
    normalized = _canonical_json(document)
    if not isinstance(normalized, dict):
        raise ProfileSnapshotError("UNVERIFIABLE", "accepted profile input must be an object")
    _reject_secrets(normalized)
    try:
        profile_builder(PROFILE_BUILDER_IDENTITY, PROFILE_BUILDER_VERSION).build(normalized)
    except FP.FlowProfileError as exc:
        raise ProfileSnapshotError("UNVERIFIABLE", exc.message) from exc
    return normalized


def build_snapshot(*, task_id: str, document: Any, profile: Any) -> dict[str, Any]:
    """建立 v2 payload；task digest 會引用其 snapshot digest。"""
    accepted = accepted_input(document)
    canonical = _canonical_json(profile.to_json_dict())
    accepted_sha = sha256_payload(accepted)
    canonical_sha = sha256_payload(canonical)
    payload = {
        "schema_version": PROFILE_SNAPSHOT_SCHEMA_VERSION,
        "task_id": task_id,
        "profile_id": profile.profile_id,
        "profile_schema_version": profile.schema_version,
        "profile_builder": {
            "identity": PROFILE_BUILDER_IDENTITY,
            "version": PROFILE_BUILDER_VERSION,
            "contract_digest": builder_contract_digest(),
        },
        "accepted_input": accepted,
        "accepted_input_sha256": accepted_sha,
        "canonical_profile": canonical,
        "canonical_profile_sha256": canonical_sha,
        "expected_profile_digest": profile.profile_digest,
    }
    return {**payload, "snapshot_digest": sha256_payload(payload)}


def rebuild_snapshot(payload: Any, *, task_id: str) -> Any:
    """驗證 v2 payload 並以 registry 的 builder 重建 Profile。"""
    if not isinstance(payload, dict):
        raise ProfileSnapshotError("UNVERIFIABLE", "profile snapshot v2 is missing")
    if payload.get("schema_version") != PROFILE_SNAPSHOT_SCHEMA_VERSION:
        raise ProfileSnapshotError("WORK_SCHEMA_UNSUPPORTED", "unknown profile snapshot schema")
    if payload.get("task_id") != task_id:
        raise ProfileSnapshotError("UNVERIFIABLE", "profile snapshot task_id mismatch")
    builder = payload.get("profile_builder")
    if not isinstance(builder, dict):
        raise ProfileSnapshotError("WORK_SCHEMA_UNSUPPORTED", "unknown profile snapshot builder")
    registered = profile_builder(builder.get("identity"), builder.get("version"))
    if builder.get("contract_digest") != builder_contract_digest():
        raise ProfileSnapshotError("UNVERIFIABLE", "profile snapshot builder contract mismatch")
    accepted = payload.get("accepted_input")
    canonical = payload.get("canonical_profile")
    if sha256_payload(accepted) != payload.get("accepted_input_sha256"):
        raise ProfileSnapshotError("UNVERIFIABLE", "accepted profile input digest mismatch")
    if sha256_payload(canonical) != payload.get("canonical_profile_sha256"):
        raise ProfileSnapshotError("UNVERIFIABLE", "canonical profile digest mismatch")
    unsigned = {key: value for key, value in payload.items() if key != "snapshot_digest"}
    if sha256_payload(unsigned) != payload.get("snapshot_digest"):
        raise ProfileSnapshotError("UNVERIFIABLE", "profile snapshot digest mismatch")
    accepted = accepted_input(accepted)
    try:
        profile = registered.build(accepted)
    except FP.FlowProfileError as exc:
        raise ProfileSnapshotError("UNVERIFIABLE", exc.message) from exc
    if _canonical_json(profile.to_json_dict()) != canonical:
        raise ProfileSnapshotError("UNVERIFIABLE", "rebuilt canonical profile differs")
    if profile.profile_digest != payload.get("expected_profile_digest"):
        raise ProfileSnapshotError("UNVERIFIABLE", "rebuilt profile digest differs")
    if profile.profile_id != payload.get("profile_id") \
            or profile.schema_version != payload.get("profile_schema_version"):
        raise ProfileSnapshotError("UNVERIFIABLE", "rebuilt profile identity differs")
    return profile


def snapshot_event_id(task_id: str) -> str:
    """由 deterministic task identity 推導 v2 snapshot event id。"""
    return "ftprofilev2" + sha256_payload({"task_id": task_id})[:52]


__all__ = [
    "PROFILE_BUILDER_IDENTITY", "PROFILE_BUILDER_VERSION", "PROFILE_SNAPSHOT_SCHEMA_VERSION",
    "ProfileBuilder", "ProfileSnapshotError", "accepted_input", "build_snapshot",
    "builder_contract_digest", "profile_builder", "rebuild_snapshot", "snapshot_event_id",
]
