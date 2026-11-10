# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""situation assessment situation assessment 的 **Package-owned use case**（proactive intelligence 抽取）。

`assess_situation` action 與 proactive intelligence proactive provider **共用這一份實作**——
proactive path 直接呼叫這裡，不經過 `ActionRegistry.call()`、不偽造已核准的
action context。抽取時 action 的名稱、arguments、risk 與輸出
格式完全不變。

流程：解析兩個明確指定的 run → 讀已記錄的 DQE → 驗證兩份 manifest 與
artifact hash → 由 ORFS policy 判 comparability 與 readiness → 組 Common
`SituationContext` → **一次** LLM 解讀 → deterministic support validation →
把 validated Situation Snapshot 以 append-only record 寫進既有 EventStore。

**絕不**執行 EDA／shell、**絕不**修改設計 artifact、**絕不**改寫 DQE 結果，
輸出也**不是**正式 signoff 決定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.result import RecordRequest, TimelineEvent
from terminux_engine.package_runtime.services import RuntimeServices
from terminux_engine.situation.snapshot import (
    UNOBSERVED_LIFECYCLE,
    SITUATION_SNAPSHOT_SCHEMA_VERSION,
    recompute_snapshot_sha256,
    SNAPSHOT_RECORD_TYPE,
    SUPPORT_INSUFFICIENT,
    Claim,
    Hypothesis,
    Readiness,
    RecommendedCheck,
    SituationSnapshot,
    compute_snapshot_id,
    compute_snapshot_sha256,
)
from terminux_engine.situation.conflict import (
    CONCURRENCY_SEMANTICS,
    guarded_append,
)
from terminux_engine.situation.support import validate_support
from terminux_engine.packages.orfs_run_intelligence import (
    comparison_facts as cf,
    dqe_reader,
    evidence as ev,
    situation_context as sc,
    situation_policy as policy,
    situation_prompt,
)
from terminux_engine.packages.orfs_run_intelligence.actions import (
    MAX_QUESTION_CHARS,
    _REJECTED_QUESTION_MARKERS,
    InputError,
    _commit,
    _receipt_for,
    _model_metadata,
)
from terminux_engine.packages.orfs_run_intelligence.schemas import NAMESPACE

_LOG = logging.getLogger(__name__)

PACKAGE_ID = "orfs_run_intelligence"
PACKAGE_VERSION = "0.1.0"

DEFAULT_QUESTION = (
    "What improved, what is still blocking, can this be treated as ready, which "
    "explanations are only hypotheses, what evidence is missing, and what is the "
    "most valuable next check?"
)


@dataclass(frozen=True)
class AssessmentRequest:
    """呼叫這個 use case 需要的全部輸入。

    刻意不含 `PackageContext`——proactive path 沒有、也不該偽造一個已核准的
    action context。"""

    current_run_id: str
    baseline_run_id: str
    question: str | None = None
    controlled_changes: tuple[str, ...] = ()
    execution_receipts: dict[str, Any] = field(default_factory=dict)
    dqe_db_path: str | None = None
    #: analysis input 是否備妥、lifecycle 三軸是否被觀察到。
    #: 由呼叫端（proactive provider）提供；action path 留空表示未宣告。
    analysis_input: dict[str, Any] = field(default_factory=dict)


def assess(request: AssessmentRequest, *, llm: Any, store: Any) -> dict[str, Any]:
    """Read-only situation assessment 的共用 use case。

    `assess_situation` action 與 proactive intelligence proactive provider 都走這裡；
    proactive path 因此不需要 `ActionRegistry.call()`。
    """
    return _assess_with_context(_use_case_context(request, llm=llm, store=store))


def _use_case_context(request: AssessmentRequest, *, llm: Any,
                      store: Any) -> PackageContext:
    """把 use-case 輸入包成既有 domain code 讀得懂的資料容器。

    這不是偽造 action 執行：`source` 明確標成 `proactive.assessment`，
    services 只有 llm 與 store，沒有任何 executor。
    """
    facts: dict[str, Any] = {
        "action_args": {
            "current_run_id": request.current_run_id,
            "baseline_run_id": request.baseline_run_id,
            "question": request.question,
            "controlled_changes": list(request.controlled_changes),
        },
    }
    if request.execution_receipts:
        facts["execution_receipts"] = dict(request.execution_receipts)
    if request.analysis_input:
        facts["analysis_input"] = dict(request.analysis_input)
    if request.dqe_db_path:
        facts["dqe_db_path"] = request.dqe_db_path
    return PackageContext(
        package_id=PACKAGE_ID, package_version=PACKAGE_VERSION, phase="action",
        event=TimelineEvent(
            event_id=f"proactive_{request.current_run_id}", trace_id=None,
            command="", argv=[], cwd="/", tool_name=None, user="", host="",
            terminal_id=None, source="proactive.assessment", timestamp="0",
            metadata={}),
        facts=facts, services=RuntimeServices(llm=llm, store=store))


def assess_situation(ctx: PackageContext, query: str | None = None) -> dict[str, Any]:
    """Action entry point。永不拋出到 Agent——失敗一律回結構化狀態。

    行為與抽取前完全相同；它只是把既有 `PackageContext` 交給共用 use case。
    """
    return _assess_with_context(ctx)


def _assess_with_context(ctx: PackageContext) -> dict[str, Any]:
    try:
        question, current_id, baseline_id = _validate_args(ctx)
    except InputError as exc:
        return _failure("invalid_arguments", str(exc))

    try:
        current_run = dqe_reader.resolve_run(ctx, current_id)
        baseline_run = dqe_reader.resolve_run(ctx, baseline_id)
    except dqe_reader.RunSelectionError as exc:
        return _failure(SUPPORT_INSUFFICIENT, f"run pair could not be resolved: {exc}")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("assess_situation run resolution failed: %r", exc)
        return _failure("failed", f"run pair resolution failed: {exc!r}")

    for label, run in (("current", current_run), ("baseline", baseline_run)):
        if not run.manifest_reference:
            return _failure(SUPPORT_INSUFFICIENT,
                            f"the {label} run {run.run_id} has DQE rows but no artifact "
                            f"manifest reference")

    try:
        current_verified = ev.load_verified_manifest(
            current_run.manifest_reference, expected_run_id=current_run.run_id,
            text_roles=sc.TEXT_ROLES)
        baseline_verified = ev.load_verified_manifest(
            baseline_run.manifest_reference, expected_run_id=baseline_run.run_id,
            text_roles=sc.TEXT_ROLES)
    except ev.ManifestVerificationError as exc:
        return _failure(SUPPORT_INSUFFICIENT,
                        f"an artifact manifest could not be verified: {exc}")

    assembly = sc.build_situation_context(
        question=question, current_run=current_run, baseline_run=baseline_run,
        current_verified=current_verified, baseline_verified=baseline_verified,
        current_receipt=_receipt_for(ctx, "current"),
        baseline_receipt=_receipt_for(ctx, "baseline"),
        declared_controlled_changes=_declared_controlled_changes(ctx),
        created_at=_now_iso())
    context = assembly.context

    if not context.evidence_refs:
        return _failure(SUPPORT_INSUFFICIENT,
                        "no verified evidence was usable for this run pair",
                        context_id=context.context_id)

    try:
        prompt_run = situation_prompt.run_prompt(ctx.services.llm, context)
    except situation_prompt.SituationToolCallForbiddenError as exc:
        # Contract violation，不是格式失誤：專屬錯誤碼、不 repair、
        # 不執行／轉交／模擬那個 call（closeout Workstream B）。
        return _failure("failed", str(exc), context_id=context.context_id,
                        error_code=situation_prompt.SituationToolCallForbiddenError.code,
                        forbidden_tool_call=dict(exc.summary))
    except situation_prompt.SituationBudgetDeniedError as exc:
        # 預算在 provider call **之前**擋下：沒有任何 provider call 發生，
        # 也不得產生 snapshot 或通知。誠實回報，不降級成「模型失敗」。
        return _failure("failed", f"provider budget denied this assessment: {exc.reason}",
                        context_id=context.context_id,
                        error_code=situation_prompt.SituationBudgetDeniedError.code,
                        budget_denial_reason=exc.reason)
    except (situation_prompt.LlmUnavailableError,
            situation_prompt.SituationOutputError) as exc:
        return _failure("failed", f"interpretation unavailable: {exc}",
                        context_id=context.context_id)

    snapshot = _build_snapshot(ctx, assembly, prompt_run)
    review = validate_support(snapshot, context,
                              deterministic_readiness=assembly.readiness.status)
    if not review.valid:
        # Invalid snapshot **不得**被部分保存，也不觸發任何其他查詢或 action。
        return _failure("failed", "the situation snapshot failed evidence support "
                                  "validation and was not recorded",
                        context_id=context.context_id,
                        validation=review.to_json_dict())

    record = RecordRequest(
        namespace=NAMESPACE, record_type=SNAPSHOT_RECORD_TYPE,
        key=f"situation:{snapshot.snapshot_id}", payload=snapshot.to_json_dict(),
        display_message=(f"Situation snapshot ({snapshot.readiness.status}) for "
                         f"{current_run.run_id} vs {baseline_run.run_id}"),
        index_tags={"current_run_id": current_run.run_id,
                    "baseline_run_id": baseline_run.run_id,
                    "readiness": snapshot.readiness.status,
                    "context_id": context.context_id})

    # ── immutable identity conflict handling（closeout Workstream A）──
    persistence = _persist_snapshot(ctx, snapshot, record)
    if persistence["outcome"] not in ("append", "idempotent"):
        return _failure("failed", persistence["message"] or "snapshot was not persisted",
                        context_id=context.context_id,
                        snapshot_id=snapshot.snapshot_id,
                        error_code=persistence["error_code"],
                        snapshot_persistence=persistence)

    return {
        "status": "ok",
        "context_id": context.context_id,
        "snapshot_id": snapshot.snapshot_id,
        "comparability": assembly.effective_comparability,
        "readiness": snapshot.readiness.to_json_dict(),
        "global_support_status": snapshot.global_support_status,
        "snapshot": snapshot.to_json_dict(),
        "support_review": review.to_json_dict(),
        "snapshot_persistence": persistence,
        "record_status": persistence["record_status"],
        "record_refs": [record.key] if persistence["outcome"] == "append" else [],
        "missing_evidence": [dict(e) for e in assembly.missing_evidence],
        "missing_evidence_count": len(assembly.missing_evidence),
        "execution": dict(snapshot.execution),
        "analysis_input": dict(snapshot.analysis_input),
        "limitations": list(snapshot.limitations),
        # initial／repair 逐次的 requested/actual/finish reason；
        # cycle total 可由此重算，不必回頭猜。
        "generation_accounting": prompt_run.accounting(),
    }


def _persist_snapshot(
    ctx: PackageContext, snapshot: SituationSnapshot, record: RecordRequest
) -> dict[str, Any]:
    """Query → conflict decide → append，全部在 per-id lock 內。

    保證上限是 `same-process conflict-checked append`：既有 EventStore 對
    record 的邏輯 key 沒有 uniqueness，也沒有 query+insert 的單一 transaction，
    因此**跨 process 的 concurrent writer 仍可能寫出同 id 不同內容**。
    這一點如實記在回傳值裡，不宣稱 globally unique 或 exactly-once。
    """
    content_hash = compute_snapshot_sha256(snapshot)
    store = getattr(ctx.services, "store", None) if ctx.services is not None else None
    committed: dict[str, Any] = {"status": "not_attempted"}

    def read_existing() -> list[dict[str, Any]]:
        if store is None:
            return []
        try:
            events = store.get_latest(n=50, event_type=SNAPSHOT_RECORD_TYPE)
        except Exception as exc:  # noqa: BLE001 - 讀不到就當作無法確認
            _LOG.warning("situation snapshot lookup failed: %r", exc)
            raise
        out = []
        for event in events:
            payload = (event or {}).get("payload") or {}
            if payload.get("snapshot_id") == snapshot.snapshot_id:
                out.append(payload)
        return out

    def hash_of(payload: dict[str, Any]) -> str | None:
        """從**內容**重算，不相信 payload 裡宣稱的 hash。

        另外比對存下來的宣稱值：兩者不一致代表既有紀錄本身已被改動，
        回 `None` 讓上層以 integrity error 拒絕，而不是默默採用其中一個。
        """
        recomputed = recompute_snapshot_sha256(payload)
        if recomputed is None:
            return None
        claimed = (payload.get("provenance") or {}).get("snapshot_sha256")
        if isinstance(claimed, str) and claimed and claimed != recomputed:
            return None
        return recomputed

    def write() -> bool:
        nonlocal committed
        committed = _commit(ctx, [record])
        return committed.get("status") == "written"

    try:
        decision = guarded_append(
            snapshot_id=snapshot.snapshot_id, content_hash=content_hash,
            read_existing=read_existing, hash_of=hash_of, write=write)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("situation snapshot persistence check failed: %r", exc)
        return {"outcome": "unreadable",
                "error_code": "SITUATION_SNAPSHOT_RECORD_UNREADABLE",
                "message": f"existing snapshot records could not be read: {exc!r}",
                "content_hash": content_hash, "write_count": 0,
                "concurrency_semantics": CONCURRENCY_SEMANTICS,
                "record_status": committed}

    payload = decision.to_json_dict()
    payload["content_hash"] = content_hash
    payload["write_count"] = 1 if decision.outcome == "append" else 0
    payload["record_status"] = committed
    return payload


# ── internals ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_args(ctx: PackageContext) -> tuple[str, str, str]:
    args = ctx.facts.get("action_args") or {}
    if not isinstance(args, dict):
        raise InputError("action_args must be an object")

    current = args.get("current_run_id")
    baseline = args.get("baseline_run_id")
    for label, value in (("current_run_id", current), ("baseline_run_id", baseline)):
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"{label} is required and must be a non-empty string")
        try:
            dqe_reader.validate_run_id(value)
        except dqe_reader.RunSelectionError as exc:
            raise InputError(f"{label}: {exc}") from exc
    if current == baseline:
        raise InputError("current_run_id and baseline_run_id must be different runs")

    question = args.get("question")
    if question is None:
        return DEFAULT_QUESTION, current, baseline
    if not isinstance(question, str) or not question.strip():
        raise InputError("question must be a non-empty string when provided")
    if len(question) > MAX_QUESTION_CHARS:
        raise InputError(f"question exceeds {MAX_QUESTION_CHARS} characters")
    lowered = question.lower()
    for marker in _REJECTED_QUESTION_MARKERS:
        if marker in lowered:
            raise InputError(f"question contains a rejected marker: {marker!r}")
    return question.strip(), current, baseline


def _declared_controlled_changes(ctx: PackageContext) -> tuple[str, ...]:
    declared = (ctx.facts.get("action_args") or {}).get("controlled_changes")
    if not isinstance(declared, (list, tuple)):
        return ()
    return tuple(str(d) for d in declared if isinstance(d, str))


def _analysis_input_block(ctx: PackageContext) -> dict[str, Any]:
    """組 snapshot 的 `analysis_input` 區塊。

    呼叫端沒宣告時，回「輸入狀態未宣告 ＋ lifecycle 未觀察」的保守預設——
    絕不因為分析成功就把 lifecycle 升級成 completed。
    """
    declared = ctx.facts.get("analysis_input")
    block: dict[str, Any] = {
        "analysis_input_status": "unverifiable",
        "analysis_input_basis": {},
        "analysis_input_provenance": [],
    }
    if isinstance(declared, dict):
        for key in ("analysis_input_status", "analysis_input_basis",
                    "analysis_input_provenance"):
            if key in declared:
                block[key] = declared[key]
    lifecycle = {k: (dict(v) if isinstance(v, dict) else v)
                 for k, v in UNOBSERVED_LIFECYCLE.items()}
    if isinstance(declared, dict):
        for key in lifecycle:
            value = declared.get(key)
            # 只有 receipt 才能升級 lifecycle；這裡照搬呼叫端宣告，而呼叫端
            # 本身受 `ProactiveObservation.__post_init__` 的列舉檢查約束。
            if value is not None:
                lifecycle[key] = dict(value) if isinstance(value, dict) else value
    block.update(lifecycle)
    return block


def _build_snapshot(
    ctx: PackageContext, assembly: sc.SituationAssembly,
    prompt_run: situation_prompt.SituationPromptRun,
) -> SituationSnapshot:
    context = assembly.context
    parsed = prompt_run.parsed

    facts = tuple(
        Claim(claim_id=f.fact_id, text=f.text, evidence_refs=f.evidence_ref_ids,
              fact_ids=(f.fact_id,))
        for f in context.deterministic_facts)
    improvements = tuple(
        c for c in facts if c.claim_id.startswith("fact_delta_")
        and any(m.metric_id in c.claim_id and m.comparison == cf.IMPROVED
                for m in assembly.comparisons))
    regressions = tuple(
        c for c in facts if c.claim_id.startswith("fact_delta_")
        and any(m.metric_id in c.claim_id and m.comparison == cf.DEGRADED
                for m in assembly.comparisons))
    blockers = tuple(c for c in facts if c.claim_id.startswith("fact_blocker_"))

    interpretations = tuple(
        Claim(claim_id=i.get("claim_id") or f"int_{n}", text=i["text"],
              evidence_refs=tuple(i.get("evidence_refs") or []),
              fact_ids=tuple(i.get("fact_ids") or []),
              support_status=i["support_status"])
        for n, i in enumerate(parsed["interpretations"], 1))
    hypotheses = tuple(
        Hypothesis(claim_id=h.get("claim_id") or f"hyp_{n}", text=h["text"],
                   supporting_evidence_refs=tuple(h.get("supporting_evidence_refs") or []),
                   missing_confirmation=tuple(h.get("missing_confirmation") or []),
                   fact_ids=tuple(h.get("fact_ids") or []))
        for n, h in enumerate(parsed["hypotheses"], 1))
    checks = tuple(
        RecommendedCheck(check_id=c.get("check_id") or f"chk_{n}",
                         description=c["description"], purpose=c.get("purpose", ""),
                         required_evidence=tuple(c.get("required_evidence") or []))
        for n, c in enumerate(parsed["recommended_checks"], 1))

    # Missing evidence 是結構化的：每個 subject 各一筆，不合併成一句話。
    missing_evidence = tuple(
        (f"{e['evidence_type']} for {e['subject_id']}: {e['status']} "
         f"({e['reason_code']}) — required_for_readiness="
         f"{str(e['required_for_readiness']).lower()}, "
         f"required_for_full_comparability="
         f"{str(e['required_for_full_comparability']).lower()}")
        for e in assembly.missing_evidence
    ) + tuple(
        f"{m.requested_source_id}: {m.reason}" for m in context.missing_sources)

    snapshot = SituationSnapshot(
        schema_version=SITUATION_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id="",  # 下面由 content 決定
        context_id=context.context_id,
        scope=dict(context.scope),
        generated_at=_now_iso(),
        producer={
            "package_id": PACKAGE_ID, "package_version": PACKAGE_VERSION,
            "prompt_version": situation_prompt.PROMPT_VERSION,
            "prompt_sha256": prompt_run.prompt_sha256,
            "prompt_template_sha256": situation_prompt.prompt_template_sha256(),
            "repaired": prompt_run.repaired,
        },
        subjects=tuple(s.to_json_dict() for s in context.subjects),
        readiness=Readiness(status=assembly.readiness.status,
                            basis=assembly.readiness.basis),
        facts=facts, improvements=improvements, regressions=regressions,
        blockers=blockers, interpretations=interpretations, hypotheses=hypotheses,
        unknowns=tuple(parsed["unknowns"]), missing_evidence=missing_evidence,
        recommended_checks=checks,
        global_support_status=parsed["global_support_status"],
        limitations=tuple(context.limitations),
        analysis_input=_analysis_input_block(ctx),
        provenance={
            "deterministic_policy_id": policy.POLICY_ID,
            "deterministic_policy_version": policy.POLICY_VERSION,
            "model": _model_metadata(ctx),
            "context_hash": context.context_id,
            "raw_llm_response_sha256": prompt_run.raw_response_sha256,
        },
    )
    from dataclasses import replace

    return replace(snapshot, snapshot_id=compute_snapshot_id(snapshot))


def _failure(status: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "global_support_status": SUPPORT_INSUFFICIENT,
        "message": message,
        "snapshot": None,
        "execution": {"action_calls": 0, "tool_calls": 0, "proposals_created": 0,
                      "execution_allowed": False},
        "limitations": ["No situation assessment was produced for this request."],
    }
    payload.update(extra)
    return payload


__all__ = ["DEFAULT_QUESTION", "AssessmentRequest", "assess", "assess_situation"]
