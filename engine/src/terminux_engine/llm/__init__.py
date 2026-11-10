# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# terminux_engine.llm — Real LLM provider adapters+ LLM Router

from terminux_engine.llm.llm_router import (
    LLMNotConfiguredError,
    LLMRouter,
    TaskType,
)
from terminux_engine.llm.router_config import (
    EmbeddingProviderConfig,
    ProviderConfig,
    RouterConfig,
    load_router_config,
)
# Embedder 類已遷移到 memory/embedder.py，此處 re-export 保持向下相容
from terminux_engine.memory.embedder import (
    Embedder,
    GeminiEmbedder,
    LocalEmbedder,
    NoOpEmbedder,
)

__all__ = [
    "Embedder",
    "EmbeddingProviderConfig",
    "GeminiEmbedder",
    "LLMNotConfiguredError",
    "LLMRouter",
    "LocalEmbedder",
    "NoOpEmbedder",
    "ProviderConfig",
    "RouterConfig",
    "TaskType",
    "load_router_config",
]
