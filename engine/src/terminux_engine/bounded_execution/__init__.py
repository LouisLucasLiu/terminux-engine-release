# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Approved Bounded Package Action Execution。

把 plan-only planner 的「建議計畫」安全地轉成「可核准、可追溯、一次只執行一個真實
Package action 的受控操作」。

職責邊界：

- `snapshot.py`：immutable `PlanSnapshot v1`，canonical hash 與衝突偵測。
- `draft.py`：session-bound、有 TTL／容量上限的 plan draft cache（**不是**
  durable authority）。
- `eligibility.py`：deterministic step eligibility 與 catalog drift 檢查。
- `binding.py`：`PlanStepBinding v1`——在既有 proposal binding **之上**再
  綁 plan／step／catalog／contract；不取代 ProposalRegistry 的權威。
- `activation.py`：production activation gate，**預設 disabled**，且不可由
  request／env／Package／LLM 開啟。
- `receipt.py`：`PackageActionExecutionReceipt v1`。
- `service.py`：propose／execute 的固定順序與穩定 error code。

`bounded_execution` 這一層不認識任何 EDA 工具、metric 或 report 語意
。
"""

from terminux_engine.bounded_execution.activation import (
    ActivationGate,
    production_activation_gate,
)
from terminux_engine.bounded_execution.binding import (
    BINDING_SCHEMA_VERSION,
    PlanStepBinding,
)
from terminux_engine.bounded_execution.draft import PlanDraftCache, PlanDraftEntry
from terminux_engine.bounded_execution.eligibility import (
    EligibilityResult,
    check_step_eligibility,
)
from terminux_engine.bounded_execution.errors import BOUNDED_EXECUTION_ERROR_CODES
from terminux_engine.bounded_execution.receipt import (
    RECEIPT_SCHEMA_VERSION,
    ExecutionReceipt,
    compute_receipt_sha256,
)
from terminux_engine.bounded_execution.snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    PlanSnapshot,
    canonical_plan_bytes,
    compute_plan_sha256,
)

__all__ = [
    "BINDING_SCHEMA_VERSION",
    "BOUNDED_EXECUTION_ERROR_CODES",
    "ActivationGate",
    "EligibilityResult",
    "ExecutionReceipt",
    "PlanDraftCache",
    "PlanDraftEntry",
    "PlanSnapshot",
    "PlanStepBinding",
    "RECEIPT_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "canonical_plan_bytes",
    "check_step_eligibility",
    "compute_plan_sha256",
    "compute_receipt_sha256",
    "production_activation_gate",
]
