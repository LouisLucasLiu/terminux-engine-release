# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Immutable Artifact Manifest Store（append-only、atomic、fail-closed）。

manifest 是**證據本體**，不是 cache。寫入後不可原地更新
——同一 `manifest_id` 若 bytes 完全相同視為 idempotent no-op，bytes 不同
則 fail-closed 拋出 `ArtifactManifestConflictError`，絕不覆寫。

Storage root 沿用既有 Terminux durable state 慣例
（`~/.terminux/<subsystem>/...`，與
`orchestration.ledger.OrchestrationLedger.DEFAULT_DB_PATH`
＝`~/.terminux/orchestration/ledger.sqlite`、既有 lineage ledger
＝`~/.terminux/lineage/ledger.jsonl` 同一個 root），因此 production
manifest 永遠不會落在 ORFS repo、tool output directory、source tree、
Package directory 或任意 cwd。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from terminux_engine.lineage.artifact_manifest import ArtifactManifest

MANIFEST_SUFFIX = ".json"


class ArtifactManifestConflictError(RuntimeError):
    """同一 `manifest_id` 已存在且 bytes 不同——immutable evidence 不得覆寫。"""


class ArtifactManifestPersistenceError(RuntimeError):
    """無法以 atomic、no-overwrite 的方式發佈 manifest——fail closed。

    典型成因：檔案系統不支援 hardlink、`os.link` 權限不足。此時**不得**
    退回 `os.replace`／先刪再 rename，因為那些做法都可能覆寫既有的
    immutable manifest。呼叫端（capture service）
    會把它轉成 `artifact_capture_status="failed"`。
    """


def canonical_manifest_bytes(manifest: ArtifactManifest) -> bytes:
    """Deterministic JSON serialization。

    UTF-8、stable key order、固定 separators、結尾單一 newline。Manifest
    SHA-256 一律對「實際持久化的這串 bytes」計算，不是對記憶體物件。
    """
    text = json.dumps(
        manifest.to_json_dict(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


def manifest_sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ManifestPersistResult:
    """一次 `persist()` 的結果。`created=False` 代表 idempotent no-op。"""

    manifest_id: str
    reference: str
    sha256: str
    created: bool


class ArtifactManifestStore:
    """Immutable manifest 的持久化與讀取。"""

    DEFAULT_ROOT: Path = Path.home() / ".terminux" / "lineage" / "artifact_manifests"

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else self.DEFAULT_ROOT
        self._manifest_dir = self._root / "manifests"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def manifest_dir(self) -> Path:
        return self._manifest_dir

    def path_for(self, manifest_id: str) -> Path:
        if "/" in manifest_id or manifest_id in {"", ".", ".."}:
            raise ValueError(f"unsafe manifest_id: {manifest_id!r}")
        return self._manifest_dir / f"{manifest_id}{MANIFEST_SUFFIX}"

    def persist(self, manifest: ArtifactManifest) -> ManifestPersistResult:
        """Atomic、non-overwriting 地寫入一份 manifest。

        流程：同 directory 建立 temporary file → 完整 write／flush
        →`os.fsync` → `os.link` 到 final path → 移除 temporary file。
        用 `os.link` 而非 `os.replace`：兩者同樣 atomic，但 `link` 在
        final path 已存在時直接失敗，天然滿足「不得覆寫既有內容」。

        **Fail-closed**：`os.link` 是本 store
        唯一的 publish primitive。舊版在 hardlink 失敗時退回
        「先 `exists()` 檢查、後 `os.replace`」——那是 check-then-act，
        兩個步驟之間仍可能被另一個 writer 插入，導致既有 immutable
        manifest 被覆寫。現在改為：hardlink 無法完成時（filesystem 不支援、
        權限不足、任何 `OSError`）直接清掉 temporary file 並拋出
        `ArtifactManifestPersistenceError`，絕不改寫 final path。
        `ArtifactCaptureService` 會把它轉成
        `artifact_capture_status="failed"`，不會宣稱 capture 完成。

        任何失敗都只留下（隨即被清除的）temporary file，不會留下看起來
        完整的 final file。
        """
        self._manifest_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.path_for(manifest.manifest_id)
        payload = canonical_manifest_bytes(manifest)
        digest = manifest_sha256(payload)

        existing = self._existing_result(final_path, manifest.manifest_id, payload, digest)
        if existing is not None:
            return existing

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._manifest_dir), prefix=f".{manifest.manifest_id}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp_path, final_path)
            except FileExistsError:
                # 競態：另一個 writer 在我們檢查之後先寫入了同一個 id。
                conflict = self._existing_result(
                    final_path, manifest.manifest_id, payload, digest
                )
                if conflict is None:  # pragma: no cover - 只可能在再次消失時發生
                    raise
                return conflict
            except OSError as exc:
                # Hardlink 不可用（filesystem 不支援／權限／其他 OSError）：
                # fail closed。不得退回 `os.replace`——那會在 check-then-act
                # 的空窗期覆寫既有 immutable manifest。
                raise ArtifactManifestPersistenceError(
                    "atomic no-overwrite publish is unavailable for manifest "
                    f"{manifest.manifest_id}: os.link failed ({exc!r}); "
                    "refusing to fall back to a replacing write"
                ) from exc
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()

        return ManifestPersistResult(
            manifest_id=manifest.manifest_id,
            reference=str(final_path),
            sha256=digest,
            created=True,
        )

    def _existing_result(
        self, final_path: Path, manifest_id: str, payload: bytes, digest: str
    ) -> ManifestPersistResult | None:
        if not final_path.exists():
            return None
        current = final_path.read_bytes()
        if current == payload:
            return ManifestPersistResult(
                manifest_id=manifest_id,
                reference=str(final_path),
                sha256=digest,
                created=False,
            )
        raise ArtifactManifestConflictError(
            f"manifest {manifest_id} already exists with different bytes: {final_path}"
        )

    def read_bytes(self, manifest_id: str) -> bytes:
        return self.path_for(manifest_id).read_bytes()

    def load(self, manifest_id: str) -> ArtifactManifest:
        payload = json.loads(self.read_bytes(manifest_id).decode("utf-8"))
        return ArtifactManifest.from_json_dict(payload)

    def sha256_of(self, manifest_id: str) -> str:
        return manifest_sha256(self.read_bytes(manifest_id))

    def exists(self, manifest_id: str) -> bool:
        return self.path_for(manifest_id).is_file()

    def iter_manifest_ids(self) -> Iterator[str]:
        if not self._manifest_dir.is_dir():
            return
        for path in sorted(self._manifest_dir.glob(f"*{MANIFEST_SUFFIX}")):
            yield path.name[: -len(MANIFEST_SUFFIX)]


__all__ = [
    "ArtifactManifestConflictError",
    "ArtifactManifestPersistenceError",
    "ArtifactManifestStore",
    "ManifestPersistResult",
    "canonical_manifest_bytes",
    "manifest_sha256",
]
