# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""把 EventStore 的 timing path record 綁回**已驗證的 report 內容**。

信任鏈：caller 本人的 receipt 投影（run／attempt／manifest id／manifest sha）→ receipt 指向的
manifest 檔（bytes 重算 sha 必須等於 receipt 記錄值）→ manifest 內與 record 宣告相同
`artifact_instance_id` 的 observation（present、stable、有 content digest）→ 該 observation
在 manifest 宣告的 output boundary 內的 report 檔（一次讀取、重算 sha 必須等於 digest）→ 以
tool adapter 注入的 reparser 從這份 bytes 重新解析 → 重解析結果必須與 record 內容逐欄相同。

通過後回傳**重解析得到的**證據（不是 record 自述的），特徵由它計算。Record 只是指標；
它自填的 digest、stages 或 features 沒有一項被當作真實性的證明。任何一環失敗都回
封閉的原因碼，呼叫端據此把 section 標成 `unavailable`。

本模組是 Platform：不認識任何 report 格式，reparser 由 adapter 經 `reparsers` 對應表注入。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from terminux_engine.lineage.artifact_manifest import (
    AVAILABILITY_PRESENT,
    STABILITY_STABLE,
    ArtifactManifest,
)
from terminux_engine.timing_evidence.model import (
    PathCompleteness,
    PathEndpoints,
    PathScope,
    PathSource,
    PathStage,
    PathTiming,
    TimingEvidenceError,
    TimingPathEvidence,
)

#: manifest 與 report 的讀取上限（report 上限與 adapter 的文字 parser 一致）。
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024

REASON_MANIFEST_UNREADABLE = "manifest_unreadable"
REASON_MANIFEST_IDENTITY_MISMATCH = "manifest_identity_mismatch"
REASON_OBSERVATION_MISSING = "observation_missing"
REASON_OBSERVATION_NOT_STABLE = "observation_not_stable"
REASON_REPORT_OUTSIDE_BOUNDARY = "report_outside_boundary"
REASON_REPORT_UNREADABLE = "report_unreadable"
REASON_REPORT_CONTENT_MISMATCH = "report_content_mismatch"
REASON_REPARSER_UNAVAILABLE = "reparser_unavailable"
REASON_REPORT_REPARSE_FAILED = "report_reparse_failed"
REASON_RECORD_CONTENT_MISMATCH = "record_content_mismatch"


class ParsedPathLike(Protocol):
    """adapter reparser 的回傳形狀：解析結果加上 section 身分與 excerpt hash。"""

    scope: PathScope
    endpoints: PathEndpoints
    timing: PathTiming
    stages: tuple[PathStage, ...]
    completeness: PathCompleteness
    section: str
    section_kind: str
    line_start: int
    line_end: int
    excerpt_hash: str


Reparser = Callable[[list[str]], ParsedPathLike]


@dataclass(frozen=True)
class VerificationResult:
    """`evidence` 是重解析得到的可信證據；失敗時為 None 並附原因碼。"""

    evidence: TimingPathEvidence | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.evidence is not None


def _read_regular_file(path: Path, limit: int) -> bytes | None:
    """不跟 symlink、只接受普通檔、一次讀完並檢查上限；任何失敗回 None。"""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _within_boundary(path: Path, boundaries: list[str]) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    for boundary in boundaries:
        try:
            resolved.relative_to(Path(boundary).resolve())
            return True
        except (OSError, ValueError, RuntimeError):
            continue
    return False


def verify_record_against_manifest(
    record: TimingPathEvidence, *, manifest_reference: str | None, manifest_id: str,
    manifest_sha256: str, reparsers: Mapping[str, Reparser],
) -> VerificationResult:
    """以 receipt 指向的 manifest 與其 report 內容驗證 record；成功回傳重解析的證據。"""
    if not manifest_reference or not isinstance(manifest_reference, str):
        return VerificationResult(None, REASON_MANIFEST_UNREADABLE)
    raw_manifest = _read_regular_file(Path(manifest_reference), MAX_MANIFEST_BYTES)
    if raw_manifest is None or hashlib.sha256(raw_manifest).hexdigest() != manifest_sha256:
        return VerificationResult(None, REASON_MANIFEST_UNREADABLE)
    try:
        manifest = ArtifactManifest.from_json_dict(json.loads(raw_manifest.decode("utf-8")))
    except Exception:  # noqa: BLE001 - manifest 任何解析失敗都是不可讀
        return VerificationResult(None, REASON_MANIFEST_UNREADABLE)
    source = record.source
    if (manifest.manifest_id != manifest_id or source.artifact_manifest_id != manifest_id
            or source.artifact_manifest_sha256 != manifest_sha256
            or manifest.producer.run_id != source.run_id or manifest.producer.attempt != source.attempt):
        return VerificationResult(None, REASON_MANIFEST_IDENTITY_MISMATCH)

    observation = next((obs for obs in manifest.observations
                        if obs.artifact_instance_id == source.artifact_instance_id
                        and obs.adapter_role == source.adapter_role), None)
    if observation is None:
        return VerificationResult(None, REASON_OBSERVATION_MISSING)
    if (observation.availability != AVAILABILITY_PRESENT or observation.stability != STABILITY_STABLE
            or observation.content_identity is None or observation.resolved_path is None):
        return VerificationResult(None, REASON_OBSERVATION_NOT_STABLE)
    digest = observation.content_identity.digest
    if source.report_content_sha256 != digest:
        return VerificationResult(None, REASON_REPORT_CONTENT_MISMATCH)

    report_path = Path(observation.resolved_path)
    if not _within_boundary(report_path, list(manifest.output_boundaries)):
        return VerificationResult(None, REASON_REPORT_OUTSIDE_BOUNDARY)
    raw_report = _read_regular_file(report_path, MAX_REPORT_BYTES)
    if raw_report is None or b"\x00" in raw_report:
        return VerificationResult(None, REASON_REPORT_UNREADABLE)
    if hashlib.sha256(raw_report).hexdigest() != digest:
        return VerificationResult(None, REASON_REPORT_CONTENT_MISMATCH)

    reparser = reparsers.get(source.adapter_role)
    if reparser is None:
        return VerificationResult(None, REASON_REPARSER_UNAVAILABLE)
    try:
        parsed = reparser(raw_report.decode("utf-8", errors="replace").splitlines())
        trusted = TimingPathEvidence(
            source=PathSource(
                run_id=source.run_id, attempt=source.attempt, step_id=source.step_id,
                artifact_manifest_id=manifest_id, artifact_manifest_sha256=manifest_sha256,
                artifact_instance_id=observation.artifact_instance_id,
                adapter_role=observation.adapter_role, report_content_sha256=digest,
                section=parsed.section, section_kind=parsed.section_kind,
                line_start=parsed.line_start, line_end=parsed.line_end,
                excerpt_hash=parsed.excerpt_hash,
            ),
            scope=parsed.scope, endpoints=parsed.endpoints, timing=parsed.timing,
            stages=tuple(parsed.stages), completeness=parsed.completeness,
        )
    except (TimingEvidenceError, Exception):  # noqa: BLE001 - 重解析失敗一律不可用
        return VerificationResult(None, REASON_REPORT_REPARSE_FAILED)
    if trusted.to_json_dict() != record.to_json_dict():
        return VerificationResult(None, REASON_RECORD_CONTENT_MISMATCH)
    return VerificationResult(trusted, None)


__all__ = [
    "MAX_MANIFEST_BYTES", "MAX_REPORT_BYTES", "ParsedPathLike", "REASON_MANIFEST_IDENTITY_MISMATCH",
    "REASON_MANIFEST_UNREADABLE", "REASON_OBSERVATION_MISSING", "REASON_OBSERVATION_NOT_STABLE",
    "REASON_RECORD_CONTENT_MISMATCH", "REASON_REPARSER_UNAVAILABLE", "REASON_REPORT_CONTENT_MISMATCH",
    "REASON_REPORT_OUTSIDE_BOUNDARY", "REASON_REPORT_REPARSE_FAILED", "REASON_REPORT_UNREADABLE",
    "Reparser", "VerificationResult", "verify_record_against_manifest",
]
