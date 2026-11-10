# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Engineering Work CLI／@@ 共用文字呈現。"""

from __future__ import annotations

from typing import Any

USAGE = """Engineering Work 指令：
  /work-create <profile_id> <package_id> <目標>
  terminux-work create ... [--expert <id>] [--review proposal_review|result_review]
  /work-authorize <work_id> <plan|proposal|bounded>
  /work-approve <work_id>
  /work-continue <work_id>
  /work <work_id>
  /works
  /work-answer <work_id> <回答或補充>
  /work-pause <work_id>
  /work-resume <work_id>
  /work-cancel <work_id>
  /work-takeover <work_id>"""


def render(response: dict[str, Any]) -> str:
    if response.get("status") == "error":
        return f"Engineering Work 錯誤 [{response.get('error_code')}]: {response.get('message')}"
    work = response.get("work")
    if not isinstance(work, dict):
        authorization = response.get("authorization")
        if isinstance(authorization, dict):
            multi = authorization.get("multi_expert") or {}
            return (f"工作授權預覽 {authorization.get('work_id')}\n"
                    f"模式：{authorization.get('mode')}\n"
                    f"範圍：{authorization.get('scope')}\n"
                    f"目標／判準：{authorization.get('objective')} / {authorization.get('criteria')}\n"
                    f"能力／方法：{authorization.get('allowed_capabilities')} / {authorization.get('allowed_methods')}\n"
                    f"修改界線：{authorization.get('allowed_changes')}\n"
                    f"可寫位置：{authorization.get('writable_roots')}\n"
                    f"預算：{authorization.get('limits')}\n"
                    f"Expert／必要審查：{multi.get('allowed_experts')} / "
                    f"{multi.get('required_reviews')}\n"
                    f"停止條件：{authorization.get('stop_conditions')}\n"
                    "確認後使用 /work-approve <work_id>；不需手算 digest。")
        works = response.get("works")
        if isinstance(works, list):
            return "目前工程工作：\n" + "\n".join(
                f"- {row.get('work_id')} [{row.get('status')}] {row.get('objective')}"
                for row in works) if works else "目前沒有可讀的工程工作。"
        return str(response)
    budget = work.get("budget") or {}
    pending = work.get("pending")
    attempts = work.get("attempts") or []
    latest = attempts[-1] if attempts else None
    lines = [
        f"工程工作：{work.get('work_id')}",
        f"狀態：{work.get('status')}（revision {work.get('revision')}）",
        f"目標：{work.get('objective')}",
        f"Project／block：{(work.get('scope') or {}).get('project_id')} / {(work.get('scope') or {}).get('block')}",
        f"Package：{(work.get('package') or {}).get('package_id')}",
        f"預算 used／remaining：{budget.get('used')} / {budget.get('remaining')}",
    ]
    if latest:
        lines.append(f"最近 attempt：{latest.get('attempt_id')} [{latest.get('status')}] proposal={latest.get('proposal_digest')}")
        outcome = latest.get("outcome") or {}
        materialization = outcome.get("materialization") or {}
        if materialization.get("path"):
            lines.append(f"候選輸出：{materialization.get('path')}")
        lines.append(
            f"工程證據：DQE={outcome.get('dqe')} formal={outcome.get('formal')} "
            f"acceptance={outcome.get('acceptance')}")
        if latest.get("domain_analysis"):
            lines.append(f"Domain 分析：{latest.get('domain_analysis')}")
    decisions = work.get("decisions") or []
    if decisions:
        decision = decisions[-1]
        lines.append(
            f"Global 決策：{decision.get('kind')} "
            f"(Domain digest={decision.get('based_on_domain_response_digest')}, "
            f"Expert digests={decision.get('based_on_expert_response_digests')})")
    collaboration = work.get("collaboration") or {}
    if collaboration:
        selected = collaboration.get("selected_experts") or []
        responses = collaboration.get("responses") or []
        latest_response = responses[-1] if responses else {}
        conflicts = collaboration.get("conflicts") or []
        lines.append(f"協作 Expert：{selected}")
        if latest_response:
            lines.append(
                f"目前處理：{latest_response.get('expert_id')} / "
                f"{latest_response.get('purpose')} / {latest_response.get('response_digest')}")
        if conflicts:
            lines.append(f"未靜默合併的分歧：{len(conflicts)} 筆")
    if pending:
        lines.append(f"等待事項：{pending}")
    if work.get("stop_reason"):
        lines.append(f"停止原因：{work.get('stop_reason')}")
    return "\n".join(lines)


__all__ = ["USAGE", "render"]
