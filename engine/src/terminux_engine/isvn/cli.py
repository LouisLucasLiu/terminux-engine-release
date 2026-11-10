# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""isvn CLI 查詢 wrapper。"""

from __future__ import annotations

import json
import logging
import subprocess

_log = logging.getLogger(__name__)


def file_query_versions(
    filepath: str,
    project: str,
    timeout: float = 10.0,
) -> dict | None:
    """呼叫 isvn file query --type versions --json，回傳解析後的 JSON dict。"""
    command = [
        "isvn",
        "file",
        "query",
        "--project",
        project,
        "--path",
        filepath,
        "--type",
        "versions",
        "--json",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        _log.warning("isvn command not found")
        return None
    except subprocess.TimeoutExpired:
        _log.warning("isvn file query timed out")
        return None

    if result.returncode != 0:
        _log.warning("isvn file query failed: %s", result.stderr.strip())
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        _log.warning("isvn file query returned invalid JSON")
        return None

    if not isinstance(payload, dict):
        _log.warning("isvn file query returned a non-object JSON payload")
        return None
    return payload
