# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""HybridSearch：BM25 + Vector 混合搜索（Reciprocal Rank Fusion）。

若 Embedder 為 NoOp（embed 回 None），graceful degrade 到純 BM25。
"""

import logging
import time

from terminux_engine.memory.embedder import Embedder
from terminux_engine.memory.search_index import SearchIndex
from terminux_engine.memory.types import SearchResult

logger = logging.getLogger(__name__)

# ── RRF 常數 ──────────────────────────────────────────────────────────────────

RRF_K = 60  # 標準 RRF 常數


# ── RRF 演算法 ────────────────────────────────────────────────────────────────

def _reciprocal_rank_fusion(
    bm25_results: list[SearchResult],
    vector_results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    """Reciprocal Rank Fusion 合併 BM25 和 vector 結果。

    score(d) = Σ 1 / (k + rank_i(d))
    k = 60，rank 為 1-based。
    """
    # 建立 event_id → data 的 mapping
    bm25_map: dict[int, tuple[int, SearchResult]] = {}  # event_id → (rank, result)
    for rank, r in enumerate(bm25_results, start=1):
        bm25_map[r.event_id] = (rank, r)

    vector_map: dict[int, tuple[int, SearchResult]] = {}
    for rank, r in enumerate(vector_results, start=1):
        vector_map[r.event_id] = (rank, r)

    # 收集所有 event_id
    all_ids = set(bm25_map.keys()) | set(vector_map.keys())

    # 計算 RRF score
    scored: list[tuple[float, int, SearchResult]] = []
    for eid in all_ids:
        rrf_score = 0.0
        in_bm25 = eid in bm25_map
        in_vector = eid in vector_map

        if in_bm25:
            rank, _ = bm25_map[eid]
            rrf_score += 1.0 / (RRF_K + rank)
        if in_vector:
            rank, _ = vector_map[eid]
            rrf_score += 1.0 / (RRF_K + rank)

        # 決定 match_type
        if in_bm25 and in_vector:
            match_type = "hybrid"
        elif in_bm25:
            match_type = "bm25"
        else:
            match_type = "vector"

        # 取 result 物件（優先 bm25，它有 content）
        base = bm25_map[eid][1] if in_bm25 else vector_map[eid][1]
        merged = SearchResult(
            event_id=base.event_id,
            event_type=base.event_type,
            session_id=base.session_id,
            terminal_id=base.terminal_id,
            timestamp=base.timestamp,
            content=base.content,
            score=rrf_score,
            match_type=match_type,
        )
        scored.append((rrf_score, eid, merged))

    # 降序排列，取 top N
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[2] for item in scored[:limit]]


# ── HybridSearch ─────────────────────────────────────────────────────────────

class HybridSearch:
    """BM25 + Vector 混合搜索。"""

    def __init__(self, index: SearchIndex, embedder: Embedder) -> None:
        self._index = index
        self._embedder = embedder

    def search(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        time_window_days: int | None = None,
    ) -> list[SearchResult]:
        """混合搜索：BM25 + vector → RRF 合併。

        time_window_days: 只搜索最近 N 天的 events。None 表示不限。
        """
        since_timestamp: float | None = None
        if time_window_days is not None:
            since_timestamp = time.time() - (time_window_days * 86400)

        # 拿多一點候選，讓 RRF 有足夠的 pool
        pool_size = limit * 2

        # 1. BM25 keyword search
        bm25_results = self._index.search_bm25(
            query, user_id, limit=pool_size, since_timestamp=since_timestamp,
        )

        # 2. Vector similarity search
        vector_results: list[SearchResult] = []
        try:
            query_embedding = self._embedder.embed(query)
            if query_embedding is not None:
                vector_results = self._index.search_vector(
                    query_embedding, user_id, limit=pool_size,
                    since_timestamp=since_timestamp,
                )
        except Exception as exc:
            logger.warning("Vector search failed (degrading to BM25-only): %s", exc)

        # 3. 合併
        if not vector_results:
            # 純 BM25（NoOpEmbedder 或 vector search 失敗）
            return bm25_results[:limit]

        if not bm25_results:
            # 純 vector
            return vector_results[:limit]

        # RRF 合併
        return _reciprocal_rank_fusion(bm25_results, vector_results, limit)
