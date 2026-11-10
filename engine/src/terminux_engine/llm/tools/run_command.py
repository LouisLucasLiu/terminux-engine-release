# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent 用的 shell 指令執行 tool。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from terminux_engine.llm.tool_registry import ToolSchema
from terminux_engine.llm.tools import _shell_subprocess

_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_CHARS = 8000
_TRUNCATED_PREFIX = "[... truncated, showing last 8000 chars ...]\n"


SCHEMA = ToolSchema(
    name="run_command",
    description=(
        "在 daemon process 中執行一條 shell 指令。回傳 stdout、stderr 與 exit_code。"
        "只適合可在 120 秒內完成的單次、非互動式 shell 工作"
        "（例如查看檔案列表、檢查磁碟用量、grep 搜尋）。"
        "長時間執行的 EDA workflow 應改用非阻塞的 "
        "orchestration.start 啟動，並以 orchestration.status 與既有 controls "
        "（pause/resume/cancel/retry/abandon）管理後續進度，不要用本 tool 執行。"
        "使用前必須先向使用者說明要執行什麼指令及原因，取得確認後才呼叫本 tool"
        "（此為既有 description-driven 使用規則，非 technical 核准或 permission gate）。"
        "不要用於需要互動式輸入的程式（vim、less、top 等）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要執行的 shell 指令（會透過 shell=True 執行）",
            },
            "cwd": {
                "type": "string",
                "description": "工作目錄的絕對路徑；省略時使用使用者的 home 目錄",
            },
        },
        "required": ["command"],
    },
)


def execute(*, command: str, cwd: str | None = None) -> dict[str, Any]:
    """執行 shell 指令，將預期失敗收斂為結構化結果。"""
    actual_cwd = Path.home() if not cwd else Path(cwd)
    result_base = {"command": command, "cwd": str(actual_cwd)}
    if not actual_cwd.exists():
        return _error_result(
            result_base, f"Working directory does not exist: {actual_cwd}"
        )

    try:
        process = _shell_subprocess.run_shell_command(
            command,
            cwd=str(actual_cwd),
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _error_result(
            result_base, f"Command timed out after {_TIMEOUT_SECONDS} seconds"
        )
    except Exception as exc:
        return _error_result(
            result_base, f"Execution failed: {type(exc).__name__}: {exc}"
        )

    return {
        **result_base,
        "exit_code": process.returncode,
        "stdout": _truncate(process.stdout),
        "stderr": _truncate(process.stderr),
    }


def _error_result(result_base: dict[str, str], error: str) -> dict[str, Any]:
    return {**result_base, "error": error, "exit_code": None, "stdout": "", "stderr": ""}


def _truncate(output: str) -> str:
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output
    return _TRUNCATED_PREFIX + output[-_MAX_OUTPUT_CHARS:]
