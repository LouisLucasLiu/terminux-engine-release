# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""get_user_profile tool。

讓 Agent 查 EnrichedUserProfile（從 event store 聚合的長期行為統計）。

依賴注入：profile_loader: Callable[[], dict | None]
- caller（jsonrpc_server）負責決定怎麼取 profile（即時 build_enriched_profile +
  model_dump，或讀 cache 等），tool 不耦合到 event_store
- loader 回 None / 拋例外 → tool 收斂回 available=false 或 error，不外傳
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


SCHEMA = ToolSchema(
    name="get_user_profile",
    description=(
        "Return the enriched user profile: typical work hours, average "
        "session duration, bottleneck corners, most-used tools, and "
        "first-run WNS per corner."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)


def create_tool(
    profile_loader: Callable[[], dict | None] | None,
) -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        if profile_loader is None:
            return {"available": False, "note": "Profile loader not configured"}

        try:
            profile = profile_loader()
        except Exception as e:
            _log.warning("get_user_profile: loader raised: %s", e)
            return {"available": False, "error": f"Profile load failed: {e}"}

        if profile is None:
            return {"available": False, "note": "User profile not available"}

        if not isinstance(profile, dict):
            # 寬鬆處理：若 caller 傳回 pydantic model 物件，嘗試 model_dump
            try:
                profile = profile.model_dump()  # type: ignore[union-attr]
            except Exception:
                return {
                    "available": False,
                    "error": f"Unexpected profile type: {type(profile).__name__}",
                }

        return {"available": True, "profile": profile}

    return executor
