# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Artifact Manifest 資料模型（domain-blind provenance evidence）。

本檔案定義 artifact lineage 的**證據本體**：`ArtifactObservation`（某次
producer execution 對一個 artifact 位置的客觀觀察）與 `ArtifactManifest`
（一次 capture 的完整、immutable 觀察集合）。

Domain-blind 邊界：本檔案不理解任何 vendor／EDA 概念——沒有
Docker、ORFS stage、GDS／DEF／ODB、timing／DRC 的名稱或成功條件。
`adapter_role` 是 adapter-owned 的 opaque namespaced 字串，這裡只原樣保存。

Exists 不等於 valid：本層只記錄機械事實（path 是否存在、file
type、是否可讀、size／mtime、hash 是否成功、hash 前後是否穩定、是否落在
output boundary 內）。「0-byte 是否合理」「GDS 是否有效」「timing 是否
通過」一律不判斷，留給 Package／DQE。

Instance identity 與 content identity 分離：
`artifact_instance_id` 代表「某次 execution 觀察到的一個產物位置／角色」，
`content_identity.digest` 代表 bytes identity。兩個不同 instance 可以有
相同 content hash；SHA-256 不得直接當 instance id。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MANIFEST_SCHEMA_VERSION = 1

# ── availability：candidate 位置的機械狀態（不含 domain validity）────────
AVAILABILITY_PRESENT = "present"
AVAILABILITY_MISSING = "missing"
AVAILABILITY_UNREADABLE = "unreadable"
AVAILABILITY_BOUNDARY_REJECTED = "boundary_rejected"
AVAILABILITY_UNSUPPORTED_TYPE = "unsupported_type"

AVAILABILITY_VALUES = frozenset(
    {
        AVAILABILITY_PRESENT,
        AVAILABILITY_MISSING,
        AVAILABILITY_UNREADABLE,
        AVAILABILITY_BOUNDARY_REJECTED,
        AVAILABILITY_UNSUPPORTED_TYPE,
    }
)

# ── stability：hash 期間 bytes 是否穩定。刻意與 availability 分離——
# 一個檔案可以同時是 `present` 且 `unstable`。────────
STABILITY_STABLE = "stable"
STABILITY_UNSTABLE = "unstable"
STABILITY_NOT_APPLICABLE = "not_applicable"

STABILITY_VALUES = frozenset(
    {STABILITY_STABLE, STABILITY_UNSTABLE, STABILITY_NOT_APPLICABLE}
)

FILE_TYPE_REGULAR = "regular"
FILE_TYPE_SYMLINK = "symlink"
FILE_TYPE_DIRECTORY = "directory"
FILE_TYPE_OTHER = "other"
FILE_TYPE_MISSING = "missing"

FILE_TYPE_VALUES = frozenset(
    {
        FILE_TYPE_REGULAR,
        FILE_TYPE_SYMLINK,
        FILE_TYPE_DIRECTORY,
        FILE_TYPE_OTHER,
        FILE_TYPE_MISSING,
    }
)

CAPTURE_STATUS_COMPLETE = "complete"
CAPTURE_STATUS_PARTIAL = "partial"
CAPTURE_STATUS_FAILED = "failed"

CAPTURE_STATUS_VALUES = frozenset(
    {CAPTURE_STATUS_COMPLETE, CAPTURE_STATUS_PARTIAL, CAPTURE_STATUS_FAILED}
)

HASH_ALGORITHM_SHA256 = "sha256"


class ArtifactManifestError(ValueError):
    """Manifest／observation 結構驗證失敗；呼叫端應 fail-closed。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ArtifactManifestError(message)


@dataclass(frozen=True)
class ContentIdentity:
    """Artifact bytes 的內容身份。第一版演算法固定 SHA-256。"""

    algorithm: str
    digest: str

    def __post_init__(self) -> None:
        _require(
            self.algorithm == HASH_ALGORITHM_SHA256,
            f"unsupported content hash algorithm: {self.algorithm!r}",
        )
        _require(
            isinstance(self.digest, str) and len(self.digest) == 64,
            "content digest must be a 64-char sha256 hex string",
        )
        _require(
            self.digest == self.digest.lower()
            and all(c in "0123456789abcdef" for c in self.digest),
            "content digest must be lowercase hex",
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {"algorithm": self.algorithm, "digest": self.digest}

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "ContentIdentity":
        return cls(algorithm=payload["algorithm"], digest=payload["digest"])


@dataclass(frozen=True)
class ProducerExecutionReference:
    """引用既有 vendor-neutral execution contract execution identity（不新增第二套 run identity）。

    `run_id`／`step_id`／`attempt` 一律來自 Kernel／Orchestration Ledger；
    本模組不生成任何 lineage 專用 run ID。
    """

    run_id: str
    step_id: str
    attempt: int
    tool_id: str
    event_tool_name: str
    adapter_id: str
    adapter_version: str | None
    receipt_reference: str | None

    def __post_init__(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("step_id", self.step_id),
            ("tool_id", self.tool_id),
            ("event_tool_name", self.event_tool_name),
            ("adapter_id", self.adapter_id),
        ):
            _require(
                isinstance(value, str) and bool(value),
                f"ProducerExecutionReference.{label} must be a non-empty string",
            )
        _require(
            isinstance(self.attempt, int) and self.attempt >= 1,
            "ProducerExecutionReference.attempt must be a positive int",
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "tool_id": self.tool_id,
            "event_tool_name": self.event_tool_name,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "receipt_reference": self.receipt_reference,
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "ProducerExecutionReference":
        return cls(
            run_id=payload["run_id"],
            step_id=payload["step_id"],
            attempt=payload["attempt"],
            tool_id=payload["tool_id"],
            event_tool_name=payload["event_tool_name"],
            adapter_id=payload["adapter_id"],
            adapter_version=payload.get("adapter_version"),
            receipt_reference=payload.get("receipt_reference"),
        )


@dataclass(frozen=True)
class ArtifactObservation:
    """一次 execution 對「一個 artifact 位置／角色」的客觀觀察。

    `content_identity` 只有在「boundary 內的 regular file、streaming hash
    成功、且 hash 前後 stat 未變」時才非 null。missing／unreadable
    ／boundary_rejected／unsupported_type／unstable 一律為 null——不得以
    空 digest 或猜測值填補。
    """

    observation_id: str
    artifact_instance_id: str
    adapter_role: str
    candidate_path: str
    resolved_path: str | None
    file_type: str
    availability: str
    stability: str
    size_bytes: int | None
    mtime_ns: int | None
    content_identity: ContentIdentity | None
    capture_error: dict[str, Any] | None
    producer: ProducerExecutionReference
    isvn_coordinate: dict[str, Any] | None = None
    symlink_target: str | None = None

    def __post_init__(self) -> None:
        _require(
            self.availability in AVAILABILITY_VALUES,
            f"invalid availability: {self.availability!r}",
        )
        _require(
            self.stability in STABILITY_VALUES, f"invalid stability: {self.stability!r}"
        )
        _require(
            self.file_type in FILE_TYPE_VALUES, f"invalid file_type: {self.file_type!r}"
        )
        _require(
            isinstance(self.artifact_instance_id, str) and bool(self.artifact_instance_id),
            "artifact_instance_id must be a non-empty string",
        )
        # instance id 不得等於 content digest。
        if self.content_identity is not None:
            _require(
                self.artifact_instance_id != self.content_identity.digest,
                "artifact_instance_id must not be the content sha256 digest",
            )
            _require(
                self.availability == AVAILABILITY_PRESENT
                and self.stability == STABILITY_STABLE,
                "content_identity is only allowed for present+stable observations",
            )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "artifact_instance_id": self.artifact_instance_id,
            "adapter_role": self.adapter_role,
            "candidate_path": self.candidate_path,
            "resolved_path": self.resolved_path,
            "file_type": self.file_type,
            "availability": self.availability,
            "stability": self.stability,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "content_identity": (
                self.content_identity.to_json_dict()
                if self.content_identity is not None
                else None
            ),
            "capture_error": self.capture_error,
            "producer": self.producer.to_json_dict(),
            "isvn_coordinate": self.isvn_coordinate,
            "symlink_target": self.symlink_target,
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "ArtifactObservation":
        content = payload.get("content_identity")
        return cls(
            observation_id=payload["observation_id"],
            artifact_instance_id=payload["artifact_instance_id"],
            adapter_role=payload["adapter_role"],
            candidate_path=payload["candidate_path"],
            resolved_path=payload.get("resolved_path"),
            file_type=payload["file_type"],
            availability=payload["availability"],
            stability=payload["stability"],
            size_bytes=payload.get("size_bytes"),
            mtime_ns=payload.get("mtime_ns"),
            content_identity=(
                ContentIdentity.from_json_dict(content) if content is not None else None
            ),
            capture_error=payload.get("capture_error"),
            producer=ProducerExecutionReference.from_json_dict(payload["producer"]),
            isvn_coordinate=payload.get("isvn_coordinate"),
            symlink_target=payload.get("symlink_target"),
        )


@dataclass(frozen=True)
class ArtifactManifest:
    """一次 capture 的 immutable evidence（manifest 是證據本體）。

    `capture_status` 的語意：
    - `complete`：所有 candidate 都完成客觀 observation。**candidate 不存在
      仍是 complete**——「確認不存在」本身就是一次完整的觀察。
    - `partial`：存在 unreadable／unstable／boundary_rejected／
      unsupported_type／hash failure 等無法固定 content identity 的 entry。
    - `failed`：manifest 無法建立或持久化。
    """

    schema_version: int
    manifest_id: str
    captured_at: str
    capture_status: str
    producer: ProducerExecutionReference
    output_boundaries: list[str]
    observations: list[ArtifactObservation]
    isvn_enrichment_status: str

    def __post_init__(self) -> None:
        _require(
            self.capture_status in CAPTURE_STATUS_VALUES,
            f"invalid capture_status: {self.capture_status!r}",
        )
        _require(
            isinstance(self.manifest_id, str) and bool(self.manifest_id),
            "manifest_id must be a non-empty string",
        )
        instance_ids = [obs.artifact_instance_id for obs in self.observations]
        _require(
            len(instance_ids) == len(set(instance_ids)),
            "artifact_instance_id must be unique within a manifest",
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "captured_at": self.captured_at,
            "capture_status": self.capture_status,
            "producer": self.producer.to_json_dict(),
            "output_boundaries": list(self.output_boundaries),
            "observations": [obs.to_json_dict() for obs in self.observations],
            "isvn_enrichment_status": self.isvn_enrichment_status,
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "ArtifactManifest":
        return cls(
            schema_version=payload["schema_version"],
            manifest_id=payload["manifest_id"],
            captured_at=payload["captured_at"],
            capture_status=payload["capture_status"],
            producer=ProducerExecutionReference.from_json_dict(payload["producer"]),
            output_boundaries=list(payload["output_boundaries"]),
            observations=[
                ArtifactObservation.from_json_dict(entry)
                for entry in payload["observations"]
            ],
            isvn_enrichment_status=payload["isvn_enrichment_status"],
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_json_dict(), indent=indent, sort_keys=True)
