# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""RAG embedding backend 的共用建構邏輯。"""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

from terminux_engine.llm.credential_file import secret as _credential_secret

if TYPE_CHECKING:
    from llama_index.core.embeddings import BaseEmbedding


def build_rag_embedding() -> BaseEmbedding | None:
    """依環境變數建立 RAG embedding，無可用設定時回傳 None。"""
    backend = os.environ.get("TERMINUX_EMBEDDING_BACKEND", "").strip().lower()
    gemini_api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        # 憑證優先來自 0600 設定檔（見 llm.credential_file）。
        or (_credential_secret("TERMINUX_GEMINI_API_KEY") or "").strip()
    )

    if backend == "local" or (not backend and not gemini_api_key):
        model_path = os.environ.get("TERMINUX_EMBEDDING_MODEL_PATH", "").strip()
        if not model_path:
            return None
        from terminux_engine.llm.rag_embedding import GeminiLlamaIndexEmbedding

        return GeminiLlamaIndexEmbedding.from_local(model_path)

    if backend in {"", "gemini"} and gemini_api_key:
        model_name = os.environ.get(
            "GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"
        )
        from terminux_engine.llm.rag_embedding import GeminiLlamaIndexEmbedding

        return GeminiLlamaIndexEmbedding.from_api_key(
            api_key=gemini_api_key,
            model_name=model_name,
        )

    return None


def __getattr__(name: str) -> Any:
    """保留原本重匯出的 adapter／基底名稱，存取時才載入相依。"""
    if name == "GeminiLlamaIndexEmbedding":
        from terminux_engine.llm.rag_embedding import GeminiLlamaIndexEmbedding

        return GeminiLlamaIndexEmbedding
    if name == "BaseEmbedding":
        from terminux_engine.optional_dependencies import import_optional

        return import_optional("llama_index.core.embeddings", "rag").BaseEmbedding
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
