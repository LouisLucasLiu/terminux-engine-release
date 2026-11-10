# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from typing import Literal

ANCHOR_KEYS: tuple[str, ...] = ('rtl', 'sdc', 'upf')

# synthesis preflight 中被視為 evidence 的 role
SUMMARY_ROLES: frozenset[str] = frozenset({
    'lint_summary', 'coverage_summary', 'cdc_summary', 'formal_summary'
})

# 產生或記錄 output 時跳過的 role（metadata 檔，非真實 artifact）
OUTPUT_SKIP_ROLES: frozenset[str] = frozenset({
    'intent', 'commit', 'preflight_report', 'preflight_detail'
})

# 只有這些 role 的 artifact 是 lineage root（自身 checksum 作為 anchor）
ROOT_ANCHOR_ROLES: frozenset[str] = frozenset({'rtl', 'sdc', 'upf'})

LineageSource = Literal['root_observation', 'producer_commit', 'derived_from_inputs']
LineageStatus = Literal['CONSISTENT', 'MIXED_LINEAGE', 'INSUFFICIENT_EVIDENCE']
FindingSeverity = Literal['info', 'medium', 'high']

LEDGER_FILENAME = "ledger.jsonl"
LEDGER_DIR = "lineage"
