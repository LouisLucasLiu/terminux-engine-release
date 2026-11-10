# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Checkpoint v1 的嚴格編碼、解碼與檢查。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from terminux_engine.orchestration.domain_runner import DomainRunState, ToolSpec


CheckpointSafePoint = Literal["before_step", "after_step"]


@dataclass(frozen=True)
class RestoredCheckpoint:
    """已通過 v1 契約驗證的 kernel checkpoint。"""

    safe_point: CheckpointSafePoint
    domain_state: DomainRunState
    pending_tool_specs: list[ToolSpec]
    current_step_id: str | None


class CheckpointValidationError(ValueError):
    """Checkpoint 格式或內容不符合 v1 嚴格契約。"""


_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "safe_point",
        "domain_run_id",
        "pending_tool_specs",
        "domain_state",
        "current_step_id",
    }
)
_TOOL_SPEC_KEYS = frozenset({"tool_id", "parameters", "cwd", "step_id"})
_DOMAIN_STATE_KEYS = frozenset({"domain_run_id", "iteration", "metadata"})


def _reject_constant(value: str) -> None:
    raise CheckpointValidationError(f"JSON 常數不允許：{value}")


def _validate_json_native(value: Any, *, path: str = "$") -> None:
    """驗證值完全屬於 checkpoint 可保存的 JSON-native 子集。"""

    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is float:
        if math.isfinite(value):
            return
        raise CheckpointValidationError(f"{path} 含非有限浮點數")
    if value_type is list:
        for index, item in enumerate(value):
            _validate_json_native(item, path=f"{path}[{index}]")
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise CheckpointValidationError(f"{path} 含非字串 dict key")
            _validate_json_native(item, path=f"{path}.{key}")
        return
    raise CheckpointValidationError(
        f"{path} 含不允許的 JSON-native 型別：{value_type.__name__}"
    )


def _require_exact_keys(value: Any, *, keys: frozenset[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise CheckpointValidationError(f"{path} 必須為 object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unknown = sorted(actual - keys)
        raise CheckpointValidationError(
            f"{path} keys 不符：missing={missing}, unknown={unknown}"
        )
    return value


def _require_string(value: Any, *, path: str) -> str:
    if type(value) is not str:
        raise CheckpointValidationError(f"{path} 必須為 str")
    return value


def _decode_payload(
    checkpoint_json: str,
    *,
    expected_domain_run_id: str | None,
) -> tuple[CheckpointSafePoint, DomainRunState, list[ToolSpec], str | None]:
    if type(checkpoint_json) is not str:
        raise CheckpointValidationError("checkpoint_json 必須為 str")
    try:
        payload = json.loads(checkpoint_json, parse_constant=_reject_constant)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, CheckpointValidationError):
            raise
        raise CheckpointValidationError("checkpoint JSON 無法解碼") from exc

    top = _require_exact_keys(payload, keys=_TOP_LEVEL_KEYS, path="$")
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise CheckpointValidationError("schema_version 必須精確為 integer 1")
    safe_point = top["safe_point"]
    if safe_point not in {"before_step", "after_step"}:
        raise CheckpointValidationError("safe_point 必須為 before_step 或 after_step")
    if type(safe_point) is not str:
        raise CheckpointValidationError("safe_point 必須為 str")
    domain_run_id = _require_string(top["domain_run_id"], path="$.domain_run_id")
    if expected_domain_run_id is not None and domain_run_id != expected_domain_run_id:
        raise CheckpointValidationError("checkpoint domain_run_id 與 ledger run 不一致")

    state_payload = _require_exact_keys(
        top["domain_state"], keys=_DOMAIN_STATE_KEYS, path="$.domain_state"
    )
    state_domain_run_id = _require_string(
        state_payload["domain_run_id"], path="$.domain_state.domain_run_id"
    )
    if state_domain_run_id != domain_run_id:
        raise CheckpointValidationError("domain_state.domain_run_id 與頂層不一致")
    iteration = state_payload["iteration"]
    if type(iteration) is not int:
        raise CheckpointValidationError("domain_state.iteration 必須為 integer")
    metadata = state_payload["metadata"]
    if type(metadata) is not dict:
        raise CheckpointValidationError("domain_state.metadata 必須為 object")
    _validate_json_native(metadata, path="$.domain_state.metadata")
    domain_state = DomainRunState(
        domain_run_id=state_domain_run_id,
        iteration=iteration,
        metadata=metadata,
    )

    raw_specs = top["pending_tool_specs"]
    if type(raw_specs) is not list:
        raise CheckpointValidationError("pending_tool_specs 必須為 array")
    pending_tool_specs: list[ToolSpec] = []
    for index, raw_spec in enumerate(raw_specs):
        spec = _require_exact_keys(
            raw_spec, keys=_TOOL_SPEC_KEYS, path=f"$.pending_tool_specs[{index}]"
        )
        parameters = spec["parameters"]
        if type(parameters) is not dict:
            raise CheckpointValidationError(
                f"$.pending_tool_specs[{index}].parameters 必須為 object"
            )
        _validate_json_native(parameters, path=f"$.pending_tool_specs[{index}].parameters")
        pending_tool_specs.append(
            ToolSpec(
                tool_id=_require_string(spec["tool_id"], path=f"$.pending_tool_specs[{index}].tool_id"),
                parameters=parameters,
                cwd=_require_string(spec["cwd"], path=f"$.pending_tool_specs[{index}].cwd"),
                step_id=_require_string(spec["step_id"], path=f"$.pending_tool_specs[{index}].step_id"),
            )
        )

    current_step_id = top["current_step_id"]
    if current_step_id is not None and type(current_step_id) is not str:
        raise CheckpointValidationError("current_step_id 必須為 str 或 null")
    if pending_tool_specs:
        if current_step_id != pending_tool_specs[0].step_id:
            raise CheckpointValidationError("current_step_id 必須等於 pending head step_id")
    elif current_step_id is not None:
        raise CheckpointValidationError("空 pending list 的 current_step_id 必須為 null")
    if safe_point == "before_step" and not pending_tool_specs:
        raise CheckpointValidationError("before_step checkpoint 必須含 pending head")

    return safe_point, domain_state, pending_tool_specs, current_step_id


def encode_checkpoint_v1(
    *,
    safe_point: CheckpointSafePoint,
    domain_state: DomainRunState,
    pending_tool_specs: list[ToolSpec],
    current_step_id: str | None,
) -> str:
    """以 deterministic JSON 產生並驗證 checkpoint v1。"""

    if safe_point not in {"before_step", "after_step"}:
        raise CheckpointValidationError("safe_point 必須為 before_step 或 after_step")
    if type(domain_state.domain_run_id) is not str:
        raise CheckpointValidationError("domain_state.domain_run_id 必須為 str")
    if type(domain_state.iteration) is not int:
        raise CheckpointValidationError("domain_state.iteration 必須為 integer")
    if type(domain_state.metadata) is not dict:
        raise CheckpointValidationError("domain_state.metadata 必須為 object")
    _validate_json_native(domain_state.metadata, path="$.domain_state.metadata")
    if type(pending_tool_specs) is not list:
        raise CheckpointValidationError("pending_tool_specs 必須為 list")

    encoded_specs: list[dict[str, Any]] = []
    for index, tool_spec in enumerate(pending_tool_specs):
        if not isinstance(tool_spec, ToolSpec):
            raise CheckpointValidationError(f"pending_tool_specs[{index}] 必須為 ToolSpec")
        if type(tool_spec.tool_id) is not str or type(tool_spec.cwd) is not str or type(tool_spec.step_id) is not str:
            raise CheckpointValidationError(f"pending_tool_specs[{index}] 的字串欄位無效")
        if type(tool_spec.parameters) is not dict:
            raise CheckpointValidationError(f"pending_tool_specs[{index}].parameters 必須為 object")
        _validate_json_native(tool_spec.parameters, path=f"$.pending_tool_specs[{index}].parameters")
        encoded_specs.append(
            {
                "tool_id": tool_spec.tool_id,
                "parameters": tool_spec.parameters,
                "cwd": tool_spec.cwd,
                "step_id": tool_spec.step_id,
            }
        )

    payload = {
        "schema_version": 1,
        "safe_point": safe_point,
        "domain_run_id": domain_state.domain_run_id,
        "pending_tool_specs": encoded_specs,
        "domain_state": {
            "domain_run_id": domain_state.domain_run_id,
            "iteration": domain_state.iteration,
            "metadata": domain_state.metadata,
        },
        "current_step_id": current_step_id,
    }
    _decode_payload(
        json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":")),
        expected_domain_run_id=domain_state.domain_run_id,
    )
    return json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))


def decode_checkpoint_v1(
    checkpoint_json: str,
    *,
    expected_domain_run_id: str,
) -> RestoredCheckpoint:
    """完整驗證並還原 checkpoint v1 dataclass。"""

    safe_point, domain_state, pending_tool_specs, current_step_id = _decode_payload(
        checkpoint_json,
        expected_domain_run_id=expected_domain_run_id,
    )
    return RestoredCheckpoint(
        safe_point=safe_point,
        domain_state=domain_state,
        pending_tool_specs=pending_tool_specs,
        current_step_id=current_step_id,
    )


def inspect_checkpoint_v1(checkpoint_json: str) -> tuple[CheckpointSafePoint, str | None, str]:
    """驗證 checkpoint 並回傳 safe point、current step 與 domain run id。"""

    safe_point, domain_state, _pending_tool_specs, current_step_id = _decode_payload(
        checkpoint_json,
        expected_domain_run_id=None,
    )
    return safe_point, current_step_id, domain_state.domain_run_id
