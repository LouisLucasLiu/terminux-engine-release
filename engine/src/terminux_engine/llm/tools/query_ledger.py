# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""query_ledger tool。

查 artifact 的血緣（producer chain），底層走 lineage.ledger 的
find_latest_commit_by_output_path / by_output_checksum。

dependency injection：ledger_path（str | Path）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from terminux_engine.lineage.ledger import (
    find_latest_commit_by_output_checksum,
    find_latest_commit_by_output_path,
)
from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


_INPUTS_OUTPUTS_LIMIT = 10


SCHEMA = ToolSchema(
    name="query_ledger",
    description=(
        "Look up an artifact's producer (JobCommit) by absolute path or "
        "sha256 checksum. Returns the latest producer record."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_path": {
                "type": "string",
                "description": "Absolute path of the artifact",
            },
            "checksum": {
                "type": "string",
                "description": "sha256 checksum of the artifact",
            },
        },
    },
)


def create_tool(ledger_path: str | Path) -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        artifact_path = (kwargs.get("artifact_path") or "").strip()
        checksum = (kwargs.get("checksum") or "").strip()

        if not artifact_path and not checksum:
            return {
                "found": False,
                "error": "Either 'artifact_path' or 'checksum' must be provided",
            }

        commit = None
        snapshot = None
        try:
            if artifact_path:
                commit, snapshot = find_latest_commit_by_output_path(
                    ledger_path, artifact_path
                )
            if commit is None and checksum:
                commit, snapshot = find_latest_commit_by_output_checksum(
                    ledger_path, checksum
                )
        except Exception as e:
            _log.warning("query_ledger: ledger lookup failed: %s", e)
            return {
                "found": False,
                "error": f"Ledger lookup failed: {e}",
            }

        if commit is None:
            return {
                "found": False,
                "note": "No producer record found",
            }

        inputs = _snapshots_summary(commit.input_snapshots)
        outputs = _snapshots_summary(commit.output_snapshots)

        return {
            "found": True,
            "producer": {
                "job_id": commit.job_id,
                "tool_name": commit.tool_name,
                "stage": commit.stage,
                "command": getattr(commit, "command", "") or "",
                "started_at": commit.started_at,
                "ended_at": commit.ended_at,
                "block_name": commit.block_name,
                "inputs": inputs["items"],
                "inputs_truncated": inputs["truncated"],
                "outputs": outputs["items"],
                "outputs_truncated": outputs["truncated"],
            },
            "matched_snapshot": (
                {
                    "role": snapshot.role,
                    "path": snapshot.path,
                    "checksum": snapshot.checksum_sha256,
                }
                if snapshot is not None
                else None
            ),
        }

    return executor


def _snapshots_summary(snapshots: list) -> dict:
    total = len(snapshots)
    truncated = total > _INPUTS_OUTPUTS_LIMIT
    kept = snapshots[:_INPUTS_OUTPUTS_LIMIT]
    items = [
        {
            "role": getattr(s, "role", ""),
            "path": getattr(s, "path", ""),
            "checksum": getattr(s, "checksum_sha256", ""),
        }
        for s in kept
    ]
    return {"items": items, "truncated": truncated}
