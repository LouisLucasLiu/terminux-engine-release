# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Memory search 型別定義。"""

from pydantic import BaseModel


class SearchResult(BaseModel):
    """搜索結果。"""
    event_id: int
    event_type: str
    session_id: str
    terminal_id: str
    timestamp: float
    content: str              # 被索引的文字內容
    score: float              # 排序分數（BM25 rank 或 cosine similarity）
    match_type: str           # "bm25" | "vector" | "hybrid"


class IndexEntry(BaseModel):
    """要寫入索引的條目。"""
    event_id: int
    event_type: str
    session_id: str
    terminal_id: str
    user_id: str
    timestamp: float
    content: str              # 要索引的文字
    embedding: list[float] | None = None  # vector（可後補）
