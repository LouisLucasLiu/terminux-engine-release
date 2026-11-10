# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`/task*` slash control 的 deterministic 文字輸出。

全部由 host 已知狀態產生：**不呼叫 LLM、不改寫內容、不下工程結論**，也
不讓任何字串暗示比 host 實際知道的更多。
"""

from __future__ import annotations

from typing import Any

USAGE = (
    "用法：\n"
    "  /task <task_id>                        查詢一筆 task（分析或 flow）\n"
    "  /task-start <task_id> <task_digest>    授權並開始（只需授權這一次）\n"
    "  /task-cancel <task_id> <task_digest>   要求停止\n"
    "  /tasks                                 列出本 daemon 內你的所有 task\n"
    "  /profile <absolute_profile_path>       選用一份 block／project Profile（本 session）\n"
    "  /profiles                              列出本 session 已選用的 Profile\n"
    "  /task-create <profile_id> <question…>  以已選用的 Profile 建立 flow task 草案\n"
    "在終端機輸入時請加 @@ 前綴。"
)

_STATUS_NOTE = {
    "DRAFT": "尚未授權，還沒有任何步驟被執行。",
    "QUEUED": "已授權，等待 daemon 名額；輪到時自動開始，取消不會啟動任何 child。",
    "RUNNING": "已授權，正在背景執行；這個查詢不會等待它。",
    "CANCEL_REQUESTED": "已要求取消；已 claim 的工作可能仍在執行。",
    "COMPLETED": "兩步分析都有效完成。這**不**代表 DQE pass、ORFS flow 完成或 signoff。",
    "FAILED": "action 已確定返回且失敗。",
    "BLOCKED": "派工前的驗證不成立，已完成的結果保留。",
    "CANCELLED": "已取消。",
    "OUTCOME_UNCONFIRMED": "有工作結果無法確認；不會重試，也不會自動繼續。",
    "INTERRUPTED": "上一個 daemon instance 留下的未完成 task，只能唯讀判讀。",
    "EXPIRED": "歷史狀態：這筆草案在舊規則下已過期。新草案沒有核准期限，不會再進入此狀態。",
}


def render(response: dict[str, Any]) -> str:
    if response.get("status") == "error":
        lines = [f"拒絕：{response.get('error_code')}",
                 str(response.get("message") or "")]
        access = response.get("run_access")
        if isinstance(access, dict):
            lines.append(f"run access：{access.get('decision')} / {access.get('reason_code')}")
        return "\n".join(x for x in lines if x)

    task = response.get("task") or {}
    lines = [
        f"task {task.get('task_id')}  status={task.get('status')}",
        f"digest={task.get('task_digest')}",
        f"template={task.get('template_id')}",
        f"project={task.get('project_id')}  （來源：{task.get('project_source') or 'session'}）",
        f"current_run={task.get('current_run_id')}  baseline_run={task.get('baseline_run_id')}",
    ]
    note = _STATUS_NOTE.get(str(task.get("status")))
    if note:
        lines.append(note)
    if task.get("persistence_status") != "durable":
        lines.append(f"⚠ persistence_status={task.get('persistence_status')}"
                     "（終態未成功寫入，重啟後依歷史保守判讀）")

    lines.append("步驟：")
    for step in task.get("steps") or []:
        parts = [f"  - {step.get('step_id')} {step.get('capability_id')}",
                 f"status={step.get('status')}"]
        if step.get("business_class"):
            parts.append(f"business={step['business_class']}")
        if step.get("background_work_may_continue"):
            parts.append("background_work_may_continue=true")
        if step.get("result_event_id"):
            parts.append(f"result_event_id={step['result_event_id']}")
        if step.get("reason"):
            parts.append(f"reason={step['reason']}")
        lines.append("  ".join(parts))

    if task.get("status") == "DRAFT":
        lines.append(
            f"授權後開始：/task-start {task.get('task_id')} {task.get('task_digest')}")
    lines.append(
        f"查完整結果：agent.analysis_task.get(task_id={task.get('task_id')!r}, "
        "include_results=true)")
    for limitation in task.get("limitations") or []:
        lines.append(f"限制：{limitation}")
    return "\n".join(lines)


__all__ = ["USAGE", "render"]
