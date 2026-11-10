# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer client launch request 建構器。

由 `@@` 對話入口與 package action 共用；daemon 只回傳啟動參數，
實際 Viewer process 由收到 RPC response 的 client 啟動。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from terminux_engine.package_runtime.context import PackageContext


def launch_viewer(
    ctx: PackageContext | None = None,
    query: str | None = None,
    job_key: str | None = None,
) -> dict[str, Any]:
    """建立交給 RPC client 的 Viewer 啟動參數，不啟動 process。"""
    if not job_key and query:
        try:
            payload = json.loads(query)
            if isinstance(payload, dict):
                raw_job_key = payload.get("job_key")
                if isinstance(raw_job_key, str):
                    job_key = raw_job_key
        except json.JSONDecodeError:
            return {"status": "failed", "message": "query must be JSON object"}

    # DB 路徑：與 DQE package 寫入端相同的解析順序，確保 Viewer 開的是實際
    # 落盤那顆 DB（先前硬寫 ~/.terminux/dqe/dqe.sqlite，與部署用的
    # TERMINUX_DQE_DB_PATH 不一致）。
    db_path = _resolve_dqe_db_path(ctx)
    payload = {
        "tab": "temporary",
        "db": db_path,
        "temporary_db_path": db_path,
    }
    if job_key:
        payload["focus_job_key"] = job_key
    services = ctx.services if ctx is not None else None
    injected = getattr(services, "dqe_viewer_launcher", None) if services is not None else None
    if callable(injected):
        result = injected(payload)
        if isinstance(result, dict):
            payload = result
    _audit_log(job_key or "all", "client_request")
    return {"status": "requested", "launch_viewer": payload}


def _resolve_dqe_db_path(ctx: PackageContext | None) -> str:
    """解析實際 DQE_DB 路徑（順序同 DQE package 寫入端）。

    facts.dqe_db_path → event.metadata.dqe_db_path → env TERMINUX_DQE_DB_PATH →
    預設 ~/.terminux/dqe/dqe.sqlite（與 dqe.purge.DEFAULT_DQE_DB_PATH 一致）。
    """
    if ctx is not None:
        facts = getattr(ctx, "facts", None)
        metadata = getattr(getattr(ctx, "event", None), "metadata", None)
        for source in (facts, metadata):
            if isinstance(source, dict):
                value = source.get("dqe_db_path")
                if isinstance(value, str) and value:
                    return str(Path(value).expanduser())
    env_value = os.environ.get("TERMINUX_DQE_DB_PATH")
    if env_value:
        return str(Path(env_value).expanduser())
    return str(Path.home() / ".terminux" / "dqe" / "dqe.sqlite")


def _audit_log(job_key: str, trigger: str) -> None:
    audit_dir = Path.home() / ".terminux" / "dqe"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "viewer_launch_audit.jsonl"
    audit_path.write_text(
        "",
        encoding="utf-8",
    ) if not audit_path.exists() else None
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"action": "launch_viewer", "job_key": job_key, "trigger": trigger}, ensure_ascii=False) + "\n")
