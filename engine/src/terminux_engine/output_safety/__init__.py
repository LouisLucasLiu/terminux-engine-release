# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Common 的模型輸出安全機制。Vendor-neutral、domain-blind。"""

from terminux_engine.output_safety.command_text import (
    COMMAND_WORDS,
    EXECUTION_VERB_INVOCATION,
    REASON_CODES,
    CommandFinding,
    find_command_invocation,
    looks_like_command,
)

__all__ = [
    "COMMAND_WORDS",
    "EXECUTION_VERB_INVOCATION",
    "REASON_CODES",
    "CommandFinding",
    "find_command_invocation",
    "looks_like_command",
]
