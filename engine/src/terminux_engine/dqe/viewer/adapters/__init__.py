# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer source adapters。"""

from terminux_engine.dqe.viewer.adapters.archived_isvn import ArchivedIsvnAdapter
from terminux_engine.dqe.viewer.adapters.attachment_payload import AttachmentPayload
from terminux_engine.dqe.viewer.adapters.base import SourceAdapter
from terminux_engine.dqe.viewer.adapters.errors import (
    AdapterIssue,
    AdapterIssueCode,
    AdapterIssueSeverity,
)
from terminux_engine.dqe.viewer.adapters.requests import (
    ArchivedIsvnQuery,
    TemporaryDbQuery,
)
from terminux_engine.dqe.viewer.adapters.responses import AdapterResult
from terminux_engine.dqe.viewer.adapters.temporary_db import TemporaryDbAdapter

__all__ = [
    "AdapterIssue",
    "AdapterIssueCode",
    "AdapterIssueSeverity",
    "AdapterResult",
    "ArchivedIsvnAdapter",
    "ArchivedIsvnQuery",
    "AttachmentPayload",
    "SourceAdapter",
    "TemporaryDbAdapter",
    "TemporaryDbQuery",
]

