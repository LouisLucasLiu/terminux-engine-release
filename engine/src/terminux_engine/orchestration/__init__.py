# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Orchestration 共用執行元件。

`BeEcoRunner` 屬於 optional workflow，以模組 `__getattr__`（PEP 562）延遲提供：
安裝了 be_eco 時 `from terminux_engine.orchestration import BeEcoRunner` 照常可用，
沒有安裝時不影響其他名稱，`__all__` 也不列出它。
"""

import importlib
import importlib.util
from typing import Any

from terminux_engine.orchestration.dispatch_engine import DispatchEngine, EngineMode
from terminux_engine.orchestration.outcome import DispatchOutcome, PackageOutcome
from terminux_engine.orchestration.ledger import (
    OrchestrationLedger,
    RunRecord,
    StepRecord,
    TransitionRecord,
)
from terminux_engine.orchestration.tool_registry import ToolDefinition, ToolRegistry
from terminux_engine.orchestration.builtin_tools import (
    BUILTIN_TOOL_DEFINITIONS,
    get_default_registry,
)
from terminux_engine.orchestration.barrier import Barrier, BarrierResult
from terminux_engine.orchestration.domain_runner import (
    DomainRunner,
    DomainRunState,
    NextStepPlan,
    ToolSpec,
)
from terminux_engine.orchestration.kernel import (
    KernelState,
    OrchestrationKernel,
    RunOutcome,
    StepOutcome,
)
from terminux_engine.orchestration.subprocess_executor import (
    SubprocessExecutor,
    SubprocessResult,
)

__all__ = [
    "DispatchEngine",
    "DispatchOutcome",
    "EngineMode",
    "OrchestrationLedger",
    "PackageOutcome",
    "RunRecord",
    "StepRecord",
    "ToolDefinition",
    "ToolRegistry",
    "TransitionRecord",
    "BUILTIN_TOOL_DEFINITIONS",
    "Barrier",
    "BarrierResult",
    "DomainRunner",
    "DomainRunState",
    "KernelState",
    "NextStepPlan",
    "OrchestrationKernel",
    "RunOutcome",
    "StepOutcome",
    "SubprocessExecutor",
    "SubprocessResult",
    "ToolSpec",
    "get_default_registry",
]

#: optional workflow 提供的名稱 → 所在模組。
_OPTIONAL_EXPORTS = {"BeEcoRunner": "terminux_engine.orchestration.be_eco_runner"}


def _optional_module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


__all__ += [name for name, module_name in _OPTIONAL_EXPORTS.items()
            if _optional_module_available(module_name)]


def __getattr__(name: str) -> Any:
    module_name = _OPTIONAL_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r} (optional workflow not installed)"
        ) from exc
    value = getattr(module, name)
    globals()[name] = value
    return value
