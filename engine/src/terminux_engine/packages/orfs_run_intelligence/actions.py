# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`orfs_run_intelligence.explain_run` — explain_run 的唯一 action。

Read-only intelligence：解析 target run → 讀已記錄的 DQE → 驗證 manifest 與
artifact hash → 組 bounded context → **一次** LLM 解讀 → 驗證輸出 →
把 Context Snapshot 與 `report_insight` 寫進既有 EventStore。

**絕不**執行 ORFS／OpenROAD／shell，**絕不**修改任何 design artifact
。這個 action 仍註冊為 `DANGEROUS`，只能經由已核准的
proposal 執行。
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.result import RecordRequest
from terminux_engine.packages.orfs_run_intelligence import (
    context_builder,
    dqe_reader,
    evidence as ev,
    prompt_runner,
)
from terminux_engine.packages.orfs_run_intelligence.schemas import (
    CONTEXT_TYPE,
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

MAX_QUESTION_CHARS = 2000

#: 明顯不是工程問題的輸入（path／command／SQL）一律拒絕。
_REJECTED_QUESTION_MARKERS = (
    "$(", "`", "&&", "||", ";--", "\x00",
    "rm -rf", "sudo ", "run_command", "drop table", "; rm ", "| sh", "curl ",
)


class InputError(ValueError):
    """Action arguments 不合法。"""


def explain_run(ctx: PackageContext, query: str | None = None) -> dict[str, Any]:
    """Action entry point。永不拋出到 Agent——失敗一律回結構化狀態。"""
    try:
        question, run_id = _validate_args(ctx)
    except InputError as exc:
        return _failure("invalid_arguments", str(exc))

    resolver = _execution_evidence_resolver(ctx)
    try:
        run = _resolve_target_run(ctx, run_id, resolver)
    except dqe_reader.RunSelectionError as exc:
        return _failure(SUPPORT_INSUFFICIENT, f"no analysable run: {exc}")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("orfs_run_intelligence run resolution failed: %r", exc)
        return _failure("failed", f"run resolution failed: {exc!r}")

    if not run.manifest_reference:
        return _failure(
            SUPPORT_INSUFFICIENT,
            f"run {run.run_id} has DQE rows but no artifact manifest reference",
            run_id=run.run_id,
        )

    try:
        verified = ev.load_verified_manifest(
            run.manifest_reference,
            expected_run_id=run.run_id,
            text_roles=context_builder.TEXT_ROLES,
        )
    except ev.ManifestVerificationError as exc:
        return _failure(
            SUPPORT_INSUFFICIENT,
            f"artifact manifest could not be verified: {exc}",
            run_id=run.run_id,
        )

    context = context_builder.build_context(
        question=question, run=run, verified=verified, requested_run_id=run_id,
        execution_evidence=resolve_execution_evidence(resolver, run),
    )

    if not verified.artifacts:
        # 沒有任何可信 artifact 內容 → 不呼叫 LLM，誠實回報。
        return _insufficient_without_llm(ctx, context)

    zero_provider_reason = _zero_provider_reason(ctx)
    if zero_provider_reason is not None:
        # 已授權的零 LLM 預算：不呼叫 LLM，明確將 narrative 標為 UNAVAILABLE。
        return _emit_zero_provider(ctx, context, zero_provider_reason)

    if getattr(getattr(ctx, "services", None), "llm", None) is None:
        # 沒有 provider 是既有的 unavailable 失敗路徑，**不是**零 provider 授權。
        # 這裡明確轉成誠實失敗，不讓 AttributeError 逸出到 Agent。
        return _failure(
            "failed", "interpretation unavailable: no LLM provider is configured",
        )

    try:
        prompt_run = prompt_runner.run_prompt(ctx.services.llm, context)
    except (prompt_runner.LlmUnavailableError, LlmOutputError) as exc:
        # 只有「模型引用了不存在的 evidence ref」這一種拒絕會被
        # 分類；分類本身由**受信任的 validator 物件**產生，不看訊息內容，
        # 也不從模型 JSON 複製。能不能續跑由 host policy 決定，不在這裡。
        from terminux_engine.packages.orfs_run_intelligence import (
            analysis_failure as AF,
        )

        classified = AF.classify(
            exc, capability_id="orfs_run_intelligence.explain_run",
            invocation={"context_id": context.context_id,
                        "run_id": run.run_id,
                        "prompt_template_sha256":
                            prompt_runner.prompt_template_sha256()})
        return _failure(
            "failed",
            f"interpretation unavailable: {exc}",
            run_id=run.run_id,
            context_id=context.context_id,
            **({"analysis_failure": classified} if classified else {}),
        )

    return _emit(ctx, context, prompt_run)


# ── Execution Outcome────────────────────────────────────────


def _execution_evidence_resolver(ctx: PackageContext) -> Any:
    """host 注入的唯讀 Evidence Resolver；沒有就是 `None`（結果一律 EXECUTION_UNVERIFIED）。"""
    return getattr(getattr(ctx, "services", None), "execution_evidence_resolver", None)


def _resolve_target_run(ctx: PackageContext, run_id: str | None,
                        resolver: Any) -> dqe_reader.DqeRun:
    """解析 target run。latest 模式以目前使用者自己的執行證據為準。

    只有 DQE rows、沒有任何執行證據的 run 不會被當成已驗證的最新 run；找不到
    任何執行證據時才退回 DQE 的最新一筆，並明確標示，結果只能是唯讀診斷。
    """
    if run_id is not None or resolver is None:
        return dqe_reader.resolve_run(ctx, run_id)
    latest = None
    try:
        latest = resolver.latest_run_id(
            dqe_db_path=dqe_reader.dqe_db_path_for(ctx, None))
    except Exception as exc:  # noqa: BLE001 - 讀不到就退回 DQE latest，並如實標示
        _LOG.warning("execution evidence latest-run lookup failed: %r", exc)
    if latest is not None:
        return dataclasses.replace(dqe_reader.resolve_run(ctx, latest),
                                   resolved_by=dqe_reader.RESOLVED_LATEST_EXECUTION)
    return dataclasses.replace(dqe_reader.resolve_run(ctx, None),
                               resolved_by=dqe_reader.RESOLVED_LATEST_DQE_ONLY)


def resolve_execution_evidence(resolver: Any, run: dqe_reader.DqeRun) -> Any:
    """以 host Resolver 判定這次執行；Resolver 不在或失敗時回 `None`（不猜）。"""
    if resolver is None:
        return None
    try:
        return resolver.resolve(run.run_id, dqe_db_path=run.db_path)
    except Exception as exc:  # noqa: BLE001 - 判定失敗只會讓結果維持 unverified
        _LOG.warning("execution evidence resolution failed for %s: %r", run.run_id, exc)
        return None


def _receipt_for(ctx: PackageContext, role: str) -> dict[str, Any] | None:
    """host 放在 `facts["execution_receipts"]` 的指定角色 receipt；沒有就是 `None`。

    receipt 目前不是 package 可 durable 查詢的來源。拿不到時 comparability policy 會如實
    列成 unknowns，絕不默認兩側相同。
    """
    receipts = ctx.facts.get("execution_receipts")
    if isinstance(receipts, dict):
        value = receipts.get(role)
        if isinstance(value, dict):
            return value
    return None


def _execution_fields(context: context_builder.ContextSnapshot) -> dict[str, Any]:
    """Execution Outcome 與 Engineering Result 分開回報。"""
    execution = context.execution
    return {
        "execution_status": execution["lifecycle_status"],
        "execution_outcome": {
            "terminal_outcome": execution["terminal_outcome"],
            "execution_verified": execution["execution_verified"],
            "lifecycle_source": execution["lifecycle_source"],
            "outcome_reasons": list(execution["outcome_reasons"]),
            "process_exit_code": execution["process_exit_code"],
            "timed_out": execution["timed_out"],
        },
        "engineering_result": {"source": "dqe",
                               "overall_status": context.dqe["overall_status"]},
        "dqe_overall_status": context.dqe["overall_status"],
    }


def _execution_limitations(context: context_builder.ContextSnapshot) -> list[str]:
    lines = ["Read-only analysis of one recorded run; nothing was executed or modified."]
    if not context.execution["execution_verified"]:
        lines.append(
            f"The execution outcome is {context.execution['terminal_outcome']}: this "
            "analysis must not be used as a verified basis for automated execution "
            "decisions.")
    return lines


# ── internals ──────────────────────────────────────────────────────────


def _validate_args(ctx: PackageContext) -> tuple[str, str | None]:
    args = ctx.facts.get("action_args") or {}
    if not isinstance(args, dict):
        raise InputError("action_args must be an object")

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        raise InputError("question is required and must be a non-empty string")
    if len(question) > MAX_QUESTION_CHARS:
        raise InputError(f"question exceeds {MAX_QUESTION_CHARS} characters")
    lowered = question.lower()
    for marker in _REJECTED_QUESTION_MARKERS:
        if marker in lowered:
            raise InputError(f"question contains a rejected marker: {marker!r}")

    run_id = args.get("run_id")
    if run_id is not None:
        if not isinstance(run_id, str):
            raise InputError("run_id must be a string")
        try:
            dqe_reader.validate_run_id(run_id)
        except dqe_reader.RunSelectionError as exc:
            # Action 對 Agent 永遠不拋例外；格式錯誤是 input error。
            raise InputError(str(exc)) from exc
    return question.strip(), run_id


def _support_status(context: context_builder.ContextSnapshot, parsed: dict) -> str:
    """由程式決定 support status，不交給 LLM。"""
    if not context.evidence_refs or not context.source_verification["artifact_hashes_match"]:
        return SUPPORT_INSUFFICIENT
    if context.unknowns or parsed["hypotheses"]:
        return SUPPORT_PARTIAL
    return SUPPORT_SUPPORTED


def _emit(
    ctx: PackageContext,
    context: context_builder.ContextSnapshot,
    prompt_run: prompt_runner.PromptRun,
) -> dict[str, Any]:
    parsed = prompt_run.parsed
    support = _support_status(context, parsed)
    unknowns = list(context.unknowns) + [u["statement"] for u in parsed["unknowns"]]

    model = _model_metadata(ctx)
    insight_identity = canonical_json({
        "context_id": context.context_id,
        "prompt_version": prompt_runner.PROMPT_VERSION,
        "question_sha256": sha256_text(context.question.strip().lower()),
    })
    insight_id = f"ins_{sha256_text(insight_identity)[:32]}"

    insight = {
        "schema_version": SCHEMA_VERSION,
        "record_type": INSIGHT_RECORD_TYPE,
        "insight_id": insight_id,
        "package": {"id": PACKAGE_ID, "version": PACKAGE_VERSION},
        "prompt": {
            "id": prompt_runner.PROMPT_ID,
            "version": prompt_runner.PROMPT_VERSION,
            "prompt_sha256": prompt_run.prompt_sha256,
            "template_sha256": prompt_runner.prompt_template_sha256(),
        },
        "model": model,
        "question": context.question,
        "context_id": context.context_id,
        "producer": dict(context.producer),
        "artifact_manifest": dict(context.artifact_manifest),
        **_execution_fields(context),
        "facts": [f.to_json_dict() for f in context.facts],
        "interpretations": parsed["interpretations"],
        "hypotheses": parsed["hypotheses"],
        "unknowns": unknowns,
        "recommended_checks": parsed["recommended_checks"],
        "support_status": support,
        "evidence_refs": [r.to_json_dict() for r in context.evidence_refs],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_llm_response_sha256": prompt_run.raw_response_sha256,
    }

    records = [
        RecordRequest(
            namespace=NAMESPACE, record_type=CONTEXT_TYPE, key=context.context_id,
            payload=context.to_json_dict(),
            display_message=f"ORFS run intelligence context for {context.producer.get('run_id')}",
            index_tags={"run_id": str(context.producer.get("run_id")),
                        "manifest_id": context.artifact_manifest["manifest_id"]},
        ),
        RecordRequest(
            namespace=NAMESPACE, record_type=INSIGHT_RECORD_TYPE, key=insight_id,
            payload=dict(insight),
            display_message=f"ORFS run insight ({support}) for {context.producer.get('run_id')}",
            index_tags={"run_id": str(context.producer.get("run_id")),
                        "context_id": context.context_id, "support_status": support},
        ),
    ]
    record_status = _commit(ctx, records)

    return {
        "status": "ok",
        "run_id": context.producer.get("run_id"),
        "context_id": context.context_id,
        "insight_id": insight_id,
        "support_status": support,
        "execution_verified": context.execution["execution_verified"],
        "insight": {
            **_execution_fields(context),
            "facts": [f.to_json_dict() for f in context.facts],
            "interpretations": parsed["interpretations"],
            "hypotheses": parsed["hypotheses"],
            "unknowns": unknowns,
            "recommended_checks": parsed["recommended_checks"],
            "evidence_refs": [r.to_json_dict() for r in context.evidence_refs],
        },
        "record_status": record_status,
        "record_refs": [r.key for r in records],
        "limitations": _execution_limitations(context) + [
            "No historical baseline was available, so no regression claim can be made.",
            "Root cause is not established by this run's artifacts alone.",
        ],
    }


def _insufficient_without_llm(
    ctx: PackageContext, context: context_builder.ContextSnapshot
) -> dict[str, Any]:
    records = [
        RecordRequest(
            namespace=NAMESPACE, record_type=CONTEXT_TYPE, key=context.context_id,
            payload=context.to_json_dict(),
            display_message="ORFS run intelligence context (insufficient evidence)",
        )
    ]
    record_status = _commit(ctx, records)
    return {
        "status": "ok",
        "run_id": context.producer.get("run_id"),
        "context_id": context.context_id,
        "insight_id": None,
        "support_status": SUPPORT_INSUFFICIENT,
        "execution_verified": context.execution["execution_verified"],
        "insight": {
            **_execution_fields(context),
            "facts": [f.to_json_dict() for f in context.facts],
            "interpretations": [],
            "hypotheses": [],
            "unknowns": list(context.unknowns),
            "recommended_checks": [],
            "evidence_refs": [],
        },
        "record_status": record_status,
        "record_refs": [r.key for r in records],
        "limitations": _execution_limitations(context) + [
            "No verified artifact content was usable, so no interpretation was produced.",
        ],
    }


#: 沒有其他佐證時，零 LLM 預算只能記為「預算關掉了 narrative」。它**不是**
#: manual proposal 授權；後者由 host 依 submit／approve／execution authorization
#: 判定後，以 `llm_suppression_reason` 明確注入。
ZERO_LLM_BUDGET_REASON = "zero_llm_budget_authorized"


def _zero_provider_reason(ctx: PackageContext) -> str | None:
    """本次 action 是否被**已授權的預算**關閉 LLM；是則回傳其 durable 理由。

    只接受 host binder 注入的 fact。`action_args` 由呼叫端提供，永遠不採信：
    否則未核准或 AI 產生的 proposal 只要自行宣告 `allow_llm: false`，就能取得
    原本不允許的「零 provider 仍算 valid」執行能力。

    缺少 LLM service 也**不是**授權。那是既有的 LlmUnavailable 失敗路徑，必須
    照原本的方式回報失敗，不得改走這條「已授權」分流。
    """
    facts = getattr(ctx, "facts", None) or {}
    if facts.get("allow_llm") is False or facts.get("body_llm_dispatches") == 0:
        reason = facts.get("llm_suppression_reason")
        if isinstance(reason, str) and reason:
            return reason
        return ZERO_LLM_BUDGET_REASON
    return None


def _emit_zero_provider(
    ctx: PackageContext, context: context_builder.ContextSnapshot, reason: str,
) -> dict[str, Any]:
    """在已授權 zero-provider 下如實記錄 DQE 事實與工程證據，不呼叫 LLM。

    support status 仍由 `_support_status()` 依證據計算。少了 narrative 可以
    繼續；少了 DQE／工程判定不行——artifact hash 不符或仍有 unknowns 時，
    這裡必須照樣降級，不得因為「本來就不打算呼叫 LLM」而一律報 supported。
    """
    records = [
        RecordRequest(
            namespace=NAMESPACE, record_type=CONTEXT_TYPE, key=context.context_id,
            payload=context.to_json_dict(),
            display_message="ORFS run intelligence context (manual zero-provider)",
            index_tags={"run_id": str(context.producer.get("run_id")),
                        "manifest_id": context.artifact_manifest["manifest_id"]},
        )
    ]
    record_status = _commit(ctx, records)
    # 沒有呼叫 LLM，因此不存在 model hypotheses；其餘證據條件照常適用。
    support = _support_status(context, {"hypotheses": []})
    return {
        "status": "ok",
        "run_id": context.producer.get("run_id"),
        "context_id": context.context_id,
        "insight_id": None,
        "support_status": support,
        "narrative_status": "UNAVAILABLE",
        "narrative_reason": reason,
        "execution_verified": context.execution["execution_verified"],
        "insight": {
            **_execution_fields(context),
            "facts": [f.to_json_dict() for f in context.facts],
            "interpretations": [],
            "hypotheses": [],
            "unknowns": list(context.unknowns),
            "recommended_checks": [],
            "evidence_refs": [r.to_json_dict() for r in context.evidence_refs],
        },
        "record_status": record_status,
        "record_refs": [r.key for r in records],
        "limitations": _execution_limitations(context) + [
            "No LLM narrative was generated: running under authorized zero-provider mode.",
            "Engineering facts and DQE metrics were verified directly from artifacts without model interpretation.",
        ],
    }


def _commit(ctx: PackageContext, records: list[RecordRequest]) -> dict[str, Any]:
    store = getattr(ctx.services, "store", None) if ctx.services is not None else None
    if store is None:
        return {"status": "unavailable", "committed": 0}
    committed = 0
    errors: list[str] = []
    for record in records:
        try:
            store.commit(record)
            committed += 1
        except Exception as exc:  # noqa: BLE001 - 誠實回報，不假裝成功
            errors.append(repr(exc))
    return {
        "status": "written" if committed == len(records) else "partial" if committed else "failed",
        "committed": committed,
        "attempted": len(records),
        "durable": store.event_store is not None,
        "errors": errors,
    }


def _model_metadata(ctx: PackageContext) -> dict[str, Any]:
    """盡力取得 model identity；取不到就是 null，不猜測。"""
    llm = getattr(ctx.services, "llm", None) if ctx.services is not None else None
    router = getattr(llm, "_router", None)
    config = getattr(router, "config", None) or getattr(router, "_config", None)
    provider_cfg = getattr(config, "background", None) if config is not None else None
    return {
        "route": "background",
        "provider": getattr(provider_cfg, "backend", None),
        "model_id": getattr(provider_cfg, "model_name", None),
    }


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


__all__ = ["explain_run", "resolve_execution_evidence"]
