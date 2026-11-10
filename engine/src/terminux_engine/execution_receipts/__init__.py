# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""外部執行 receipt 的 durable projection。Vendor-neutral、domain-blind。"""

from terminux_engine.execution_receipts.projection import (
    CONCURRENCY_SEMANTICS,
    PROJECTION_SCHEMA_VERSION,
    RECEIPT_RECORD_TYPE,
    ProjectionResult,
    build_projection,
    content_hash_of,
    project_receipt,
)

__all__ = [
    "CONCURRENCY_SEMANTICS",
    "PROJECTION_SCHEMA_VERSION",
    "ProjectionResult",
    "RECEIPT_RECORD_TYPE",
    "build_projection",
    "content_hash_of",
    "project_receipt",
]
