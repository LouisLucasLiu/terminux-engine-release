# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Evidence integrity：additive invalidation record 與正式讀取端的預設排除。"""

from terminux_engine.evidence_integrity.invalidation import (
    INVALIDATION_RECORD_TYPE,
    INVALIDATION_SCHEMA_VERSION,
    InvalidationRecordError,
    InvalidationSet,
    build_invalidation_record,
    dqe_row_identity,
    load_invalidation_set,
    validate_invalidation_record,
)

__all__ = [
    "INVALIDATION_RECORD_TYPE",
    "INVALIDATION_SCHEMA_VERSION",
    "InvalidationRecordError",
    "InvalidationSet",
    "build_invalidation_record",
    "dqe_row_identity",
    "load_invalidation_set",
    "validate_invalidation_record",
]
