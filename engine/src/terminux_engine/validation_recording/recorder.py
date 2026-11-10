# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Validation transcript recorder。

先前有兩個保存缺陷：

1. `fresh=True` 重建 sandbox 時把**其他 case** 的 transcript 一起刪掉；
2. adapter recorder 把 provider response 截斷在 1500 字元，於是真實回應無法
   逐位元重放——診斷因此卡住。

這個模組把兩件事都收斂掉：每個 case 一個獨立目錄、完整 bytes 收到後**立即
atomic 落地**、SHA-256 一律由**完整 bytes** 計算，excerpt 只是顯示用欄位，
永遠不能取代 raw transcript。

Vendor-neutral：不認識任何 provider、model 或 domain。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: console／報表用的顯示長度。**只影響 excerpt 欄位**，不影響落地的 raw bytes。
DISPLAY_EXCERPT_CHARS = 1500


class EvidenceConflict(RuntimeError):
    """同一 path 已存在但 bytes 不同——fail closed，絕不覆寫。"""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, raw: bytes) -> None:
    """先寫同目錄的暫存檔再 `os.replace`，避免留下半截檔案。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class CaseRecorder:
    """單一 validation case 的 immutable evidence directory。"""

    case_id: str
    root: Path
    _temporary: list[Path] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        d = self.root / self.case_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── immutable write ──────────────────────────────────────────────
    def write_bytes(self, name: str, raw: bytes) -> dict[str, Any]:
        """同 bytes ⇒ idempotent；不同 bytes ⇒ `EvidenceConflict`。"""
        digest = sha256_bytes(raw)
        path = self.dir / name
        if path.exists():
            existing = sha256_bytes(path.read_bytes())
            if existing == digest:
                return {"path": str(path), "sha256": digest, "bytes": len(raw),
                        "outcome": "idempotent"}
            raise EvidenceConflict(
                f"{path} already exists with different bytes "
                f"(existing {existing[:16]}…, new {digest[:16]}…)")
        _atomic_write(path, raw)
        return {"path": str(path), "sha256": digest, "bytes": len(raw),
                "outcome": "written"}

    def write_text(self, name: str, text: str) -> dict[str, Any]:
        return self.write_bytes(name, text.encode("utf-8"))

    def write_json(self, name: str, payload: Any) -> dict[str, Any]:
        return self.write_text(name, json.dumps(payload, ensure_ascii=False,
                                                indent=1, default=str) + "\n")

    # ── raw provider transcript ──────────────────────────────────────
    def record_response(self, label: str, response: Any) -> dict[str, Any]:
        """完整 response **立刻** 落地。

        回傳的 dict 帶一個 `excerpt` 供 console／報表顯示，但 `sha256` 與
        `bytes` 一律由**完整 bytes** 計算，而且 `raw_path` 永遠指向完整檔案。
        excerpt 不是 transcript，也不得被當成 transcript 使用。
        """
        text = response if isinstance(response, str) else json.dumps(
            response, ensure_ascii=False, default=str)
        raw = text.encode("utf-8")
        record = self.write_bytes(f"raw_response_{label}.txt", raw)
        return {
            "label": label,
            "raw_path": record["path"],
            "sha256": record["sha256"],
            "bytes": record["bytes"],
            "outcome": record["outcome"],
            "truncated_on_disk": False,
            "excerpt": text[:DISPLAY_EXCERPT_CHARS],
            "excerpt_is_display_only": True,
            "excerpt_is_not_the_transcript": True,
        }

    # ── teardown ─────────────────────────────────────────────────────
    def temporary(self, path: Path) -> Path:
        """登記一個 teardown 可刪的 runtime 檔（必須在本 case 目錄內）。"""
        self._temporary.append(path)
        return path

    def teardown(self) -> list[str]:
        """只刪本 case **明確登記** 且位於本 case 目錄內的檔案。"""
        import shutil

        removed: list[str] = []
        base = str(self.dir)
        for path in self._temporary:
            if path.exists() and str(path.resolve()).startswith(base):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                removed.append(str(path))
        self._temporary.clear()
        return removed

    def manifest(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "dir": str(self.dir),
                "files": {p.name: {"sha256": sha256_bytes(p.read_bytes()),
                                   "bytes": p.stat().st_size}
                          for p in sorted(self.dir.iterdir()) if p.is_file()}}


class TranscriptStore:
    """多個 case 的容器。initialize 一個 case **不會**動到其他 case。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def case(self, case_id: str) -> CaseRecorder:
        return CaseRecorder(case_id=case_id, root=self.root)

    def case_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())


__all__ = ["CaseRecorder", "DISPLAY_EXCERPT_CHARS", "EvidenceConflict",
           "TranscriptStore", "sha256_bytes"]
