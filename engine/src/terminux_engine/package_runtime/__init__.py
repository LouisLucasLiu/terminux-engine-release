# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime v1 skeleton。

本 package 只定義最小 runtime contract；尚未接入 production flow。
"""

from terminux_engine.package_runtime.action_registry import ActionRegistry
from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.dispatcher import PackageDispatcher
from terminux_engine.package_runtime.loader import PackageLoader, RuntimePackage
from terminux_engine.package_runtime.manifest import PackageManifest, load_manifest
from terminux_engine.package_runtime.registry import PackageRegistry
from terminux_engine.package_runtime.result import (
    NotificationRequest,
    PhaseResult,
    RecordRequest,
    ReportRequest,
    TimelineEvent,
)
from terminux_engine.package_runtime.services import RuntimeServices
from terminux_engine.package_runtime.version import API_VERSION

__all__ = [
    "API_VERSION",
    "ActionRegistry",
    "NotificationRequest",
    "PackageContext",
    "PackageDispatcher",
    "PackageLoader",
    "PackageManifest",
    "PackageRegistry",
    "PhaseResult",
    "RecordRequest",
    "ReportRequest",
    "RuntimePackage",
    "RuntimeServices",
    "TimelineEvent",
    "load_manifest",
]
