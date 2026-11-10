# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime v1 permission helpers。"""

from __future__ import annotations

from typing import Any

_KNOWN_SERVICES = frozenset({"file", "store", "notify", "report", "llm"})


def declared_service_permissions(permissions: dict[str, Any]) -> dict[str, Any]:
    """取出 manifest 宣告的 service permissions。

    v1 只保留宣告資料，不在此實作完整 permission enforcement。
    """
    services = permissions.get("services", {})
    if not isinstance(services, dict):
        return {}
    return {key: value for key, value in services.items() if key in _KNOWN_SERVICES}
