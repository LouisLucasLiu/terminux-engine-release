# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""將 Experience Distillation tarball 匯入 RAG knowledge。"""

from __future__ import annotations

import json
import logging
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)

IMPORT_TOOL_TIMEOUT_SECONDS = 120.0
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


SCHEMA = ToolSchema(
    name="import_experience",
    description=(
        "匯入 Experience Distillation 產生的 tarball 到 RAG knowledge，並重建 RAG index。"
        "tarball 必須包含 manifest.json 與 distilled Markdown 檔案。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "tarball_path": {
                "type": "string",
                "description": "要匯入的 experience_export_*.tar.gz 絕對路徑",
            },
        },
        "required": ["tarball_path"],
    },
)


RebuildIndex = Callable[[Path], dict[str, Any]]


def create_tool(
    *,
    knowledge_dir: str | Path | None = None,
    rebuild_index: RebuildIndex | None = None,
) -> Callable[..., dict[str, Any]]:
    """建立 Experience 匯入 executor。"""
    knowledge_root = (
        Path(knowledge_dir).expanduser()
        if knowledge_dir is not None
        else Path.home() / ".terminux" / "rag_knowledge"
    )
    rebuild = rebuild_index or _rebuild_rag_index

    def executor(*, tarball_path: str | None = None, **_: Any) -> dict[str, Any]:
        if not isinstance(tarball_path, str) or not tarball_path.strip():
            return _error("必須提供 tarball_path")

        archive_path = Path(tarball_path).expanduser()
        if not archive_path.is_file():
            return _error(f"Tarball 不存在或不是檔案：{archive_path}")

        try:
            manifest, markdown_files = _read_archive(archive_path)
        except (OSError, tarfile.TarError, ValueError, UnicodeDecodeError) as exc:
            _log.warning("Experience import archive rejected: %s", exc)
            return _error(f"無法匯入 tarball：{exc}")

        try:
            destination = _write_import(knowledge_root, manifest, markdown_files)
        except OSError as exc:
            _log.warning("Experience import write failed: %s", exc)
            return _error(f"匯入檔案寫入失敗：{exc}")

        result: dict[str, Any] = {
            "import_dir": str(destination),
            "imported_count": len(markdown_files),
        }
        try:
            result["rebuild"] = rebuild(knowledge_root)
        except Exception as exc:
            _log.warning("Experience import RAG rebuild failed: %s", exc)
            return {
                "status": "warning",
                "message": "經驗檔已匯入，但 RAG index 重建失敗；重啟 daemon 前不會載入新知識",
                "warning": str(exc),
                **result,
            }

        return {
            "status": "ok",
            "message": f"已匯入 {len(markdown_files)} 個經驗檔並重建 RAG index",
            **result,
        }

    return executor


def _read_archive(archive_path: Path) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise ValueError("tarball 是空的")
        for member in members:
            _validated_member_path(member)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ValueError(f"tarball 包含不允許的 member：{member.name}")

        manifest_members = [
            member
            for member in members
            if member.isfile() and PurePosixPath(member.name).name == "manifest.json"
        ]
        if len(manifest_members) != 1:
            raise ValueError("tarball 必須且只能包含一個 manifest.json")

        manifest_member = manifest_members[0]
        root = PurePosixPath(manifest_member.name).parent
        if len(root.parts) != 1:
            raise ValueError("manifest.json 必須位於 tarball 的單一根目錄")
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            raise ValueError("無法讀取 manifest.json")
        manifest_raw = json.loads(manifest_stream.read().decode("utf-8"))
        manifest = _validate_manifest(manifest_raw)

        markdown_files: list[tuple[str, bytes]] = []
        for member in members:
            member_path = PurePosixPath(member.name)
            if not member.isfile() or member_path.suffix.lower() != ".md":
                continue
            if member_path.parent != root:
                raise ValueError("Markdown 檔必須位於 manifest 的同一根目錄")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"無法讀取 Markdown 檔：{member.name}")
            markdown_files.append((member_path.name, stream.read()))

    if not markdown_files:
        raise ValueError("tarball 沒有可匯入的 Markdown 經驗檔")
    if len({name for name, _ in markdown_files}) != len(markdown_files):
        raise ValueError("tarball 含有重複的 Markdown 檔名")
    return manifest, markdown_files


def _validated_member_path(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"tarball 含有不安全路徑：{member.name}")


def _validate_manifest(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("manifest.json 必須是物件")
    source_project = value.get("source_project")
    export_date = value.get("export_date")
    if not isinstance(source_project, str) or not source_project.strip():
        raise ValueError("manifest.json 缺少 source_project")
    if not isinstance(export_date, str) or not export_date.strip():
        raise ValueError("manifest.json 缺少 export_date")
    return {"source_project": source_project.strip(), "export_date": export_date.strip()}


def _write_import(
    knowledge_root: Path,
    manifest: dict[str, str],
    markdown_files: list[tuple[str, bytes]],
) -> Path:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    destination = _available_destination(
        knowledge_root,
        manifest["source_project"],
        manifest["export_date"],
    )
    with tempfile.TemporaryDirectory(dir=knowledge_root) as temp_dir:
        staged = Path(temp_dir) / destination.name
        staged.mkdir()
        for filename, content in markdown_files:
            (staged / filename).write_bytes(content)
        staged.rename(destination)
    return destination


def _available_destination(knowledge_root: Path, source_project: str, export_date: str) -> Path:
    base_name = f"imported_{_safe_segment(source_project)}_{_safe_segment(export_date)}"
    candidate = knowledge_root / base_name
    suffix = 2
    while candidate.exists():
        candidate = knowledge_root / f"{base_name}_{suffix}"
        suffix += 1
    return candidate


def _safe_segment(value: str) -> str:
    normalized = _SAFE_SEGMENT_RE.sub("_", value).strip("._")
    return normalized or "unknown"


def _rebuild_rag_index(knowledge_root: Path) -> dict[str, Any]:
    from terminux_engine.llm.embedding_factory import build_rag_embedding
    from terminux_engine.llm.rag_index import RAGIndex
    from terminux_engine.rag.knowledge_codec import resolve_shared_knowledge_dir
    from terminux_engine.rag.shared_knowledge import (
        compute_shared_fingerprint,
        load_shared_documents,
        save_fingerprint,
    )

    embedding = build_rag_embedding()
    if embedding is None:
        raise RuntimeError("缺少可用的 RAG embedding 設定")
    index_dir = Path.home() / ".terminux" / "rag_index"
    shared_directory = resolve_shared_knowledge_dir()
    stats = RAGIndex.build_from_directory(
        source_dir=knowledge_root,
        index_dir=index_dir,
        embedding=embedding,
        extra_documents=load_shared_documents(shared_directory),
    )
    save_fingerprint(index_dir, compute_shared_fingerprint(shared_directory))
    return stats


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}
