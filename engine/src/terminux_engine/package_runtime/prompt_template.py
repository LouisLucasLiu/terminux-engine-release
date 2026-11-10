# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Prompt phase 的簡易 template renderer。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from terminux_engine.package_runtime.context import PackageContext

_log = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"\{\{(.*?)\}\}")
_MAX_FILE_BYTES = 1_000_000


def render_prompt_template(template: str, ctx: PackageContext) -> str:
    """填入 prompt template；可解析的失敗一律以 fallback 文字取代。"""
    variables = _template_variables(ctx)

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key == "previous_result":
            return _format_previous_result(ctx)
        if key == "session_summary":
            return "session 摘要尚未啟用"
        if key.startswith("file_head:"):
            return _read_file_head(key[len("file_head:"):], ctx)
        if key.startswith("file:"):
            return _read_file(key[len("file:"):], ctx)
        if key in variables:
            return variables[key]
        _log.warning("Prompt template unknown variable: %s", key)
        return f"（未知變數：{key}）"

    return _TOKEN_RE.sub(_replace, template)


def _template_variables(ctx: PackageContext) -> dict[str, str]:
    event = ctx.event
    return {
        "command": event.command,
        "tool_name": event.tool_name or "",
        "cwd": event.cwd,
        "exit_code": "" if event.exit_code is None else str(event.exit_code),
        "stdout_tail": "\n".join(str(item) for item in event.stdout_tail),
        "timestamp": event.timestamp,
        "source": event.source,
        "terminal_id": event.terminal_id or "",
        "user": event.user,
        "host": event.host,
        "package_id": ctx.package_id,
        "package_version": ctx.package_version,
        "facts": _format_json(ctx.facts),
        "evidence": _format_json(ctx.evidence),
    }


def _format_previous_result(ctx: PackageContext) -> str:
    store = ctx.services.store if ctx.services is not None else None
    if store is None:
        _log.warning("Prompt template previous_result has no StoreService")
        return "尚無歷史資料"
    try:
        records = store.get_latest(n=1)
    except Exception as exc:
        _log.warning("Prompt template previous_result lookup failed: %s", exc)
        return "尚無歷史資料"
    if not records:
        return "尚無歷史資料"

    record = records[0]
    timestamp = _format_timestamp(record.get("timestamp"))
    event_type = str(record.get("event_type", "")) or "未標示"
    payload = record.get("payload", {})
    return "\n".join(
        [
            "上次結果：",
            f"- 時間：{timestamp}",
            f"- 類型：{event_type}",
            "- 內容：",
            _format_json(payload),
        ]
    )


def _read_file(raw_path: str, ctx: PackageContext) -> str:
    path = _resolve_file_path(raw_path, ctx)
    if path is None:
        return f"（無法讀取檔案：{raw_path}）"
    service = ctx.services.file if ctx.services is not None else None
    if service is None:
        _log.warning("Prompt template file service is unavailable: %s", path)
        return f"（無法讀取檔案：{path}）"
    try:
        truncated = path.stat().st_size > _MAX_FILE_BYTES
        content = service.read_text(str(path), max_bytes=_MAX_FILE_BYTES)
    except Exception as exc:
        _log.warning("Prompt template file read failed: %s: %s", path, exc)
        return f"（無法讀取檔案：{path}）"
    if truncated:
        return content + "\n（檔案已截斷至 1MB）"
    return content


def _read_file_head(raw_value: str, ctx: PackageContext) -> str:
    try:
        raw_path, raw_count = raw_value.rsplit(":", 1)
        line_count = int(raw_count)
        if line_count <= 0:
            raise ValueError("line count must be positive")
    except ValueError:
        _log.warning("Prompt template invalid file_head reference: %s", raw_value)
        return f"（無法讀取檔案前段：{raw_value}）"
    content = _read_file(raw_path, ctx)
    if content.startswith("（無法讀取檔案："):
        return content
    return "\n".join(content.splitlines()[:line_count])


def _resolve_file_path(raw_path: str, ctx: PackageContext) -> Path | None:
    if not raw_path:
        _log.warning("Prompt template file reference has empty path")
        return None
    path = Path(raw_path)
    return path if path.is_absolute() else Path(ctx.event.cwd) / path


def _format_timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value or "未標示")


def _format_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
