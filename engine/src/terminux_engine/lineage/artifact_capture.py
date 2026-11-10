# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Artifact Capture Service：Kernel 唯一需要認識的 capture facade。

把 `ArtifactCollector`（觀察）→ `ArtifactManifestStore`（immutable
evidence）→ `ArtifactLineageIndex`（可重建 projection）串成一次呼叫，
並回傳 Receipt／TimelineEvent 需要的中性 metadata。

Capture timing：本 service 由 Kernel 在「subprocess 結束、
adapter discovery 之後、Package dispatch 之前」呼叫，確保 Package 讀到的
是已持久化的 manifest，而不是自己重新猜 artifact 路徑。

capture 的 partial／failed **不得**改寫 process 事實。
本 service 從不拋出例外到 Kernel——任何失敗都轉成
`ArtifactCaptureResult(capture_status="failed", ...)`，讓 execution／
dispatch／evidence 狀態各自獨立。Manifest 成功但 index 失敗時，manifest
仍是 source of evidence，`index_status` 獨立記錄，projection 之後可
`rebuild_from_store()` 修復。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from terminux_engine.lineage.artifact_collector import ArtifactCollector
from terminux_engine.lineage.artifact_index import ArtifactLineageIndex
from terminux_engine.lineage.artifact_manifest import (
    CAPTURE_STATUS_FAILED,
    ProducerExecutionReference,
)
from terminux_engine.lineage.artifact_manifest_store import ArtifactManifestStore

_LOG = logging.getLogger(__name__)

INDEX_RECORDED = "recorded"
INDEX_FAILED = "failed"
INDEX_SKIPPED = "skipped"


@dataclass(frozen=True)
class ArtifactCaptureResult:
    """Capture 的中性結果，供 Receipt 與 TimelineEvent 攜帶。"""

    capture_status: str
    manifest_id: str | None
    manifest_reference: str | None
    manifest_sha256: str | None
    index_status: str
    observation_count: int
    error: str | None = None

    def to_event_metadata(self) -> dict[str, Any]:
        """TimelineEvent.metadata 用的中性 payload。

        只有 manifest 的 id／reference／hash／capture status——不含任何
        role 或 content，Core 不解讀 manifest 內容。
        """
        return {
            "artifact_manifest_id": self.manifest_id,
            "artifact_manifest_reference": self.manifest_reference,
            "artifact_manifest_sha256": self.manifest_sha256,
            "artifact_capture_status": self.capture_status,
        }


class ArtifactCaptureService:
    """Collector＋Store＋Index 的組合；Kernel 只依賴這一個介面。"""

    def __init__(
        self,
        *,
        store: ArtifactManifestStore,
        index: ArtifactLineageIndex | None = None,
        collector: ArtifactCollector | None = None,
        isvn_resolver: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self._store = store
        self._index = index if index is not None else ArtifactLineageIndex(store.root)
        self._collector = collector if collector is not None else ArtifactCollector()
        self._isvn_resolver = isvn_resolver

    @classmethod
    def with_default_root(
        cls, root: str | Path | None = None
    ) -> "ArtifactCaptureService":
        """以既有 Terminux durable state root 建立 service。

        `root=None` 時使用 `ArtifactManifestStore.DEFAULT_ROOT`
        （`~/.terminux/lineage/artifact_manifests`）——與既有
        orchestration ledger／lineage ledger 同一個 durable root，永遠不
        落在 tool repo、source tree 或 cwd。
        """
        store = ArtifactManifestStore(root)
        return cls(store=store, index=ArtifactLineageIndex(store.root))

    @property
    def store(self) -> ArtifactManifestStore:
        return self._store

    @property
    def index(self) -> ArtifactLineageIndex:
        return self._index

    def capture(
        self,
        candidates: Sequence[Any],
        *,
        producer: ProducerExecutionReference,
        output_boundaries: Sequence[str | Path],
    ) -> ArtifactCaptureResult:
        """觀察 → 持久化 → 投影。永不對呼叫端拋出例外。"""
        try:
            manifest = self._collector.capture(
                candidates,
                producer=producer,
                output_boundaries=output_boundaries,
                isvn_resolver=self._isvn_resolver,
            )
        except Exception as exc:  # noqa: BLE001 - capture 失敗不得中斷 execution
            _LOG.warning("artifact capture failed for run %s: %r", producer.run_id, exc)
            return ArtifactCaptureResult(
                capture_status=CAPTURE_STATUS_FAILED,
                manifest_id=None,
                manifest_reference=None,
                manifest_sha256=None,
                index_status=INDEX_SKIPPED,
                observation_count=0,
                error=repr(exc),
            )

        try:
            persisted = self._store.persist(manifest)
        except Exception as exc:  # noqa: BLE001 - 持久化失敗即 capture failed
            _LOG.warning(
                "artifact manifest persistence failed for run %s: %r",
                producer.run_id,
                exc,
            )
            return ArtifactCaptureResult(
                capture_status=CAPTURE_STATUS_FAILED,
                manifest_id=manifest.manifest_id,
                manifest_reference=None,
                manifest_sha256=None,
                index_status=INDEX_SKIPPED,
                observation_count=len(manifest.observations),
                error=repr(exc),
            )

        index_status = INDEX_RECORDED
        index_error: str | None = None
        try:
            self._index.record_manifest(
                manifest,
                manifest_reference=persisted.reference,
                manifest_sha256=persisted.sha256,
            )
        except Exception as exc:  # noqa: BLE001 - manifest 仍是證據
            _LOG.warning(
                "artifact lineage index write failed for manifest %s: %r",
                manifest.manifest_id,
                exc,
            )
            index_status = INDEX_FAILED
            index_error = repr(exc)

        return ArtifactCaptureResult(
            capture_status=manifest.capture_status,
            manifest_id=persisted.manifest_id,
            manifest_reference=persisted.reference,
            manifest_sha256=persisted.sha256,
            index_status=index_status,
            observation_count=len(manifest.observations),
            error=index_error,
        )


__all__ = [
    "ArtifactCaptureResult",
    "ArtifactCaptureService",
    "INDEX_FAILED",
    "INDEX_RECORDED",
    "INDEX_SKIPPED",
]
