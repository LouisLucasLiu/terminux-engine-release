# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# terminux_engine.memory — Memory search 索引、embedding、compaction、dreaming

from terminux_engine.memory.compaction import ConversationCompactor
from terminux_engine.memory.dreaming import (
    ACTIVITY_GAP_THRESHOLD_SEC,
    DailyCronResult,
    DailyDigest,
    Dreamer,
    PeriodDigest,
    run_daily_cron,
    split_into_periods,
)
from terminux_engine.memory.embedder import (
    Embedder,
    GeminiEmbedder,
    LocalEmbedder,
    NoOpEmbedder,
)
from terminux_engine.memory.flush import MemoryFlusher
from terminux_engine.memory.hybrid_search import HybridSearch
from terminux_engine.memory.search_index import SearchIndex
from terminux_engine.memory.types import IndexEntry, SearchResult

__all__ = [
    "ACTIVITY_GAP_THRESHOLD_SEC",
    "ConversationCompactor",
    "DailyCronResult",
    "DailyDigest",
    "Dreamer",
    "Embedder",
    "GeminiEmbedder",
    "HybridSearch",
    "IndexEntry",
    "MemoryFlusher",
    "LocalEmbedder",
    "NoOpEmbedder",
    "PeriodDigest",
    "SearchIndex",
    "SearchResult",
    "run_daily_cron",
    "split_into_periods",
]
