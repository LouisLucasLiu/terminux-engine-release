# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer adapter query models。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchivedIsvnQuery:
    workspace_root: str | None = None
    version_path: str | None = None
    version_dir: str | None = None
    package_glob: str = "dqe_*.json"
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class TemporaryDbQuery:
    db_path: str
    retention_days: int | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int | None = None
    offset: int | None = None

