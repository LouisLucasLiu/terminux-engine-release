# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""retrieve_knowledge tool。

讓 Agent 從 RAG 知識庫撈 EDA domain 內容。索引內容暫由
`terminux-build-rag-index` CLI 離線建置（知識源由使用者提供）。

依賴注入：rag_index: RAGIndex | None。為 None 時 tool 仍註冊（spec 規定），
executor 回 {"available": False, "note": "Knowledge base not initialized"}。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


SCHEMA = ToolSchema(
    name="retrieve_knowledge",
    description=(
        "Search the EDA domain knowledge base for relevant information. "
        "Use this when you need background knowledge about EDA tools, "
        "STA flow best practices, timing closure strategies, "
        "or playbook rules and definitions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query describing what knowledge you need",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return, default 5, max 10",
            },
        },
        "required": ["query"],
    },
)


def create_tool(rag_index: Any | Callable[[], Any | None] | None) -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        # 部分既有 index mock 亦可呼叫；有 query() 時一律視為 index 本身。
        current_index = (
            rag_index
            if rag_index is None or hasattr(rag_index, "query")
            else rag_index()
        )
        if current_index is None:
            return {
                "available": False,
                "note": "Knowledge base not initialized",
            }

        query = (kwargs.get("query") or "").strip()
        if not query:
            return {
                "available": True,
                "results": [],
                "query": "",
                "note": "Empty query",
            }

        try:
            top_k = int(kwargs.get("top_k", 5))
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(10, top_k))

        try:
            results = current_index.query(query, top_k=top_k)
        except Exception as e:
            _log.warning("retrieve_knowledge: rag_index.query failed: %s", e)
            return {
                "available": True,
                "results": [],
                "query": query,
                "error": f"Query failed: {e}",
            }

        return {
            "available": True,
            "query": query,
            "results": [
                {
                    "text": r.text_chunk,
                    "source": r.source_file,
                    "score": round(r.relevance_score, 4),
                }
                for r in results
            ],
            "note": "No results found" if not results else None,
        }

    return executor
