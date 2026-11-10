# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""get_playbook_info tool。

查 playbook stages / gates / corners / overview。
Playbook spec 無頂層 rules / corners 欄位 →
- corners 從 stage_specs[].recommended_corner_order + required_reports[].corner_list 聚合
- rules section 回 "Not available in current playbook"
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


_VALID_SECTIONS = ("overview", "stages", "gates", "corners", "rules", "all")

SCHEMA = ToolSchema(
    name="get_playbook_info",
    description=(
        "Query the current playbook's stages, quality gates, corners, or "
        "overall structure. Use this to understand the canonical EDA flow "
        "definition this session is anchored to."
    ),
    parameters={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "enum": list(_VALID_SECTIONS),
                "description": "Which playbook section to return (default: overview)",
            },
        },
    },
)


def create_tool(playbook: Any | None) -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        if playbook is None:
            return {"available": False, "note": "ECC 未啟用：未載入 STA playbook。"}

        raw = (kwargs.get("section") or "overview").strip().lower()
        section = raw if raw in _VALID_SECTIONS else "overview"

        try:
            if section == "overview":
                return {"available": True, "section": "overview", **_overview(playbook)}
            if section == "stages":
                return {"available": True, "section": "stages", "stages": _stages(playbook)}
            if section == "gates":
                return {"available": True, "section": "gates", "gates": _gates(playbook)}
            if section == "corners":
                return {"available": True, "section": "corners", "corners": _corners(playbook)}
            if section == "rules":
                return {"available": True, "section": "rules", "rules": [],
                        "note": "Not available in current playbook"}
            return {
                "available": True, "section": "all",
                "overview": _overview(playbook),
                "stages": _stages(playbook),
                "gates": _gates(playbook),
                "corners": _corners(playbook),
                "rules_note": "Not available in current playbook",
            }
        except Exception as e:
            _log.warning("get_playbook_info: section %s failed: %s", section, e)
            return {"available": False, "error": f"Playbook read failed: {e}"}

    return executor


# ── section extractors ──────────────────────────────────────────────────────


def _overview(pb: Any) -> dict:
    stages = list(getattr(pb, "stage_specs", []) or [])
    corners = _collect_corners(stages)
    gate_count = sum(len(getattr(s, "quality_gates", []) or []) for s in stages)
    return {
        "id": getattr(pb, "id", ""),
        "name": getattr(pb, "name", ""),
        "version": getattr(pb, "version", ""),
        "playbook_type": getattr(pb, "playbook_type", ""),
        "description": getattr(pb, "description", ""),
        "stage_count": len(stages),
        "gate_count": gate_count,
        "corner_count": len(corners),
    }


def _stages(pb: Any) -> list[dict]:
    out: list[dict] = []
    for s in getattr(pb, "stage_specs", []) or []:
        ts = getattr(s, "tool_signatures", None)
        out.append({
            "id": getattr(s, "id", ""),
            "name": getattr(s, "name", ""),
            "tool_commands": list(getattr(ts, "commands", []) or []) if ts else [],
            "work_directory_pattern": getattr(ts, "work_directory_pattern", "") if ts else "",
            "gate_count": len(getattr(s, "quality_gates", []) or []),
            "recommended_corner_order": list(getattr(s, "recommended_corner_order", []) or []),
        })
    return out


def _gates(pb: Any) -> list[dict]:
    out: list[dict] = []
    for s in getattr(pb, "stage_specs", []) or []:
        sid = getattr(s, "id", "")
        for g in getattr(s, "quality_gates", []) or []:
            out.append({
                "stage_id": sid,
                "id": getattr(g, "id", ""),
                "metric": getattr(g, "metric", ""),
                "operator": getattr(g, "operator", ""),
                "threshold": getattr(g, "threshold", None),
                "on_fail": getattr(g, "on_fail", ""),
                "scope": getattr(g, "scope", ""),
                "description": getattr(g, "description", ""),
            })
    return out


def _corners(pb: Any) -> dict:
    stages = list(getattr(pb, "stage_specs", []) or [])
    by_stage = {
        getattr(s, "id", ""): list(getattr(s, "recommended_corner_order", []) or [])
        for s in stages
        if getattr(s, "recommended_corner_order", None)
    }
    return {"all_corners": _collect_corners(stages), "recommended_order_by_stage": by_stage}


def _collect_corners(stages: list) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in stages:
        sources = list(getattr(s, "recommended_corner_order", []) or [])
        for rr in getattr(s, "required_reports", []) or []:
            sources.extend(getattr(rr, "corner_list", []) or [])
        for c in sources:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out
