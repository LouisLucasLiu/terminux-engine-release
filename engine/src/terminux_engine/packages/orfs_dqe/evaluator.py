# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`orfs_dqe` 的 deterministic evaluator。

把「已驗證的 manifest」＋「package-owned policy」變成一組 per-check 結果
與一個 overall judgment。完全 deterministic：沒有 LLM、沒有 shell、
沒有 subprocess、不寫任何檔案。

Aggregation（policy `aggregation.rule`）：

1. 任一 mandatory check `fail` → overall `fail`
2. 否則任一 check `warn` → overall `warn`
3. 否則任一 mandatory check `unknown` → overall `unknown`
4. 否則 → overall `pass`

Optional 的 `unknown` 永遠不會自動變成 pass——它會留在 `unknowns` 清單裡
（policy `optional_unknown_never_becomes_pass`）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from terminux_engine.packages.orfs_dqe import parsers
from terminux_engine.packages.orfs_dqe.manifest_input import (
    ManifestInput,
    RoleSelection,
    select_role,
    unknown_roles,
)
from terminux_engine.packages.orfs_dqe.models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    CheckEvidence,
    CheckResult,
    CheckStatus,
    DqeEvaluation,
)

POLICY_V1_PATH = Path(__file__).parent / "rules" / "policy_v1.json"
POLICY_V2_PATH = Path(__file__).parent / "rules" / "policy_v2.json"
POLICY_PATH = POLICY_V1_PATH

PACKAGE_ID = "orfs_dqe"
PACKAGE_VERSION = "0.1.0"


class PolicyError(RuntimeError):
    """Policy 檔案損壞或含未知 extractor——evaluator 層級失敗。"""


def load_policy(path: Path | str | None = None) -> dict[str, Any]:
    if path == "v1":
        target = POLICY_V1_PATH
    elif path == "v2":
        target = POLICY_V2_PATH
    elif isinstance(path, str):
        target = Path(path)
    elif isinstance(path, Path):
        target = path
    else:
        target = POLICY_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "checks" not in payload:
        raise PolicyError("policy file is malformed")
    return payload


def evaluation_id(manifest_id: str, policy_version: str) -> str:
    """Deterministic evaluation id：同一 manifest ＋ 同一 policy 永遠相同。"""
    return f"orfs-dqe:{manifest_id}:{policy_version}"


def evaluate(
    manifest_input: ManifestInput,
    *,
    process_exit_code: int | None,
    policy: dict[str, Any] | None = None,
    now: str | None = None,
) -> DqeEvaluation:
    """執行整組 v1 checks。"""
    policy = policy if policy is not None else load_policy()
    policy_version = str(policy["policy_version"])
    required_roles: list[str] = list(policy.get("required_roles", []))
    deliverable_roles: list[str] = list(policy.get("deliverable_roles", []))

    # 每個 role 只選一次，後續 checks 共用同一份選取結果。
    selections = {role: select_role(manifest_input, role) for role in required_roles}
    # 只讀一次檔案，避免同一份 report 被重複解析。
    text_cache: dict[str, list[str] | None] = {}

    checks: list[CheckResult] = []
    for spec in policy["checks"]:
        checks.append(
            _run_check(
                spec,
                manifest_input=manifest_input,
                selections=selections,
                deliverable_roles=deliverable_roles,
                required_roles=required_roles,
                process_exit_code=process_exit_code,
                text_cache=text_cache,
            )
        )

    unknowns = [check.check_id for check in checks if check.status == STATUS_UNKNOWN]
    extra_roles = unknown_roles(manifest_input, required_roles)
    if extra_roles:
        unknowns.append("unrecognized_roles:" + ",".join(extra_roles))

    return DqeEvaluation(
        evaluation_id=evaluation_id(
            manifest_input.manifest.manifest_id, policy_version
        ),
        package_id=PACKAGE_ID,
        package_version=PACKAGE_VERSION,
        policy_id=str(policy["policy_id"]),
        policy_version=policy_version,
        producer=dict(manifest_input.producer),
        artifact_manifest={
            "manifest_id": manifest_input.manifest.manifest_id,
            "manifest_sha256": manifest_input.manifest_sha256,
            "manifest_reference": manifest_input.manifest_reference,
            "capture_status": manifest_input.capture_status,
        },
        overall_status=aggregate(checks),
        checks=checks,
        unknowns=unknowns,
        evaluated_at=now or datetime.now(timezone.utc).isoformat(),
    )


def aggregate(checks: list[CheckResult]) -> CheckStatus:
    if any(c.status == STATUS_FAIL and c.mandatory for c in checks):
        return STATUS_FAIL
    if any(c.status == STATUS_FAIL and not c.mandatory for c in checks):
        # Optional check 的 fail 依 policy 已在 _grade() 降級成 warn；
        # 走到這裡代表 policy 明確要求 optional fail，仍以 warn 呈現。
        return STATUS_WARN
    if any(c.status == STATUS_WARN for c in checks):
        return STATUS_WARN
    if any(c.status == STATUS_UNKNOWN and c.mandatory for c in checks):
        return STATUS_UNKNOWN
    return STATUS_PASS


# ── 個別 check ─────────────────────────────────────────────────────────


def _run_check(
    spec: dict[str, Any],
    *,
    manifest_input: ManifestInput,
    selections: dict[str, RoleSelection],
    deliverable_roles: list[str],
    required_roles: list[str],
    process_exit_code: int | None,
    text_cache: dict[str, list[str] | None],
) -> CheckResult:
    extractor = spec["extractor"]
    if extractor == "manifest_integrity":
        return _check_manifest_integrity(spec, manifest_input)
    if extractor == "artifact_completeness":
        return _check_role_set(spec, selections, required_roles)
    if extractor == "final_deliverables":
        return _check_role_set(spec, selections, deliverable_roles)
    if extractor == "process_exit_code":
        return _check_process_exit_code(spec, process_exit_code)
    if extractor in {"finish_section_scalar", "drc_violation_count", "log_marker_count"}:
        return _check_from_artifact(
            spec, selections=selections, text_cache=text_cache
        )
    raise PolicyError(f"unknown extractor in policy: {extractor!r}")


def _check_manifest_integrity(
    spec: dict[str, Any], manifest_input: ManifestInput
) -> CheckResult:
    """走到這裡代表 `load_manifest_input()` 的全部驗證都已通過。

    Manifest 讀取／SHA／schema／producer／boundary 任何一項失敗都會在更早
    的階段拋出 `ManifestInputError`，由 `after.run()` 轉成 evaluator
    failure（`PhaseResult.fail`），而不是一個 quality check 的 `fail`
    ——兩者語意不同。
    """
    capture_status = manifest_input.capture_status
    if capture_status == "complete":
        status: CheckStatus = STATUS_PASS
        message = "manifest identity, canonical SHA-256, schema and producer all verified"
    elif capture_status == "partial":
        status = STATUS_WARN
        message = "manifest verified but artifact capture was only partial"
    else:
        status = STATUS_UNKNOWN
        message = f"manifest verified but capture status is {capture_status!r}"
    return CheckResult(
        check_id=spec["check_id"],
        category=spec["category"],
        status=status,
        mandatory=bool(spec.get("mandatory", False)),
        message=message,
        evidence=CheckEvidence(content_sha256=manifest_input.manifest_sha256),
    )


def _check_role_set(
    spec: dict[str, Any], selections: dict[str, RoleSelection], roles: list[str]
) -> CheckResult:
    """計算「不可用的 role 數」，與 threshold 0 比較。"""
    problems = []
    for role in roles:
        selection = selections.get(role)
        if selection is None or not selection.usable:
            reason = selection.problem if selection is not None else "role_not_selected"
            problems.append(f"{role}({reason})")
    observed = len(problems)
    status = _grade(spec, observed)
    label = spec.get("unit") or "roles"
    message = (
        f"all {len(roles)} {spec['category']} {label} are present and stable"
        if observed == 0
        else f"unusable {spec['category']} roles: " + ", ".join(problems)
    )
    return CheckResult(
        check_id=spec["check_id"],
        category=spec["category"],
        status=status,
        mandatory=bool(spec.get("mandatory", False)),
        observed_value=observed,
        unit=spec.get("unit"),
        comparator=spec.get("comparator"),
        threshold=spec.get("threshold"),
        message=message,
    )


def _check_process_exit_code(
    spec: dict[str, Any], process_exit_code: int | None
) -> CheckResult:
    """記錄 process 事實；**不改寫**它。"""
    if process_exit_code is None:
        return CheckResult(
            check_id=spec["check_id"],
            category=spec["category"],
            status=STATUS_UNKNOWN,
            mandatory=bool(spec.get("mandatory", False)),
            comparator=spec.get("comparator"),
            threshold=spec.get("threshold"),
            message="event carries no process exit code",
        )
    status = _grade(spec, process_exit_code)
    return CheckResult(
        check_id=spec["check_id"],
        category=spec["category"],
        status=status,
        mandatory=bool(spec.get("mandatory", False)),
        observed_value=process_exit_code,
        comparator=spec.get("comparator"),
        threshold=spec.get("threshold"),
        message=f"tool process exit code is {process_exit_code}",
    )


def _check_from_artifact(
    spec: dict[str, Any],
    *,
    selections: dict[str, RoleSelection],
    text_cache: dict[str, list[str] | None],
) -> CheckResult:
    role = spec["source_role"]
    selection = selections.get(role)
    mandatory = bool(spec.get("mandatory", False))

    if selection is None or not selection.usable:
        reason = selection.problem if selection is not None else "role_not_in_policy"
        return CheckResult(
            check_id=spec["check_id"],
            category=spec["category"],
            status=STATUS_UNKNOWN,
            mandatory=mandatory,
            unit=spec.get("unit"),
            comparator=spec.get("comparator"),
            threshold=spec.get("threshold"),
            message=f"source artifact unusable: {role} ({reason})",
            evidence=CheckEvidence(adapter_role=role),
        )

    observation = selection.observation
    assert observation is not None and observation.resolved_path is not None
    base_evidence = CheckEvidence(
        artifact_instance_id=observation.artifact_instance_id,
        adapter_role=role,
        content_sha256=(
            observation.content_identity.digest
            if observation.content_identity is not None
            else None
        ),
    )

    lines = _read_lines(observation.resolved_path, text_cache)
    if lines is None:
        return CheckResult(
            check_id=spec["check_id"],
            category=spec["category"],
            status=STATUS_UNKNOWN,
            mandatory=mandatory,
            unit=spec.get("unit"),
            comparator=spec.get("comparator"),
            threshold=spec.get("threshold"),
            message=f"source artifact could not be read as text: {role}",
            evidence=base_evidence,
        )

    args = spec.get("extractor_args", {})
    extractor = spec["extractor"]
    if extractor == "finish_section_scalar":
        extraction = parsers.extract_finish_scalar(
            lines, section=args["section"], pattern=args["pattern"]
        )
        value, problem = extraction.value, extraction.problem
        evidence = CheckEvidence(
            artifact_instance_id=base_evidence.artifact_instance_id,
            adapter_role=role,
            content_sha256=base_evidence.content_sha256,
            section=extraction.section,
            line_start=extraction.line_start,
            line_end=extraction.line_end,
            excerpt_hash=extraction.excerpt_hash,
        )
    elif extractor == "drc_violation_count":
        count = parsers.count_drc_violations(lines)
        value, problem = count.count, count.problem
        evidence = CheckEvidence(
            artifact_instance_id=base_evidence.artifact_instance_id,
            adapter_role=role,
            content_sha256=base_evidence.content_sha256,
            section="violation type: markers",
            line_start=count.line_start,
            line_end=count.line_end,
            excerpt_hash=count.excerpt_hash,
        )
    else:  # log_marker_count
        count = parsers.count_log_markers(lines, kind=args["kind"])
        value, problem = count.count, count.problem
        evidence = CheckEvidence(
            artifact_instance_id=base_evidence.artifact_instance_id,
            adapter_role=role,
            content_sha256=base_evidence.content_sha256,
            section=f"[{args['kind'].upper()}] markers",
            line_start=count.line_start,
            line_end=count.line_end,
            excerpt_hash=count.excerpt_hash,
        )

    if value is None:
        # 找不到就是 unknown，**不填 0**。
        return CheckResult(
            check_id=spec["check_id"],
            category=spec["category"],
            status=STATUS_UNKNOWN,
            mandatory=mandatory,
            unit=spec.get("unit"),
            comparator=spec.get("comparator"),
            threshold=spec.get("threshold"),
            message=f"value not extractable: {problem or 'not_found'}",
            evidence=evidence,
        )

    status = _grade(spec, value)
    return CheckResult(
        check_id=spec["check_id"],
        category=spec["category"],
        status=status,
        mandatory=mandatory,
        observed_value=value,
        unit=spec.get("unit"),
        comparator=spec.get("comparator"),
        threshold=spec.get("threshold"),
        message=_grade_message(spec, value, status),
        evidence=evidence,
    )


def _read_lines(
    path: str, text_cache: dict[str, list[str] | None]
) -> list[str] | None:
    if path in text_cache:
        return text_cache[path]
    try:
        lines = parsers.read_text(Path(path))
    except (OSError, parsers.ParseError):
        lines = None
    text_cache[path] = lines
    return lines


def _grade(spec: dict[str, Any], value: float | int) -> CheckStatus:
    """套用 comparator／threshold；違反時依 mandatory 決定 fail 或 warn。"""
    comparator = spec.get("comparator")
    threshold = spec.get("threshold")
    if comparator is None or threshold is None:
        return STATUS_PASS
    if _compare(value, comparator, threshold):
        return STATUS_PASS
    severity = spec.get("severity_on_fail")
    if severity == "warn":
        return STATUS_WARN
    return STATUS_FAIL if spec.get("mandatory", False) else STATUS_WARN


def _compare(value: float | int, comparator: str, threshold: float | int) -> bool:
    if comparator == "==":
        return value == threshold
    if comparator == "!=":
        return value != threshold
    if comparator == ">=":
        return value >= threshold
    if comparator == ">":
        return value > threshold
    if comparator == "<=":
        return value <= threshold
    if comparator == "<":
        return value < threshold
    raise PolicyError(f"unknown comparator in policy: {comparator!r}")


def _grade_message(spec: dict[str, Any], value: float | int, status: CheckStatus) -> str:
    unit = f" {spec['unit']}" if spec.get("unit") else ""
    expectation = f"{spec.get('comparator')} {spec.get('threshold')}"
    if status == STATUS_PASS:
        return f"observed {value}{unit} satisfies {expectation}"
    return f"observed {value}{unit} violates {expectation}"


__all__ = [
    "PACKAGE_ID",
    "PACKAGE_VERSION",
    "POLICY_PATH",
    "PolicyError",
    "aggregate",
    "evaluate",
    "evaluation_id",
    "load_policy",
]
