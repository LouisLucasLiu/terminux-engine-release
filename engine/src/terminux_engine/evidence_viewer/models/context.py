# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Evidence context view model。"""

from __future__ import annotations

from dataclasses import dataclass, field

_NA_SENTINEL = "_NA_"


@dataclass
class ContextFieldView:
    """同時保存 raw debug 值與 UI display 值的 context 欄位。"""

    key: str
    label: str
    raw_value: str
    display_value: str
    is_missing: bool


@dataclass
class EvidenceContextView:
    """Evidence group 的 domain-neutral hierarchical context。"""

    project: ContextFieldView
    phase: ContextFieldView
    block: ContextFieldView
    flow: ContextFieldView
    stage: ContextFieldView
    version: ContextFieldView
    context_path: list[ContextFieldView] = field(default_factory=list)
    display_path: str = ""


def normalize_context_field(key: str, label: str, value: object) -> ContextFieldView:
    """把 None / 空字串 / _NA_ 正規化成可顯示但可追蹤的 missing 欄位。"""

    if value is None:
        return ContextFieldView(
            key=key,
            label=label,
            raw_value=_NA_SENTINEL,
            display_value="",
            is_missing=True,
        )

    raw_value = str(value)
    if raw_value == "" or raw_value == _NA_SENTINEL:
        return ContextFieldView(
            key=key,
            label=label,
            raw_value=_NA_SENTINEL,
            display_value="",
            is_missing=True,
        )

    return ContextFieldView(
        key=key,
        label=label,
        raw_value=raw_value,
        display_value=raw_value,
        is_missing=False,
    )

