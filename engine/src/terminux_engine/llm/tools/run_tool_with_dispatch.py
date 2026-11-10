# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""執行 EDA 工具並觸發 package after dispatch 的 Agent tool。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema
from terminux_engine.llm.tools import _shell_subprocess

_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_CHARS = 8000

SCHEMA = ToolSchema(
    name="run_tool_with_dispatch",
    description=(
        "執行單次 EDA 工具指令，並在工具結束後觸發對應 DQE package 判讀。"
        "只適合可在 120 秒內完成的單次、非互動式執行；不適合驅動完整的多輪 "
        "EDA workflow（例如完整 BE ECO 迭代）——那類長時間、多步驟的 workflow "
        "應改用非阻塞的 orchestration.start 啟動，並以 orchestration.status 與"
        "既有 controls（pause/resume/cancel/retry/abandon）管理後續進度。"
        "使用前需先向使用者說明要執行什麼指令及原因，取得確認後才呼叫本 tool"
        "（此為既有 description-driven 使用規則，非 technical 核准或 permission gate）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "完整工具指令"},
            "cwd": {"type": "string", "description": "工具工作目錄"},
            "tool_name": {"type": "string", "description": "用於 package 匹配的工具名稱"},
            "metadata": {"type": "object", "description": "注入 TimelineEvent 的 metadata"},
        },
        "required": ["command", "cwd", "tool_name"],
    },
)


def create_tool(
    dispatch_callback: Callable[..., None] | None,
) -> Callable[..., dict[str, Any]]:
    """建立注入 daemon dispatch callback 的 tool executor。"""
    def execute(
        *,
        command: str,
        cwd: str,
        tool_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """執行指令並將 caller metadata 傳給 package dispatch。"""
        actual_cwd = Path(cwd)
        base = {"command": command, "cwd": str(actual_cwd), "tool_name": tool_name}
        if not actual_cwd.is_dir():
            return {**base, "exit_code": None, "stdout": "", "stderr": "", "error": f"Working directory does not exist: {actual_cwd}", "triggered_packages": []}
        try:
            process = _shell_subprocess.run_shell_command(
                command,
                cwd=str(actual_cwd),
                timeout=_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {**base, "exit_code": None, "stdout": "", "stderr": "", "error": f"Command timed out after {_TIMEOUT_SECONDS} seconds", "triggered_packages": []}
        except Exception as error:
            return {**base, "exit_code": None, "stdout": "", "stderr": "", "error": f"Execution failed: {type(error).__name__}: {error}", "triggered_packages": []}
        if dispatch_callback is not None:
            dispatch_callback(
                command=command,
                tool_name=tool_name,
                cwd=str(actual_cwd),
                exit_code=process.returncode,
                stdout_tail=process.stdout.splitlines()[-100:],
                source="agent.run_tool_with_dispatch",
                terminal_id="default",
                extra_metadata=metadata or {},
            )
        return {
            **base,
            "exit_code": process.returncode,
            "stdout": _truncate(process.stdout),
            "stderr": _truncate(process.stderr),
            "triggered_packages": [tool_name] if dispatch_callback is not None else [],
        }
    return execute


def _truncate(output: str) -> str:
    """限制 tool 回傳內容大小。"""
    return output if len(output) <= _MAX_OUTPUT_CHARS else "[... truncated ...]\n" + output[-_MAX_OUTPUT_CHARS:]
