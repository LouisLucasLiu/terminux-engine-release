# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Artifact Lineage Index：manifest 的**可重建 projection**。

這不是第三套 ledger：它不保存任何 manifest 沒有的資訊，不擁有 execution
lifecycle（那永遠屬於 `orchestration.ledger.OrchestrationLedger`），也不是
artifact observation 的唯一保存地——整份 index 隨時可以由 immutable
manifests 重建（`rebuild_from_store()`）。刪掉 index 只損失查詢速度，不
損失任何證據。

儲存形式刻意選 append-only JSONL 而非新的 SQLite：JSONL 明確表達
「這是投影，不是 authority」，也與既有 `lineage/ledger.py` 的 JSONL 慣例
一致。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from terminux_engine.lineage.artifact_manifest import ArtifactManifest
from terminux_engine.lineage.artifact_manifest_store import ArtifactManifestStore

INDEX_FILENAME = "projection.jsonl"


@dataclass(frozen=True)
class ArtifactIndexEntry:
    """單一 observation 的投影列。"""

    artifact_instance_id: str
    manifest_id: str
    manifest_reference: str
    manifest_sha256: str
    capture_status: str
    captured_at: str
    adapter_role: str
    candidate_path: str
    resolved_path: str | None
    availability: str
    stability: str
    file_type: str
    size_bytes: int | None
    content_sha256: str | None
    producer: dict[str, Any]
    output_boundaries: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "artifact_instance_id": self.artifact_instance_id,
            "manifest_id": self.manifest_id,
            "manifest_reference": self.manifest_reference,
            "manifest_sha256": self.manifest_sha256,
            "capture_status": self.capture_status,
            "captured_at": self.captured_at,
            "adapter_role": self.adapter_role,
            "candidate_path": self.candidate_path,
            "resolved_path": self.resolved_path,
            "availability": self.availability,
            "stability": self.stability,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "content_sha256": self.content_sha256,
            "producer": self.producer,
            "output_boundaries": list(self.output_boundaries),
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "ArtifactIndexEntry":
        return cls(
            artifact_instance_id=payload["artifact_instance_id"],
            manifest_id=payload["manifest_id"],
            manifest_reference=payload["manifest_reference"],
            manifest_sha256=payload["manifest_sha256"],
            capture_status=payload["capture_status"],
            captured_at=payload["captured_at"],
            adapter_role=payload["adapter_role"],
            candidate_path=payload["candidate_path"],
            resolved_path=payload.get("resolved_path"),
            availability=payload["availability"],
            stability=payload["stability"],
            file_type=payload["file_type"],
            size_bytes=payload.get("size_bytes"),
            content_sha256=payload.get("content_sha256"),
            producer=payload["producer"],
            output_boundaries=list(payload.get("output_boundaries") or []),
        )


def project_manifest(
    manifest: ArtifactManifest,
    *,
    manifest_reference: str,
    manifest_sha256: str,
) -> list[ArtifactIndexEntry]:
    """把一份 manifest 投影成 index entries——純函式，無 I/O。"""
    return [
        ArtifactIndexEntry(
            artifact_instance_id=obs.artifact_instance_id,
            manifest_id=manifest.manifest_id,
            manifest_reference=manifest_reference,
            manifest_sha256=manifest_sha256,
            capture_status=manifest.capture_status,
            captured_at=manifest.captured_at,
            adapter_role=obs.adapter_role,
            candidate_path=obs.candidate_path,
            resolved_path=obs.resolved_path,
            availability=obs.availability,
            stability=obs.stability,
            file_type=obs.file_type,
            size_bytes=obs.size_bytes,
            content_sha256=(
                obs.content_identity.digest if obs.content_identity is not None else None
            ),
            producer=obs.producer.to_json_dict(),
            output_boundaries=list(manifest.output_boundaries),
        )
        for obs in manifest.observations
    ]


class ArtifactLineageIndex:
    """Append-only JSONL projection，可由 manifests 完整重建。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._index_dir = self._root / "index"
        self._path = self._index_dir / INDEX_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def record_manifest(
        self,
        manifest: ArtifactManifest,
        *,
        manifest_reference: str,
        manifest_sha256: str,
    ) -> int:
        """把一份 manifest 的所有 observation 追加進 projection。"""
        entries = project_manifest(
            manifest,
            manifest_reference=manifest_reference,
            manifest_sha256=manifest_sha256,
        )
        self._append(entries)
        return len(entries)

    def _append(self, entries: list[ArtifactIndexEntry]) -> None:
        if not entries:
            return
        self._index_dir.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(
                    json.dumps(entry.to_json_dict(), sort_keys=True, ensure_ascii=False)
                    + "\n"
                )

    def entries(self) -> list[ArtifactIndexEntry]:
        return list(self.iter_entries())

    def iter_entries(self) -> Iterator[ArtifactIndexEntry]:
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                yield ArtifactIndexEntry.from_json_dict(json.loads(line))

    def rebuild_from_store(self, store: ArtifactManifestStore) -> int:
        """由 immutable manifests 完整重建 projection。

        先寫到 temporary file 再 `os.replace`——重建失敗時舊 projection
        保持完整，不會留下半截檔案。
        """
        self._index_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._index_dir), suffix=".rebuild")
        tmp_path = Path(tmp_name)
        count = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for manifest_id in store.iter_manifest_ids():
                    manifest = store.load(manifest_id)
                    entries = project_manifest(
                        manifest,
                        manifest_reference=str(store.path_for(manifest_id)),
                        manifest_sha256=store.sha256_of(manifest_id),
                    )
                    for entry in entries:
                        handle.write(
                            json.dumps(
                                entry.to_json_dict(), sort_keys=True, ensure_ascii=False
                            )
                            + "\n"
                        )
                        count += 1
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        return count


__all__ = [
    "ArtifactIndexEntry",
    "ArtifactLineageIndex",
    "INDEX_FILENAME",
    "project_manifest",
]
