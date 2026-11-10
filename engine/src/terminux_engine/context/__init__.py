# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Vendor-neutral Context Snapshot 共同格式。

這個 package 只認識 subjects、sources、identity、content hash、budget 與
missing status。它**不知道**任何工具、metric、節錄選段或「哪個 run 比較新」
——那些一律屬於 Package。
"""

from terminux_engine.context.contract import (
    CONTEXT_SCHEMA_VERSION,
    ContextBudget,
    ContextRequest,
    ContextSnapshot,
    ContextSource,
    ContextSourceRequest,
    ContextSubject,
    DeterministicFact,
    EvidenceRef,
    MissingContext,
    SUBJECT_ROLES,
    SOURCE_STATUSES,
    canonical_json,
    normalized_question_hash,
    sha256_text,
)
from terminux_engine.context.assembler import ContextAssemblyError, assemble_context

__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "ContextAssemblyError",
    "ContextBudget",
    "ContextRequest",
    "ContextSnapshot",
    "ContextSource",
    "ContextSourceRequest",
    "ContextSubject",
    "DeterministicFact",
    "EvidenceRef",
    "MissingContext",
    "SOURCE_STATUSES",
    "SUBJECT_ROLES",
    "assemble_context",
    "canonical_json",
    "normalized_question_hash",
    "sha256_text",
]
