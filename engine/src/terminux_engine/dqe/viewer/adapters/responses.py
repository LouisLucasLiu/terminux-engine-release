# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer adapter response models。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from terminux_engine.dqe.viewer.adapters.errors import (
    AdapterIssue,
    AdapterIssueSeverity,
)

T = TypeVar("T")


@dataclass
class AdapterResult(Generic[T]):
    """Adapter operation result；ok=True 仍可包含 warning/info。"""

    ok: bool
    value: T | None = None
    issues: list[AdapterIssue] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.ok:
            has_error = any(
                item.severity == AdapterIssueSeverity.ERROR for item in self.issues
            )
            if not has_error:
                raise ValueError("ok=False AdapterResult must include an error issue")

