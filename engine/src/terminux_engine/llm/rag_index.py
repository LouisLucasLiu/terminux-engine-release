# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""RAG VectorStoreIndex 管理。

封裝 llama-index-core 的 in-memory VectorStoreIndex：
- 從目錄建 index（.yaml / .yml / .md / .txt / .json）
- 持久化到 ~/.terminux/rag_index/
- 載入 / 查詢

不引入外部 vector DB（ChromaDB 等）。建 index 是離線動作（CLI script
terminux-build-rag-index），daemon 啟動時只 load + query。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from terminux_engine.optional_dependencies import (
    MissingOptionalDependencyError, import_optional,
)

_log = logging.getLogger(__name__)


_SUPPORTED_EXTS = frozenset({".yaml", ".yml", ".md", ".txt", ".json"})


class RAGResult(BaseModel):
    """單筆 retrieval 結果。"""

    text_chunk: str
    source_file: str
    relevance_score: float
    metadata: dict[str, str] = Field(default_factory=dict)


class RAGIndex:
    """llama-index VectorStoreIndex 的薄包裝。

    用法：

        idx = RAGIndex(index_dir="~/.terminux/rag_index", embedding=embed_model)
        if idx.load():
            results = idx.query("STA timing closure", top_k=5)
    """

    def __init__(
        self,
        index_dir: str | Path,
        embedding: Any | None = None,
    ) -> None:
        self._index_dir = Path(index_dir).expanduser()
        self._embedding = embedding
        self._index: Any = None  # llama_index.core.VectorStoreIndex | None

    @property
    def index_dir(self) -> Path:
        return self._index_dir

    @property
    def is_loaded(self) -> bool:
        return self._index is not None

    # ── load / query ────────────────────────────────────────────────────────

    def load(self) -> bool:
        """從 index_dir 載入 persisted index。

        - 目錄不存在 / 空 / load 失敗 → 回 False（log warning）
        - 成功 → 回 True，後續 query() 可用
        """
        if not self._index_dir.exists():
            _log.info("RAG index dir does not exist: %s", self._index_dir)
            return False
        try:
            if not any(self._index_dir.iterdir()):
                _log.info("RAG index dir is empty: %s", self._index_dir)
                return False
        except OSError as e:
            _log.warning("RAG index dir not readable (%s): %s", self._index_dir, e)
            return False

        try:
            core = import_optional("llama_index.core", "rag")
            StorageContext = core.StorageContext
            load_index_from_storage = core.load_index_from_storage

            storage = StorageContext.from_defaults(persist_dir=str(self._index_dir))
            kwargs: dict[str, Any] = {}
            if self._embedding is not None:
                kwargs["embed_model"] = self._embedding
            self._index = load_index_from_storage(storage, **kwargs)
            _log.info("RAG index loaded from %s", self._index_dir)
            return True
        except MissingOptionalDependencyError:
            raise
        except Exception as e:
            _log.warning("RAG index load failed (%s): %s", self._index_dir, e)
            self._index = None
            return False

    def query(self, query_text: str, top_k: int = 5) -> list[RAGResult]:
        """Retrieve top_k 相關 chunk。未 load / 失敗 → 回空 list。"""
        if self._index is None:
            return []
        if not query_text or not query_text.strip():
            return []
        try:
            retriever = self._index.as_retriever(similarity_top_k=top_k)
            nodes = retriever.retrieve(query_text)
        except Exception as e:
            _log.warning("RAG query failed: %s", e)
            return []

        results: list[RAGResult] = []
        for n in nodes:
            try:
                results.append(_node_to_result(n))
            except Exception as e:
                _log.warning("RAG node serialize failed (skipped): %s", e)
        return results

    # ── build_from_directory ────────────────────────────────────────────────

    @staticmethod
    def build_from_directory(
        source_dir: str | Path,
        index_dir: str | Path,
        embedding: Any,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        extra_documents: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """從目錄與可選虛擬文件讀取內容 → chunk → embed → persist index。

        回傳統計 dict：{files_read, chunks, elapsed_sec, index_dir, total_bytes}
        """
        if embedding is None:
            raise ValueError("embedding is required for build_from_directory")

        source_path = Path(source_dir).expanduser()
        if (not source_path.exists() or not source_path.is_dir()) and not extra_documents:
            raise FileNotFoundError(f"Source dir not found: {source_path}")

        index_path = Path(index_dir).expanduser()
        index_path.mkdir(parents=True, exist_ok=True)

        # 動態 import（避免 daemon 啟動時 import cost）
        core = import_optional("llama_index.core", "rag")
        VectorStoreIndex = core.VectorStoreIndex
        SentenceSplitter = import_optional("llama_index.core.node_parser", "rag").SentenceSplitter

        documents, total_bytes = (
            _read_documents(source_path) if source_path.is_dir() else ([], 0)
        )
        extra, extra_bytes = _read_extra_documents(extra_documents or {})
        documents.extend(extra)
        total_bytes += extra_bytes
        if not documents:
            raise ValueError(
                f"No supported files found in {source_path} "
                f"(extensions: {sorted(_SUPPORTED_EXTS)})"
            )

        splitter = SentenceSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )

        t0 = time.monotonic()
        index = VectorStoreIndex.from_documents(
            documents,
            embed_model=embedding,
            transformations=[splitter],
        )
        index.storage_context.persist(persist_dir=str(index_path))
        elapsed = time.monotonic() - t0

        chunks = len(index.docstore.docs)
        _log.info(
            "RAG index built: %d files, %d chunks, %.2fs → %s",
            len(documents),
            chunks,
            elapsed,
            index_path,
        )
        return {
            "files_read": len(documents),
            "chunks": chunks,
            "elapsed_sec": elapsed,
            "index_dir": str(index_path),
            "total_bytes": total_bytes,
        }


# ── helpers ─────────────────────────────────────────────────────────────────


def _read_documents(source_path: Path) -> tuple[list[Any], int]:
    """掃描 source_path 下支援副檔名的檔案，回傳 (Documents, 總 bytes)。"""
    Document = import_optional("llama_index.core", "rag").Document

    documents: list[Any] = []
    total_bytes = 0
    for path in sorted(source_path.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SUPPORTED_EXTS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            _log.warning("Skipping %s: %s", path, e)
            continue
        if not text.strip():
            continue
        rel = str(path.relative_to(source_path)) if source_path in path.parents or path == source_path else path.name
        documents.append(
            Document(
                text=text,
                metadata={
                    "file_path": str(path),
                    "file_name": path.name,
                    "relative_path": rel,
                    "source": "user",
                },
            )
        )
        total_bytes += len(text.encode("utf-8"))
    return documents, total_bytes


def _read_extra_documents(extra_documents: dict[str, str]) -> tuple[list[Any], int]:
    """將 shared knowledge 的虛擬路徑與內容轉為與檔案相同的 Document。"""
    Document = import_optional("llama_index.core", "rag").Document

    documents: list[Any] = []
    total_bytes = 0
    for virtual_path, text in sorted(extra_documents.items()):
        if not isinstance(text, str) or not text.strip():
            continue
        path = Path(virtual_path)
        documents.append(
            Document(
                text=text,
                metadata={
                    "file_path": virtual_path,
                    "file_name": path.name,
                    "relative_path": virtual_path,
                    "source": "shared",
                },
            )
        )
        total_bytes += len(text.encode("utf-8"))
    return documents, total_bytes


def _node_to_result(node) -> RAGResult:
    """llama-index NodeWithScore → RAGResult。"""
    inner = getattr(node, "node", node)
    text = ""
    try:
        text = inner.get_content() if hasattr(inner, "get_content") else getattr(inner, "text", "") or ""
    except Exception:
        text = getattr(inner, "text", "") or ""
    metadata_raw = getattr(inner, "metadata", None) or {}
    metadata = {str(k): str(v) for k, v in metadata_raw.items()}
    source = (
        metadata.get("file_path")
        or metadata.get("file_name")
        or metadata.get("relative_path")
        or "unknown"
    )
    score_raw = getattr(node, "score", None)
    score = float(score_raw) if score_raw is not None else 0.0
    return RAGResult(
        text_chunk=text,
        source_file=source,
        relevance_score=score,
        metadata=metadata,
    )
