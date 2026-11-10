# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent Intelligence — Tool 註冊與執行。

提供：

- ToolSchema：tool 的 JSON Schema 描述（name / description / parameters）
- ToolExecutor：tool callable 的 Protocol（接受 **kwargs，回傳 str）
- ToolRegistry：register / unregister / execute / get_schemas

關鍵設計：

- execute() 有 per-call timeout（預設 5 秒），用 ThreadPoolExecutor 實作
- executor 拋例外時不中斷 Agent loop，回傳 success=False 的 ToolCallResult
- get_schemas 支援 'openai' 與 'xml' 雙路徑

注意：Python ThreadPoolExecutor 無法真正 kill 已啟動的 thread；timeout 後
worker 仍在背景跑，shutdown(wait=False) 不等。測試時用 threading.Event
讓 worker 可被 set() 中止，避免 process 退出時 hang。
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from terminux_engine.llm.agent_models import ToolCallResult
from terminux_engine.llm import agent_audit
from terminux_engine.llm.tool_safety import authorize_dangerous_tool

_log = logging.getLogger(__name__)

_CLOUD_EVIDENCE_TOOLS = frozenset({
    "query_orchestration_evidence", "query_orchestration_evidence_set",
})
_agent_tool_scope: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "terminux_agent_tool_scope", default=False,
)


@contextmanager
def _agent_tool_call_scope():
    token = _agent_tool_scope.set(True)
    try:
        yield
    finally:
        _agent_tool_scope.reset(token)


def _cloud_agent_mode() -> bool:
    return _agent_tool_scope.get() and os.environ.get("TERMINUX_LLM_BACKEND", "").strip().lower() in {
        "openai_compatible", "gemini",
    }


class ToolSchema(BaseModel):
    """Tool 的描述 schema。`parameters` 為 JSON Schema 物件。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolRisk(str, Enum):
    """每一個 Agent Tool 的 canonical runtime risk classification。"""

    SAFE = "SAFE"
    DANGEROUS = "DANGEROUS"


@runtime_checkable
class ToolExecutor(Protocol):
    """Tool executor 必須是接受 keyword arguments 並回傳值的 callable。

    回傳值約定為 str（JSON 序列化過的字串）；若回傳非 str，ToolRegistry
    會自動 json.dumps 後放入 ToolCallResult.result。
    """

    def __call__(self, **kwargs: Any) -> Any:
        ...


@dataclass
class _RegisteredTool:
    schema: ToolSchema
    executor: ToolExecutor
    timeout_sec: float = 5.0
    risk: ToolRisk | None = None


class ToolRegistry:
    """Tool 註冊與執行的中央表。

    用法::

        reg = ToolRegistry()
        reg.register("query_events", executor_fn, schema)
        result = reg.execute("query_events", {"limit": 10})
    """

    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}

    # ── Registration ─────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        executor: ToolExecutor,
        schema: ToolSchema,
        timeout_sec: float = 5.0,
        *,
        risk: ToolRisk | str | None = None,
    ) -> None:
        """註冊 tool。

        - name 已存在 → ValueError
        - schema.name 與 name 不一致 → ValueError（避免錯接 schema）
        """
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        if schema.name != name:
            raise ValueError(
                f"Schema name {schema.name!r} != registered name {name!r}"
            )
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        if risk is not None and not isinstance(risk, ToolRisk):
            try:
                risk = ToolRisk(risk)
            except (TypeError, ValueError) as exc:
                raise ValueError("tool risk must be SAFE or DANGEROUS") from exc
        self._tools[name] = _RegisteredTool(
            schema=schema,
            executor=executor,
            timeout_sec=timeout_sec,
            risk=risk,
        )

    def unregister(self, name: str) -> None:
        """移除 tool。不存在時 silent ignore。"""
        self._tools.pop(name, None)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def get_tool_risks(self) -> dict[str, ToolRisk | None]:
        """回傳完整 registered inventory；None 代表 unclassified 且不可執行。"""
        return {name: self._tools[name].risk for name in sorted(self._tools)}

    # ── Execution ────────────────────────────────────────────────────────────

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout_sec: float | None = None,
        *,
        tool_call_id: str | None = None,
    ) -> ToolCallResult:
        """執行 tool，所有 failure 都收斂成 ToolCallResult(success=False)。"""
        start = time.monotonic()

        if _cloud_agent_mode() and name not in _CLOUD_EVIDENCE_TOOLS:
            return self._authorization_denial(
                name, "CLOUD_AGENT_CAPABILITY_DENIED", session_id=None,
            )

        if name not in self._tools:
            agent_audit.emit(
                "tool.policy.decision", tool_call_id=tool_call_id, tool_name=name,
                classification="UNKNOWN", policy_source="tool_registry",
                decision="DENY", denial_code="TOOL_UNKNOWN", executor_entered=False,
                argument_keys=list(arguments), argument_value=arguments,
            )
            return self._authorization_denial(
                name,
                "TOOL_UNKNOWN",
                session_id=None,
            )

        tool = self._tools[name]
        if tool.risk is None:
            agent_audit.emit(
                "tool.policy.decision", tool_call_id=tool_call_id, tool_name=name,
                classification="UNCLASSIFIED", policy_source="tool_registry",
                decision="DENY", denial_code="TOOL_RISK_UNCLASSIFIED", executor_entered=False,
                argument_keys=list(arguments), argument_value=arguments,
            )
            return self._authorization_denial(
                name,
                "TOOL_RISK_UNCLASSIFIED",
                session_id=None,
            )
        if tool.risk is ToolRisk.DANGEROUS:
            decision = authorize_dangerous_tool(
                name,
                arguments=arguments,
                known_dangerous_tools=frozenset(
                    registered_name
                    for registered_name, registered_tool in self._tools.items()
                    if registered_tool.risk is ToolRisk.DANGEROUS
                ),
            )
            if not decision.allowed:
                agent_audit.emit(
                    "tool.policy.decision", tool_call_id=tool_call_id, tool_name=name,
                    classification=tool.risk.value, policy_source="request_scoped_runtime_policy",
                    decision="DENY", denial_code=decision.code, executor_entered=False,
                    argument_keys=list(arguments), argument_value=arguments,
                )
                return self._authorization_denial(
                    name,
                    decision.code or "TOOL_AUTHORIZATION_DENIED",
                    session_id=decision.session_id,
                )
            agent_audit.emit(
                "tool.policy.decision", tool_call_id=tool_call_id, tool_name=name,
                classification=tool.risk.value, policy_source="request_scoped_runtime_policy",
                decision="ALLOW", executor_entered=False,
                argument_keys=list(arguments), argument_value=arguments,
            )
        else:
            agent_audit.emit(
                "tool.policy.decision", tool_call_id=tool_call_id, tool_name=name,
                classification=tool.risk.value, policy_source="canonical_safe_classification",
                decision="ALLOW", executor_entered=False,
                argument_keys=list(arguments), argument_value=arguments,
            )
        executor = tool.executor
        effective_timeout = tool.timeout_sec if timeout_sec is None else timeout_sec
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # ThreadPoolExecutor.submit() 不會自動複製呼叫端的
        # contextvars context（只有 asyncio.loop.run_in_executor 才會）。
        # 明確 copy_context() 後用 captured.run 包裝 executor，讓 worker
        # thread 內可透過 get_current_request_context() 讀到與呼叫端相同的
        # CanonicalRequestContext；不把 context 塞進 executor 的 kwargs
        # （ToolSchema／LLM tool-call arguments 不曝露 identity）。
        captured_context = contextvars.copy_context()

        def invoke_executor() -> Any:
            executor_started = time.monotonic()
            agent_audit.emit(
                "tool.executor.started", tool_call_id=tool_call_id, tool_name=name,
                classification=tool.risk.value, executor_entered=True,
            )
            try:
                raw_value = executor(**arguments)
            except Exception as exc:
                agent_audit.emit(
                    "tool.executor.finished", tool_call_id=tool_call_id, tool_name=name,
                    classification=tool.risk.value, executor_entered=True,
                    result_status="error", result_value={"error_type": type(exc).__name__},
                    duration_ms=agent_audit.monotonic_ms(executor_started),
                )
                raise
            agent_audit.emit(
                "tool.executor.finished", tool_call_id=tool_call_id, tool_name=name,
                classification=tool.risk.value, executor_entered=True,
                result_status="success", result_value=raw_value,
                duration_ms=agent_audit.monotonic_ms(executor_started),
            )
            return raw_value

        try:
            future = pool.submit(captured_context.run, invoke_executor)
            try:
                raw = future.result(timeout=effective_timeout)
            except concurrent.futures.TimeoutError:
                elapsed = time.monotonic() - start
                _log.warning(
                    "Tool %s outer timeout after %.2fs; the worker thread cannot be "
                    "cancelled and may still complete", name, elapsed)
                return ToolCallResult(
                    call_id="",
                    tool_name=name,
                    result="",
                    success=False,
                    error="Tool execution timeout (outcome unconfirmed: the worker "
                          "thread cannot be cancelled and may still complete)",
                    elapsed_sec=elapsed,
                    timed_out=True,
                )
            except Exception as e:
                elapsed = time.monotonic() - start
                _log.warning("Tool %s raised: %s", name, e)
                return ToolCallResult(
                    call_id="",
                    tool_name=name,
                    result="",
                    success=False,
                    error=str(e),
                    elapsed_sec=elapsed,
                )

            elapsed = time.monotonic() - start
            result_str = (
                raw
                if isinstance(raw, str)
                else json.dumps(raw, ensure_ascii=False, default=str)
            )
            return ToolCallResult(
                call_id="",
                tool_name=name,
                result=result_str,
                success=True,
                elapsed_sec=elapsed,
            )
        finally:
            # 不等 hung thread；Python 無法 kill 已啟動的 worker，靠 caller
            # 設計可中止的 executor（如 Event.wait）來避免 process 退出時 hang。
            pool.shutdown(wait=False)

    @staticmethod
    def _authorization_denial(
        name: str,
        code: str,
        *,
        session_id: str | None,
    ) -> ToolCallResult:
        """回傳零副作用、無 arguments 的穩定 structured denial error。"""
        _log.warning(
            "TERM_TOOL_GATE_DENIED tool=%s code=%s session_id=%s",
            name,
            code,
            session_id or "-",
        )
        return ToolCallResult(
            call_id="",
            tool_name=name,
            result="",
            success=False,
            error=json.dumps(
                {
                    "code": code,
                    "message": "Tool call denied by runtime authorization policy",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            elapsed_sec=0.0,
        )

    # ── Schema Export ────────────────────────────────────────────────────────

    def get_schemas(
        self,
        format: Literal["openai", "xml"] = "openai",
    ) -> list[dict[str, Any]] | str:
        """匯出已註冊 tool 的 schema。

        - 'openai': 回傳 list[dict]，每筆為 OpenAI function calling 標準格式
        - 'xml':    回傳描述字串（內含 <tool> 區塊與 <tool_call> 範例 + [NO_ACTION] 指示）
        """
        if format == "openai":
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t.schema.name,
                        "description": t.schema.description,
                        "parameters": t.schema.parameters,
                    },
                }
                for t in self._tools.values()
                if not _cloud_agent_mode() or t.schema.name in _CLOUD_EVIDENCE_TOOLS
            ]

        if format == "xml":
            lines: list[str] = ["<tools>"]
            for t in self._tools.values():
                if _cloud_agent_mode() and t.schema.name not in _CLOUD_EVIDENCE_TOOLS:
                    continue
                params_json = json.dumps(t.schema.parameters, ensure_ascii=False)
                lines.extend(
                    [
                        "<tool>",
                        f"<name>{t.schema.name}</name>",
                        f"<description>{t.schema.description}</description>",
                        f"<parameters>{params_json}</parameters>",
                        "</tool>",
                    ]
                )
            lines.extend(
                [
                    "</tools>",
                    "",
                    "To call a tool, respond with one or more blocks:",
                    "<tool_call>",
                    "<name>tool_name</name>",
                    '<arguments>{"arg": "value"}</arguments>',
                    "</tool_call>",
                    "",
                    "To indicate no action is needed, respond with: [NO_ACTION]",
                ]
            )
            return "\n".join(lines)

        raise ValueError(f"Unknown schema format: {format}")

    def get_schemas_for_names(self, names: tuple[str, ...]) -> list[dict[str, Any]]:
        """只輸出 host-validated capability；未知名稱 fail-closed。"""
        if any(name not in self._tools or
               (_cloud_agent_mode() and name not in _CLOUD_EVIDENCE_TOOLS)
               for name in names):
            raise ValueError("unknown selected tool")
        return [
            {"type": "function", "function": {"name": self._tools[name].schema.name,
             "description": self._tools[name].schema.description,
             "parameters": self._tools[name].schema.parameters}}
            for name in names
        ]
