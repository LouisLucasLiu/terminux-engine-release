# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""shell.post_exec Host Tool Identity Validation — 窄幅 validator。

**Security statement（必讀）**：本模組提供的是 *server-side consistency
validation*——確認 request 內「command 衍生出的 tool identity」與「client
宣稱的 tool_name」互相一致，並以 Canonical EDA Tool Catalog v1 canonicalize
兩者。這**不是** kernel-attested command execution proof：`command` 與
`claimed_tool_name` 仍然來自同一個 same-UID client，惡意 client 依然可能
一致地偽造這兩個欄位（例如送 `command="dc_shell -f x"` 且
`tool_name="dc_shell"`，而 dc_shell 從未真的執行）。本模組只消除「單獨偽造
一個不一致的 tool_name 欄位即可選中 package」這一種漏洞，不提供也不宣稱提供
完整 shell security boundary 或 command 真的執行過的證明。

只處理 identity 判斷本身，不得引用／耦合 Agent ToolRegistry、
PackageRegistry、orchestration 或任何 individual package。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from terminux_engine.tool_identity.catalog import ToolCatalog


class ValidationDecision(str, Enum):
    """Decision matrix 的 Result 欄。"""

    ACCEPT = "ACCEPT"
    ACCEPT_NON_EDA = "ACCEPT_NON_EDA"
    DENY_MISMATCH = "DENY_MISMATCH"
    DENY_UNVERIFIABLE_CLAIM = "DENY_UNVERIFIABLE_CLAIM"
    DENY_MALFORMED = "DENY_MALFORMED"


@dataclass(frozen=True)
class HostValidationResult:
    """Immutable structured 結果。Consumer 不得重新解讀 decision——
    `dispatch_allowed` 與 `timeline_tool_name` 是唯一該被消費的欄位，
    其餘（`reason_code`／`normalized_claim`）只供 audit 記錄。"""

    decision: ValidationDecision
    reason_code: str
    derived_canonical_id: str | None
    normalized_claim: str | None
    dispatch_allowed: bool

    @property
    def timeline_tool_name(self) -> str | None:
        """`TimelineEvent.tool_name` 唯一合法來源——只用 server-derived
        canonical id 或 `None`，絕不使用 client 原始宣稱字串。"""
        return self.derived_canonical_id if self.dispatch_allowed else None


def validate_shell_post_exec_tool_identity(
    *,
    command: str | None,
    claimed_tool_name: str | None,
    catalog: ToolCatalog,
) -> HostValidationResult:
    """`shell.post_exec` tool identity 驗證。

    Args:
        command: 已 sanitize 過的 command 字串（ANSI 已移除）。`None` 代表
            request 層級型別驗證失敗（正常 RPC 流程應已在更早的
            `_check_shell_post_exec_params` 擋下並回傳 JSON-RPC error，
            此處接受 `None` 是為了讓本函式可獨立單元測試 malformed input，
            不依賴呼叫端行為）。空字串／純空白視為「無 command」，走
            unknown/non-EDA 分支，非 malformed。
        claimed_tool_name: client 送來的 `tool_name` 欄位原始值；`None`
            代表 client 未提供（目前所有 official client 皆屬此類——沒有
            任何 client 會主動宣稱 tool_name，此欄位是為了讓未來任何
            claim-bearing client 都必須經過本驗證，而非被直接信任）。
        catalog: Canonical EDA Tool Catalog v1（唯一 authoritative
            derivation 依據）。

    **Derivation 方法的誠實限制**：本函式對 `command` 做的仍是
    `command.split()` 取第一個 token 再丟進 catalog——因為目前沒有任何
    official client 送出結構化 `argv`（preflight 階段）。
    這不是「更好的原始資料」，差別在於：這個 token 不再被直接當成
    tool_name 使用，而是先經過 catalog canonicalization（basename／
    case-sensitive exact match），且**唯一**能決定 dispatch 的值。
    """
    if command is None or not isinstance(command, str):
        return HostValidationResult(
            decision=ValidationDecision.DENY_MALFORMED,
            reason_code="malformed_command_type",
            derived_canonical_id=None,
            normalized_claim=None,
            dispatch_allowed=False,
        )

    if claimed_tool_name is not None and not isinstance(claimed_tool_name, str):
        return HostValidationResult(
            decision=ValidationDecision.DENY_MALFORMED,
            reason_code="malformed_claim_type",
            derived_canonical_id=None,
            normalized_claim=None,
            dispatch_allowed=False,
        )

    tokens = command.split()
    derived = catalog.resolve(tokens[0]) if tokens else None
    claim_provided = claimed_tool_name is not None and claimed_tool_name.strip() != ""
    normalized_claim = catalog.resolve(claimed_tool_name) if claim_provided else None

    if derived is not None:
        # command 衍生出已知 EDA tool。
        if not claim_provided:
            return HostValidationResult(
                decision=ValidationDecision.ACCEPT,
                reason_code="known_no_claim",
                derived_canonical_id=derived,
                normalized_claim=None,
                dispatch_allowed=True,
            )
        if normalized_claim == derived:
            # 涵蓋「相同 canonical id」與「valid alias of same id」兩列
            # （catalog.resolve 已把 alias 正規化成同一個 canonical id）。
            return HostValidationResult(
                decision=ValidationDecision.ACCEPT,
                reason_code="known_claim_matches",
                derived_canonical_id=derived,
                normalized_claim=normalized_claim,
                dispatch_allowed=True,
            )
        # 涵蓋「claim 是另一個已知 id」與「claim 是 unknown」兩列，
        # 皆為 DENY_MISMATCH（decision matrix 明確要求兩者結果相同）。
        return HostValidationResult(
            decision=ValidationDecision.DENY_MISMATCH,
            reason_code=(
                "known_claim_different_known_id"
                if normalized_claim is not None
                else "known_claim_unknown"
            ),
            derived_canonical_id=derived,
            normalized_claim=normalized_claim,
            dispatch_allowed=False,
        )

    # derived is None：command 衍生不出已知 EDA tool（unknown／non-EDA／空白）。
    if not claim_provided:
        return HostValidationResult(
            decision=ValidationDecision.ACCEPT_NON_EDA,
            reason_code="non_eda_no_claim",
            derived_canonical_id=None,
            normalized_claim=None,
            dispatch_allowed=False,
        )
    if normalized_claim is not None:
        # command 衍生不出已知 EDA tool，但 client 宣稱一個已知 EDA tool——
        # 這正是本模組要消除的漏洞：無法驗證的 claim 不得觸發 dispatch。
        return HostValidationResult(
            decision=ValidationDecision.DENY_UNVERIFIABLE_CLAIM,
            reason_code="non_eda_known_eda_claim",
            derived_canonical_id=None,
            normalized_claim=normalized_claim,
            dispatch_allowed=False,
        )
    # claim 也是 unknown（例如 sta_run／mmmc_sum 這類 logical-only 名稱、
    # 或任意非 catalog 字串）——維持既有 non-EDA 相容行為，不得誤判為
    # mismatch 攻擊。
    return HostValidationResult(
        decision=ValidationDecision.ACCEPT_NON_EDA,
        reason_code="non_eda_unknown_claim",
        derived_canonical_id=None,
        normalized_claim=normalized_claim,
        dispatch_allowed=False,
    )
