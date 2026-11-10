# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Deterministic Plan Validator。

不相信 model 填的 `risk`、`approval_required`、`side_effects` 或
`validation`——那四個欄位一律以 Capability Contract authority **重填**，
model 填的值只在不一致時被記成 error。

Validator 只回傳結果，不修補後執行、不建立 proposal、不呼叫任何東西。
"""

from __future__ import annotations

import json
import re
from typing import Any

from terminux_engine.planning.capability import (
    FORBIDDEN_CAPABILITY_IDS,
    CapabilitySnapshot,
)
from terminux_engine.planning.plan import (
    MAX_ARGUMENT_BYTES,
    MAX_GOAL_CHARS,
    MAX_LIST_ITEMS,
    MAX_STEPS,
    MAX_TEXT_ITEM_CHARS,
    PLAN_MODE,
    PLAN_SCHEMA_VERSION,
    STEP_ID_PATTERN,
    UNSUPPORTED_BINDING_MARKERS,
    UNSUPPORTED_STEP_KEYS,
    Plan,
    PlanStep,
    PlanValidationResult,
    compute_plan_id,
)

PLAN_ERROR_CODES: tuple[str, ...] = (
    "PLAN_SCHEMA_INVALID",
    "PLAN_MODE_INVALID",
    "PLAN_EXECUTION_FLAG_FORBIDDEN",
    "PLAN_STEP_LIMIT_EXCEEDED",
    "PLAN_DEPENDENCY_INVALID",
    "PLAN_DEPENDENCY_CYCLE",
    "PLAN_CAPABILITY_UNKNOWN",
    "PLAN_CAPABILITY_FORBIDDEN",
    "PLAN_ARGUMENTS_INVALID",
    "PLAN_RISK_MISMATCH",
    "PLAN_APPROVAL_MISMATCH",
    "PLAN_REQUIRED_FIELD_MISSING",
    "PLAN_UNSUPPORTED_BINDING",
    "PLAN_UNSUPPORTED_CONTROL_FLOW",
    "PLAN_ID_MISMATCH",
    "PLAN_UNSUPPORTED_COMPLETION_CLAIM",
)

_REQUIRED_TOP_LEVEL = ("goal", "steps")
_REQUIRED_STEP_FIELDS = ("step_id", "capability", "arguments")

#: Observation 不得宣稱尚未發生的事。這些是「已完成」語氣的標記；命中
#: 時要求該 observation 必須由 `created_from.authoritative_observations`
#: 支持，否則判 `PLAN_UNSUPPORTED_COMPLETION_CLAIM`。
_COMPLETION_MARKERS: tuple[str, ...] = (
    "passed", "pass", "completed", "succeeded", "finished", "resolved",
    "fixed", "improved", "regressed", "已通過", "已完成", "已修復", "已改善",
)

_JSON_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float),
    "boolean": (bool,), "object": (dict,), "array": (list,),
}


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _texts_ok(items: Any) -> bool:
    return (
        isinstance(items, (list, tuple))
        and len(items) <= MAX_LIST_ITEMS
        and all(isinstance(i, str) and i.strip() and len(i) <= MAX_TEXT_ITEM_CHARS
                for i in items)
    )


def _contains_binding(value: Any) -> str | None:
    if isinstance(value, str):
        for marker in UNSUPPORTED_BINDING_MARKERS:
            if marker in value:
                return marker
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            hit = _contains_binding(k) or _contains_binding(v)
            if hit:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for v in value:
            hit = _contains_binding(v)
            if hit:
                return hit
    return None


RUN_ID_PATTERN = re.compile(r"^run_[0-9a-fA-F]{32}$")
_CHINESE_PLACEHOLDER_MARKERS = (
    "尚未提供",
    "需由使用者",
    "請由使用者",
    "由使用者",
    "待提供",
    "待輸入",
    "待補",
    "未指定",
    "未填寫",
)
_ENGLISH_PLACEHOLDER_WORD_PATTERN = re.compile(r"\b(todo|tbd)\b", re.IGNORECASE)
_PLACEHOLDER_TAG_PATTERN = re.compile(r"<[a-zA-Z0-9_\s-]+>")

_FREE_TEXT_FIELD_NAMES = frozenset({
    "question", "description", "prompt", "comment", "note",
    "message", "query", "summary", "text", "content", "reason", "details",
})


def _is_constrained_or_identifier_field(name: str, spec: dict[str, Any]) -> bool:
    """判斷欄位是否為 identifier / path / enum 或具備 pattern/format 的結構化欄位。"""
    if spec.get("pattern") or spec.get("format") or spec.get("enum"):
        return True
    lower_name = name.lower()
    if lower_name in _FREE_TEXT_FIELD_NAMES:
        return False
    if lower_name in ("id", "identifier", "uuid", "key") or lower_name.endswith(("_id", "_uuid", "_key", "_ref")):
        return True
    if lower_name in ("path", "file", "dir", "filename", "filepath", "directory") or lower_name.endswith(("_path", "_file", "_dir", "_filename")):
        return True
    return False


def _contains_placeholder_marker(value: str) -> bool:
    """檢查字串值是否含有佔位符標記（英文整詞比對，中文子字串比對）。"""
    stripped = value.strip()
    if any(marker in stripped for marker in _CHINESE_PLACEHOLDER_MARKERS):
        return True
    if _ENGLISH_PLACEHOLDER_WORD_PATTERN.search(stripped) is not None:
        return True
    if _PLACEHOLDER_TAG_PATTERN.search(stripped) is not None:
        return True
    return False


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """對既有 Tool Registry 的 JSON Schema 做最小、deterministic 的驗證。

    只支援 v1 需要的子集：`type: object`、`properties`、`required`。無法理解
    的 schema 一律回報，不靜默放行。
    """
    problems: list[str] = []
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return ["capability input schema is not a JSON object schema"]
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ["capability input schema has no properties map"]

    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        problems.append(f"unknown argument(s): {unknown}")
    for name in schema.get("required") or []:
        if name not in arguments:
            problems.append(f"missing required argument: {name}")
    for name, value in arguments.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        checks = _JSON_TYPE_CHECKS.get(expected) if isinstance(expected, str) else None
        if checks is None:
            continue
        if isinstance(value, bool) and bool not in checks:
            problems.append(f"argument {name} must be {expected}")
            continue
        elif not isinstance(value, checks):
            problems.append(f"argument {name} must be {expected}")
            continue

        if isinstance(value, str):
            # 檢查佔位說明文字（只針對 identifier / path / enum 或有 pattern/format 的欄位）
            if _is_constrained_or_identifier_field(name, spec) and _contains_placeholder_marker(value):
                problems.append(
                    f"argument {name} contains placeholder or descriptive text: {value!r}"
                )
                continue

            # 檢查 schema 宣告的 pattern
            pattern = spec.get("pattern")
            if pattern:
                try:
                    if not re.search(pattern, value):
                        problems.append(f"argument {name} must match pattern {pattern!r}")
                        continue
                except re.error:
                    pass

            # 檢查 run id 格式：若 spec 宣告 format 為 run_id/run_<32 hex>，或 description 包含 run_<32 hex>
            desc = str(spec.get("description") or "")
            spec_format = str(spec.get("format") or "")
            requires_run_id_format = (
                "run_<32 hex>" in desc
                or spec_format in ("run_id", "run_<32 hex>")
                or (name in ("run_id", "baseline_run_id", "candidate_run_id") and "run_<32 hex>" in desc)
            )
            if requires_run_id_format:
                if not RUN_ID_PATTERN.match(value):
                    problems.append(f"argument {name} must match format 'run_<32 hex>'; got {value!r}")
                    continue

    try:
        size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        problems.append("arguments are not JSON-serializable")
    else:
        if size > MAX_ARGUMENT_BYTES:
            problems.append(f"arguments exceed {MAX_ARGUMENT_BYTES} bytes")
    return problems


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        for nxt in graph.get(node, []):
            if nxt in graph and visit(nxt):
                return True
        state[node] = 2
        return False

    return any(visit(n) for n in graph)


def validate_plan(
    payload: Any,
    snapshot: CapabilitySnapshot,
    *,
    created_from: dict[str, Any] | None = None,
) -> Plan:
    """驗證一份 draft plan payload，回傳帶 validation 結果的 `Plan`。

    永遠回傳一個 `Plan`（可能是 invalid）——呼叫端只能把它交還使用者，
    不得因為它 valid 就去執行任何東西。
    """
    errors: list[dict[str, Any]] = []
    created_from = dict(created_from or {})
    authoritative = set(created_from.get("authoritative_observations") or [])

    if not isinstance(payload, dict):
        return _invalid([_err("PLAN_SCHEMA_INVALID", "plan payload is not a JSON object")],
                        snapshot, created_from)

    # ── 1. schema / required fields ───────────────────────────────────
    for field_name in _REQUIRED_TOP_LEVEL:
        if field_name not in payload:
            errors.append(_err("PLAN_REQUIRED_FIELD_MISSING",
                               f"missing required field: {field_name}", field=field_name))
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errors.append(_err("PLAN_SCHEMA_INVALID", "goal must be a non-empty string"))
        goal = ""
    elif len(goal) > MAX_GOAL_CHARS:
        errors.append(_err("PLAN_SCHEMA_INVALID", f"goal exceeds {MAX_GOAL_CHARS} characters"))

    version = payload.get("schema_version", PLAN_SCHEMA_VERSION)
    if version != PLAN_SCHEMA_VERSION:
        errors.append(_err("PLAN_SCHEMA_INVALID",
                           f"schema_version must be {PLAN_SCHEMA_VERSION}", got=version))

    # ── 2. mode / execution flag ──────────────────────────────────────
    mode = payload.get("mode", PLAN_MODE)
    if mode != PLAN_MODE:
        errors.append(_err("PLAN_MODE_INVALID", f"mode must be {PLAN_MODE!r}", got=mode))
    if payload.get("execution_allowed", False):
        errors.append(_err("PLAN_EXECUTION_FLAG_FORBIDDEN",
                           "execution_allowed must be false in a plan-only plan"))

    catalog_id = payload.get("catalog_snapshot_id", snapshot.snapshot_id)
    if catalog_id != snapshot.snapshot_id:
        errors.append(_err("PLAN_SCHEMA_INVALID",
                           "catalog_snapshot_id does not match the snapshot used",
                           got=catalog_id, expected=snapshot.snapshot_id))

    # ── 3. bounded lists ──────────────────────────────────────────────
    observations = payload.get("observations", []) or []
    unknowns = payload.get("unknowns", []) or []
    global_stop = payload.get("global_stop_conditions", []) or []
    for label, items in (("observations", observations), ("unknowns", unknowns),
                         ("global_stop_conditions", global_stop)):
        if not _texts_ok(items):
            errors.append(_err("PLAN_SCHEMA_INVALID",
                               f"{label} must be at most {MAX_LIST_ITEMS} non-empty strings "
                               f"of at most {MAX_TEXT_ITEM_CHARS} characters"))

    # ── 10. observation 不得宣稱未發生的事 ────────────────────────────
    if isinstance(observations, (list, tuple)):
        for item in observations:
            if not isinstance(item, str):
                continue
            lowered = item.lower()
            if any(m in lowered for m in _COMPLETION_MARKERS) and item not in authoritative:
                errors.append(_err(
                    "PLAN_UNSUPPORTED_COMPLETION_CLAIM",
                    "observation asserts a completed/graded outcome that is not backed by "
                    "an authoritative observation supplied to the planner",
                    observation=item))

    # ── 4./5./6./7./8./9. steps ───────────────────────────────────────
    raw_steps = payload.get("steps")
    steps: list[PlanStep] = []
    if not isinstance(raw_steps, list) or not raw_steps:
        errors.append(_err("PLAN_SCHEMA_INVALID", "steps must be a non-empty list"))
        raw_steps = []
    elif len(raw_steps) > MAX_STEPS:
        errors.append(_err("PLAN_STEP_LIMIT_EXCEEDED",
                           f"plan has {len(raw_steps)} steps (max {MAX_STEPS})"))

    seen_ids: set[str] = set()
    collected_unknowns: list[str] = [u for u in unknowns if isinstance(u, str)]
    for index, raw in enumerate(raw_steps[:MAX_STEPS] if isinstance(raw_steps, list) else []):
        if not isinstance(raw, dict):
            errors.append(_err("PLAN_SCHEMA_INVALID", f"step {index} is not an object"))
            continue

        unsupported = sorted(set(raw) & UNSUPPORTED_STEP_KEYS)
        if unsupported:
            errors.append(_err("PLAN_UNSUPPORTED_CONTROL_FLOW",
                               f"step {index} uses unsupported control flow keys",
                               keys=unsupported))

        for field_name in _REQUIRED_STEP_FIELDS:
            if field_name not in raw:
                errors.append(_err("PLAN_REQUIRED_FIELD_MISSING",
                                   f"step {index} is missing {field_name}", field=field_name))

        step_id = raw.get("step_id")
        if not isinstance(step_id, str) or not STEP_ID_PATTERN.match(step_id or ""):
            errors.append(_err("PLAN_SCHEMA_INVALID",
                               f"step {index} has an invalid step_id", got=step_id))
            step_id = f"__invalid_{index}"
        if step_id in seen_ids:
            errors.append(_err("PLAN_DEPENDENCY_INVALID",
                               f"duplicate step_id: {step_id}", step_id=step_id))
        seen_ids.add(step_id)

        capability_id = raw.get("capability")
        capability = None
        if not isinstance(capability_id, str) or not capability_id:
            errors.append(_err("PLAN_CAPABILITY_UNKNOWN",
                               f"step {step_id} has no capability id"))
            capability_id = ""
        elif capability_id in FORBIDDEN_CAPABILITY_IDS:
            errors.append(_err("PLAN_CAPABILITY_FORBIDDEN",
                               f"step {step_id} uses a forbidden capability",
                               capability=capability_id))
        else:
            capability = snapshot.get(capability_id)
            if capability is None:
                errors.append(_err("PLAN_CAPABILITY_UNKNOWN",
                                   f"step {step_id} uses a capability that is not in the "
                                   f"catalog snapshot", capability=capability_id))

        arguments = raw.get("arguments")
        arg_problems: list[str] = []
        if not isinstance(arguments, dict):
            errors.append(_err("PLAN_ARGUMENTS_INVALID",
                               f"step {step_id} arguments must be an object"))
            arg_problems.append("arguments must be an object")
            arguments = {}
        else:
            binding = _contains_binding(arguments)
            if binding is not None:
                errors.append(_err("PLAN_UNSUPPORTED_BINDING",
                                   f"step {step_id} arguments use an unsupported dynamic "
                                   f"binding marker", marker=binding))
                arg_problems.append(f"unsupported dynamic binding marker {binding!r}")
            elif capability is not None:
                arg_problems = _validate_arguments(capability.input_schema, arguments)
                for problem in arg_problems:
                    errors.append(_err("PLAN_ARGUMENTS_INVALID",
                                       f"step {step_id}: {problem}", capability=capability_id))

        if arg_problems:
            # 不符合的步驟不要放進 steps，改列入 unknowns，並說明缺少什麼
            collected_unknowns.append(
                (f"step {step_id} ({capability_id or 'unknown'}): " + "; ".join(arg_problems))[:MAX_TEXT_ITEM_CHARS]
            )
            continue

        depends_on = raw.get("depends_on", []) or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            errors.append(_err("PLAN_DEPENDENCY_INVALID",
                               f"step {step_id} depends_on must be a list of step ids"))
            depends_on = []

        for label in ("preconditions", "expected_evidence", "stop_conditions"):
            items = raw.get(label, []) or []
            if not _texts_ok(items) or not items:
                errors.append(_err("PLAN_REQUIRED_FIELD_MISSING",
                                   f"step {step_id} needs a non-empty, bounded {label}",
                                   field=label))

        # ── 8. authority 重填 risk / approval / side effects ──────────
        if capability is not None:
            declared_risk = raw.get("risk")
            if declared_risk is not None and str(declared_risk).lower() != capability.risk:
                errors.append(_err("PLAN_RISK_MISMATCH",
                                   f"step {step_id} declared risk {declared_risk!r} but the "
                                   f"authority says {capability.risk!r}",
                                   capability=capability_id))
            declared_approval = raw.get("approval_required")
            if (declared_approval is not None
                    and bool(declared_approval) != capability.approval_required):
                errors.append(_err("PLAN_APPROVAL_MISMATCH",
                                   f"step {step_id} declared approval_required "
                                   f"{declared_approval!r} but the authority says "
                                   f"{capability.approval_required!r}",
                                   capability=capability_id))
            risk = capability.risk
            approval_required = capability.approval_required
            side_effects = capability.side_effects
        else:
            risk, approval_required, side_effects = "dangerous", True, ()

        steps.append(PlanStep(
            step_id=step_id, capability=capability_id, arguments=dict(arguments),
            depends_on=tuple(depends_on),
            preconditions=tuple(raw.get("preconditions") or []),
            expected_evidence=tuple(raw.get("expected_evidence") or []),
            stop_conditions=tuple(raw.get("stop_conditions") or []),
            risk=risk, approval_required=approval_required, side_effects=tuple(side_effects),
        ))

    # ── dependency graph ──────────────────────────────────────────────
    ids = {s.step_id for s in steps}
    graph: dict[str, list[str]] = {}
    for step in steps:
        for dep in step.depends_on:
            if dep not in ids:
                errors.append(_err("PLAN_DEPENDENCY_INVALID",
                                   f"step {step.step_id} depends on unknown step {dep!r}",
                                   step_id=step.step_id))
            if dep == step.step_id:
                errors.append(_err("PLAN_DEPENDENCY_CYCLE",
                                   f"step {step.step_id} depends on itself"))
        graph[step.step_id] = [d for d in step.depends_on if d in ids]
    if graph and _has_cycle(graph):
        errors.append(_err("PLAN_DEPENDENCY_CYCLE", "the dependency graph contains a cycle"))

    # ── 11. plan id 重算 ──────────────────────────────────────────────
    plan_id = compute_plan_id(
        goal=goal, catalog_snapshot_id=snapshot.snapshot_id, steps=steps,
        observations=[o for o in observations if isinstance(o, str)],
        unknowns=collected_unknowns,
        global_stop_conditions=[c for c in global_stop if isinstance(c, str)],
    )
    declared_plan_id = payload.get("plan_id")
    if declared_plan_id is not None and declared_plan_id != plan_id:
        errors.append(_err("PLAN_ID_MISMATCH",
                           "plan_id does not match the canonical content",
                           got=declared_plan_id, expected=plan_id))

    limitations = payload.get("limitations", []) or []
    if not _texts_ok(limitations):
        limitations = []

    return Plan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        mode=PLAN_MODE,
        goal=goal,
        catalog_snapshot_id=snapshot.snapshot_id,
        created_from=created_from,
        observations=tuple(o for o in observations if isinstance(o, str)),
        unknowns=tuple(collected_unknowns),
        steps=tuple(steps),
        global_stop_conditions=tuple(c for c in global_stop if isinstance(c, str)),
        limitations=tuple(limitations),
        validation=PlanValidationResult(
            status="valid" if not errors else "invalid", errors=tuple(errors)),
    )


def _invalid(errors: list[dict[str, Any]], snapshot: CapabilitySnapshot,
             created_from: dict[str, Any]) -> Plan:
    return Plan(
        schema_version=PLAN_SCHEMA_VERSION, plan_id="plan_invalid", mode=PLAN_MODE,
        goal="", catalog_snapshot_id=snapshot.snapshot_id, created_from=created_from,
        observations=(), unknowns=(), steps=(), global_stop_conditions=(), limitations=(),
        validation=PlanValidationResult(status="invalid", errors=tuple(errors)),
    )


__all__ = ["PLAN_ERROR_CODES", "validate_plan"]
