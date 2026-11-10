# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""isvn provenance query wrapper。"""

from __future__ import annotations

import json
import logging
import subprocess

_log = logging.getLogger(__name__)


def provenance_parent(
    project: str,
    version: str,
    timeout: float = 10.0,
) -> dict | None:
    """查詢指定 version 的直接 parent。"""
    return _provenance_query(project, version, "parent", timeout=timeout)


def provenance_ancestor_chain(
    project: str,
    version: str,
    max_depth: int = 20,
    timeout: float = 10.0,
) -> dict | None:
    """查詢指定 version 的祖先鏈。"""
    return _provenance_query(
        project,
        version,
        "ancestor-chain",
        max_depth=max_depth,
        timeout=timeout,
    )


def provenance_root(
    project: str,
    version: str,
    timeout: float = 10.0,
) -> dict | None:
    """查詢指定 version 的 root。"""
    return _provenance_query(project, version, "root", timeout=timeout)


def provenance_pedigree(
    project: str,
    version: str,
    max_depth: int = 20,
    timeout: float = 10.0,
) -> dict | None:
    """查詢指定 version 的 root-to-subject pedigree。"""
    return _provenance_query(
        project,
        version,
        "pedigree",
        max_depth=max_depth,
        timeout=timeout,
    )


def _provenance_query(
    project: str,
    version: str,
    query_type: str,
    *,
    max_depth: int | None = None,
    timeout: float,
) -> dict | None:
    """執行單一 isvn provenance query 並驗證成功狀態。"""
    command = [
        "isvn",
        "provenance",
        "query",
        "--project",
        project,
        "--version",
        version,
        "--type",
        query_type,
    ]
    if max_depth is not None:
        command.extend(["--max-depth", str(max_depth)])
    command.append("--json")

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
        _log.warning("isvn provenance query timed out")
        return None

    if result.returncode != 0:
        _log.warning("isvn provenance query failed: %s", result.stderr.strip())
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        _log.warning("isvn provenance query returned invalid JSON")
        return None

    if not isinstance(payload, dict):
        _log.warning("isvn provenance query returned a non-object JSON payload")
        return None
    if payload.get("status") != "ok":
        _log.warning("isvn provenance query returned non-ok status")
        return None
    return payload
