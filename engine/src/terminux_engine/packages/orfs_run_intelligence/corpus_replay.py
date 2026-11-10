# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""通用、僅限 qualification 的 corpus replay adapter。

它不啟動 OpenROAD；每次只將已驗證的 corpus bytes 複製到 task-owned output，
並明確標示 fixture replay。選擇只依 manifest 的 method／extension／slot，
不看 design 名稱、參數名稱、檔名或 candidate 值。

schema：

- ``orfs.fixture_corpus.v1``：檔案原樣複製到 output root 的同名相對路徑。
- ``orfs.fixture_corpus.v2``：每個檔案另有 ``target`` 樣板，可帶
  ``{platform}``／``{design}``／``{variant}``／``{slot}``；slot 另宣告
  ``method_value`` 與 evidence 來源，供 replay executable 產生 per-slot 證據。

manifest 自身的 digest 由呼叫端（host 或 replay executable）提供並在這裡核對，
因此「哪一份 corpus」與「corpus 內每一個 byte」都是被固定的身分。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

SCHEMA_V1 = "orfs.fixture_corpus.v1"
SCHEMA_V2 = "orfs.fixture_corpus.v2"
SUPPORTED_SCHEMAS = (SCHEMA_V1, SCHEMA_V2)


class CorpusReplayError(RuntimeError):
    pass


def manifest_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest(path: str | Path, *, method_identity: str,
                  extension_version: str, slot: str,
                  expected_sha256: str | None = None) -> dict[str, Any]:
    """讀回 manifest 並核對 digest、method 身分與 slot。"""
    manifest_path = Path(path)
    if expected_sha256 is not None and manifest_sha256(manifest_path) != expected_sha256:
        raise CorpusReplayError("fixture corpus manifest digest differs")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusReplayError(f"fixture corpus manifest is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") not in SUPPORTED_SCHEMAS:
        raise CorpusReplayError("fixture corpus schema is unsupported")
    if (payload.get("method_identity") != method_identity
            or payload.get("extension_version") != extension_version):
        raise CorpusReplayError("fixture corpus method extension mismatch")
    slots = payload.get("slots")
    entry = slots.get(slot) if isinstance(slots, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("files"), list):
        raise CorpusReplayError("fixture corpus slot is unavailable")
    return {**entry, "schema_version": payload["schema_version"], "manifest": payload}


def _target_relpath(row: dict[str, Any], schema_version: str,
                    substitutions: dict[str, str]) -> str:
    if schema_version == SCHEMA_V1:
        return str(row["path"])
    template = row.get("target")
    if not isinstance(template, str) or not template:
        raise CorpusReplayError("fixture corpus file has no target template")
    try:
        return template.format(**substitutions)
    except KeyError as exc:
        raise CorpusReplayError(
            f"fixture corpus target template needs {exc}") from exc


def replay(*, manifest_path: str | Path, method_identity: str,
           extension_version: str, slot: str, output_root: str | Path,
           expected_sha256: str | None = None,
           substitutions: dict[str, str] | None = None) -> dict[str, Any]:
    """把本 slot 的 corpus 複製到 task-owned output；逐檔核對 hash。

    corpus 內的 task／grant／run／receipt 身分**不會**被複製成本次身分：本函式
    只搬檔案內容，身分一律由呼叫端以本次 run 的值另外產生。
    """
    entry = load_manifest(manifest_path, method_identity=method_identity,
                          extension_version=extension_version, slot=slot,
                          expected_sha256=expected_sha256)
    schema_version = entry["schema_version"]
    values = dict(substitutions or {})
    values.setdefault("slot", slot)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_root = Path(manifest_path).resolve().parent
    copied: list[dict[str, str]] = []
    for row in entry["files"]:
        if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                or not isinstance(row.get("sha256"), str)):
            raise CorpusReplayError("fixture corpus file identity is malformed")
        source = (source_root / row["path"]).resolve()
        relative = _target_relpath(row, schema_version, values)
        target = (root / relative).resolve()
        if not source.is_file() or not source.is_relative_to(source_root):
            raise CorpusReplayError("fixture corpus file escapes its boundary")
        if not target.is_relative_to(root):
            raise CorpusReplayError("fixture corpus target escapes the task output root")
        if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
            raise CorpusReplayError("fixture corpus file digest differs")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append({"path": row["path"], "target": relative, "sha256": row["sha256"]})
    return {"runner": "fixture_corpus_replay", "fixture": True,
            "openroad_launched": False, "method_identity": method_identity,
            "extension_version": extension_version, "slot": slot,
            "schema_version": schema_version,
            "method_value": entry.get("method_value"),
            "evidence": dict(entry.get("evidence") or {}),
            "manifest_sha256": manifest_sha256(manifest_path),
            "files": copied}


__all__ = ["CorpusReplayError", "SCHEMA_V1", "SCHEMA_V2", "SUPPORTED_SCHEMAS",
           "load_manifest", "manifest_sha256", "replay"]
