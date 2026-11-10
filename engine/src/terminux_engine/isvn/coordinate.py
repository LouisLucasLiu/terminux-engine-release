# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""isvn 四層設計座標。"""

from __future__ import annotations

from dataclasses import dataclass

from terminux_engine.isvn.cli import file_query_versions


@dataclass
class DesignCoordinate:
    """由 isvn canonical URL 解析出的設計座標。"""

    phase: str | None
    block: str | None
    stage: str | None
    flow: str | None
    version: str | None
    canonical: str | None
    resolved_by: str


def parse_canonical(canonical_url: str) -> dict | None:
    """從 isvn canonical URL 解析 phase、block、stage 與 version。"""
    prefix = "isvn://"
    if not isinstance(canonical_url, str) or not canonical_url.startswith(prefix):
        return None

    parts = canonical_url[len(prefix) :].split("/")
    if len(parts) != 5 or not all(parts):
        return None

    return {
        "phase": parts[1],
        "block": parts[2],
        "stage": parts[3],
        "version": parts[4],
    }


def resolve_coordinate(
    filepath: str,
    project: str,
    timeout: float = 10.0,
) -> DesignCoordinate:
    """查詢 filepath 的 isvn 座標；無法解析時回傳未解析座標。"""
    query_result = file_query_versions(filepath, project, timeout)
    if query_result is None or query_result.get("managed") is not True:
        return _unresolved_coordinate()

    versions = query_result.get("versions")
    if not isinstance(versions, list) or not versions:
        return _unresolved_coordinate()

    first_version = versions[0]
    if not isinstance(first_version, dict):
        return _unresolved_coordinate()

    canonical = first_version.get("canonical")
    if not isinstance(canonical, str):
        return _unresolved_coordinate()

    parsed = parse_canonical(canonical)
    if parsed is None:
        return _unresolved_coordinate()

    return DesignCoordinate(
        phase=parsed["phase"],
        block=parsed["block"],
        stage=parsed["stage"],
        flow=None,
        version=parsed["version"],
        canonical=canonical,
        resolved_by="file_query",
    )


def _unresolved_coordinate() -> DesignCoordinate:
    """建立未解析的空座標。"""
    return DesignCoordinate(
        phase=None,
        block=None,
        stage=None,
        flow=None,
        version=None,
        canonical=None,
        resolved_by="unresolved",
    )
