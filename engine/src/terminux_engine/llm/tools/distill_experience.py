# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""從 dreaming digest 與 chat 歷史導出可攜式經驗 tarball。"""

from __future__ import annotations

import json
import logging
import re
import tarfile
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema
from terminux_engine.llm.tools.distill_experience_prompt import (
    build_distill_experience_prompt,
)

_log = logging.getLogger(__name__)

DISTILL_TOOL_TIMEOUT_SECONDS = 120.0
_ROUTER_TIMEOUT_SECONDS = 90.0
_MAX_SOURCE_CHARS = 60_000
_MAX_CHAT_CHARS = 12_000
_EXPERIENCE_RE = re.compile(r"\[EXPERIENCE\]\s*(.*?)\s*\[/EXPERIENCE\]", re.S)


SCHEMA = ToolSchema(
    name="distill_experience",
    description=(
        "Export reusable engineering experience from the current user's dreaming "
        "digests and optional Agent chat history. Call this when the user asks to "
        "distill, export, or preserve project experience. Returns a tarball path."
    ),
    parameters={"type": "object", "properties": {}},
)


def create_tool(
    event_store: Any | None,
    llm_router: Any | None,
    default_user_id: str,
    *,
    project_id: str | None = None,
    output_dir: str | Path | None = None,
) -> Callable[..., dict[str, Any]]:
    """建立綁定目前 user、EventStore 與 LLMRouter 的 distill executor。"""
    export_root = Path(output_dir).expanduser() if output_dir else Path.home() / ".terminux"
    source_project = project_id or "unknown_project"

    def executor(**_: Any) -> dict[str, Any]:
        if event_store is None or not default_user_id:
            return _noop("歷史資料服務未初始化，無法提煉經驗")

        try:
            digests = _query_events(event_store, default_user_id, ("daily_digest", "period_digest"))
            chats = _query_events(event_store, default_user_id, ("chat_query", "ai_response"))
        except Exception as exc:
            _log.warning("Experience distill EventStore query failed: %s", exc)
            return _noop("讀取歷史資料失敗，無法提煉經驗")

        digest_entries = _digest_entries(digests)
        if not digest_entries:
            return _noop("沒有足夠歷史可提煉")
        chat_entries = _chat_entries(chats)
        source_material, truncated = _build_source_material(digest_entries, chat_entries)

        if llm_router is None:
            _log.warning("Experience distill LLMRouter is unavailable")
            return _noop("LLM 不可用，無法提煉經驗")
        try:
            from terminux_engine.llm.llm_router import TaskType

            response = llm_router.generate(
                build_distill_experience_prompt(source_project, source_material),
                task_type=TaskType.BACKGROUND,
                max_tokens=2048,
                timeout=_ROUTER_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            _log.warning("Experience distill LLM call failed: %s", exc)
            return _noop("LLM 提煉失敗，未產生匯出檔")

        experiences = _parse_experiences(response)
        if not experiences:
            _log.warning("Experience distill response parse failed: %s", str(response)[:300])
            return _noop("LLM 回覆無法解析，未產生匯出檔")

        try:
            tarball_path = _write_export(
                export_root,
                source_project,
                experiences,
                digest_count=len(digest_entries),
                chat_count=len(chat_entries),
            )
        except Exception as exc:
            _log.warning("Experience distill export write failed: %s", exc)
            return _noop("匯出檔寫入失敗，未產生 tarball")
        return {
            "status": "ok",
            "message": f"已導出 {len(experiences)} 條可複用經驗",
            "tarball_path": str(tarball_path),
            "digest_count": len(digest_entries),
            "chat_count": len(chat_entries),
            "experience_count": len(experiences),
            "input_truncated": truncated,
        }

    return executor


def _query_events(event_store: Any, user_id: str, event_types: tuple[str, ...]) -> list[Any]:
    events: list[Any] = []
    for event_type in event_types:
        events.extend(event_store.query(user_id=user_id, event_type=event_type, order="desc"))
    return events


def _digest_entries(events: list[Any]) -> list[str]:
    entries: list[tuple[float, str]] = []
    for event in events:
        payload = getattr(event, "payload", {}) or {}
        summary = payload.get("llm_summary")
        if isinstance(summary, str) and summary.strip():
            entries.append((float(getattr(event, "timestamp", 0)), f"[{event.event_type}]\n{summary.strip()}"))
    return [text for _, text in sorted(entries, reverse=True)]


def _chat_entries(events: list[Any]) -> list[str]:
    entries: list[tuple[float, str]] = []
    for event in events:
        payload = getattr(event, "payload", {}) or {}
        if event.event_type == "chat_query":
            text = f"Q: {payload.get('query', '')}\nA: {payload.get('response', '')}".strip()
        else:
            text = str(payload.get("response_text", "")).strip()
        if text:
            entries.append((float(getattr(event, "timestamp", 0)), f"[{event.event_type}]\n{text}"))
    return [text for _, text in sorted(entries, reverse=True)]


def _build_source_material(digests: list[str], chats: list[str]) -> tuple[str, bool]:
    digest_budget = _MAX_SOURCE_CHARS if not chats else _MAX_SOURCE_CHARS - _MAX_CHAT_CHARS
    digest_text, digest_truncated = _take_entries(digests, digest_budget)
    chat_text, chat_truncated = _take_entries(chats, _MAX_CHAT_CHARS)
    sections = [f"## Dreaming digests\n{digest_text}"]
    if chat_text:
        sections.append(f"## Chat records\n{chat_text}")
    return "\n\n".join(sections), digest_truncated or chat_truncated


def _take_entries(entries: list[str], budget: int) -> tuple[str, bool]:
    selected: list[str] = []
    used = 0
    for entry in entries:
        separator = 2 if selected else 0
        remaining = budget - used - separator
        if remaining <= 0:
            return "\n\n".join(selected), True
        if len(entry) > remaining:
            selected.append(entry[:remaining] + "\n[內容已因預算截斷]")
            return "\n\n".join(selected), True
        selected.append(entry)
        used += len(entry) + separator
    return "\n\n".join(selected), False


def _parse_experiences(response: Any) -> list[str]:
    if not isinstance(response, str):
        return []
    return [match.strip() + "\n" for match in _EXPERIENCE_RE.findall(response) if match.strip()]


def _write_export(
    export_root: Path,
    source_project: str,
    experiences: list[str],
    *,
    digest_count: int,
    chat_count: int,
) -> Path:
    export_root.mkdir(parents=True, exist_ok=True)
    export_date = date.today().strftime("%Y%m%d")
    export_name = f"experience_export_{export_date}"
    tarball_path = export_root / f"{export_name}.tar.gz"
    with tempfile.TemporaryDirectory(dir=export_root) as temp_dir:
        export_dir = Path(temp_dir) / export_name
        export_dir.mkdir()
        manifest = {
            "source_project": source_project,
            "export_date": export_date,
            "digest_count": digest_count,
            "chat_count": chat_count,
        }
        (export_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for index, experience in enumerate(experiences, start=1):
            (export_dir / f"distilled_{index:03d}.md").write_text(experience, encoding="utf-8")
        with tarfile.open(tarball_path, "w:gz") as archive:
            archive.add(export_dir, arcname=export_name)
    return tarball_path


def _noop(message: str) -> dict[str, Any]:
    return {"status": "noop", "message": message}
