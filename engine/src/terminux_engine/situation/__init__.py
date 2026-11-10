# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Run-Level Situation Intelligence 的 vendor-neutral 共同格式。

只定義**資料形狀**與 identity，不做任何 domain 判斷：

- `context.py`：`SituationContext v1`——subjects、sources、facts、evidence、
  budget 與 opaque `domain_payload`。
- `snapshot.py`：`SituationSnapshot v1`——readiness、facts／interpretations／
  hypotheses／unknowns、support status 與 execution counters。
- `support.py`：deterministic evidence support validator。

這一層**不知道** WNS、TNS、corner、utilization、DQE check id 或任何 report
檔名。metric 方向、comparability 與 readiness policy 全部屬於
domain Package。
"""

from terminux_engine.situation.context import (
    SITUATION_CONTEXT_SCHEMA_VERSION,
    SOURCE_STATUSES,
    ContextBudget,
    DeterministicFact,
    EvidenceRef,
    MissingSource,
    SituationContext,
    SituationSource,
    SituationSubject,
    canonical_json,
    sha256_text,
)
from terminux_engine.situation.snapshot import (
    GLOBAL_SUPPORT_STATUSES,
    READINESS_STATUSES,
    SITUATION_SNAPSHOT_SCHEMA_VERSION,
    SUPPORT_STATUSES,
    Claim,
    Hypothesis,
    Readiness,
    RecommendedCheck,
    SituationSnapshot,
    compute_snapshot_sha256,
)
from terminux_engine.situation.support import SupportReview, validate_support

__all__ = [
    "Claim",
    "ContextBudget",
    "DeterministicFact",
    "EvidenceRef",
    "GLOBAL_SUPPORT_STATUSES",
    "Hypothesis",
    "MissingSource",
    "READINESS_STATUSES",
    "Readiness",
    "RecommendedCheck",
    "SITUATION_CONTEXT_SCHEMA_VERSION",
    "SITUATION_SNAPSHOT_SCHEMA_VERSION",
    "SOURCE_STATUSES",
    "SUPPORT_STATUSES",
    "SituationContext",
    "SituationSnapshot",
    "SituationSource",
    "SituationSubject",
    "SupportReview",
    "canonical_json",
    "compute_snapshot_sha256",
    "sha256_text",
    "validate_support",
]
