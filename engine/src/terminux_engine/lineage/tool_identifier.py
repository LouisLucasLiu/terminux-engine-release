# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ToolMatch:
    tool_name: str
    category: str
    confidence: float
    matched_pattern: str
    known_flags: tuple[str, ...]


def _load_signatures(signatures_path: Path) -> list[dict]:
    """載入並驗證 YAML，回傳 tools list。"""
    if not signatures_path.exists():
        raise FileNotFoundError(f"signatures file not found: {signatures_path}")

    with signatures_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("tool_signatures.yaml: top-level must be a mapping")

    if "tools" not in data:
        raise ValueError("tool_signatures.yaml: missing required key 'tools'")

    tools = data["tools"]
    if not isinstance(tools, list):
        raise ValueError("tool_signatures.yaml: 'tools' must be a list")

    for i, tool in enumerate(tools):
        for required in ("name", "category", "patterns"):
            if required not in tool:
                raise ValueError(
                    f"tool_signatures.yaml: tool[{i}] missing required key '{required}'"
                )
        if not isinstance(tool["patterns"], list):
            raise ValueError(
                f"tool_signatures.yaml: tool[{i}] 'patterns' must be a list"
            )

    return tools


def identify_tool(command: str, signatures_path: str | Path) -> ToolMatch | None:
    """
    從 YAML 載入 signatures，依序比對 patterns（re.search）。
    第一個 match 即回傳 ToolMatch，confidence 固定 1.0。
    unknown command 回傳 None，不拋 exception。
    """
    command = command.strip()
    tools = _load_signatures(Path(signatures_path))

    for tool in tools:
        for pattern in tool["patterns"]:
            if re.search(pattern, command):
                return ToolMatch(
                    tool_name=tool["name"],
                    category=tool["category"],
                    confidence=1.0,
                    matched_pattern=pattern,
                    known_flags=tuple(tool.get("known_flags", [])),
                )

    return None
