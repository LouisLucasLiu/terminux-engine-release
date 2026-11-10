# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Manifest 驗證與 EvidenceRef 建立。

**Manifest 是 artifact 的唯一權威**：本模組只讀 manifest
登記、且 **current content hash 仍相符**的 artifact。cwd 掃描、固定路徑撿檔
一律不做；current hash 不符時**連讀都不讀**。

Excerpt 只保留支持某個 claim 的短片段，line range 為 1-based inclusive；
空檔案不虛構 line range。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.lineage.artifact_manifest import (
    AVAILABILITY_PRESENT,
    STABILITY_STABLE,
    ArtifactManifest,
)
from terminux_engine.lineage.artifact_manifest_store import manifest_sha256
from terminux_engine.packages.orfs_run_intelligence.schemas import EvidenceRef, sha256_text

#: 單一 excerpt 與整份 context 的預算（依實際 artifact 大小訂定）。
MAX_EXCERPT_LINES = 40
MAX_EXCERPT_BYTES = 4_000
MAX_ARTIFACT_BYTES = 64_000
MAX_TOTAL_CONTEXT_BYTES = 24_000

#: 讀進來當文字處理的上限；超過或含 NUL 一律拒讀（避免誤讀 GDS／ODB）。
_MAX_TEXT_BYTES = 8 * 1024 * 1024


class ManifestVerificationError(RuntimeError):
    """Manifest 不存在、hash 不符、schema 不支援或 producer 不一致。"""


@dataclass(frozen=True)
class VerifiedArtifact:
    adapter_role: str
    artifact_instance_id: str
    resolved_path: str
    content_sha256: str | None
    size_bytes: int | None
    current_content_verified: bool
    lines: list[str]


@dataclass(frozen=True)
class VerifiedManifest:
    manifest: ArtifactManifest
    manifest_id: str
    manifest_sha256: str
    manifest_reference: str
    capture_status: str
    artifacts: dict[str, VerifiedArtifact]
    unreadable_roles: dict[str, str]


def load_verified_manifest(
    manifest_reference: str,
    *,
    expected_run_id: str,
    text_roles: tuple[str, ...],
) -> VerifiedManifest:
    """讀回 manifest，驗證身份，並只載入 hash 仍相符的文字 artifact。"""
    path = Path(manifest_reference)
    if not path.is_file():
        raise ManifestVerificationError(f"manifest reference is not a file: {path}")

    payload = path.read_bytes()
    actual_sha = manifest_sha256(payload)
    import json

    try:
        manifest = ArtifactManifest.from_json_dict(json.loads(payload.decode("utf-8")))
    except Exception as exc:  # noqa: BLE001
        raise ManifestVerificationError(f"manifest is unreadable: {exc!r}") from exc

    if manifest.producer.run_id != expected_run_id:
        raise ManifestVerificationError(
            f"manifest producer run_id {manifest.producer.run_id} != DQE run {expected_run_id}"
        )

    boundaries = [Path(b) for b in manifest.output_boundaries]
    artifacts: dict[str, VerifiedArtifact] = {}
    unreadable: dict[str, str] = {}

    for observation in manifest.observations:
        role = observation.adapter_role
        if role not in text_roles:
            continue
        if observation.availability != AVAILABILITY_PRESENT:
            unreadable[role] = f"availability:{observation.availability}"
            continue
        if observation.stability != STABILITY_STABLE:
            unreadable[role] = f"stability:{observation.stability}"
            continue
        if observation.resolved_path is None:
            unreadable[role] = "unresolved_path"
            continue
        artifact_path = Path(observation.resolved_path)
        if not any(_within(artifact_path, b) for b in boundaries):
            unreadable[role] = "outside_output_boundary"
            continue
        if not artifact_path.is_file():
            unreadable[role] = "missing_on_disk"
            continue

        expected_digest = (
            observation.content_identity.digest
            if observation.content_identity is not None
            else None
        )
        try:
            raw = artifact_path.read_bytes()
        except OSError as exc:
            unreadable[role] = f"read_error:{exc.__class__.__name__}"
            continue
        if len(raw) > _MAX_TEXT_BYTES or b"\x00" in raw:
            unreadable[role] = "not_text"
            continue

        current_digest = hashlib.sha256(raw).hexdigest()
        if expected_digest is not None and current_digest != expected_digest:
            # hash 不符就不使用內容。
            unreadable[role] = "content_hash_drift"
            continue

        lines = raw.decode("utf-8", errors="replace").splitlines()
        artifacts[role] = VerifiedArtifact(
            adapter_role=role,
            artifact_instance_id=observation.artifact_instance_id,
            resolved_path=str(artifact_path),
            content_sha256=expected_digest,
            size_bytes=observation.size_bytes,
            current_content_verified=True,
            lines=lines,
        )

    return VerifiedManifest(
        manifest=manifest,
        manifest_id=manifest.manifest_id,
        manifest_sha256=actual_sha,
        manifest_reference=str(path),
        capture_status=manifest.capture_status,
        artifacts=artifacts,
        unreadable_roles=unreadable,
    )


def _within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def make_evidence_ref(
    ref_id: str,
    artifact: VerifiedArtifact,
    manifest_id: str,
    *,
    section: str | None,
    line_start: int | None,
    line_end: int | None,
) -> EvidenceRef:
    """由 1-based inclusive line range 建立可重算的 EvidenceRef。

    空檔案（無任何行）→ line range 為 `None`，excerpt 為空字串，**不虛構**。
    """
    if not artifact.lines or line_start is None or line_end is None:
        return EvidenceRef(
            evidence_ref_id=ref_id,
            artifact_manifest_id=manifest_id,
            artifact_instance_id=artifact.artifact_instance_id,
            adapter_role=artifact.adapter_role,
            content_sha256=artifact.content_sha256,
            section=section,
            line_start=None,
            line_end=None,
            excerpt_hash=None,
            excerpt_text="",
            current_content_verified=artifact.current_content_verified,
        )

    start = max(1, line_start)
    end = min(len(artifact.lines), max(start, line_end))
    end = min(end, start + MAX_EXCERPT_LINES - 1)
    excerpt = "\n".join(artifact.lines[start - 1 : end])
    if len(excerpt.encode("utf-8")) > MAX_EXCERPT_BYTES:
        excerpt = excerpt.encode("utf-8")[:MAX_EXCERPT_BYTES].decode("utf-8", "ignore")
    return EvidenceRef(
        evidence_ref_id=ref_id,
        artifact_manifest_id=manifest_id,
        artifact_instance_id=artifact.artifact_instance_id,
        adapter_role=artifact.adapter_role,
        content_sha256=artifact.content_sha256,
        section=section,
        line_start=start,
        line_end=end,
        excerpt_hash=sha256_text(excerpt),
        excerpt_text=excerpt,
        current_content_verified=artifact.current_content_verified,
    )


def recompute_excerpt_hash(artifact_lines: list[str], ref: EvidenceRef) -> str | None:
    """驗算用：由原始行重算 excerpt hash。"""
    if ref.line_start is None or ref.line_end is None:
        return None
    excerpt = "\n".join(artifact_lines[ref.line_start - 1 : ref.line_end])
    return sha256_text(excerpt)


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_EXCERPT_BYTES",
    "MAX_EXCERPT_LINES",
    "MAX_TOTAL_CONTEXT_BYTES",
    "ManifestVerificationError",
    "VerifiedArtifact",
    "VerifiedManifest",
    "load_verified_manifest",
    "make_evidence_ref",
    "recompute_excerpt_hash",
]
