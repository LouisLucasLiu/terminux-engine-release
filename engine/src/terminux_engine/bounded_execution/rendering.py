# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""approved package action 的 user-visible rendering。

只把 service 的 machine-readable response 轉成文字。不重新判斷任何狀態，
也絕不輸出「已完成／已執行／approved」這類與實際狀態不符的字眼。
"""

from __future__ import annotations

from typing import Any

HEADER = "Bounded execution — plan step"
_MAX_ITEMS = 12


def _bullets(items: Any, indent: str = "    ") -> list[str]:
    if not isinstance(items, (list, tuple)) or not items:
        return [f"{indent}- (none)"]
    return [f"{indent}- {i}" for i in list(items)[:_MAX_ITEMS]]


def render(response: dict[str, Any]) -> str:
    status = response.get("status")
    if status == "proposed":
        return _render_proposed(response)
    if status == "blocked":
        return _render_blocked(response)
    if status == "execution_outcome_unconfirmed":
        return _render_unconfirmed(response)
    if status in ("completed", "failed"):
        return _render_executed(response)
    return _render_error(response)


def _render_proposed(r: dict[str, Any]) -> str:
    lines = [
        f"[{HEADER}] 提案已建立，等待明確核准",
        f"proposal_id: {r.get('proposal_id')}",
        f"proposal_digest: {r.get('proposal_digest')}",
        f"plan_id: {r.get('plan_id')}    step_id: {r.get('step_id')}",
        f"capability: {r.get('capability_id')}",
        f"arguments: {r.get('arguments')}",
        f"risk: {r.get('risk')}    approval_required: "
        f"{str(bool(r.get('approval_required'))).lower()}",
        f"catalog_snapshot_id: {r.get('catalog_snapshot_id')}",
        f"expires_at: {r.get('expires_at')}",
        "side_effects:", *_bullets(r.get("side_effects")),
        "preconditions:", *_bullets(r.get("preconditions")),
        "expected_evidence（預期產出，尚未發生）:", *_bullets(r.get("expected_evidence")),
        "stop_conditions:", *_bullets(r.get("stop_conditions")),
        "",
        f"capability availability: {r.get('availability_state')}",
        f"bounded_execution_enabled: {str(bool(r.get('bounded_execution_enabled'))).lower()}"
        f"（{r.get('activation_reason')}）",
        "",
        "尚未執行任何東西。核准指令：",
        f"  /approve {r.get('proposal_id')} {r.get('proposal_digest')}",
    ]
    return "\n".join(lines)


def _render_blocked(r: dict[str, Any]) -> str:
    return "\n".join([
        f"[{HEADER}] 已阻擋",
        f"error_code: {r.get('error_code')}",
        f"plan_id: {r.get('plan_id')}    step_id: {r.get('step_id')}",
        f"capability: {r.get('capability_id')}",
        "",
        "Bounded execution is implemented but disabled pending safety validation.",
        "No action was executed and the proposal was not consumed.",
        f"action_execution_count: {r.get('action_execution_count', 0)}",
        f"proposal_claimed: {str(bool(r.get('proposal_claimed'))).lower()}",
    ])


def _render_executed(r: dict[str, Any]) -> str:
    return "\n".join([
        f"[{HEADER}] {r.get('status')}",
        f"plan_id: {r.get('plan_id')}    step_id: {r.get('step_id')}",
        f"capability: {r.get('capability_id')}",
        f"dispatch_status: {r.get('dispatch_status')}",
        f"action_result_status: {r.get('action_result_status')}",
        f"dqe_status: {r.get('dqe_status')}",
        "",
        "注意：dispatch 成功不代表品質判定通過；上面兩個狀態是分開的。",
        f"receipt_id: {r.get('receipt_id')}",
        f"receipt_sha256: {r.get('receipt_sha256')}",
        f"receipt_persistence_status: {r.get('receipt_persistence_status')}",
        f"action_execution_count: {r.get('action_execution_count')}",
    ])


def _render_unconfirmed(r: dict[str, Any]) -> str:
    return "\n".join([
        f"[{HEADER}] 執行結果無法確認",
        f"error_code: {r.get('error_code')}",
        f"plan_id: {r.get('plan_id')}    step_id: {r.get('step_id')}",
        "",
        "這個 action **可能已經執行**，但它的收據沒有成功寫入 durable store。",
        "系統不會自動重試，也不會釋放一次性核准；請人工調查後再決定。",
        f"automatic_retry_allowed: {str(bool(r.get('automatic_retry_allowed'))).lower()}",
        f"manual_investigation_required: "
        f"{str(bool(r.get('manual_investigation_required'))).lower()}",
        f"receipt_persistence_status: {r.get('receipt_persistence_status')}",
    ])


def _render_error(r: dict[str, Any]) -> str:
    lines = [
        f"[{HEADER}] 無法建立或執行",
        f"error_code: {r.get('error_code')}",
        f"message: {r.get('message')}",
    ]
    details = r.get("details")
    if isinstance(details, dict) and details:
        for key in ("plan_catalog", "current_catalog", "allowlist", "problems", "conditions"):
            if key in details:
                lines.append(f"{key}: {details[key]}")
    lines += [
        "",
        "沒有任何步驟被執行，也沒有消耗任何核准。",
        "action_execution_count: 0",
        f"proposal_claimed: {str(bool(r.get('proposal_claimed'))).lower()}",
    ]
    return "\n".join(lines)


USAGE = (
    f"[{HEADER}] 用法：/plan-propose <plan_id> <step_id>\n"
    "先用 /plan-only <goal> 產生計畫，再用這個指令對其中**一個** step 建立提案。\n"
    "這個指令只建立提案，不會執行任何東西；核准仍需 /approve。\n"
    "在終端機輸入時請加 @@ 前綴。"
)


__all__ = ["HEADER", "USAGE", "render"]
