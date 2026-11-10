# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Proactive Read-Only Intelligence。

主動**發現**、主動**分析**、主動**通知**——不主動執行。

- `observation.py`：`ProactiveObservation`／`TriggerDecision`／deterministic
  trigger identity。
- `activation.py`：production activation gate，**預設 disabled**。
- `lifecycle.py`：append-only lifecycle records ＋ same-process claim。
- `coordinator.py`：固定順序的 bounded coordinator，建構子沒有任何執行能力。

這一層不認識任何 EDA 工具、metric、report 或 readiness 語意。
"""

from terminux_engine.proactive.activation import (
    BLOCKED_CODE,
    ProactiveActivationGate,
    production_proactive_gate,
)
from terminux_engine.proactive.coordinator import (
    ProactiveCoordinator,
    ProactiveDeps,
    ProactiveProvider,
)
from terminux_engine.proactive.lifecycle import (
    LIFECYCLE_SEMANTICS,
    RECORD_CLAIMED,
    RECORD_COMPLETED,
    RECORD_FAILED,
    ClaimResult,
    LifecycleRecord,
    claim_trigger,
)
from terminux_engine.proactive.observation import (
    DECISION_INSUFFICIENT,
    DECISION_SUPPRESS,
    DECISION_TRIGGER,
    OBSERVATION_SCHEMA_VERSION,
    TRIGGER_ID_SCHEMA_VERSION,
    ProactiveObservation,
    TriggerDecision,
    compute_trigger_id,
)

__all__ = [
    "BLOCKED_CODE",
    "ClaimResult",
    "DECISION_INSUFFICIENT",
    "DECISION_SUPPRESS",
    "DECISION_TRIGGER",
    "LIFECYCLE_SEMANTICS",
    "LifecycleRecord",
    "OBSERVATION_SCHEMA_VERSION",
    "ProactiveActivationGate",
    "ProactiveCoordinator",
    "ProactiveDeps",
    "ProactiveObservation",
    "ProactiveProvider",
    "RECORD_CLAIMED",
    "RECORD_COMPLETED",
    "RECORD_FAILED",
    "TRIGGER_ID_SCHEMA_VERSION",
    "TriggerDecision",
    "claim_trigger",
    "compute_trigger_id",
    "production_proactive_gate",
]
