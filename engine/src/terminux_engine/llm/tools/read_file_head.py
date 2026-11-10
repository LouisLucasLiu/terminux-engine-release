# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""read_file_head tool。

讀檔前 N 行，僅允許 allowed_dirs 白名單下的檔案。
- Path.resolve() 消 symlink/..，用 is_relative_to 對白名單逐一比對
- 空白名單 → 全部拒絕；二進位（首 8KB 含 NUL）拒讀；> 10MB 跳過行數計算
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


_BINARY_PROBE_BYTES = 8192
_LINE_COUNT_BYTES_LIMIT = 10 * 1024 * 1024  # 10 MB
_MAX_LINES = 200


SCHEMA = ToolSchema(
    name="read_file_head",
    description=(
        "Read the first N lines of a text file. Only files under whitelisted "
        "directories (e.g. ~/.terminux/, current session cwd) are accessible."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or cwd-relative file path",
            },
            "n_lines": {
                "type": "integer",
                "description": "Number of leading lines to read (default 50, max 200)",
            },
        },
        "required": ["path"],
    },
)


def create_tool(allowed_dirs: list[str] | None = None) -> Callable[..., dict]:
    """Factory：注入 allowed_dirs 白名單。

    - allowed_dirs=None → 視為空 list（拒絕所有讀取）
    - 白名單 element 也走 resolve() 一次，避免使用者傳 symlink dir
    """
    resolved_allowed: list[Path] = []
    for d in allowed_dirs or []:
        try:
            resolved_allowed.append(Path(d).expanduser().resolve())
        except (OSError, RuntimeError) as e:
            _log.warning("read_file_head: skipping bad allowed_dir %r: %s", d, e)

    def executor(**kwargs: Any) -> dict:
        path_str = (kwargs.get("path") or "").strip()
        if not path_str:
            return {"error": "Missing 'path' argument"}

        try:
            n_lines = int(kwargs.get("n_lines", 50))
        except (TypeError, ValueError):
            n_lines = 50
        n_lines = max(1, min(_MAX_LINES, n_lines))

        # ── Safety: resolve + 白名單檢查 ──────────────────────────────────
        try:
            target = Path(path_str).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as e:
            return {"error": f"Path resolution failed: {e}"}

        if not _is_under_allowed(target, resolved_allowed):
            return {
                "error": (
                    f"Access denied: path '{target}' not under any allowed "
                    f"directory"
                )
            }

        if not target.exists():
            return {"error": f"File not found: {target}"}
        if not target.is_file():
            return {"error": f"Not a regular file: {target}"}

        # ── Binary probe ──────────────────────────────────────────────────
        try:
            with target.open("rb") as f:
                head_bytes = f.read(_BINARY_PROBE_BYTES)
        except OSError as e:
            return {"error": f"Read failed: {e}"}
        if b"\x00" in head_bytes:
            return {"error": "Binary file, cannot read as text"}

        # ── 讀內容 + 計總行數 ────────────────────────────────────────────
        file_size = target.stat().st_size if target.exists() else 0
        try:
            with target.open("r", encoding="utf-8", errors="replace") as f:
                lines: list[str] = []
                for i, line in enumerate(f):
                    if i >= n_lines:
                        break
                    lines.append(line.rstrip("\n"))
        except OSError as e:
            return {"error": f"Read failed: {e}"}

        total_lines: int = -1
        note: str | None = None
        if file_size <= _LINE_COUNT_BYTES_LIMIT:
            try:
                with target.open("rb") as f:
                    total_lines = sum(1 for _ in f)
            except OSError:
                pass
        else:
            note = "File too large to count lines"

        result: dict = {
            "path": str(target),
            "lines_read": len(lines),
            "total_lines": total_lines,
            "content": "\n".join(lines),
            "truncated": total_lines > len(lines) if total_lines >= 0 else True,
        }
        if note:
            result["note"] = note
        return result

    return executor


def _is_under_allowed(target: Path, allowed: list[Path]) -> bool:
    """檢查 target（已 resolve）是否落在任一 allowed dir 下。"""
    if not allowed:
        return False
    for d in allowed:
        try:
            if target == d or target.is_relative_to(d):
                return True
        except (ValueError, OSError):
            continue
    return False
