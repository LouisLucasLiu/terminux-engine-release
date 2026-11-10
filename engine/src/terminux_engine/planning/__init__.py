# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Plan-only Planner v1。

三個東西，職責分得很開：

- `capability.py`：把**既有** Tool Registry entry normalize 成 planning 用的
  Capability Contract view。它不是第二份 action registry——identity、input
  schema 與 risk 一律以 registry 為權威（設計規則）。
- `plan.py`：bounded、不可執行的 Plan schema v1。
- `validator.py`：deterministic validator。不相信 model 填的 risk、approval
  或 validation 結果。
- `service.py`：`PlanOnlyPlanner`。它的建構子**沒有**任何可以呼叫 executor、
  action registry、proposal registry 或 kernel 的參數。

Common 這一層不認識任何工具、檔名或 metric。
"""

from terminux_engine.planning.capability import (
    CAPABILITY_SCHEMA_VERSION,
    Capability,
    CapabilitySnapshot,
    FORBIDDEN_CAPABILITY_IDS,
    build_capability_snapshot,
)
from terminux_engine.planning.plan import (
    PLAN_SCHEMA_VERSION,
    MAX_STEPS,
    Plan,
    PlanStep,
    PlanValidationResult,
    compute_plan_id,
)
from terminux_engine.planning.validator import (
    PLAN_ERROR_CODES,
    validate_plan,
)
from terminux_engine.planning.service import (
    AgentRunMode,
    PlanOnlyPlanner,
    PlanRequest,
    PlanResult,
)

__all__ = [
    "AgentRunMode",
    "CAPABILITY_SCHEMA_VERSION",
    "Capability",
    "CapabilitySnapshot",
    "FORBIDDEN_CAPABILITY_IDS",
    "MAX_STEPS",
    "PLAN_ERROR_CODES",
    "PLAN_SCHEMA_VERSION",
    "Plan",
    "PlanOnlyPlanner",
    "PlanRequest",
    "PlanResult",
    "PlanStep",
    "PlanValidationResult",
    "build_capability_snapshot",
    "compute_plan_id",
    "validate_plan",
]
