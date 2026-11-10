# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Plan-only 的 user-visible rendering。

只把 handler 的 machine-readable response 轉成人可讀文字。它不重新判斷任何
東西——risk、approval、validation 與 `execution_allowed` 一律照抄 response。

絕不輸出「已完成／已執行／approved」這類會誤導的狀態；也絕不輸出 credential、
完整 stack trace、internal prompt 或環境變數。
"""

from __future__ import annotations

from typing import Any

HEADER = "Plan-only — not executed"
_MAX_LINE_ITEMS = 12


def _bullets(items: Any, indent: str = "      ") -> list[str]:
    if not isinstance(items, (list, tuple)) or not items:
        return [f"{indent}- (none)"]
    return [f"{indent}- {item}" for item in list(items)[:_MAX_LINE_ITEMS]]


def render_plan_only_response(response: dict[str, Any]) -> str:
    """把 response 轉成 Terminal 顯示文字。"""
    status = response.get("status")
    if status == "valid":
        return _render_valid(response)
    if status == "invalid":
        return _render_invalid(response)
    return _render_error(response)


def _common_footer(response: dict[str, Any]) -> list[str]:
    return [
        "",
        f"execution_allowed: {str(bool(response.get('execution_allowed'))).lower()}",
        "沒有任何步驟被執行，也沒有建立任何 proposal 或授權。",
        "若要實際執行，未來仍須重新查詢 capability、重新驗證 arguments 並取得明確核准。",
    ]


def _render_valid(response: dict[str, Any]) -> str:
    plan = response.get("plan") or {}
    lines = [
        f"[{HEADER}]",
        f"plan_id: {plan.get('plan_id')}",
        f"catalog_snapshot_id: {response.get('catalog_snapshot_id')}",
        f"goal: {plan.get('goal')}",
        "",
        f"steps ({len(plan.get('steps') or [])}):",
    ]
    for index, step in enumerate(plan.get("steps") or [], 1):
        lines.append(
            f"  {index}. [{step.get('step_id')}] {step.get('capability')}"
            f"  risk={step.get('risk')}  approval_required="
            f"{str(bool(step.get('approval_required'))).lower()}"
        )
        lines.append(f"      arguments: {step.get('arguments')}")
        depends = step.get("depends_on") or []
        lines.append(f"      depends_on: {depends if depends else '(none)'}")
        lines.append("      preconditions:")
        lines.extend(_bullets(step.get("preconditions"), "        "))
        lines.append("      expected_evidence (預期產出，尚未發生):")
        lines.extend(_bullets(step.get("expected_evidence"), "        "))
        lines.append("      stop_conditions:")
        lines.extend(_bullets(step.get("stop_conditions"), "        "))
        side_effects = step.get("side_effects") or []
        if side_effects:
            lines.append("      side_effects:")
            lines.extend(_bullets(side_effects, "        "))

    lines.append("")
    lines.append("unknowns:")
    lines.extend(_bullets(plan.get("unknowns"), "    "))
    lines.append("global_stop_conditions:")
    lines.extend(_bullets(plan.get("global_stop_conditions"), "    "))
    limitations = plan.get("limitations") or []
    if limitations:
        lines.append("limitations:")
        lines.extend(_bullets(limitations, "    "))
    lines.extend(_common_footer(response))
    return "\n".join(lines)


def _render_invalid(response: dict[str, Any]) -> str:
    validation = response.get("validation") or {}
    lines = [
        f"[{HEADER}] plan 未通過驗證",
        f"catalog_snapshot_id: {response.get('catalog_snapshot_id')}",
        f"goal: {response.get('goal')}",
        "",
        f"validation errors ({len(validation.get('errors') or [])}):",
    ]
    for error in (validation.get("errors") or [])[:_MAX_LINE_ITEMS]:
        lines.append(f"  - {error.get('code')}: {error.get('message')}")
    lines.append("")
    lines.append("這份 draft plan 不會被部分執行，也不會被自動轉成 proposal。")
    lines.extend(_common_footer(response))
    return "\n".join(lines)


def _render_error(response: dict[str, Any]) -> str:
    lines = [
        f"[{HEADER}] 無法產生 plan",
        f"error_code: {response.get('error_code')}",
        f"message: {response.get('message')}",
    ]
    if response.get("goal"):
        lines.append(f"goal: {response.get('goal')}")
    if response.get("catalog_snapshot_id"):
        lines.append(f"catalog_snapshot_id: {response.get('catalog_snapshot_id')}")
    for label in ("conflicts", "excluded"):
        if response.get(label):
            lines.append(f"{label}:")
            lines.extend(_bullets(response.get(label), "    "))
    lines.extend(_common_footer(response))
    return "\n".join(lines)


USAGE = (
    f"[{HEADER}] 用法：/plan-only <goal>\n"
    "例：/plan-only 規劃如何分析這次 run 為什麼沒過品質閘，只產生計畫、不要執行。\n"
    "這個指令只會產生一份計畫；它不會執行任何步驟、不會建立 proposal。\n"
    "在終端機輸入時請加 @@ 前綴。"
)


__all__ = ["HEADER", "USAGE", "render_plan_only_response"]
