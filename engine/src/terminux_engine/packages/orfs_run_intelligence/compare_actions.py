# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`orfs_run_intelligence.compare_runs` — compare_runs 的第二個 action。

Read-only intelligence：解析 baseline／candidate 兩個 run → 讀已記錄的 DQE
→ 驗證兩份 manifest 與 artifact hash → 由 ORFS policy 判定 comparability →
產生 deterministic metric deltas → 組 Common Context Snapshot → **一次**
LLM 解讀 → 驗證輸出 → 把 Context Snapshot 與 `report_insight` 寫進既有
EventStore。

**絕不**執行 ORFS／OpenROAD／shell，**絕不**修改任何 design artifact。
這個 action 與 `explain_run` 一樣註冊為 `DANGEROUS`，只能經由已核准的
proposal 執行。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.result import RecordRequest
from terminux_engine.packages.orfs_run_intelligence import (
    comparison_context as cc,
    comparison_prompt,
    dqe_reader,
    evidence as ev,
)
from terminux_engine.packages.orfs_run_intelligence.actions import (
    MAX_QUESTION_CHARS,
    _REJECTED_QUESTION_MARKERS,
    InputError,
    _commit,
    _receipt_for,
    _execution_evidence_resolver,
    _model_metadata,
    _zero_provider_reason,
    resolve_execution_evidence,
)
from terminux_engine.packages.orfs_run_intelligence.comparison import (
    STATUS_NOT_COMPARABLE,
)
from terminux_engine.packages.orfs_run_intelligence.schemas import (
    INSIGHT_RECORD_TYPE,
    NAMESPACE,
    SCHEMA_VERSION,
    SUPPORT_INSUFFICIENT,
    SUPPORT_PARTIAL,
    SUPPORT_SUPPORTED,
    LlmOutputError,
    canonical_json,
    sha256_text,
)

_LOG = logging.getLogger(__name__)

PACKAGE_ID = "orfs_run_intelligence"
PACKAGE_VERSION = "0.1.0"
COMPARISON_CONTEXT_TYPE = "orfs_run_comparison_context"

DEFAULT_QUESTION = (
    "What improved or degraded between these two runs, are they really "
    "comparable, and what evidence is still missing?"
)


def compare_runs(ctx: PackageContext, query: str | None = None) -> dict[str, Any]:
    """Action entry point。永不拋出到 Agent——失敗一律回結構化狀態。"""
    try:
        question, baseline_id, candidate_id = _validate_args(ctx)
    except InputError as exc:
        return _failure("invalid_arguments", str(exc))

    try:
        baseline = dqe_reader.resolve_run(ctx, baseline_id)
        candidate = dqe_reader.resolve_run(ctx, candidate_id)
    except dqe_reader.RunSelectionError as exc:
        return _failure(SUPPORT_INSUFFICIENT, f"run pair could not be resolved: {exc}")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("orfs_run_intelligence run pair resolution failed: %r", exc)
        return _failure("failed", f"run pair resolution failed: {exc!r}")

    for role, run in (("baseline", baseline), ("candidate", candidate)):
        if not run.manifest_reference:
            return _failure(
                SUPPORT_INSUFFICIENT,
                f"the {role} run {run.run_id} has DQE rows but no artifact manifest reference",
            )

    try:
        baseline_verified = ev.load_verified_manifest(
            baseline.manifest_reference, expected_run_id=baseline.run_id,
            text_roles=cc.TEXT_ROLES)
        candidate_verified = ev.load_verified_manifest(
            candidate.manifest_reference, expected_run_id=candidate.run_id,
            text_roles=cc.TEXT_ROLES)
    except ev.ManifestVerificationError as exc:
        return _failure(SUPPORT_INSUFFICIENT,
                        f"an artifact manifest could not be verified: {exc}")

    # host 端已驗證的實際輸入身分。拿不到就維持 `None`，
    # policy 照舊把該維度列成 unknown，不默認兩側相同。
    baseline_inputs = _verified_inputs_for(ctx, baseline.run_id)
    candidate_inputs = _verified_inputs_for(ctx, candidate.run_id)
    for role, inputs, verified in (("baseline", baseline_inputs, baseline_verified),
                                   ("candidate", candidate_inputs, candidate_verified)):
        bound = getattr(inputs, "artifact_manifest_sha256", None)
        if bound and bound != verified.manifest_sha256:
            # RC-4：durable receipt 已綁定 manifest bytes；DQE PATH 指到的檔案
            # 不同就是被替換或竄改，不以「讀得到的那一份」繼續比較。
            return _failure(
                SUPPORT_INSUFFICIENT,
                f"the {role} artifact manifest does not match the durable receipt")

    built = cc.build_comparison_context(
        question=question,
        baseline_run=baseline, candidate_run=candidate,
        baseline_verified=baseline_verified, candidate_verified=candidate_verified,
        baseline_receipt=_receipt_for(ctx, "baseline"),
        candidate_receipt=_receipt_for(ctx, "candidate"),
        baseline_verified_inputs=baseline_inputs,
        candidate_verified_inputs=candidate_inputs,
        declared_controlled_changes=_declared_controlled_changes(ctx),
        comparison_policy_id=_comparison_policy_id(ctx),
        # host Evidence Resolver 的判定，只進入 facts 與輸出。
        baseline_execution_evidence=resolve_execution_evidence(
            _execution_evidence_resolver(ctx), baseline),
        candidate_execution_evidence=resolve_execution_evidence(
            _execution_evidence_resolver(ctx), candidate),
    )
    snapshot = built.snapshot

    if not snapshot.evidence_refs():
        return _emit_without_llm(ctx, built)

    zero_provider_reason = _zero_provider_reason(ctx)
    if zero_provider_reason is not None:
        # 已授權的零 LLM 預算：不呼叫 LLM，明確將 narrative 標為 UNAVAILABLE。
        return _emit_zero_provider(ctx, built, zero_provider_reason)

    if getattr(getattr(ctx, "services", None), "llm", None) is None:
        # 沒有 provider 是既有的 unavailable 失敗路徑，**不是**零 provider 授權。
        # 這裡明確轉成誠實失敗，不讓 AttributeError 逸出到 Agent。
        return _failure(
            "failed", "interpretation unavailable: no LLM provider is configured",
        )

    try:
        prompt_run = comparison_prompt.run_prompt(ctx.services.llm, snapshot)
    except (comparison_prompt.LlmUnavailableError, LlmOutputError) as exc:
        from terminux_engine.packages.orfs_run_intelligence import (
            analysis_failure as AF,
        )

        classified = AF.classify(
            exc, capability_id="orfs_run_intelligence.compare_runs",
            invocation={"context_id": snapshot.context_id,
                        "baseline_run_id": baseline.run_id,
                        "candidate_run_id": candidate.run_id})
        # 解說失敗不會讓**已經算好的 deterministic 比較**消失：它在呼叫模型
        # 之前就完成，來源是兩次 run 的已驗證證據，與模型輸出無關。這裡如實
        # 帶回，讓 host 的固定 gate 仍有完整輸入；`status` 維持 failed，
        # validator 的拒絕不會被洗成成功。
        return _failure("failed", f"interpretation unavailable: {exc}",
                        context_id=snapshot.context_id,
                        comparison_status=built.decision.status,
                        selection_eligibility=_selection_eligibility(
                            built.decision, _formal_proof_fact(ctx)),
                        metric_comparisons=snapshot.domain_payload[
                            "metric_comparisons"],
                        **({"analysis_failure": classified} if classified else {}))

    return _emit(ctx, built, prompt_run)


# ── internals ──────────────────────────────────────────────────────────


def _validate_args(ctx: PackageContext) -> tuple[str, str, str]:
    args = ctx.facts.get("action_args") or {}
    if not isinstance(args, dict):
        raise InputError("action_args must be an object")

    baseline = args.get("baseline_run_id")
    candidate = args.get("candidate_run_id")
    for label, value in (("baseline_run_id", baseline), ("candidate_run_id", candidate)):
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"{label} is required and must be a non-empty string")
        try:
            dqe_reader.validate_run_id(value)
        except dqe_reader.RunSelectionError as exc:
            raise InputError(f"{label}: {exc}") from exc
    if baseline == candidate:
        raise InputError("baseline_run_id and candidate_run_id must be different runs")

    question = args.get("question")
    if question is None:
        return DEFAULT_QUESTION, baseline, candidate
    if not isinstance(question, str) or not question.strip():
        raise InputError("question must be a non-empty string when provided")
    if len(question) > MAX_QUESTION_CHARS:
        raise InputError(f"question exceeds {MAX_QUESTION_CHARS} characters")
    lowered = question.lower()
    for marker in _REJECTED_QUESTION_MARKERS:
        if marker in lowered:
            raise InputError(f"question contains a rejected marker: {marker!r}")
    return question.strip(), baseline, candidate


#: host 注入的正式 reader。Package 只呼叫它，不自己讀 ledger／事件。
RUN_INPUT_EVIDENCE_ATTR = "run_input_evidence_reader"


def _verified_inputs_for(ctx: PackageContext, run_id: str) -> Any:
    """向 host 的正式 reader 索取這一筆 run 的已驗證輸入身分。

    這**不是** action arguments 或 provider 可以塞的東西：只有 daemon 端
    composition 注入的 reader 才會回傳結果。任何失敗都回 `None`——identity
    維持 unknown，絕不默認兩側相同。
    """
    reader = getattr(getattr(ctx, "services", None), RUN_INPUT_EVIDENCE_ATTR, None)
    if reader is None:
        return None
    context = getattr(getattr(ctx, "services", None), "request_context", None)
    try:
        if callable(context):
            context = context()
        return reader.read_for_scope(context, run_id)
    except Exception:  # noqa: BLE001 - 讀不到就是 unknown，不是猜
        return None


def _comparison_policy_id(ctx: PackageContext) -> str | None:
    """host 傳來的固定 comparison policy 身分。未知值不會放寬任何維度。"""
    value = (ctx.facts.get("action_args") or {}).get("comparison_policy_id")
    return str(value) if isinstance(value, str) and value.strip() else None


def _declared_controlled_changes(ctx: PackageContext) -> tuple[str, ...]:
    declared = (ctx.facts.get("action_args") or {}).get("controlled_changes")
    if not isinstance(declared, (list, tuple)):
        return ()
    return tuple(str(d) for d in declared if isinstance(d, str))


def _support_status(built: cc.ComparisonContext) -> str:
    snapshot = built.snapshot
    if built.decision.status == STATUS_NOT_COMPARABLE or not snapshot.evidence_refs():
        return SUPPORT_INSUFFICIENT
    if snapshot.missing_context or built.decision.unknowns or snapshot.budget.truncated:
        return SUPPORT_PARTIAL
    if any(c.comparison == "not_comparable" or c.status_comparison == "unresolved"
           for c in built.comparisons):
        return SUPPORT_PARTIAL
    return SUPPORT_SUPPORTED


def _records(built: cc.ComparisonContext, insight: dict[str, Any] | None) -> list[RecordRequest]:
    snapshot = built.snapshot
    roles = {s.role: s.subject_id for s in snapshot.subjects}
    tags = {"baseline_run_id": str(roles.get("baseline")),
            "candidate_run_id": str(roles.get("candidate")),
            "comparison_status": built.decision.status}
    records = [RecordRequest(
        namespace=NAMESPACE, record_type=COMPARISON_CONTEXT_TYPE, key=snapshot.context_id,
        payload=snapshot.to_json_dict(),
        display_message=(f"ORFS run comparison context "
                         f"({roles.get('baseline')} vs {roles.get('candidate')})"),
        index_tags=dict(tags),
    )]
    if insight is not None:
        records.append(RecordRequest(
            namespace=NAMESPACE, record_type=INSIGHT_RECORD_TYPE, key=insight["insight_id"],
            payload=dict(insight),
            display_message=(f"ORFS run comparison insight ({insight['support_status']}) "
                             f"for {roles.get('candidate')} vs {roles.get('baseline')}"),
            index_tags={**tags, "context_id": snapshot.context_id,
                        "support_status": insight["support_status"]},
        ))
    return records


def _formal_proof_fact(ctx: PackageContext) -> dict[str, Any] | None:
    """本次比較的 formal proof 事實。只採信 host binder 注入的那一份。

    `action_args` 由呼叫端提供，永遠不接受：否則任何呼叫端都能自稱 proof
    已成立而取得 candidate eligibility。
    """
    facts = getattr(ctx, "facts", None) or {}
    proof = facts.get("formal_proof")
    return proof if isinstance(proof, dict) else None


def _selection_eligibility(decision: Any, proof: dict[str, Any] | None = None
                           ) -> dict[str, Any]:
    from terminux_engine.packages.orfs_run_intelligence.comparison import (
        evaluate_selection_eligibility,
    )

    return evaluate_selection_eligibility(decision, proof=proof).to_json_dict()


def _emit(
    ctx: PackageContext, built: cc.ComparisonContext,
    prompt_run: comparison_prompt.ComparisonPromptRun,
) -> dict[str, Any]:
    snapshot = built.snapshot
    parsed = prompt_run.parsed
    support = _support_status(built)
    identity = canonical_json({
        "context_id": snapshot.context_id,
        "prompt_version": comparison_prompt.PROMPT_VERSION,
        "question_sha256": snapshot.normalized_question_hash,
    })
    insight = {
        "schema_version": SCHEMA_VERSION,
        "record_type": INSIGHT_RECORD_TYPE,
        "insight_id": f"ins_{sha256_text(identity)[:32]}",
        "package": {"id": PACKAGE_ID, "version": PACKAGE_VERSION},
        "purpose": snapshot.purpose,
        "prompt": {
            "id": comparison_prompt.PROMPT_ID,
            "version": comparison_prompt.PROMPT_VERSION,
            "prompt_sha256": prompt_run.prompt_sha256,
            "template_sha256": comparison_prompt.prompt_template_sha256(),
        },
        "model": _model_metadata(ctx),
        "question": snapshot.question,
        "context_id": snapshot.context_id,
        "subjects": [s.to_json_dict() for s in snapshot.subjects],
        "comparison_decision": snapshot.domain_payload["comparison_decision"],
        "metric_comparisons": snapshot.domain_payload["metric_comparisons"],
        "deterministic_facts": [f.to_json_dict() for f in snapshot.deterministic_facts],
        "execution_outcomes": _execution_outcomes(built),
        "comparability_statement": parsed["comparability_statement"],
        "interpretations": parsed["interpretations"],
        "hypotheses": parsed["hypotheses"],
        "unknowns": parsed["unknowns"],
        "recommended_checks": parsed["recommended_checks"],
        "missing_context": [m.to_json_dict() for m in snapshot.missing_context],
        "support_status": support,
        "evidence_refs": [r.to_json_dict() for r in snapshot.evidence_refs()],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_llm_response_sha256": prompt_run.raw_response_sha256,
    }
    records = _records(built, insight)
    return {
        "status": "ok",
        "context_id": snapshot.context_id,
        "insight_id": insight["insight_id"],
        "comparison_status": built.decision.status,
        "execution_outcomes": _execution_outcomes(built),
        "support_status": support,
        # 由 comparability **派生**的可否自動選擇判定。
        # 它不修改也不隱藏 comparison_decision 的任何欄位。
        "selection_eligibility": _selection_eligibility(
            built.decision, _formal_proof_fact(ctx)),
        "insight": {
            "comparability_statement": parsed["comparability_statement"],
            "comparison_decision": snapshot.domain_payload["comparison_decision"],
            "metric_comparisons": snapshot.domain_payload["metric_comparisons"],
            "metric_comparison_summary": snapshot.domain_payload["metric_comparison_summary"],
            "interpretations": parsed["interpretations"],
            "hypotheses": parsed["hypotheses"],
            "unknowns": parsed["unknowns"],
            "recommended_checks": parsed["recommended_checks"],
            "evidence_refs": [r.to_json_dict() for r in snapshot.evidence_refs()],
        },
        "record_status": _commit(ctx, records),
        "record_refs": [r.key for r in records],
        "limitations": _execution_limitations(built) + [
            "A declared controlled change is correlation evidence only; one paired run "
            "does not establish causation.",
            "Only the metrics recorded by the DQE package are compared.",
        ],
    }


def _emit_zero_provider(ctx: PackageContext, built: cc.ComparisonContext,
                        reason: str) -> dict[str, Any]:
    """在已授權 zero-provider 下如實記錄 deterministic comparison 結果，不呼叫 LLM。"""
    snapshot = built.snapshot
    support = _support_status(built)
    records = _records(built, None)
    return {
        "status": "ok",
        "context_id": snapshot.context_id,
        "insight_id": None,
        "comparison_status": built.decision.status,
        "execution_outcomes": _execution_outcomes(built),
        "support_status": support,
        "narrative_status": "UNAVAILABLE",
        "narrative_reason": reason,
        "selection_eligibility": _selection_eligibility(
            built.decision, _formal_proof_fact(ctx)),
        "insight": {
            "comparability_statement": None,
            "comparison_decision": snapshot.domain_payload["comparison_decision"],
            "metric_comparisons": snapshot.domain_payload["metric_comparisons"],
            "metric_comparison_summary": snapshot.domain_payload["metric_comparison_summary"],
            "interpretations": [],
            "hypotheses": [],
            "unknowns": [
                m.to_json_dict()["reason"] if hasattr(m, "to_json_dict") else str(m)
                for m in snapshot.missing_context
            ],
            "recommended_checks": [],
            "evidence_refs": [r.to_json_dict() for r in snapshot.evidence_refs()],
        },
        "record_status": _commit(ctx, records),
        "record_refs": [r.key for r in records],
        "limitations": _execution_limitations(built) + [
            "No LLM narrative was generated: running under authorized zero-provider mode.",
            "Comparison metrics and comparability decisions were verified directly from artifacts without model interpretation.",
        ],
    }


def _emit_without_llm(ctx: PackageContext, built: cc.ComparisonContext) -> dict[str, Any]:
    snapshot = built.snapshot
    return {
        "status": "ok",
        "context_id": snapshot.context_id,
        "insight_id": None,
        "comparison_status": built.decision.status,
        "execution_outcomes": _execution_outcomes(built),
        "support_status": SUPPORT_INSUFFICIENT,
        # 由 comparability **派生**的可否自動選擇判定。
        # 它不修改也不隱藏 comparison_decision 的任何欄位。
        "selection_eligibility": _selection_eligibility(
            built.decision, _formal_proof_fact(ctx)),
        "insight": {
            "comparability_statement": None,
            "comparison_decision": snapshot.domain_payload["comparison_decision"],
            "metric_comparisons": snapshot.domain_payload["metric_comparisons"],
            "metric_comparison_summary": snapshot.domain_payload["metric_comparison_summary"],
            "interpretations": [], "hypotheses": [],
            "unknowns": [m.to_json_dict()["reason"] for m in snapshot.missing_context],
            "recommended_checks": [], "evidence_refs": [],
        },
        "record_status": _commit(ctx, _records(built, None)),
        "record_refs": [snapshot.context_id],
        "limitations": _execution_limitations(built) + [
            "No verified artifact excerpt was usable, so no interpretation was produced.",
        ],
    }


def _execution_outcomes(built: cc.ComparisonContext) -> dict[str, Any]:
    """兩側的 Execution Outcome，與 comparability／metric 判定分開回報。"""
    return dict(built.snapshot.domain_payload["execution_outcomes"])


def _execution_limitations(built: cc.ComparisonContext) -> list[str]:
    lines = ["Read-only comparison of two recorded runs; nothing was executed or modified."]
    unverified = sorted(role for role, outcome in _execution_outcomes(built).items()
                        if not outcome["execution_verified"])
    if unverified:
        lines.append(
            f"The execution outcome of the {' and '.join(unverified)} run is not "
            "SUCCEEDED: this comparison must not be used as a verified basis for "
            "automated execution decisions.")
    return lines


def _failure(status: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "support_status": SUPPORT_INSUFFICIENT if status != "ok" else status,
        "message": message,
        "insight": None,
        "limitations": ["No engineering judgment was produced for this request."],
    }
    payload.update(extra)
    return payload


__all__ = ["COMPARISON_CONTEXT_TYPE", "DEFAULT_QUESTION", "compare_runs"]
