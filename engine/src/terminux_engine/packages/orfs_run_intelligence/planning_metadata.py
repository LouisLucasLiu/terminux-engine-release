# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS package 的 planning metadata provider。

**不是** action registry**：它無權新增任何 action。Capability assembler 的
集合永遠來自既有 Tool Registry；這裡只在該集合已經包含某個 action 時，補上
planning 需要的 domain 資訊（preconditions／evidence／限制）。

刻意不改 `package_runtime/manifest.py`：那個 parser 由所有 package 共用，
因此採 package-owned provider 路徑，不動共用解析器。
"""

from __future__ import annotations

from typing import Any

EXPLAIN_RUN = "orfs_run_intelligence.explain_run"
COMPARE_RUNS = "orfs_run_intelligence.compare_runs"
#: 固定 flow template 的兩個內部步驟。它們**只**能由已核准的 flow
#: task step 派生授權；一般 proposal／plan 拿不到 host binding。
PREPARE_VARIANT = "orfs_run_intelligence.prepare_variant"
#: 固定 setup repair template 的內部步驟。同樣只能由已核准的 flow task
#: step 派生授權。
PREPARE_TIMING_REPAIR = "orfs_run_intelligence.prepare_timing_repair"
RUN_FLOW = "orfs_run_intelligence.run_flow"

#: explain_run／compare_runs 的能力已實作但尚未通過 safety validation。Planner 必須把這件事
#: 表示成 availability，而不是假裝它們已驗證。
_VALIDATION_DEFERRED = {
    "state": "validation_deferred",
    "limitations": [
        "Implemented but not yet validated: no formal tests, no real Agent "
        "demonstration and no real LLM run have been accepted for this capability.",
    ],
}

#: compare_runs G-1：execution receipt 目前不是 package 可 durable 查詢的來源。
_RECEIPT_GAP = (
    "Execution receipts are not durably queryable by packages today, so tool "
    "image, flow identity and config/RTL/SDC content hashes may be unavailable "
    "at run time."
)

_FLOW_TASK_ONLY = (
    "Only reachable from an approved flow task step with a live host binding; "
    "a general proposal or plan cannot obtain this capability."
)

_METADATA: dict[str, dict[str, Any]] = {
    PREPARE_VARIANT: {
        "preconditions": [
            "An approved flow task step provides a live host binding.",
            "The fixed baseline input snapshot and its manifest verify.",
        ],
        "evidence_produced": [
            "A candidate input snapshot whose tree hash equals the value bound "
            "into the task digest at create time.",
        ],
        "availability": {
            "state": "available",
            "limitations": [
                _FLOW_TASK_ONLY,
                "It applies exactly one approved single-token change to the design "
                "config and never runs any EDA tool, Make target or shell command.",
            ],
        },
    },
    PREPARE_TIMING_REPAIR: {
        "preconditions": [
            "An approved flow task step provides a live host binding.",
            "The fixed baseline input snapshot and its manifest verify.",
        ],
        "evidence_produced": [
            "A candidate input snapshot whose tree hash equals the value bound "
            "into the task digest at create time.",
        ],
        "availability": {
            "state": "available",
            "limitations": [
                _FLOW_TASK_ONLY,
                "It only copies server-approved, hash-pinned settings into the "
                "candidate source. It accepts no caller-supplied script, recipe or "
                "path, and never runs any EDA tool, Make target or shell command.",
                "Do not propose it on its own: it is a step of a fixed server plan, "
                "and a standalone proposal can never obtain the host binding it "
                "requires.",
            ],
        },
    },
    RUN_FLOW: {
        "preconditions": [
            "An approved flow task step provides a live host binding.",
            "The pinned image is present locally and the fixed input snapshot "
            "verifies.",
        ],
        "evidence_produced": [
            "A Kernel run reference recorded in the orchestration ledger before "
            "the child starts.",
        ],
        "availability": {
            "state": "available",
            "limitations": [
                _FLOW_TASK_ONLY,
                "Accepting the launch handoff does not mean the flow finished; "
                "completion evidence is verified separately.",
            ],
        },
    },
    EXPLAIN_RUN: {
        "preconditions": [
            "The run id exists in the DQE database with recorded rows.",
            "The run's artifact manifest is readable and its producer run id matches.",
            "At least one text artifact still matches its recorded content hash.",
        ],
        "evidence_produced": [
            "A bounded context snapshot of the run's DQE checks and verified report excerpts.",
            "An evidence-backed insight record separating facts, interpretations, "
            "hypotheses, unknowns and recommended checks.",
        ],
        "side_effects": [
            "Reads verified artifacts read-only.",
            "Makes one LLM call.",
            "Writes two analysis records to the existing event store.",
        ],
        "stop_signals": [
            "No artifact manifest reference is recorded for the run.",
            "The manifest cannot be verified.",
            "No verified artifact content is usable, so no interpretation is produced.",
        ],
        "idempotency": "non_idempotent",
        "availability": _VALIDATION_DEFERRED,
    },
    COMPARE_RUNS: {
        "preconditions": [
            "Both run ids exist in the DQE database with recorded rows.",
            "Both runs have readable artifact manifests whose producer run ids match.",
            "The two run ids are different.",
        ],
        "evidence_produced": [
            "A comparability decision from the versioned run-comparison policy.",
            "Deterministic per-metric baseline/candidate/delta pairs.",
            "A bounded comparison context snapshot with evidence excerpts from both runs.",
        ],
        "side_effects": [
            "Reads verified artifacts read-only.",
            "Makes one LLM call.",
            "Writes two analysis records to the existing event store.",
        ],
        "stop_signals": [
            "The run pair is not_comparable, in which case no directional conclusion "
            "is produced.",
            "A required artifact role is missing or its content hash has drifted.",
        ],
        "idempotency": "non_idempotent",
        "availability": {
            "state": "validation_deferred",
            "limitations": _VALIDATION_DEFERRED["limitations"] + [
                _RECEIPT_GAP,
                "Without receipts the comparability decision degrades to "
                "conditionally_comparable and lists those dimensions as unknowns.",
            ],
        },
    },
}


def planning_metadata(capability_id: str) -> dict[str, Any] | None:
    """回傳該 capability 的 planning metadata；未知一律 `None`。"""
    metadata = _METADATA.get(capability_id)
    return dict(metadata) if metadata is not None else None


#: 只能由已核准 flow task step 使用的 capability。它們仍在 task service 的
#: catalog 裡（flow task 靠它派生授權），但不進 plan-only 與一般 Agent
#: proposal／plan 的 catalog——那些路徑永遠拿不到 host binding。
FLOW_TASK_ONLY_CAPABILITY_IDS: frozenset[str] = frozenset({
    PREPARE_VARIANT, PREPARE_TIMING_REPAIR, RUN_FLOW,
})


def exposed_capability_ids() -> tuple[str, ...]:
    """這個 package 有 planning metadata 的全部 capability（task service 的 catalog）。

    只是「願意暴露」——實際是否進 catalog 仍取決於它是否真的註冊在既有
    Tool Registry 裡。plan-only 改用 `plan_only_capability_ids()`。"""
    return (EXPLAIN_RUN, COMPARE_RUNS, PREPARE_VARIANT, PREPARE_TIMING_REPAIR,
            RUN_FLOW)


def plan_only_capability_ids() -> tuple[str, ...]:
    """暴露給 plan-only planner（與其 bounded execution）的 capability：
    `exposed_capability_ids()` 扣掉 flow task 專用的部分。"""
    return tuple(capability_id for capability_id in exposed_capability_ids()
                 if capability_id not in FLOW_TASK_ONLY_CAPABILITY_IDS)


__all__ = ["COMPARE_RUNS", "EXPLAIN_RUN", "FLOW_TASK_ONLY_CAPABILITY_IDS",
           "PREPARE_TIMING_REPAIR", "PREPARE_VARIANT", "RUN_FLOW",
           "exposed_capability_ids", "plan_only_capability_ids", "planning_metadata"]
