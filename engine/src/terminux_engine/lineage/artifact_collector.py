# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Artifact Collector：domain-blind、read-only 的 artifact 觀察器。

職責只有一件事：把 adapter 回報的 `ArtifactCandidate`（只是「發現提示」）
重新以客觀方式檢查一次，產生 immutable `ArtifactManifest`。

No artifact mutation：本模組只允許 `lstat`／`stat`／`resolve`／
streaming read／hash。不修改 bytes、不 rename／move／copy、不 chmod／chown、
不刪除、不產生任何 domain report。

Vendor-neutral：不含 Docker／ORFS／GDS／DEF／ODB／timing／DRC 的
任何名稱或語意。`adapter_role` 只是被原樣攜帶的 opaque 字串。

刻意**不** import `orchestration.execution_contract`：`kernel.py` 會 import
本模組，反向 import 會造成 circular import。Candidate 以 duck-typing 接受
（只需要 `.adapter_role` 與 `.host_path`），與
`execution_contract.build_receipt()` 對 `StepOutcome` 的作法一致。
"""

from __future__ import annotations

import hashlib
import stat as stat_module
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from terminux_engine.lineage.artifact_manifest import (
    AVAILABILITY_BOUNDARY_REJECTED,
    AVAILABILITY_MISSING,
    AVAILABILITY_PRESENT,
    AVAILABILITY_UNREADABLE,
    AVAILABILITY_UNSUPPORTED_TYPE,
    CAPTURE_STATUS_COMPLETE,
    CAPTURE_STATUS_PARTIAL,
    FILE_TYPE_DIRECTORY,
    FILE_TYPE_MISSING,
    FILE_TYPE_OTHER,
    FILE_TYPE_REGULAR,
    FILE_TYPE_SYMLINK,
    HASH_ALGORITHM_SHA256,
    MANIFEST_SCHEMA_VERSION,
    STABILITY_NOT_APPLICABLE,
    STABILITY_STABLE,
    STABILITY_UNSTABLE,
    ArtifactManifest,
    ArtifactManifestError,
    ArtifactObservation,
    ContentIdentity,
    ProducerExecutionReference,
)

DEFAULT_CHUNK_SIZE = 1024 * 1024

ISVN_ENRICHMENT_NOT_ATTEMPTED = "not_attempted"
ISVN_ENRICHMENT_RESOLVED = "resolved"
ISVN_ENRICHMENT_UNAVAILABLE = "unavailable"


class OutputBoundaryError(ValueError):
    """Output boundary 過寬或不合法；capture 必須 fail-closed，不得放行。"""


def canonical_boundary(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def validate_output_boundary(path: str | Path) -> Path:
    """驗證一個 output boundary 不是「過寬位置」。

    拒絕 filesystem root、使用者 home 本身，以及任何是 home 祖先的路徑
    （例如 `/`、`/home`）——這些都寬到足以讓 capture 讀到 credential 或
    無關的使用者資料。
    """
    resolved = canonical_boundary(path)
    home = Path.home().resolve()
    if resolved == Path(resolved.anchor):
        raise OutputBoundaryError(f"output boundary is the filesystem root: {resolved}")
    if resolved == home or home.is_relative_to(resolved):
        raise OutputBoundaryError(
            f"output boundary is too broad (home or an ancestor of home): {resolved}"
        )
    return resolved


def is_within_boundaries(candidate: Path, boundaries: Sequence[Path]) -> bool:
    """結構化 boundary 檢查——用 path component 比較，不用字串 prefix。

    純字串 prefix 會把 `/a/bc` 誤判為在 `/a/b` 內；`Path.relative_to()`
    以 path component 比較，`/a/bc` 不會被接受。
    """
    for boundary in boundaries:
        try:
            candidate.relative_to(boundary)
        except ValueError:
            continue
        return True
    return False


def _default_chunk_reader(path: Path, chunk_size: int) -> Iterator[bytes]:
    """預設 streaming reader——固定 chunk，永不整檔載入記憶體。"""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


@dataclass(frozen=True)
class PathFacts:
    """對單一 path 的一次 read-only 觀察結果（機械事實，無 domain 判斷）。"""

    resolved_path: str | None
    file_type: str
    availability: str
    stability: str
    size_bytes: int | None
    mtime_ns: int | None
    digest: str | None
    capture_error: dict[str, Any] | None
    symlink_target: str | None


def _stat_identity(st: Any) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _file_type_from_mode(mode: int) -> str:
    if stat_module.S_ISREG(mode):
        return FILE_TYPE_REGULAR
    if stat_module.S_ISDIR(mode):
        return FILE_TYPE_DIRECTORY
    return FILE_TYPE_OTHER


def observe_path(
    candidate_path: Path,
    boundaries: Sequence[Path],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_reader: Callable[[Path, int], Iterable[bytes]] = _default_chunk_reader,
) -> PathFacts:
    """對一個 candidate path 做完整的 read-only 觀察。

    順序：保存 candidate path → `lstat` 取得原始 type → resolve →
    boundary 檢查 → 只對 boundary 內的 regular file 做 streaming hash，
    並在 hash 前後各做一次 stat 比對穩定性。
    """
    symlink_target: str | None = None

    # 1. lstat：不跟隨 symlink，取得 candidate 位置本身的原始型別。
    try:
        lst = candidate_path.lstat()
    except FileNotFoundError:
        return PathFacts(
            resolved_path=None,
            file_type=FILE_TYPE_MISSING,
            availability=AVAILABILITY_MISSING,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=None,
            mtime_ns=None,
            digest=None,
            capture_error=None,
            symlink_target=None,
        )
    except OSError as exc:
        return PathFacts(
            resolved_path=None,
            file_type=FILE_TYPE_OTHER,
            availability=AVAILABILITY_UNREADABLE,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=None,
            mtime_ns=None,
            digest=None,
            capture_error={"kind": "lstat_failed", "detail": repr(exc)},
            symlink_target=None,
        )

    is_symlink = stat_module.S_ISLNK(lst.st_mode)
    declared_type = FILE_TYPE_SYMLINK if is_symlink else _file_type_from_mode(lst.st_mode)

    # 2. resolve target（symlink 也在此解析；後續 boundary 檢查針對 target，
    #    確保絕不跟隨到 boundary 外）。
    try:
        resolved = candidate_path.resolve()
    except OSError as exc:
        return PathFacts(
            resolved_path=None,
            file_type=declared_type,
            availability=AVAILABILITY_UNREADABLE,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=None,
            mtime_ns=None,
            digest=None,
            capture_error={"kind": "resolve_failed", "detail": repr(exc)},
            symlink_target=None,
        )
    if is_symlink:
        symlink_target = str(resolved)

    # 3. boundary 檢查——界外一律不讀內容。
    if not is_within_boundaries(resolved, boundaries):
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type,
            availability=AVAILABILITY_BOUNDARY_REJECTED,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=None,
            mtime_ns=None,
            digest=None,
            capture_error=None,
            symlink_target=symlink_target,
        )

    # 4. stat（跟隨 symlink）取得 target 的事實。
    try:
        st_before = resolved.stat()
    except FileNotFoundError:
        # Dangling symlink：candidate 本身存在，但 target 不存在。
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type,
            availability=AVAILABILITY_MISSING,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=None,
            mtime_ns=None,
            digest=None,
            capture_error=None,
            symlink_target=symlink_target,
        )
    except OSError as exc:
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type,
            availability=AVAILABILITY_UNREADABLE,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=None,
            mtime_ns=None,
            digest=None,
            capture_error={"kind": "stat_failed", "detail": repr(exc)},
            symlink_target=symlink_target,
        )

    target_type = _file_type_from_mode(st_before.st_mode)
    if target_type != FILE_TYPE_REGULAR:
        # 第一版只對 regular file 計算 content hash。directory／
        # socket／fifo 等一律 unsupported_type，且明確不含 content identity。
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type if is_symlink else target_type,
            availability=AVAILABILITY_UNSUPPORTED_TYPE,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=None,
            mtime_ns=None,
            digest=None,
            capture_error=None,
            symlink_target=symlink_target,
        )

    size_bytes = st_before.st_size
    mtime_ns = st_before.st_mtime_ns

    # 5. streaming SHA-256。0-byte regular file 一樣照算——得到的是
    #    empty bytes 的 SHA-256，不是「無效」判斷。
    digest_obj = hashlib.sha256()
    try:
        for chunk in chunk_reader(resolved, chunk_size):
            digest_obj.update(chunk)
    except FileNotFoundError as exc:
        # 檔案在 hash 過程中消失——留下明確 observation，不讓 Kernel crash。
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type,
            availability=AVAILABILITY_MISSING,
            stability=STABILITY_UNSTABLE,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            digest=None,
            capture_error={"kind": "disappeared_during_hash", "detail": repr(exc)},
            symlink_target=symlink_target,
        )
    except OSError as exc:
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type,
            availability=AVAILABILITY_UNREADABLE,
            stability=STABILITY_NOT_APPLICABLE,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            digest=None,
            capture_error={"kind": "hash_read_failed", "detail": repr(exc)},
            symlink_target=symlink_target,
        )

    # 6. hash 後再 stat 一次；關鍵欄位任一變動即 unstable，content 不採信。
    try:
        st_after = resolved.stat()
    except OSError as exc:
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type,
            availability=AVAILABILITY_PRESENT,
            stability=STABILITY_UNSTABLE,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            digest=None,
            capture_error={"kind": "post_hash_stat_failed", "detail": repr(exc)},
            symlink_target=symlink_target,
        )

    if _stat_identity(st_before) != _stat_identity(st_after):
        return PathFacts(
            resolved_path=str(resolved),
            file_type=declared_type,
            availability=AVAILABILITY_PRESENT,
            stability=STABILITY_UNSTABLE,
            size_bytes=st_after.st_size,
            mtime_ns=st_after.st_mtime_ns,
            digest=None,
            capture_error={
                "kind": "mutated_during_hash",
                "detail": "stat identity changed across the hash window",
            },
            symlink_target=symlink_target,
        )

    return PathFacts(
        resolved_path=str(resolved),
        file_type=declared_type,
        availability=AVAILABILITY_PRESENT,
        stability=STABILITY_STABLE,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        digest=digest_obj.hexdigest(),
        capture_error=None,
        symlink_target=symlink_target,
    )


def _is_partial(facts: PathFacts) -> bool:
    """判斷一個 observation 是否讓整份 manifest 降級為 `partial`。

    刻意**不**把 `missing` 算進去：「確認候選不存在」是一次完整觀察。
    """
    if facts.availability in (
        AVAILABILITY_UNREADABLE,
        AVAILABILITY_BOUNDARY_REJECTED,
        AVAILABILITY_UNSUPPORTED_TYPE,
    ):
        return True
    if facts.stability == STABILITY_UNSTABLE:
        return True
    if facts.capture_error is not None:
        return True
    return False


class ArtifactCollector:
    """把 adapter candidates 轉成 immutable `ArtifactManifest`。"""

    def __init__(
        self,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_reader: Callable[[Path, int], Iterable[bytes]] = _default_chunk_reader,
        clock: Callable[[], datetime] | None = None,
        manifest_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_reader = chunk_reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._manifest_id_factory = manifest_id_factory or (
            lambda: f"am_{uuid.uuid4().hex}"
        )

    def capture(
        self,
        candidates: Sequence[Any],
        *,
        producer: ProducerExecutionReference,
        output_boundaries: Sequence[str | Path],
        manifest_id: str | None = None,
        isvn_resolver: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> ArtifactManifest:
        """對每個 candidate 建立客觀 observation，回傳 immutable manifest。

        `candidates` 是 duck-typed：只需要 `.adapter_role`（opaque 字串）與
        `.host_path`。Adapter 自帶的 `exists` 欄位（若有）**刻意忽略**——
        那只是 discovery 當下的提示，正式 observation 一律由這裡重新檢查
       。
        """
        if not output_boundaries:
            raise OutputBoundaryError("capture requires at least one output boundary")
        boundaries = [validate_output_boundary(b) for b in output_boundaries]

        resolved_manifest_id = manifest_id or self._manifest_id_factory()
        observations: list[ArtifactObservation] = []
        any_partial = False
        isvn_attempted = False
        isvn_resolved_any = False

        for ordinal, candidate in enumerate(candidates):
            candidate_path = Path(candidate.host_path)
            facts = observe_path(
                candidate_path,
                boundaries,
                chunk_size=self._chunk_size,
                chunk_reader=self._chunk_reader,
            )
            if _is_partial(facts):
                any_partial = True

            content_identity = (
                ContentIdentity(algorithm=HASH_ALGORITHM_SHA256, digest=facts.digest)
                if facts.digest is not None
                else None
            )

            isvn_coordinate: dict[str, Any] | None = None
            if isvn_resolver is not None and facts.resolved_path is not None:
                # isvn 是 optional enrichment，絕不可成為 capture
                # 的前提——resolver 失敗只影響 enrichment status。
                isvn_attempted = True
                try:
                    isvn_coordinate = isvn_resolver(facts.resolved_path)
                except Exception:  # noqa: BLE001 - enrichment 不得讓 capture 失敗
                    isvn_coordinate = None
                if isvn_coordinate is not None:
                    isvn_resolved_any = True

            observations.append(
                ArtifactObservation(
                    observation_id=f"obs_{resolved_manifest_id}_{ordinal:03d}",
                    # instance id 由 manifest_id + entry
                    # ordinal 產生，與 content hash 完全分離；同內容不同
                    # path／role 必然得到不同 instance id。
                    artifact_instance_id=f"ai_{resolved_manifest_id}_{ordinal:03d}",
                    adapter_role=candidate.adapter_role,
                    candidate_path=str(candidate_path),
                    resolved_path=facts.resolved_path,
                    file_type=facts.file_type,
                    availability=facts.availability,
                    stability=facts.stability,
                    size_bytes=facts.size_bytes,
                    mtime_ns=facts.mtime_ns,
                    content_identity=content_identity,
                    capture_error=facts.capture_error,
                    producer=producer,
                    isvn_coordinate=isvn_coordinate,
                    symlink_target=facts.symlink_target,
                )
            )

        if not isvn_attempted:
            isvn_status = ISVN_ENRICHMENT_NOT_ATTEMPTED
        elif isvn_resolved_any:
            isvn_status = ISVN_ENRICHMENT_RESOLVED
        else:
            isvn_status = ISVN_ENRICHMENT_UNAVAILABLE

        return ArtifactManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            manifest_id=resolved_manifest_id,
            captured_at=self._clock().isoformat(),
            capture_status=(
                CAPTURE_STATUS_PARTIAL if any_partial else CAPTURE_STATUS_COMPLETE
            ),
            producer=producer,
            output_boundaries=[str(b) for b in boundaries],
            observations=observations,
            isvn_enrichment_status=isvn_status,
        )


__all__ = [
    "ArtifactCollector",
    "ArtifactManifestError",
    "OutputBoundaryError",
    "PathFacts",
    "canonical_boundary",
    "is_within_boundaries",
    "observe_path",
    "validate_output_boundary",
    "DEFAULT_CHUNK_SIZE",
    "ISVN_ENRICHMENT_NOT_ATTEMPTED",
    "ISVN_ENRICHMENT_RESOLVED",
    "ISVN_ENRICHMENT_UNAVAILABLE",
]
