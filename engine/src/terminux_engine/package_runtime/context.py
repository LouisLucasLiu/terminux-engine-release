# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime context model。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from terminux_engine.package_runtime.result import PhaseName, TimelineEvent

if TYPE_CHECKING:
    from terminux_engine.package_runtime.services import (
        ResultFactory,
        RuntimeHelpers,
        RuntimeServices,
    )


@dataclass
class PackageContext:
    """傳給 package phase/action 的最小 context。"""

    package_id: str
    package_version: str
    phase: PhaseName
    event: TimelineEvent

    facts: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    services: "RuntimeServices | None" = None
    helpers: "RuntimeHelpers | None" = None
    result: "ResultFactory | None" = None
