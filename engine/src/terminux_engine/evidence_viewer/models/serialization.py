# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""JSON-safe serialization helpers for Evidence Viewer models。"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints

from terminux_engine.evidence_viewer.models.context import EvidenceContextView

# canonical GUI-consumer 欄位為 context.primary_fields.<field>。
# flat context.<field> 只當 legacy 相容輸出，不是 production contract。
_CONTEXT_PRIMARY_FIELDS = ("project", "phase", "block", "stage", "flow", "version")


def to_dict(model: object) -> dict[str, Any]:
    """把 dataclass model 轉成 JSON-safe dict。"""

    value = _to_json_value(model)
    if not isinstance(value, dict):
        raise TypeError("to_dict() expects a dataclass model that serializes to dict")
    return value


def from_dict(data: dict[str, Any], model_type: type[Any]) -> Any:
    """從 dict 重建 dataclass model。"""

    if not is_dataclass(model_type):
        raise TypeError("from_dict() expects a dataclass model type")
    return _from_value(data, model_type)


def _to_json_value(value: object) -> Any:
    if isinstance(value, bytes):
        raise TypeError("bytes are not JSON-safe and must not appear in view models")
    if is_dataclass(value):
        result = {
            field.name: _to_json_value(getattr(value, field.name))
            for field in fields(value)
        }
        if isinstance(value, EvidenceContextView):
            # 補 canonical primary_fields（正本）；保留 flat 欄位僅供 legacy reader 相容。
            result["primary_fields"] = {
                name: result[name] for name in _CONTEXT_PRIMARY_FIELDS
            }
        return result
    if isinstance(value, list):
        return [_to_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _to_json_value(item)
            for key, item in value.items()
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{type(value).__name__} is not JSON-safe")


def _from_value(value: Any, expected_type: Any) -> Any:
    inferred_type = _infer_domain_extension_type(value, expected_type)
    if inferred_type is not None:
        return _from_value(value, inferred_type)

    origin = get_origin(expected_type)
    args = get_args(expected_type)

    if origin is list:
        item_type = args[0] if args else Any
        return [_from_value(item, item_type) for item in value]

    if origin in (UnionType, getattr(__import__("typing"), "Union")):
        non_none = [arg for arg in args if arg is not type(None)]
        if value is None:
            return None
        if len(non_none) == 1:
            return _from_value(value, non_none[0])
        return value

    if expected_type is Any or expected_type is object:
        return value

    if isinstance(expected_type, type) and is_dataclass(expected_type):
        type_hints = get_type_hints(expected_type)
        kwargs: dict[str, Any] = {}
        for field in fields(expected_type):
            if field.name in value:
                field_type = type_hints.get(field.name, Any)
                kwargs[field.name] = _from_value(value[field.name], field_type)
        return expected_type(**kwargs)

    return value


def _infer_domain_extension_type(value: Any, expected_type: Any) -> type[Any] | None:
    if not isinstance(value, dict):
        return None
    if expected_type is not object:
        return None

    if {"job_key", "dqe_source_type", "dqe_lifecycle_state"}.issubset(value):
        from terminux_engine.dqe.viewer.models import DqeGroupExtension

        return DqeGroupExtension

    if "dqe_id" in value and "raw_value" in value:
        from terminux_engine.dqe.viewer.models import DqeItemExtension

        return DqeItemExtension

    return None

