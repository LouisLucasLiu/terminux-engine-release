# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import hashlib
import time
from pathlib import Path

from terminux_engine.lineage.models import ArtifactSnapshot
from terminux_engine.lineage.types import ANCHOR_KEYS

_CHUNK_SIZE = 65536


def build_snapshot(
    path: str | Path,
    role: str,
    observed_at_epoch: float | None = None,
) -> ArtifactSnapshot:
    """
    計算 sha256、size_bytes、modified_time_epoch，回傳 ArtifactSnapshot。
    path 不存在 → raise FileNotFoundError（不捕獲）。
    lineage_anchors / lineage_source / producer_* 保持預設值。
    """
    p = Path(path)
    stat = p.stat()  # 不存在時自然拋出 FileNotFoundError

    # 計算 sha256
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)

    return ArtifactSnapshot(
        role=role,
        path=str(p),
        checksum_sha256=h.hexdigest(),
        size_bytes=stat.st_size,
        modified_time_epoch=stat.st_mtime,
        lineage_anchors={k: [] for k in ANCHOR_KEYS},
        lineage_source="root_observation",
        producer_job_id=None,
        producer_stage=None,
    )
