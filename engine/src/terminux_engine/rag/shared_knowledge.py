# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Shared RAG knowledge 的 fingerprint 與背景安全重建支援。"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from terminux_engine.rag.knowledge_codec import load_shared_knowledge


_LOGGER = logging.getLogger(__name__)
_EMPTY_FINGERPRINT = "none"


def compute_shared_fingerprint(knowledge_dir: Path | None) -> str:
    """以相對路徑、大小與奈秒 mtime 穩定計算 shared knowledge 指紋。"""
    if knowledge_dir is None or not knowledge_dir.is_dir():
        return _EMPTY_FINGERPRINT

    digest = hashlib.sha256()
    for path in sorted(item for item in knowledge_dir.rglob("*") if item.is_file()):
        stat = path.stat()
        relative_path = str(path.relative_to(knowledge_dir)).encode("utf-8")
        digest.update(relative_path)
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_stored_fingerprint(index_dir: Path) -> str | None:
    """讀取已完成 index 的 shared fingerprint；不存在時回傳 None。"""
    fingerprint_path = index_dir / ".shared_fingerprint"
    try:
        return fingerprint_path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except OSError as error:
        _LOGGER.warning("無法讀取 shared knowledge fingerprint: %s", error)
        return None


def save_fingerprint(index_dir: Path, fingerprint: str) -> None:
    """以 replace 寫入 fingerprint，避免讀者看到部分內容。"""
    index_dir.mkdir(parents=True, exist_ok=True)
    fingerprint_path = index_dir / ".shared_fingerprint"
    temporary_path = index_dir / ".shared_fingerprint.tmp"
    temporary_path.write_text(f"{fingerprint}\n", encoding="utf-8")
    os.replace(temporary_path, fingerprint_path)


def rebuild_shared_index_if_needed(
    *,
    user_knowledge_dir: Path,
    index_dir: Path,
    shared_knowledge_dir: Path | None,
    embedding: Any,
    on_reloaded: Callable[[], None] | None = None,
    stop_event: threading.Event | None = None,
    build_index: Callable[..., dict[str, Any]] | None = None,
) -> bool:
    """指紋變化時建立暫存 index、swap，最後通知記憶體 search 層重載。"""
    current_fingerprint = compute_shared_fingerprint(shared_knowledge_dir)
    if current_fingerprint == load_stored_fingerprint(index_dir):
        return False

    if _should_stop(stop_event):
        return False

    temporary_dir = index_dir.parent / f".{index_dir.name}_building"
    try:
        shared_documents = load_shared_documents(shared_knowledge_dir)
        if not user_knowledge_dir.is_dir() and not shared_documents:
            _LOGGER.info("沒有 user 或 shared RAG knowledge，略過 index rebuild")
            save_fingerprint(index_dir, current_fingerprint)
            return False

        _remove_directory(temporary_dir)
        temporary_dir.mkdir(parents=True, exist_ok=True)

        if build_index is None:
            from terminux_engine.llm.rag_index import RAGIndex

            build_index = RAGIndex.build_from_directory

        build_index(
            source_dir=user_knowledge_dir,
            index_dir=temporary_dir,
            embedding=embedding,
            extra_documents=shared_documents,
        )
        if _should_stop(stop_event):
            _remove_directory(temporary_dir)
            return False

        _swap_index_directories(temporary_dir, index_dir)
        save_fingerprint(index_dir, current_fingerprint)
        if on_reloaded is not None:
            on_reloaded()
        _LOGGER.info("Shared RAG knowledge index rebuilt: %s", index_dir)
        return True
    except Exception as error:
        _remove_directory(temporary_dir)
        _LOGGER.warning("Shared RAG knowledge rebuild failed; keeping old index: %s", error)
        return False


def start_background_rebuild(**kwargs: Any) -> threading.Thread:
    """以 daemon thread 執行一次 rebuild，避免阻擋 daemon 啟動。"""
    thread = threading.Thread(
        target=rebuild_shared_index_if_needed,
        kwargs=kwargs,
        daemon=True,
        name="terminux-shared-rag-rebuild",
    )
    thread.start()
    return thread


def load_shared_documents(shared_knowledge_dir: Path | None) -> dict[str, str]:
    """讀取 shared 文件並加上不對應實體檔案的 shared 虛擬路徑。"""
    if shared_knowledge_dir is None:
        return {}
    loaded = load_shared_knowledge(shared_knowledge_dir.parent)
    return {f"shared/{relative_path}": content for relative_path, content in loaded.items()}


def _swap_index_directories(temporary_dir: Path, index_dir: Path) -> None:
    """在同一父目錄內交換 index，失敗時盡力還原舊 index。"""
    backup_dir = index_dir.parent / f".{index_dir.name}_previous"
    _remove_directory(backup_dir)
    had_previous_index = index_dir.exists()
    if had_previous_index:
        os.replace(index_dir, backup_dir)
    try:
        os.replace(temporary_dir, index_dir)
    except Exception:
        if had_previous_index and backup_dir.exists():
            os.replace(backup_dir, index_dir)
        raise
    _remove_directory(backup_dir)


def _remove_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _should_stop(stop_event: threading.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()
