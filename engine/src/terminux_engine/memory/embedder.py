# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Embedding provider interface + 實作。

從 llm/llm_router.py 遷移而來。
llm/ 模組改為 import from 此處，保持向下相容。
"""

import logging
import math
import threading
from typing import Any

from terminux_engine.optional_dependencies import (
    MissingOptionalDependencyError, import_optional,
)

logger = logging.getLogger(__name__)


class Embedder:
    """Embedding provider interface。"""

    def embed(self, text: str) -> list[float] | None:
        raise NotImplementedError

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        raise NotImplementedError


class NoOpEmbedder(Embedder):
    """不做任何 embedding，回傳 None。"""

    def embed(self, text: str) -> list[float] | None:
        return None

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        return [None] * len(texts)


class GeminiEmbedder(Embedder):
    """使用 Gemini embedding API（gemini-embedding-001）。"""

    def __init__(self, api_key: str, model_name: str = "gemini-embedding-001") -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._client: Any = None  # lazy init: genai.Client

    def _init_client(self) -> None:
        """Lazy 初始化 Gemini client。"""
        try:
            from google import genai  # type: ignore[import]
        except ImportError as e:
            raise RuntimeError(
                f"google-genai package is not available: {e}"
            ) from e
        self._client = genai.Client(api_key=self._api_key)

    def embed(self, text: str) -> list[float] | None:
        if self._client is None:
            self._init_client()
        response = self._client.models.embed_content(
            model=self._model_name,
            contents=text,
        )
        return list(response.embeddings[0].values)

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        if self._client is None:
            self._init_client()
        response = self._client.models.embed_content(
            model=self._model_name,
            contents=texts,
        )
        return [list(e.values) for e in response.embeddings]


class LocalEmbedder(Embedder):
    """使用 llama-cpp-python 的本地 GGUF embedding provider。"""

    def __init__(
        self,
        model_path: str,
        n_ctx: int = 512,
        n_threads: int | None = None,
    ) -> None:
        self._model_path = model_path
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._llm: Any = None
        # llama_cpp.Llama 不保證可供多執行緒同時呼叫。
        self._lock = threading.Lock()

    def _init(self) -> None:
        """延遲載入 GGUF 模型，避免 router 建構時立即占用記憶體。"""
        Llama = import_optional("llama_cpp", "local-llm").Llama

        self._llm = Llama(
            model_path=self._model_path,
            embedding=True,
            n_ctx=self._n_ctx,
            # BERT GGUF 在 llama-cpp-python 0.3.x 中以 n_batch == n_ctx
            # 解碼會失敗；保留空間給 batch decode。
            n_batch=min(self._n_ctx, 256),
            n_ubatch=min(self._n_ctx, 256),
            n_threads=self._n_threads,
            verbose=False,
        )

    def embed(self, text: str) -> list[float] | None:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []

        try:
            with self._lock:
                if self._llm is None:
                    self._init()
                # llama-cpp-python 0.3.16 對 BERT GGUF 的多 sequence embed
                # 會觸發 llama_decode -1；逐筆呼叫可保留順序並穩定運作。
                raw_vectors = [self._llm.embed(text) for text in texts]
        except MissingOptionalDependencyError:
            raise
        except Exception as exc:
            logger.warning("Local embedding failed: %s", exc)
            return [None] * len(texts)

        result: list[list[float] | None] = []
        for raw in raw_vectors:
            vectors = _coerce_embedding_vectors(raw, 1)
            if vectors is None:
                logger.warning("Local embedding returned an unexpected result shape")
                result.append(None)
            else:
                result.append(_l2_normalize(vectors[0]))
        return result


def _coerce_embedding_vectors(raw: Any, expected_count: int) -> list[list[Any]] | None:
    """統一 llama_cpp 不同版本可能回傳的單筆或批次向量形狀。"""
    if not isinstance(raw, list) or not raw:
        return None

    if expected_count == 1 and all(isinstance(value, (int, float)) for value in raw):
        return [raw]

    if len(raw) != expected_count:
        return None
    if not all(isinstance(vector, list) for vector in raw):
        return None
    return raw


def _l2_normalize(vector: list[Any]) -> list[float] | None:
    """將向量做 L2 正規化；零向量與非數值結果皆視為無效。"""
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return None
    return [value / norm for value in values]
