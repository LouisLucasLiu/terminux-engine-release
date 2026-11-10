# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""flow task 的 deterministic 文字輸出。

全部由 host 已知狀態產生：不呼叫 LLM、不下工程結論，也不讓任何字串暗示比
host 實際知道的更多。特別是：**啟動交接不等於 EDA 已完成**。
"""

from __future__ import annotations

from typing import Any

_STATUS_NOTE = {
    "DRAFT": "尚未授權，還沒有任何 EDA 被執行，也沒有寫入任何候選檔案。",
    "QUEUED": "已授權，等待 daemon 名額；輪到時自動開始，取消不會啟動任何 child。",
    "RUNNING": "已授權，正在背景執行；這個查詢不會等待它。",
    "CANCEL_REQUESTED": "已要求取消；已啟動的 EDA 可能仍在執行。",
    "COMPLETED": ("兩次執行與比較都完成。這**不**代表 timing 有改善，"
                  "更不代表已完成 timing repair。"),
    "FAILED": "步驟已確定返回且失敗。",
    "BLOCKED": "派工前的驗證不成立，已完成的結果保留。",
    "CANCELLED": "已取消。",
    "OUTCOME_UNCONFIRMED": "有工作結果無法確認；不會重試，也不會自動繼續。",
    "INTERRUPTED": "上一個 daemon instance 留下的未完成 task，只能唯讀判讀。",
    "EXPIRED": "歷史狀態：這筆草案在舊規則下已過期。新草案沒有核准期限，不會再進入此狀態。",
}

_OPERATION_NOTE = {
    "verifying_inputs": "正在完整比對輸入檔案",
    "waiting_execution": "已交給 Kernel 啟動，等待執行",
    "waiting_evidence": "執行已結束，等待完成證據（receipt／manifest／DQE）",
    "preparing_variant": "正在建立候選輸入",
    "analyzing": "正在分析",
    "waiting_slot": "等待 EDA／provider 平行度名額",
}


#: 三種解說狀態的固定說明。它只描述解說，不描述工程結論。
_NARRATIVE_NOTE = {
    "COMPLETE": "已嘗試的解說都有效。",
    "PARTIAL": "有一段解說因引用錯誤被拒絕，已標示為不可用；工程證據不受影響。",
    "UNAVAILABLE": "兩段解說都被拒絕，沒有可用的解說；工程證據不受影響。",
}

#: 授權摘要必須逐字包含的告知。
_NARRATIVE_AUTHORIZATION_NOTICE = (
    "我會執行已列出的固定 ECO 與驗證。若解說因引用錯誤被拒絕，會顯示"
    "「解說不可用」；工程證據仍可信且授權有效時會繼續。證據、等價或執行"
    "狀態無法確認時會停止。")


_PROJECT_SOURCE_NOTE = {
    "session": "session 明確宣告",
    "profile": "session 未宣告，採用 Profile 的 project",
    "unset": "session 未宣告",
}


def _project_line(task: dict[str, Any]) -> str:
    """DRAFT 明確顯示有效 project 與來源。"""
    source = str(task.get("project_source") or "session")
    return (f"project={task.get('project_id')}  "
            f"（來源：{_PROJECT_SOURCE_NOTE.get(source, source)}）")


def render_flow_task(response: dict[str, Any]) -> str:
    if response.get("status") == "error":
        lines = [f"拒絕：{response.get('error_code')}",
                 str(response.get("message") or "")]
        return "\n".join(x for x in lines if x)

    task = response.get("task") or {}
    parameter = task.get("parameter") or {}
    lines = [
        f"task {task.get('task_id')}  status={task.get('status')}",
        f"digest={task.get('task_digest')}",
        f"profile={task.get('profile_id')}  template={task.get('template_id')}",
        _project_line(task),
        f"design={task.get('design_id')}/{task.get('platform_id')}  "
        f"{parameter.get('name')}: {parameter.get('baseline')} → {parameter.get('candidate')}",
    ]
    note = _STATUS_NOTE.get(str(task.get("status")))
    if note:
        lines.append(note)
    operation = task.get("current_operation")
    if operation and operation != "idle":
        lines.append(f"目前：{_OPERATION_NOTE.get(operation, operation)}")
    if task.get("persistence_status") != "durable":
        lines.append(f"⚠ persistence_status={task.get('persistence_status')}")

    # 這次 task 用的是哪一份 Profile、會怎麼執行、會寫到哪裡。
    source = task.get("profile_source") or {}
    if source:
        where = source.get("resolved_path") or "(inline JSON)"
        symlink = "（symlink）" if source.get("is_symlink") else ""
        lines.append(f"Profile 來源：{source.get('kind')} {where}{symlink}")
        lines.append(f"Profile digest：{source.get('profile_digest')}  "
                     f"raw sha256：{source.get('raw_sha256')}")
    plan = task.get("execution_plan") or {}
    if plan:
        if plan.get("native_tool_binding_sha256"):
            binding = plan.get("native_tool_binding") or {}
            lines.append(
                f"執行方式：runner={plan.get('runner')}  host-native  "
                f"tool binding sha256={plan.get('native_tool_binding_sha256')}")
            for name, pin in sorted((binding.get("tools") or {}).items()):
                lines.append(f"  {name}：{pin.get('realpath')}  sha256={pin.get('sha256')}")
            env_pin = binding.get("env_script") or {}
            if env_pin:
                lines.append(f"  env script：{env_pin.get('realpath')}  "
                             f"sha256={env_pin.get('sha256')}")
        else:
            lines.append(
                f"執行方式：runner={plan.get('runner')}  image={plan.get('docker_image_id')}  "
                f"docker={plan.get('docker_executable')}")
            if plan.get("user_docker_args"):
                lines.append(f"  使用者 docker args：{' '.join(plan['user_docker_args'])}")
            if plan.get("host_env_script"):
                lines.append(f"  host env script：{plan['host_env_script']}  "
                             f"make：{plan.get('host_make_executable') or 'make'}")
            if plan.get("host_tool_env"):
                lines.append("  host tool env：" + " ".join(
                    f"{k}={v}" for k, v in sorted(plan["host_tool_env"].items())))
        proof = plan.get("proof") or {}
        if proof:
            lines.append("  proof：" + " ".join(
                f"{k}={v}" for k, v in proof.items() if not isinstance(v, dict)))
        lines.append("  可寫位置：" + ", ".join(plan.get("writable_roots") or []))
        sinks = plan.get("evidence_sinks") or {}
        if sinks:
            lines.append("  證據落點：" + " ".join(f"{k}={v}" for k, v in sinks.items()))
        for effect in plan.get("side_effects") or []:
            lines.append(f"  side effect：{effect}")
        declared = plan.get("user_declared")
        if declared:
            lines.append(
                "  使用者自述（未經系統驗證）："
                + " ".join(f"{k}={v}" for k, v in declared.items()
                           if k != "system_verified"))
    queue = task.get("queue") or {}
    if queue and task.get("status") == "QUEUED":
        lines.append(f"排隊：位置 {queue.get('position')}，原因 {queue.get('reason')}")
    lineage = task.get("lineage") or {}
    for slot, entry in sorted(lineage.items()):
        state = entry.get("state") or ("intent" if entry.get("intent") else "")
        lines.append(f"lineage[{slot}]：{state or 'pending'}")
    comparison = task.get("comparison") or {}
    if comparison:
        lines.append(
            f"比較基礎：{comparison.get('status')}  變更維度："
            + (", ".join(comparison.get("changed_dimensions") or []) or "無")
            + f"  允許表述：{comparison.get('allowed_claim')}")
    repair = task.get("repair") or {}
    if repair.get("comparison_status"):
        lines.append(
            f"工程判定：repair_status={repair.get('repair_status')}  "
            f"comparison_status={repair.get('comparison_status')}"
            + (f"  gate={repair.get('gate_repair_status')}"
               if repair.get("gate_repair_status") else ""))

    budget = task.get("budget") or {}
    lines.append(
        f"預算：EDA {budget.get('eda_launches_used')}/{budget.get('eda_launch_limit')}"
        f"　候選寫入 {budget.get('candidate_writes_used')}/{budget.get('candidate_write_limit')}"
        f"　LLM {budget.get('body_llm_dispatches_used')}/{budget.get('body_llm_dispatch_limit')}")

    lines.append("步驟：")
    for step in task.get("steps") or []:
        parts = [f"  - {step.get('step_id')} {step.get('capability_id')}",
                 f"status={step.get('status')}"]
        for key in ("run_id", "business_class", "effective_value", "reason"):
            if step.get(key):
                parts.append(f"{key}={step[key]}")
        lines.append("  ".join(parts))

    runs = task.get("runs") or []
    if runs:
        lines.append("已綁定的執行：")
        for run in runs:
            lines.append(f"  - {run.get('slot')}: {run.get('run_id')}")

    # 解說狀態與工程結果分列顯示。
    narrative = task.get("narrative_status")
    if narrative:
        lines.append(f"解說狀態：{_NARRATIVE_NOTE.get(narrative, narrative)}")
        for rejection in task.get("narrative_rejections") or []:
            lines.append(
                f"  - {rejection.get('step_id')} 解說不可用："
                f"{rejection.get('failure_kind')}；工程結果不受影響")
        for entry in task.get("narrative_unavailable") or []:
            lines.append(
                f"  - {entry.get('step_id')} 沒有產生解說"
                f"（business_class={entry.get('business_class')}）；"
                "工程結果不受影響")

    # 候選會修改隔離複本的 RTL。這件事必須在授權前就講清楚。
    recipe = task.get("repair_recipe") or {}
    patch = recipe.get("rtl_patch") if isinstance(recipe, dict) else None
    if isinstance(patch, dict):
        lines.append(
            f"候選會在**隔離複本**內套用一份已核准的 RTL patch："
            f"{patch.get('path')}，只改 "
            + "／".join(patch.get("modules") or [])
            + " 的組合運算式（固定 parallel-prefix borrow 網路）。")
        lines.append(
            f"  patch 身分：{patch.get('before_sha256', '')[:16]} → "
            f"{patch.get('after_sha256', '')[:16]}"
            f"（patch {patch.get('patch_sha256', '')[:16]}）")
        lines.append(
            "  模組介面、state、reset、enable、handshake 與其他每一個 source "
            "檔案都不變；原 SDC 與 library 不變。")
        policy = recipe.get("proof_policy_id")
        if policy:
            lines.append(
                f"  兩組等價證明由固定的 {policy} 產生：每個 flop 只有一個 "
                "canonical state bit，因此證明涵蓋所有輸入與所有 state。")

    if task.get("status") == "DRAFT":
        # 授權摘要必須先講清楚解說被拒時會發生什麼。
        if task.get("template_id") == "orfs.setup_repair.v2":
            lines.append(_NARRATIVE_AUTHORIZATION_NOTICE)
        lines.append(
            f"授權後開始：/task-start {task.get('task_id')} {task.get('task_digest')}")
    lines.append(
        f"查完整結果：agent.flow_task.get(task_id={task.get('task_id')!r}, "
        "include_results=true)")
    for limitation in task.get("limitations") or []:
        lines.append(f"限制：{limitation}")
    return "\n".join(lines)


def render_profile_selection(response: dict[str, Any]) -> str:
    """`/profile <path>` 的回覆。"""
    if response.get("status") == "error":
        return "\n".join(x for x in (
            f"拒絕：{response.get('error_code')}",
            str(response.get("message") or "")) if x)
    profile = response.get("profile") or {}
    source = profile.get("profile_source") or {}
    lines = [
        f"已選用 Profile：{profile.get('profile_id')}  template={profile.get('template_id')}",
        f"design={profile.get('design_id')}/{profile.get('platform_id')}  "
        f"digest={profile.get('profile_digest')}",
        f"來源：{source.get('kind')} {source.get('resolved_path') or '(inline)'}"
        + ("（symlink）" if source.get("is_symlink") else ""),
        "只在本 session 有效；用自然語言（@@ …）或 /task-create <profile_id> <問題> 建立草案。",
    ]
    return "\n".join(lines)


def render_profile_list(response: dict[str, Any]) -> str:
    if response.get("status") == "error":
        return f"拒絕：{response.get('error_code')} {response.get('message') or ''}".strip()
    profiles = response.get("profiles") or []
    if not profiles:
        return ("本 session 尚未選用任何 Profile。用 /profile <絕對路徑> 選用；"
                "daemon 不再帶預設 Profile。")
    lines = ["本 session 已選用的 Profile："]
    for entry in profiles:
        source = entry.get("profile_source") or {}
        lines.append(
            f"  - {entry.get('profile_id')}  {entry.get('design_id')}/"
            f"{entry.get('platform_id')}  template={entry.get('template_id')}  "
            f"來源={source.get('kind')} {source.get('resolved_path') or ''}".rstrip())
    return "\n".join(lines)


def render_task_list(response: dict[str, Any]) -> str:
    if response.get("status") == "error":
        return f"拒絕：{response.get('error_code')} {response.get('message') or ''}".strip()
    tasks = response.get("tasks") or []
    limits = response.get("scheduler") or {}
    lines = [f"本 daemon 內你的 task（active 上限 "
             f"{(limits.get('limits') or {}).get('max_active_tasks')}）："]
    if not tasks:
        lines.append("  （沒有）")
    for entry in tasks:
        queue = entry.get("queue") or {}
        extra = (f"  queue#{queue.get('position')} {queue.get('reason')}"
                 if entry.get("status") == "QUEUED" else "")
        lines.append(
            f"  - {entry.get('task_id')}  {entry.get('status')}  "
            f"{entry.get('task_kind')}  profile={entry.get('profile_id')}  "
            f"design={entry.get('design_id')}{extra}")
    return "\n".join(lines)


__all__ = ["render_flow_task", "render_profile_list", "render_profile_selection",
           "render_task_list"]
