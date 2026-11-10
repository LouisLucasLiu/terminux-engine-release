# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Core Agent Tools（15 個）。

組成（依 Round 順序）：
- 核心 (5)：query_events / query_metric_trend / query_ledger /
                  get_current_ecc / get_team_status
- RAG (1)：retrieve_knowledge
- 延伸 (4)：read_file_head / list_sessions / get_playbook_info /
                  get_user_profile

每個 tool module 都有 SCHEMA + create_tool() factory。register_core_tools()
是便利函數，依注入的 dependencies 一次註冊全部 15 個 tool。所有新增的依賴
（rag_index / allowed_dirs / playbook / profile_loader）都有 default 值，
舊 caller 不必改。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolRegistry, ToolRisk

from terminux_engine.llm.tools.distill_experience import (
    DISTILL_TOOL_TIMEOUT_SECONDS,
    SCHEMA as DISTILL_EXPERIENCE_SCHEMA,
    create_tool as create_distill_experience,
)
from terminux_engine.llm.tools.import_experience import (
    IMPORT_TOOL_TIMEOUT_SECONDS,
    SCHEMA as IMPORT_EXPERIENCE_SCHEMA,
    create_tool as create_import_experience,
)
from terminux_engine.llm.tools.get_current_ecc import (
    SCHEMA as GET_CURRENT_ECC_SCHEMA,
    create_tool as create_get_current_ecc,
)
from terminux_engine.llm.tools.get_playbook_info import (
    SCHEMA as GET_PLAYBOOK_INFO_SCHEMA,
    create_tool as create_get_playbook_info,
)
from terminux_engine.llm.tools.get_team_status import (
    SCHEMA as GET_TEAM_STATUS_SCHEMA,
    create_tool as create_get_team_status,
)
from terminux_engine.llm.tools.get_user_profile import (
    SCHEMA as GET_USER_PROFILE_SCHEMA,
    create_tool as create_get_user_profile,
)
from terminux_engine.llm.tools.list_sessions import (
    SCHEMA as LIST_SESSIONS_SCHEMA,
    create_tool as create_list_sessions,
)
from terminux_engine.llm.tools.query_events import (
    SCHEMA as QUERY_EVENTS_SCHEMA,
    create_tool as create_query_events,
)
from terminux_engine.llm.tools.query_ledger import (
    SCHEMA as QUERY_LEDGER_SCHEMA,
    create_tool as create_query_ledger,
)
from terminux_engine.llm.tools.query_metric_trend import (
    SCHEMA as QUERY_METRIC_TREND_SCHEMA,
    create_tool as create_query_metric_trend,
)
from terminux_engine.llm.tools.read_file_head import (
    SCHEMA as READ_FILE_HEAD_SCHEMA,
    create_tool as create_read_file_head,
)
from terminux_engine.llm.tools.run_command import (
    SCHEMA as RUN_COMMAND_SCHEMA,
    execute as execute_run_command,
)
from terminux_engine.llm.tools.run_tool_with_dispatch import (
    SCHEMA as RUN_TOOL_WITH_DISPATCH_SCHEMA,
    create_tool as create_run_tool_with_dispatch,
)
from terminux_engine.llm.tools.retrieve_knowledge import (
    SCHEMA as RETRIEVE_KNOWLEDGE_SCHEMA,
    create_tool as create_retrieve_knowledge,
)
from terminux_engine.llm.tools.open_report import (
    SCHEMA as OPEN_REPORT_SCHEMA,
    create_tool as create_open_report,
)
from terminux_engine.llm.tools.orchestration_tools import register_orchestration_tools
from terminux_engine.llm.tools.query_orchestration_evidence import (
    SCHEMA as QUERY_ORCHESTRATION_EVIDENCE_SCHEMA,
    create_tool as create_query_orchestration_evidence,
)
from terminux_engine.llm.tools.query_orchestration_evidence_set import (
    SCHEMA as QUERY_ORCHESTRATION_EVIDENCE_SET_SCHEMA,
    create_tool as create_query_orchestration_evidence_set,
)
from terminux_engine.orchestration.daemon_service import OrchestrationService

__all__ = [
    "DISTILL_EXPERIENCE_SCHEMA",
    "IMPORT_EXPERIENCE_SCHEMA",
    "OPEN_REPORT_SCHEMA",
    "GET_CURRENT_ECC_SCHEMA",
    "GET_PLAYBOOK_INFO_SCHEMA",
    "GET_TEAM_STATUS_SCHEMA",
    "GET_USER_PROFILE_SCHEMA",
    "LIST_SESSIONS_SCHEMA",
    "QUERY_EVENTS_SCHEMA",
    "QUERY_LEDGER_SCHEMA",
    "QUERY_METRIC_TREND_SCHEMA",
    "READ_FILE_HEAD_SCHEMA",
    "RUN_COMMAND_SCHEMA",
    "RUN_TOOL_WITH_DISPATCH_SCHEMA",
    "RETRIEVE_KNOWLEDGE_SCHEMA",
    "create_distill_experience",
    "create_import_experience",
    "create_get_current_ecc",
    "create_get_playbook_info",
    "create_get_team_status",
    "create_get_user_profile",
    "create_list_sessions",
    "create_query_events",
    "create_query_ledger",
    "create_query_metric_trend",
    "create_read_file_head",
    "execute_run_command",
    "create_run_tool_with_dispatch",
    "create_retrieve_knowledge",
    "create_open_report",
    "register_core_tools",
]


def register_core_tools(
    registry: ToolRegistry,
    *,
    event_store: Any,
    ledger_path: str | Path,
    accumulator: Any,
    team_snapshot: Any,
    user_id: str,
    rag_index: Any | None = None,
    allowed_dirs: list[str] | None = None,
    playbook: Any | None = None,
    profile_loader: Callable[[], dict | None] | None = None,
    report_open_callback: Callable[[str], None] | None = None,
    llm_router: Any | None = None,
    project_id: str | None = None,
    dispatch_callback: Callable[..., None] | None = None,
    orchestration_service: OrchestrationService | None = None,
) -> None:
    """一次註冊 15 個 core tools 到 registry。

    依賴注入規約：

    - event_store / accumulator / team_snapshot / rag_index / playbook /
      profile_loader 可為 None；對應 tool 會在 executor 內 fallback
      （例如 playbook=None → get_playbook_info 回 available=false）
    - user_id 由 caller 顯式傳入，通常為 getpass.getuser()
    - allowed_dirs：read_file_head 的白名單。None → 預設只允許 ~/.terminux/
      （fail-safe；caller 應顯式傳入 cwd 等其它目錄）
    - profile_loader：0-arg callable，回傳 dict 或 None
    - orchestration_service：optional injection。None → 不註冊任何
      `orchestration.*` tool 與 Evidence Bundle tool；非 None 時額外
      註冊八項既有 `orchestration.*` core Agent tool，以及一項獨立的
      `query_orchestration_evidence` read-only tool。
    """
    default_user_id = user_id or ""

    if allowed_dirs is None:
        allowed_dirs = [str(Path.home() / ".terminux")]

    # core 5
    registry.register(
        "query_events",
        create_query_events(event_store, default_user_id),
        QUERY_EVENTS_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    registry.register(
        "query_metric_trend",
        create_query_metric_trend(event_store, default_user_id),
        QUERY_METRIC_TREND_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    registry.register(
        "query_ledger",
        create_query_ledger(ledger_path),
        QUERY_LEDGER_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    registry.register(
        "get_current_ecc",
        create_get_current_ecc(accumulator),
        GET_CURRENT_ECC_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    registry.register(
        "get_team_status",
        create_get_team_status(team_snapshot),
        GET_TEAM_STATUS_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    # RAG
    registry.register(
        "retrieve_knowledge",
        create_retrieve_knowledge(rag_index),
        RETRIEVE_KNOWLEDGE_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    # expansion 4
    registry.register(
        "read_file_head",
        create_read_file_head(allowed_dirs),
        READ_FILE_HEAD_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    # 內層 subprocess timeout 為 120 秒（見 run_command.py／
    # run_tool_with_dispatch.py 的 `_TIMEOUT_SECONDS`）；外層 ToolRegistry
    # 明確指定 125 秒（> 120），確保內層先完成 timeout／kill／reap 並回傳
    # 正常 tool failure result，外層 ThreadPoolExecutor 不會在內層仍在跑
    # 時搶先判定「Tool execution timeout」而讓 worker thread 背景繼續執行。
    registry.register(
        "run_command",
        execute_run_command,
        RUN_COMMAND_SCHEMA,
        timeout_sec=125.0,
        risk=ToolRisk.DANGEROUS,
    )
    registry.register(
        "run_tool_with_dispatch",
        create_run_tool_with_dispatch(dispatch_callback),
        RUN_TOOL_WITH_DISPATCH_SCHEMA,
        timeout_sec=125.0,
        risk=ToolRisk.DANGEROUS,
    )
    registry.register(
        "distill_experience",
        create_distill_experience(
            event_store,
            llm_router,
            default_user_id,
            project_id=project_id,
        ),
        DISTILL_EXPERIENCE_SCHEMA,
        timeout_sec=DISTILL_TOOL_TIMEOUT_SECONDS,
        risk=ToolRisk.DANGEROUS,
    )
    registry.register(
        "import_experience",
        create_import_experience(),
        IMPORT_EXPERIENCE_SCHEMA,
        timeout_sec=IMPORT_TOOL_TIMEOUT_SECONDS,
        risk=ToolRisk.DANGEROUS,
    )
    registry.register(
        "list_sessions",
        create_list_sessions(event_store, default_user_id),
        LIST_SESSIONS_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    registry.register(
        "get_playbook_info",
        create_get_playbook_info(playbook),
        GET_PLAYBOOK_INFO_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    registry.register(
        "get_user_profile",
        create_get_user_profile(profile_loader),
        GET_USER_PROFILE_SCHEMA,
        risk=ToolRisk.SAFE,
    )
    # open_report：唯一有副作用的 tool（請求開啟既有報表 HTML）。
    # report_open_callback=None → tool 仍可用，回 opened=True 但不實際觸發開啟
    # （無 GUI 通道時的 fail-safe）。
    registry.register(
        "open_report",
        create_open_report(report_open_callback),
        OPEN_REPORT_SCHEMA,
        risk=ToolRisk.DANGEROUS,
    )
    # orchestration_service=None 時不註冊，保持既有 core tool 集合不變。
    if orchestration_service is not None:
        register_orchestration_tools(registry, service=orchestration_service)
        registry.register(
            "query_orchestration_evidence",
            create_query_orchestration_evidence(orchestration_service),
            QUERY_ORCHESTRATION_EVIDENCE_SCHEMA,
            risk=ToolRisk.SAFE,
        )
        # scoped multi-run（2～3）Evidence Set comparison tool。
        registry.register(
            "query_orchestration_evidence_set",
            create_query_orchestration_evidence_set(orchestration_service),
            QUERY_ORCHESTRATION_EVIDENCE_SET_SCHEMA,
            risk=ToolRisk.SAFE,
        )
