# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent tool 的 request-scoped runtime authorization policy。

這不是身分系統。caller identity 一律沿用 session scope isolation 的
``CanonicalRequestContext``；本模組只保存 host-side 在目前 request 內授予的
dangerous tool allow-list。沒有 policy、context 或合法 allow-list 時一律 deny。
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from typing import Any

from terminux_engine.ipc.request_context import try_get_current_request_context


@dataclass(frozen=True)
class ProposalExecutionBinding:
    """把一次 dangerous tool 執行精確綁定到單一已核准 proposal 的
    action/arguments/scope。由 host-side operator control path 在
    ProposalRegistry.claim_for_execution() 贏得 atomic claim 之後建立——
    不由 provider tool-call arguments 提供任何欄位。

    這不是 action-name-only allowlist：`authorize_dangerous_tool()` 會用
    canonical JSON 比對實際呼叫的 arguments 是否與這筆 proposal 建立時一字
    不差；任何差異（包含 caller 試圖換一組 arguments 執行同一個已核准
    action）都會被拒絕。
    """

    proposal_id: str
    proposal_digest: str
    action_name: str
    canonical_arguments: str
    member_id: str
    project_id: str
    session_id: str


def canonicalize_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class ToolAuthorizationPolicy:
    """由可信 host-side code 建立的 immutable dangerous-tool allow-list。

    此型別不包含 member、project、session 或任何 caller-provided identity；gate
    會在執行時另行確認 session scope isolation canonical request context 存在。

    `proposal_binding` 為 evidence-first planner 新增的選擇性欄位：非 None 時，
    `authorize_dangerous_tool()` 除了既有 name-based allowlist 檢查外，還會
    要求 tool_name／arguments／member_id／project_id／session_id 與這個
    binding 完全相符，才會 ALLOW——把「這個 request 允許呼叫哪個 dangerous
    tool」收斂成「這個 request 只允許執行這一筆已核准的 proposal」。既有
    （非 proposal）dangerous tool 呼叫路徑不設這個欄位，行為完全不變。
    """

    allowed_dangerous_tools: frozenset[str]
    proposal_binding: ProposalExecutionBinding | None = None


@dataclass(frozen=True)
class ToolAuthorizationDecision:
    """Runtime gate 的內部判定；僅含穩定 reason code 與 audit session id。"""

    allowed: bool
    code: str | None = None
    session_id: str | None = None


# ContextVar 是 per-request/per-thread execution context，不是 module-global
# mutable caller 或 policy state。值只可由 host-side request boundary 設定，LLM
# tool arguments、workflow config 與 ToolSchema 都不會走到這裡。
_current_tool_authorization_policy: contextvars.ContextVar[object | None] = (
    contextvars.ContextVar("terminux_tool_authorization_policy", default=None)
)


def set_current_tool_authorization_policy(
    policy: object,
) -> contextvars.Token[object | None]:
    """在目前 trusted request 範圍設定 host-side policy，回傳 reset token。

    參數刻意以 ``object`` 接收：即使 host-side integration 誤傳 malformed
    policy，gate 仍會在副作用前 fail-closed，而不是因 setter exception 而回退到
    既有未受保護的執行路徑。
    """

    return _current_tool_authorization_policy.set(policy)


def reset_current_tool_authorization_policy(
    token: contextvars.Token[object | None],
) -> None:
    """以 setter 回傳 token 清除目前 request 的 policy。"""

    _current_tool_authorization_policy.reset(token)


def authorize_dangerous_tool(
    tool_name: str,
    *,
    known_dangerous_tools: frozenset[str],
    arguments: dict[str, Any] | None = None,
) -> ToolAuthorizationDecision:
    """在任何 registered dangerous executor 前做 deterministic fail-closed 判定。"""

    context = try_get_current_request_context()
    if context is None:
        return ToolAuthorizationDecision(
            allowed=False,
            code="REQUEST_CONTEXT_MISSING",
        )

    policy = _current_tool_authorization_policy.get()
    if policy is None:
        return ToolAuthorizationDecision(
            allowed=False,
            code="TOOL_AUTHORIZATION_POLICY_MISSING",
            session_id=context.session_id,
        )
    if not isinstance(policy, ToolAuthorizationPolicy):
        return ToolAuthorizationDecision(
            allowed=False,
            code="TOOL_AUTHORIZATION_POLICY_MALFORMED",
            session_id=context.session_id,
        )

    allowed_tools = policy.allowed_dangerous_tools
    if (
        not isinstance(allowed_tools, frozenset)
        or not all(isinstance(item, str) and item for item in allowed_tools)
        or not allowed_tools.issubset(known_dangerous_tools)
    ):
        return ToolAuthorizationDecision(
            allowed=False,
            code="TOOL_AUTHORIZATION_POLICY_MALFORMED",
            session_id=context.session_id,
        )
    if tool_name not in allowed_tools:
        return ToolAuthorizationDecision(
            allowed=False,
            code="TOOL_AUTHORIZATION_DENIED",
            session_id=context.session_id,
        )

    binding = policy.proposal_binding
    if binding is not None:
        if not isinstance(binding, ProposalExecutionBinding):
            return ToolAuthorizationDecision(
                allowed=False,
                code="TOOL_AUTHORIZATION_POLICY_MALFORMED",
                session_id=context.session_id,
            )
        # run_command（或任何非 binding.action_name 的 tool）即使被誤放進
        # allowed_dangerous_tools，只要不是 binding 綁定的那個 action，一律
        # 拒絕——這裡刻意再檢查一次 tool_name，不只依賴上面的 allowlist 檢查。
        actual_arguments_canonical = canonicalize_arguments(arguments or {})
        if (
            tool_name != binding.action_name
            or actual_arguments_canonical != binding.canonical_arguments
            or context.member_id != binding.member_id
            or context.project_id != binding.project_id
            or context.session_id != binding.session_id
        ):
            return ToolAuthorizationDecision(
                allowed=False,
                code="TOOL_AUTHORIZATION_PROPOSAL_MISMATCH",
                session_id=context.session_id,
            )

    return ToolAuthorizationDecision(allowed=True, session_id=context.session_id)
