# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`prepare_variant` 與 `run_flow` 兩個正式 DANGEROUS actions。

兩者都**只依 host live binding**工作：

- 參數只有 `task_id`（＋ `run_flow` 的 `slot`）。它們**不是**授權——
  binding 由 host 從 live task grant 注入 `ctx.services`，provider 就算猜對
  這兩個字串也拿不到能力。
- `prepare_variant` 在 Package 內做受控寫入（唯一允許的 diff）。
- `run_flow` 不碰 Docker／subprocess，只透過 `FlowExecutionService` 請
  Kernel 啟動；回覆是**啟動交接**，不是「EDA 已完成」。

一般 proposal、plan-only planner／舊 Plan、`package.invoke_action` 或 provider 直接呼叫
都拿不到 binding，因此在這裡一律 fail closed。
"""

from __future__ import annotations

import json
from typing import Any

from terminux_engine.package_runtime.context import PackageContext

#: binding 由 host 注入在這個 services 屬性上。名稱固定。
BINDING_ATTR = "flow_task_binding"
#: 執行 facade 同樣由 host 注入。
EXECUTION_ATTR = "flow_execution_service"
#: 候選寫入器（Package 內部 helper，由 host 注入以便測試替身）。
VARIANT_WRITER_ATTR = "flow_variant_writer"

STATUS_OK = "ok"
STATUS_FAILED = "failed"

BINDING_REQUIRED = "FLOW_TASK_BINDING_REQUIRED"
BINDING_MISMATCH = "FLOW_TASK_BINDING_MISMATCH"
EXECUTION_UNAVAILABLE = "FLOW_EXECUTION_UNAVAILABLE"


def _binding(ctx: PackageContext) -> Any:
    services = getattr(ctx, "services", None)
    binding = getattr(services, BINDING_ATTR, None) if services is not None else None
    if binding is None:
        raise _ActionDenied(
            BINDING_REQUIRED,
            "this action is only available from an approved flow task step; "
            "no live task binding was provided by the host")
    return binding


class _ActionDenied(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _args(ctx: PackageContext, query: str | None) -> dict[str, Any]:
    """action arguments。`query` 是 registry 傳來的 JSON 字串。"""
    if isinstance(query, str) and query.strip():
        try:
            parsed = json.loads(query)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            return parsed
    facts = getattr(ctx, "facts", None) or {}
    args = facts.get("action_args")
    return args if isinstance(args, dict) else {}


def _require_task_id(ctx: PackageContext, args: dict[str, Any]) -> Any:
    binding = _binding(ctx)
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise _ActionDenied(BINDING_MISMATCH, "task_id is required")
    if task_id != binding.task_id:
        # 參數與 binding 不符——不是授權問題，是明顯的錯誤呼叫。
        raise _ActionDenied(
            BINDING_MISMATCH,
            "task_id does not match the live task binding for this step")
    return binding


def prepare_variant(ctx: PackageContext, query: str | None = None) -> dict[str, Any]:
    """在候選區完成已核准的**單一** diff，回傳 `VariantInputRef`。

    這是 Package 內的受控寫入：它複製 baseline input snapshot，只把
    design config 內的那一個數字 token 換掉，然後驗證實際 tree hash 等於
    create 時綁進 task digest 的預期值。任何不符一律拒絕，不自行改用
    其他寫法。

    它**不執行** Make／Tcl／EDA。
    """
    try:
        args = _args(ctx, query)
        binding = _require_task_id(ctx, args)
        # `slot` 是可選的（v1 相容）。給了就必須與 live binding 相符——
        # enum 變大**不會**讓任何 task 取得額外的 slot。
        slot = args.get("slot")
        if slot is not None and slot != binding.slot:
            raise _ActionDenied(
                BINDING_MISMATCH,
                "slot does not match the live task binding for this step")
        services = getattr(ctx, "services", None)
        writer = getattr(services, VARIANT_WRITER_ATTR, None)
        if writer is None:
            raise _ActionDenied(
                EXECUTION_UNAVAILABLE,
                "the flow variant writer is not configured on this daemon")
        ref = writer(binding)
    except _ActionDenied as denied:
        return {"status": STATUS_FAILED, "error_code": denied.code,
                "message": denied.message}
    except Exception as exc:  # noqa: BLE001 - 結構化回報，不吞掉
        return {"status": STATUS_FAILED, "error_code": "VARIANT_PREPARE_FAILED",
                "message": repr(exc)}
    return {
        "status": STATUS_OK,
        "variant_input_ref": ref.to_json_dict(),
        "message": ("the candidate input snapshot was created with exactly the "
                    "approved single-token diff"),
    }


def run_flow(ctx: PackageContext, query: str | None = None) -> dict[str, Any]:
    """請 Kernel 啟動本 slot 的完整 flow，非阻塞回 `RunRef`。

    **回覆只是啟動交接**：`launch_accepted` 為真不代表 EDA 已完成，也不代表
    這一步 SUCCEEDED。完成證據由 task worker 另外驗證。
    """
    try:
        args = _args(ctx, query)
        binding = _require_task_id(ctx, args)
        slot = args.get("slot")
        if not isinstance(slot, str) or slot != binding.slot:
            raise _ActionDenied(
                BINDING_MISMATCH,
                "slot does not match the live task binding for this step")
        services = getattr(ctx, "services", None)
        execution = getattr(services, EXECUTION_ATTR, None)
        if execution is None:
            raise _ActionDenied(
                EXECUTION_UNAVAILABLE,
                "the flow execution service is not configured on this daemon")
        result = execution.start_run(binding, slot)
    except _ActionDenied as denied:
        return {"status": STATUS_FAILED, "error_code": denied.code,
                "message": denied.message}
    except Exception as exc:  # noqa: BLE001
        return {"status": STATUS_FAILED, "error_code": "FLOW_LAUNCH_FAILED",
                "message": repr(exc)}
    return {
        "status": STATUS_OK,
        "launch_result": result.to_json_dict(),
        "run_id": result.run_id,
        "slot": result.slot,
        "launch_accepted": result.accepted,
        "message": ("the Kernel accepted the launch handoff; the EDA flow has "
                    "not finished and this step is not complete yet"),
    }


REPAIR_WRITER_ATTR = "flow_repair_writer"


def prepare_timing_repair(ctx: PackageContext, query: str | None = None) -> dict[str, Any]:
    """在隔離候選區寫入**已核准的固定 ECO hook 設定**。

    它做的事只有一件：把 server 事先固定、且 hash 已綁進 task digest 的
    helper bytes 與 config 追加內容，複製到 candidate 的 source。

    它**不執行** OpenROAD／ABC／Yosys／Make／shell，也**不收**使用者的 Tcl、
    recipe、檔案路徑或任何參數——`slot` 固定為 `candidate`，其餘全部由 host
    的 live binding 決定。
    """
    try:
        args = _args(ctx, query)
        binding = _require_task_id(ctx, args)
        slot = args.get("slot")
        if slot != "candidate" or binding.slot != "candidate":
            raise _ActionDenied(
                BINDING_MISMATCH,
                "prepare_timing_repair only ever runs for the candidate slot")
        # 任何其他 argument 都是想繞過固定 recipe。
        extra = sorted(set(args) - {"task_id", "slot"})
        if extra:
            raise _ActionDenied(
                BINDING_MISMATCH,
                f"prepare_timing_repair does not accept: {extra}")
        services = getattr(ctx, "services", None)
        writer = getattr(services, REPAIR_WRITER_ATTR, None)
        if writer is None:
            raise _ActionDenied(
                EXECUTION_UNAVAILABLE,
                "the flow repair writer is not configured on this daemon")
        ref = writer(binding)
    except _ActionDenied as denied:
        return {"status": STATUS_FAILED, "error_code": denied.code,
                "message": denied.message}
    except Exception as exc:  # noqa: BLE001 - 結構化回報，不吞掉
        return {"status": STATUS_FAILED, "error_code": "REPAIR_PREPARE_FAILED",
                "message": repr(exc)}
    return {
        "status": STATUS_OK,
        "variant_input_ref": ref.to_json_dict(),
        "message": ("the candidate source was created with exactly the approved "
                    "ECO hook settings; no EDA tool was run"),
    }


__all__ = [
    "BINDING_ATTR",
    "BINDING_MISMATCH",
    "BINDING_REQUIRED",
    "EXECUTION_ATTR",
    "EXECUTION_UNAVAILABLE",
    "REPAIR_WRITER_ATTR",
    "VARIANT_WRITER_ATTR",
    "prepare_timing_repair",
    "prepare_variant",
    "run_flow",
]
