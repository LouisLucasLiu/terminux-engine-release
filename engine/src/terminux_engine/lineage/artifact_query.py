# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Artifact Lineage Query：read-only 的 artifact → producer 查詢。

本模組完全 read-only——查詢與 current verification 都只做
`lstat`／`stat`／`resolve`／streaming hash，絕不修改 artifact，也絕不修改
既有 manifest（verification 結果是回傳值，不回寫證據）。

沒有 LLM。所有結果都是 deterministic 的機械比對。

Surface 選擇：既有的 `llm/tools/query_ledger.py` read path 綁定
`lineage.ledger` 的 `JobCommit`／`ArtifactSnapshot` schema（wrapper／
fake_runner 的世界，identity 是 `job_id`／`stage`），與 artifact lineage 的
`ArtifactManifest`／Kernel `run_id`／`step_id`／`attempt` 是不同 schema，
無法 backward-compatible 地共用同一個 read path。因此這裡提供 internal
read-only service，**不新增任何 Agent `ToolSchema`**，也不動既有
`query_ledger`。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from terminux_engine.lineage.artifact_collector import (
    DEFAULT_CHUNK_SIZE,
    _default_chunk_reader,
    canonical_boundary,
    observe_path,
)
from terminux_engine.lineage.artifact_index import ArtifactIndexEntry, ArtifactLineageIndex
from terminux_engine.lineage.artifact_manifest import (
    AVAILABILITY_BOUNDARY_REJECTED,
    AVAILABILITY_MISSING,
    AVAILABILITY_PRESENT,
    AVAILABILITY_UNREADABLE,
    AVAILABILITY_UNSUPPORTED_TYPE,
    STABILITY_UNSTABLE,
    ArtifactManifest,
)
from terminux_engine.lineage.artifact_manifest_store import ArtifactManifestStore

VERIFICATION_SAME_CONTENT = "same_content"
VERIFICATION_CHANGED = "changed"
VERIFICATION_MISSING = "missing"
VERIFICATION_UNREADABLE = "unreadable"
VERIFICATION_UNSTABLE = "unstable"
VERIFICATION_BOUNDARY_REJECTED = "boundary_rejected"
VERIFICATION_NO_BASELINE = "no_baseline"

MANIFEST_INTEGRITY_OK = "manifest_hash_matches"
MANIFEST_INTEGRITY_MISMATCH = "manifest_hash_mismatch"
MANIFEST_INTEGRITY_ABSENT = "manifest_absent"


@dataclass(frozen=True)
class ArtifactLineageRecord:
    """一筆 artifact → producer 查詢結果。"""

    artifact_instance_id: str
    content_identity: dict[str, Any] | None
    adapter_role: str
    availability: str
    stability: str
    manifest_id: str
    manifest_reference: str
    manifest_sha256: str
    captured_at: str
    candidate_path: str
    resolved_path: str | None
    producer: dict[str, Any]
    current_verification: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "artifact_instance_id": self.artifact_instance_id,
            "content_identity": self.content_identity,
            "adapter_role": self.adapter_role,
            "availability": self.availability,
            "stability": self.stability,
            "manifest_id": self.manifest_id,
            "manifest_reference": self.manifest_reference,
            "manifest_sha256": self.manifest_sha256,
            "captured_at": self.captured_at,
            "candidate_path": self.candidate_path,
            "resolved_path": self.resolved_path,
            "producer": self.producer,
            "current_verification": self.current_verification,
        }


def _record_from_entry(
    entry: ArtifactIndexEntry, verification: dict[str, Any] | None
) -> ArtifactLineageRecord:
    return ArtifactLineageRecord(
        artifact_instance_id=entry.artifact_instance_id,
        content_identity=(
            {"algorithm": "sha256", "digest": entry.content_sha256}
            if entry.content_sha256 is not None
            else None
        ),
        adapter_role=entry.adapter_role,
        availability=entry.availability,
        stability=entry.stability,
        manifest_id=entry.manifest_id,
        manifest_reference=entry.manifest_reference,
        manifest_sha256=entry.manifest_sha256,
        captured_at=entry.captured_at,
        candidate_path=entry.candidate_path,
        resolved_path=entry.resolved_path,
        producer=dict(entry.producer),
        current_verification=verification,
    )


def _sort_key(entry: ArtifactIndexEntry) -> tuple[str, str, str]:
    """明確排序規則：`captured_at` 由新到舊，同時間再以 manifest_id、
    instance id 作為穩定 tie-break（呼叫端以 `reverse=True` 取新→舊）。"""
    return (entry.captured_at, entry.manifest_id, entry.artifact_instance_id)


class ArtifactLineageQuery:
    """Read-only lineage query service。"""

    def __init__(
        self,
        *,
        store: ArtifactManifestStore,
        index: ArtifactLineageIndex,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_reader: Callable[[Path, int], Iterable[bytes]] = _default_chunk_reader,
    ) -> None:
        self._store = store
        self._index = index
        self._chunk_size = chunk_size
        self._chunk_reader = chunk_reader

    # ── 查詢 ────────────────────────────────────────────────────────────

    def by_instance(
        self, artifact_instance_id: str, *, verify: bool = False
    ) -> ArtifactLineageRecord | None:
        for entry in self._index.iter_entries():
            if entry.artifact_instance_id == artifact_instance_id:
                return _record_from_entry(
                    entry, self._verify_entry(entry) if verify else None
                )
        return None

    def by_path(
        self, path: str | Path, *, latest_only: bool = False, verify: bool = False
    ) -> list[ArtifactLineageRecord]:
        """以 exact captured path 或 resolved path 反查（新→舊）。"""
        target = str(path)
        resolved_target = str(canonical_boundary(path))
        matches = [
            entry
            for entry in self._index.iter_entries()
            if entry.candidate_path == target
            or entry.resolved_path == target
            or entry.resolved_path == resolved_target
        ]
        matches.sort(key=_sort_key, reverse=True)
        if latest_only:
            matches = matches[:1]
        return [
            _record_from_entry(entry, self._verify_entry(entry) if verify else None)
            for entry in matches
        ]

    def by_content_sha256(
        self, digest: str, *, verify: bool = False
    ) -> list[ArtifactLineageRecord]:
        """同一 content 的所有 instance——刻意可能回傳多筆。"""
        matches = [
            entry
            for entry in self._index.iter_entries()
            if entry.content_sha256 == digest
        ]
        matches.sort(key=_sort_key, reverse=True)
        return [
            _record_from_entry(entry, self._verify_entry(entry) if verify else None)
            for entry in matches
        ]

    def by_manifest(self, manifest_id: str) -> dict[str, Any] | None:
        """完整 manifest summary（直接讀 immutable evidence，不經 index）。"""
        if not self._store.exists(manifest_id):
            return None
        manifest: ArtifactManifest = self._store.load(manifest_id)
        return {
            "manifest_id": manifest.manifest_id,
            "manifest_reference": str(self._store.path_for(manifest_id)),
            "manifest_sha256": self._store.sha256_of(manifest_id),
            "schema_version": manifest.schema_version,
            "captured_at": manifest.captured_at,
            "capture_status": manifest.capture_status,
            "producer": manifest.producer.to_json_dict(),
            "output_boundaries": list(manifest.output_boundaries),
            "isvn_enrichment_status": manifest.isvn_enrichment_status,
            "observation_count": len(manifest.observations),
            "observations": [obs.to_json_dict() for obs in manifest.observations],
        }

    # ── Integrity／verification ─────────────────────────────────────────

    def verify_manifest_integrity(
        self, manifest_id: str, expected_sha256: str
    ) -> dict[str, Any]:
        """比對 receipt 上記錄的 manifest SHA-256 與磁碟上實際 bytes。"""
        if not self._store.exists(manifest_id):
            return {
                "manifest_id": manifest_id,
                "status": MANIFEST_INTEGRITY_ABSENT,
                "expected_sha256": expected_sha256,
                "actual_sha256": None,
            }
        actual = self._store.sha256_of(manifest_id)
        return {
            "manifest_id": manifest_id,
            "status": (
                MANIFEST_INTEGRITY_OK
                if actual == expected_sha256
                else MANIFEST_INTEGRITY_MISMATCH
            ),
            "expected_sha256": expected_sha256,
            "actual_sha256": actual,
        }

    def _verify_entry(self, entry: ArtifactIndexEntry) -> dict[str, Any]:
        """重新 read-only 觀察目前檔案，與 manifest 記錄比較。

        絕不修改原 manifest——結果只作為回傳值。
        """
        target = Path(entry.resolved_path or entry.candidate_path)
        boundaries = [Path(b) for b in entry.output_boundaries] or [
            canonical_boundary(target.parent)
        ]
        facts = observe_path(
            target,
            boundaries,
            chunk_size=self._chunk_size,
            chunk_reader=self._chunk_reader,
        )

        if facts.availability == AVAILABILITY_MISSING:
            status = VERIFICATION_MISSING
        elif facts.availability == AVAILABILITY_BOUNDARY_REJECTED:
            status = VERIFICATION_BOUNDARY_REJECTED
        elif facts.availability in (
            AVAILABILITY_UNREADABLE,
            AVAILABILITY_UNSUPPORTED_TYPE,
        ):
            status = VERIFICATION_UNREADABLE
        elif facts.stability == STABILITY_UNSTABLE:
            status = VERIFICATION_UNSTABLE
        elif entry.content_sha256 is None:
            # Manifest 當時沒能固定 content identity，現在沒有可比較的基準。
            status = VERIFICATION_NO_BASELINE
        elif facts.availability == AVAILABILITY_PRESENT and facts.digest == entry.content_sha256:
            status = VERIFICATION_SAME_CONTENT
        else:
            status = VERIFICATION_CHANGED

        return {
            "status": status,
            "recorded_sha256": entry.content_sha256,
            "current_sha256": facts.digest,
            "current_availability": facts.availability,
            "current_stability": facts.stability,
            "current_size_bytes": facts.size_bytes,
            "checked_path": str(target),
        }


__all__ = [
    "ArtifactLineageQuery",
    "ArtifactLineageRecord",
    "MANIFEST_INTEGRITY_ABSENT",
    "MANIFEST_INTEGRITY_MISMATCH",
    "MANIFEST_INTEGRITY_OK",
    "VERIFICATION_BOUNDARY_REJECTED",
    "VERIFICATION_CHANGED",
    "VERIFICATION_MISSING",
    "VERIFICATION_NO_BASELINE",
    "VERIFICATION_SAME_CONTENT",
    "VERIFICATION_UNREADABLE",
    "VERIFICATION_UNSTABLE",
]
