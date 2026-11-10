# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""llama-index BaseEmbedding adapter（策略 B）。

把現有 `memory.embedder.GeminiEmbedder`（用 google-genai 新 SDK）包成
llama-index `BaseEmbedding` 介面，避免引入 llama-index-embeddings-google
（後者會額外拉 google-generativeai 舊 SDK + ~13 個 transitive deps）。

BaseEmbedding 是 Pydantic v2 model；adapter 的私有狀態用 PrivateAttr。
"""

from __future__ import annotations

import logging

from typing import Any

from terminux_engine.optional_dependencies import import_optional
from pydantic import PrivateAttr

from terminux_engine.memory.embedder import Embedder, GeminiEmbedder, LocalEmbedder

_log = logging.getLogger(__name__)


def _build_embedding_class() -> type:
    """只有存取 adapter 類別時才載入 llama-index 的 Pydantic 基底。"""
    BaseEmbedding = import_optional("llama_index.core.embeddings", "rag").BaseEmbedding

    class GeminiLlamaIndexEmbedding(BaseEmbedding):
        """把任何符合 Embedder 介面的物件接入 llama-index。

        用法：

            from terminux_engine.memory.embedder import GeminiEmbedder
            gemini = GeminiEmbedder(api_key=...)
            embed_model = GeminiLlamaIndexEmbedding(embedder=gemini)

        Embedder.embed() 回 None 時（NoOpEmbedder 或 API 失敗）會 raise
        RuntimeError——RAG 需要真實 embedding 才能運作，否則 cosine 相似度沒意義。
        """

        _embedder: Embedder = PrivateAttr()

        def __init__(
            self,
            *,
            embedder: Embedder,
            model_name: str = "gemini-embedding-001",
            embed_batch_size: int = 16,
            **kwargs,
        ) -> None:
            super().__init__(
                model_name=model_name,
                embed_batch_size=embed_batch_size,
                **kwargs,
            )
            self._embedder = embedder

        @classmethod
        def class_name(cls) -> str:
            return "GeminiLlamaIndexEmbedding"

        @classmethod
        def from_api_key(
            cls,
            api_key: str,
            model_name: str = "gemini-embedding-001",
        ) -> "GeminiLlamaIndexEmbedding":
            """Convenience：直接從 api key 建出一個 wrapping GeminiEmbedder 的實例。"""
            return cls(
                embedder=GeminiEmbedder(api_key=api_key, model_name=model_name),
                model_name=model_name,
            )

        @classmethod
        def from_local(cls, model_path: str) -> "GeminiLlamaIndexEmbedding":
            """直接從本地 GGUF 路徑建出 embedding adapter。"""
            return cls(
                embedder=LocalEmbedder(model_path=model_path),
                model_name=model_path.rsplit("/", maxsplit=1)[-1],
            )

        # ── BaseEmbedding 抽象方法實作 ──────────────────────────────────────────

        def _get_query_embedding(self, query: str) -> list[float]:
            v = self._embedder.embed(query)
            if v is None:
                raise RuntimeError(
                    "Embedder returned None (likely NoOpEmbedder or API error); "
                    "RAG requires a real embedding backend"
                )
            return list(v)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            # 同步包成 async（GeminiEmbedder 沒有原生 async path；llama-index 大多場景
            # 走 sync，async 只在 query engine 的 streaming path 才會踩到）
            return self._get_query_embedding(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._get_query_embedding(text)

        def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
            """Batch 版；走底層 embedder.embed_batch 一次 round-trip。"""
            if not texts:
                return []
            raw = self._embedder.embed_batch(list(texts))
            out: list[list[float]] = []
            for i, v in enumerate(raw):
                if v is None:
                    raise RuntimeError(
                        f"Embedder returned None for batch item {i}; "
                        f"RAG requires a real embedding backend"
                    )
                out.append(list(v))
            return out

    GeminiLlamaIndexEmbedding.__qualname__ = "GeminiLlamaIndexEmbedding"
    return GeminiLlamaIndexEmbedding


def __getattr__(name: str) -> Any:
    """保留既有類別匯入介面，載入成功後快取類別。"""
    if name == "BaseEmbedding":
        return import_optional("llama_index.core.embeddings", "rag").BaseEmbedding
    if name != "GeminiLlamaIndexEmbedding":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    cls = _build_embedding_class()
    globals()[name] = cls
    return cls
