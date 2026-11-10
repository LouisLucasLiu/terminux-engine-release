# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""不呼叫 provider／EDA 的安裝版本機診斷入口。"""

from __future__ import annotations

import argparse
import json
import os
import stat
from importlib.metadata import version
from pathlib import Path
from typing import Any

from terminux_engine._version import __version__
from terminux_engine.version_cli import check_version_arg
from terminux_engine.ipc.agent_socket import AgentSocketError, client_agent_socket, client_lineage_socket
from terminux_engine.ipc.daemon_owner import owner_present, read_owner
from terminux_engine.resource_paths import bundled_knowledge_path, llm_config_path, plugin_dir


def _file_status(path: Path) -> dict[str, Any]:
    return {"path": str(path), "exists": path.is_file(), "readable": os.access(path, os.R_OK)}


def _credential_status() -> dict[str, Any]:
    if os.environ.get("TERMINUX_DISABLE_CREDENTIAL_FILE", "").strip() == "1":
        return {"source": "disabled", "secure": False}
    raw = os.environ.get("TERMINUX_CREDENTIALS_FILE", "").strip()
    path = Path(raw).expanduser() if raw else Path.home() / ".terminux" / "credentials.json"
    try:
        info = path.lstat()
    except OSError:
        return {"source": "file", "path": str(path), "secure": False}
    secure = (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
              and stat.S_IMODE(info.st_mode) == 0o600)
    return {"source": "file", "path": str(path), "secure": secure}


def collect_diagnostics(*, agent_socket: Path | None = None,
                        lineage_socket: Path | None = None) -> dict[str, Any]:
    """讀取本機發行資源與部署設定來源；不讀憑證內容或派送工作。"""
    def endpoint(explicit: Path | None, resolver: Any) -> Path:
        if explicit is not None:
            return explicit
        try:
            return resolver().path
        except AgentSocketError as error:
            return Path(error.expected_path)

    agent = endpoint(agent_socket, client_agent_socket)
    lineage = endpoint(lineage_socket, client_lineage_socket)
    running = owner_present()
    try:
        owner = read_owner() if running else {}
    except (OSError, ValueError):
        owner = {}
    config = llm_config_path()
    bundled_config = Path(__file__).resolve().parents[1] / "resources" / "config" / "llm_config.yaml"
    knowledge = {name: _file_status(bundled_knowledge_path(name)) for name in (
        "canonical_eda_tool_catalog.yaml", "extract_rules.yaml", "ipc_schema.json",
        "lineage_rules.yaml", "tool_signatures.yaml")}
    bindings_raw = os.environ.get("TERMINUX_EAP3_BINDINGS_FILE", "").strip()
    bindings = _file_status(Path(bindings_raw).expanduser()) if bindings_raw else None
    result = {
        "schema_version": "terminux.release_diagnostics.v1",
        "package_version": __version__,
        "generator": f"Terminux {__version__}",
        "agent_socket": {"path": str(agent), "reachable": None},
        "lineage_socket": {"path": str(lineage), "reachable": None},
        "daemon": {"running": running, "state": owner.get("state"),
                   "host": owner.get("host"), "pid": owner.get("pid")},
        "llm_config": {**_file_status(config),
                       "source": "packaged_template" if config == bundled_config else "user_or_explicit"},
        "bundled_knowledge": knowledge,
        # 0.1.0 起不隨附 plugin，目錄不存在或為空都是正常狀態；不輸出路徑。
        "plugin_dir": {"status": "ok", "exists": plugin_dir().is_dir()},
        "eap3_bindings": bindings,
        "credentials": _credential_status(),
        "background": {
            "rag_rebuild_disabled": os.environ.get(
                "TERMINUX_DISABLE_RAG_BACKGROUND_REBUILD", "").strip().lower()
                in {"1", "true", "yes"},
            "embedding_backend": os.environ.get("TERMINUX_EMBEDDING_BACKEND", "").strip()
                or "default",
        },
        "diagnostic_provider_http_calls": 0,
    }
    result["status"] = "ready" if (
        running and owner.get("state") == "ready"
        and result["llm_config"]["readable"]
        and all(row["readable"] for row in knowledge.values())) else "attention"
    return result


def main(argv: list[str] | None = None) -> int:
    check_version_arg(argv)
    parser = argparse.ArgumentParser(prog="terminux-doctor")
    parser.add_argument("--agent-socket", type=Path)
    parser.add_argument("--lineage-socket", type=Path)
    args = parser.parse_args(argv)
    result = collect_diagnostics(agent_socket=args.agent_socket,
                                 lineage_socket=args.lineage_socket)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
