# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""approved package action propose／execute 的固定順序。

兩個入口，順序寫死：

    propose_step:
        取 session-bound draft → 驗證 identity → 建 immutable snapshot
        → 確認 durable 保存成功 → deterministic eligibility ＋ catalog drift
        → 建 PlanStepBinding → 交給**既有** ProposalRegistry 建 proposal
        → 保存 binding → 回傳待核准內容

    execute_step:
        載入 proposal ＋ binding → 驗證 identity 與 approval 狀態
        → 確認 proposal_kind = plan_step
        → **activation gate**（disabled 就在這裡停，不 claim、不呼叫 action）
        → 重組 current catalog → drift 檢查 → arguments／risk 重驗
        → dependency receipts ＋ stop conditions
        → atomic one-shot claim（既有 ProposalRegistry）
        → 執行恰一個 Package action（既有 execution bridge）
        → 保存 durable receipt

這一層不認識任何 EDA 工具、metric 或 report 語意。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from terminux_engine.bounded_execution import errors as E
from terminux_engine.bounded_execution.activation import ActivationGate
from terminux_engine.bounded_execution.binding import (
    BINDING_RECORD_TYPE,
    BINDING_SCHEMA_VERSION,
    PROPOSAL_KIND_PLAN_STEP,
    PlanStepBinding,
    canonical_json,
    compute_arguments_sha256,
    compute_capability_contract_sha256,
)
from terminux_engine.bounded_execution.eligibility import check_step_eligibility
from terminux_engine.bounded_execution.receipt import (
    DISPATCH_COMPLETED,
    DISPATCH_FAILED,
    DISPATCH_OUTCOME_UNCONFIRMED,
    MAX_INLINE_RESULT_BYTES,
    PERSISTENCE_DURABLE,
    PERSISTENCE_FAILED,
    RECEIPT_RECORD_TYPE,
    RECEIPT_SCHEMA_VERSION,
    ExecutionReceipt,
    compute_receipt_sha256,
    compute_result_digest,
)
from terminux_engine.bounded_execution.snapshot import (
    SNAPSHOT_RECORD_TYPE,
    PlanSnapshot,
    SnapshotConflictError,
    build_plan_snapshot,
    reconcile_existing,
)

_LOG = logging.getLogger(__name__)

PLANNER_VERSION = "ai3_plan_only_v1"
VALIDATOR_VERSION = "ai3_plan_validator_v1"


@dataclass(frozen=True)
class BoundedExecutionDeps:
    """approved package action 的**完整**依賴集合。

    刻意沒有 shell runner、Kernel、SubprocessExecutor 或 ORFS adapter 的位置。
    `action_executor` 是既有 Package action execution bridge 的 callable，
    不是原始 handler——approved package action 不直接 import 任何 Package。
    """

    draft_cache: Any
    proposal_registry: Any
    #: 讀寫 durable record：`put(record_type, key, payload) -> bool`、
    #: `query(record_type, **filters) -> list[dict]`。由 composition 以既有
    #: EventStore 實作，approved package action 不新增 store。
    record_store: Any
    #: `() -> (catalog_snapshot_id, {capability_id: normalized_capability})`
    catalog_provider: Callable[[], tuple[str, dict[str, Any]]]
    activation_gate: ActivationGate
    #: `(action_name, arguments, proposal_id, proposal_digest) -> dict`
    #: 既有的 approved-proposal 執行路徑；approved package action 不自行安裝 authorization policy。
    action_executor: Callable[..., dict[str, Any]] | None = None
    clock: Callable[[], float] | None = None

    def assert_no_forbidden_capability(self) -> None:
        forbidden = ("shell", "subprocess", "kernel", "dispatch_engine",
                     "orfs_adapter", "run_command", "ledger")
        present = [n for n in forbidden if hasattr(self, n)]
        if present:
            raise RuntimeError(f"bounded execution deps must not carry: {present}")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error", "error_code": code, "message": message,
        "action_execution_count": 0, "proposal_claimed": False,
        "proposal_created": False,
    }
    payload.update(extra)
    return payload


# ── Part B/C：propose ─────────────────────────────────────────────────


def propose_step(
    deps: BoundedExecutionDeps,
    *,
    plan_id: str,
    step_id: str,
    member_id: str,
    project_id: str,
    session_id: str,
    source_request_id: str | None = None,
) -> dict[str, Any]:
    """從 session-bound plan 取出**一個** step 並建立 proposal。永不執行。"""
    deps.assert_no_forbidden_capability()

    entry, denial = deps.draft_cache.get(
        plan_id, member_id=member_id, project_id=project_id, session_id=session_id)
    if entry is None:
        return _error(denial or E.PLAN_DRAFT_NOT_FOUND,
                      "the plan is not available for this session; produce it again")

    plan = entry.plan
    snapshot = build_plan_snapshot(
        plan, created_at=_now_iso(), member_id=member_id, project_id=project_id,
        session_id=session_id, planner_version=PLANNER_VERSION,
        validator_version=VALIDATOR_VERSION, source_request_id=source_request_id)

    # ── snapshot 必須先 durable，才可能有 proposal ────────────
    try:
        existing = deps.record_store.query(SNAPSHOT_RECORD_TYPE, plan_id=plan_id)
        already, _payload = reconcile_existing(snapshot, existing)
        if not already:
            ok = deps.record_store.put(SNAPSHOT_RECORD_TYPE, snapshot.plan_id,
                                       snapshot.to_json_dict())
            if not ok:
                return _error(E.PLAN_SNAPSHOT_PERSIST_FAILED,
                              "the plan snapshot could not be persisted; no proposal "
                              "was created")
    except SnapshotConflictError as exc:
        return _error(E.PLAN_SNAPSHOT_HASH_MISMATCH, str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("plan snapshot persistence failed: %r", exc)
        return _error(E.PLAN_SNAPSHOT_PERSIST_FAILED,
                      f"the plan snapshot could not be persisted: {exc!r}")

    catalog_id, capabilities = deps.catalog_provider()
    capability = capabilities.get((snapshot.step(step_id) or {}).get("capability"))

    completed = _completed_step_ids(deps, plan_id)
    proposed = _live_proposed_step_ids(deps, plan_id)
    result = check_step_eligibility(
        snapshot=snapshot, step_id=step_id, capability=capability,
        current_catalog_snapshot_id=catalog_id,
        completed_step_ids=completed, proposed_step_ids=proposed,
        requesting_identity=(member_id, project_id, session_id))
    if not result.eligible:
        return _error(result.denial_code or E.PLAN_STEP_NOT_ELIGIBLE,
                      result.message or "step is not eligible", details=result.details)

    step = result.details["step"]
    arguments = dict(step.get("arguments") or {})

    try:
        proposal = deps.proposal_registry.create(
            action_name=step["capability"], arguments=arguments,
            reason=(f"approved bounded execution of plan {plan_id} step {step_id}"),
            expected_effects=list(step.get("expected_evidence") or []),
            possible_side_effects=list(capability.get("side_effects") or []),
            preconditions=list(step.get("preconditions") or []),
            evidence_references=[f"plan:{plan_id}", f"plan_sha256:{snapshot.plan_sha256}"],
            catalog=_proposable_catalog(capabilities),
            context=_context(member_id, project_id, session_id),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("proposal creation failed: %r", exc)
        return _error(E.PLAN_PROPOSAL_BINDING_MISMATCH,
                      f"the proposal could not be created: {exc!r}")

    binding = PlanStepBinding(
        schema_version=BINDING_SCHEMA_VERSION, record_type=BINDING_RECORD_TYPE,
        proposal_id=proposal.proposal_id, proposal_kind=PROPOSAL_KIND_PLAN_STEP,
        plan_id=plan_id, plan_sha256=snapshot.plan_sha256, step_id=step_id,
        capability_id=step["capability"],
        canonical_arguments=canonical_json(arguments),
        arguments_sha256=compute_arguments_sha256(arguments),
        catalog_snapshot_id=catalog_id,
        capability_contract_sha256=compute_capability_contract_sha256(capability),
        risk=str(capability.get("risk")),
        member_id=member_id, project_id=project_id, session_id=session_id,
        side_effects=tuple(capability.get("side_effects") or []),
        preconditions=tuple(step.get("preconditions") or []),
        expected_evidence=tuple(step.get("expected_evidence") or []),
        stop_conditions=tuple(step.get("stop_conditions") or []),
        created_at=_now_iso(), expires_at=getattr(proposal, "expires_at", None),
    )
    deps.record_store.put(BINDING_RECORD_TYPE, binding.proposal_id, binding.to_json_dict())

    return {
        "status": "proposed",
        "proposal_created": True,
        "proposal_kind": PROPOSAL_KIND_PLAN_STEP,
        "proposal_id": proposal.proposal_id,
        "proposal_digest": proposal.proposal_digest,
        "plan_id": plan_id, "plan_sha256": snapshot.plan_sha256, "step_id": step_id,
        "capability_id": step["capability"], "arguments": arguments,
        "risk": binding.risk, "approval_required": True,
        "side_effects": list(binding.side_effects),
        "preconditions": list(binding.preconditions),
        "expected_evidence": list(binding.expected_evidence),
        "stop_conditions": list(binding.stop_conditions),
        "catalog_snapshot_id": catalog_id,
        "availability_state": snapshot.availability_state,
        "bounded_execution_enabled": deps.activation_gate.enabled,
        "activation_reason": deps.activation_gate.reason,
        "expires_at": binding.expires_at,
        "action_execution_count": 0, "proposal_claimed": False,
    }


# ── Part D/E/F：execute ──────────────────────────────────────────────


def execute_step(
    deps: BoundedExecutionDeps,
    *,
    proposal_id: str,
    proposal_digest: str,
    member_id: str,
    project_id: str,
    session_id: str,
) -> dict[str, Any]:
    """執行一個已核准的 plan step。Gate disabled 時在 claim **之前**停下。"""
    deps.assert_no_forbidden_capability()

    bindings = deps.record_store.query(BINDING_RECORD_TYPE, proposal_id=proposal_id)
    if not bindings:
        return _error(E.PLAN_PROPOSAL_BINDING_MISMATCH,
                      "this proposal has no plan-step binding")
    raw = bindings[0]
    if raw.get("proposal_kind") != PROPOSAL_KIND_PLAN_STEP:
        return _error(E.PLAN_PROPOSAL_BINDING_MISMATCH,
                      "this proposal is not a plan step")
    binding = _binding_from_payload(raw)

    if (member_id, project_id, session_id) != (
            binding.member_id, binding.project_id, binding.session_id):
        return _error(E.BOUNDED_EXECUTION_IDENTITY_MISMATCH,
                      "the requesting identity does not match the approved proposal")

    view = deps.proposal_registry.get_for_scope(
        proposal_id, _context(member_id, project_id, session_id))
    if view is None:
        return _error(E.BOUNDED_EXECUTION_NOT_APPROVED,
                      "the proposal does not exist, expired, or is out of scope")
    if view.get("proposal_digest") != proposal_digest:
        return _error(E.PLAN_PROPOSAL_BINDING_MISMATCH, "proposal digest mismatch")
    if view.get("status") in ("EXECUTING", "SUCCEEDED", "FAILED"):
        return _error(E.BOUNDED_EXECUTION_REPLAY_DENIED,
                      "this proposal has already been claimed for execution")
    if view.get("status") != "APPROVED":
        return _error(E.BOUNDED_EXECUTION_NOT_APPROVED,
                      f"the proposal is {view.get('status')!r}, not APPROVED")

    # ── activation gate：在 claim 與 action call **之前** ─────
    if not deps.activation_gate.enabled:
        blocked = deps.activation_gate.blocked_response()
        blocked.update({"proposal_id": proposal_id, "plan_id": binding.plan_id,
                        "step_id": binding.step_id,
                        "capability_id": binding.capability_id,
                        "proposal_created": False})
        return blocked

    catalog_id, capabilities = deps.catalog_provider()
    capability = capabilities.get(binding.capability_id)
    if capability is None:
        return _error(E.PLAN_CATALOG_DRIFT_REPLAN_REQUIRED,
                      f"{binding.capability_id!r} is no longer registered")
    arguments = _decode_arguments(binding.canonical_arguments)
    denial = binding.matches_runtime(
        capability=capability, catalog_snapshot_id=catalog_id, arguments=arguments,
        member_id=member_id, project_id=project_id, session_id=session_id)
    if denial is not None:
        return _error(denial, "revalidation failed before execution; nothing was executed")

    completed = _completed_step_ids(deps, binding.plan_id)
    snapshots = deps.record_store.query(SNAPSHOT_RECORD_TYPE, plan_id=binding.plan_id)
    if not snapshots:
        return _error(E.PLAN_SNAPSHOT_HASH_MISMATCH, "the plan snapshot is missing")
    if snapshots[0].get("plan_sha256") != binding.plan_sha256:
        return _error(E.PLAN_SNAPSHOT_HASH_MISMATCH,
                      "the durable plan snapshot no longer matches this binding")
    step = next((s for s in snapshots[0].get("steps") or []
                 if s.get("step_id") == binding.step_id), None) or {}
    for dependency in step.get("depends_on") or []:
        if dependency not in completed:
            return _error(E.PLAN_DEPENDENCY_NOT_SATISFIED,
                          f"dependency {dependency!r} has no durable completed receipt")
    if binding.step_id in completed:
        return _error(E.PLAN_STEP_ALREADY_EXECUTED,
                      "this step already has a terminal execution receipt")

    if deps.action_executor is None:
        return _error(E.BOUNDED_EXECUTION_VALIDATION_REQUIRED,
                      "no bounded execution bridge is configured")

    clock = deps.clock or (lambda: 0.0)
    claimed_at = clock()
    outcome = deps.action_executor(
        action_name=binding.capability_id, arguments=arguments,
        proposal_id=proposal_id, proposal_digest=proposal_digest)
    finished_at = clock()

    return _finalize(deps, binding, catalog_id, capability, outcome,
                     claimed_at=claimed_at, started_at=claimed_at,
                     finished_at=finished_at)


def _finalize(
    deps: BoundedExecutionDeps, binding: PlanStepBinding, catalog_id: str,
    capability: dict[str, Any], outcome: dict[str, Any], *,
    claimed_at: float, started_at: float, finished_at: float,
) -> dict[str, Any]:
    success = bool(outcome.get("success"))
    # outer timeout **不是** failed。ThreadPoolExecutor 無法 kill
    # 已啟動的 thread，所以 action 可能仍在背景執行並寫入副作用；把它記成
    # failed 會讓讀者誤以為什麼都沒發生。
    outcome_unconfirmed = bool(outcome.get("timed_out"))
    result = outcome.get("result")
    digest, size = compute_result_digest(result)
    receipt = ExecutionReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION, record_type=RECEIPT_RECORD_TYPE,
        receipt_id=f"rcpt_{uuid4().hex}", execution_instance_id=uuid4().hex,
        plan_id=binding.plan_id, plan_sha256=binding.plan_sha256,
        step_id=binding.step_id, proposal_id=binding.proposal_id,
        approval_binding_sha256=binding.binding_sha256(),
        capability_id=binding.capability_id, arguments_sha256=binding.arguments_sha256,
        catalog_snapshot_id_at_plan=binding.catalog_snapshot_id,
        catalog_snapshot_id_at_execution=catalog_id,
        capability_contract_sha256=compute_capability_contract_sha256(capability),
        risk=binding.risk, member_id=binding.member_id,
        project_id=binding.project_id, session_id=binding.session_id,
        claimed_at=claimed_at, started_at=started_at, finished_at=finished_at,
        dispatch_status=(DISPATCH_OUTCOME_UNCONFIRMED if outcome_unconfirmed
                         else DISPATCH_COMPLETED if success else DISPATCH_FAILED),
        action_result_status=(result or {}).get("status") if isinstance(result, dict) else None,
        action_result_digest=digest,
        action_result_reference=(outcome.get("result_reference")
                                 if size > MAX_INLINE_RESULT_BYTES else None),
        dqe_status=_dqe_status(result),
        record_references=tuple((result or {}).get("record_refs") or [])
        if isinstance(result, dict) else (),
        evidence_references=tuple(outcome.get("evidence_references") or []),
    )
    try:
        ok = deps.record_store.put(RECEIPT_RECORD_TYPE, receipt.receipt_id,
                                   receipt.with_persistence(PERSISTENCE_DURABLE).to_json_dict())
    except Exception as exc:  # noqa: BLE001
        _LOG.error("receipt persistence failed: %r", exc)
        ok = False
    if not ok:
        # Action 可能已經執行，但收據不 durable——不自動 retry、不釋放 claim。
        return {
            "status": "execution_outcome_unconfirmed",
            "error_code": E.BOUNDED_EXECUTION_OUTCOME_UNCONFIRMED,
            "automatic_retry_allowed": False,
            "manual_investigation_required": True,
            "proposal_id": binding.proposal_id, "plan_id": binding.plan_id,
            "step_id": binding.step_id,
            "receipt_persistence_status": PERSISTENCE_FAILED,
            "action_execution_count": 1, "proposal_claimed": True,
            "message": ("The action may have executed but its receipt is not durable. "
                        "No automatic retry is performed and the one-shot claim is not "
                        "released."),
        }
    payload = {
        "status": ("execution_outcome_unconfirmed" if outcome_unconfirmed
                   else "completed" if success else "failed"),
        "proposal_id": binding.proposal_id, "plan_id": binding.plan_id,
        "step_id": binding.step_id, "capability_id": binding.capability_id,
        "dispatch_status": receipt.dispatch_status,
        "action_result_status": receipt.action_result_status,
        "dqe_status": receipt.dqe_status,
        "receipt_id": receipt.receipt_id,
        "receipt_sha256": compute_receipt_sha256(receipt),
        "receipt_persistence_status": PERSISTENCE_DURABLE,
        "action_execution_count": 1, "proposal_claimed": True,
    }
    if outcome_unconfirmed:
        # 明確揭露：claim 不釋放、不自動 retry、不會有第二次 action call，
        # 而且**背景工作可能仍在進行**。
        payload.update({
            "outcome_unconfirmed": True,
            "automatic_retry_allowed": False,
            "approval_released": False,
            "message": ("The outer execution timeout expired. The worker thread "
                        "cannot be cancelled, so this action may still be running "
                        "and may still write its own records. The one-shot claim is "
                        "not released and no automatic retry is performed."),
        })
    return payload


# ── helpers ──────────────────────────────────────────────────────────


def _dqe_status(result: Any) -> str:
    """Package result 的品質狀態。**與 dispatch 成功與否完全分開**：
    action 可以成功完成分析，而其工程結論是 fail。"""
    if not isinstance(result, dict):
        return "not_applicable"
    insight = result.get("insight")
    if isinstance(insight, dict):
        for key in ("dqe_overall_status", "candidate_overall_status"):
            value = insight.get(key)
            if isinstance(value, str) and value:
                return value
    value = result.get("dqe_overall_status")
    return value if isinstance(value, str) and value else "not_applicable"


def _completed_step_ids(deps: BoundedExecutionDeps, plan_id: str) -> set[str]:
    try:
        receipts = deps.record_store.query(RECEIPT_RECORD_TYPE, plan_id=plan_id)
    except Exception:  # noqa: BLE001
        return set()
    return {r.get("step_id") for r in receipts
            if r.get("dispatch_status") == DISPATCH_COMPLETED and r.get("step_id")}


def _live_proposed_step_ids(deps: BoundedExecutionDeps, plan_id: str) -> set[str]:
    try:
        bindings = deps.record_store.query(BINDING_RECORD_TYPE, plan_id=plan_id)
    except Exception:  # noqa: BLE001
        return set()
    live: set[str] = set()
    for b in bindings:
        view = None
        try:
            view = deps.proposal_registry.get_for_scope(
                b.get("proposal_id"),
                _context(b.get("member_id"), b.get("project_id"), b.get("session_id")))
        except Exception:  # noqa: BLE001
            view = None
        if view is not None and view.get("status") in ("PENDING", "APPROVED", "EXECUTING"):
            live.add(b.get("step_id"))
    return live


def _binding_from_payload(payload: dict[str, Any]) -> PlanStepBinding:
    return PlanStepBinding(
        schema_version=payload["schema_version"], record_type=payload["record_type"],
        proposal_id=payload["proposal_id"], proposal_kind=payload["proposal_kind"],
        plan_id=payload["plan_id"], plan_sha256=payload["plan_sha256"],
        step_id=payload["step_id"], capability_id=payload["capability_id"],
        canonical_arguments=payload["canonical_arguments"],
        arguments_sha256=payload["arguments_sha256"],
        catalog_snapshot_id=payload["catalog_snapshot_id"],
        capability_contract_sha256=payload["capability_contract_sha256"],
        risk=payload["risk"], member_id=payload["member_id"],
        project_id=payload["project_id"], session_id=payload["session_id"],
        side_effects=tuple(payload.get("side_effects") or []),
        preconditions=tuple(payload.get("preconditions") or []),
        expected_evidence=tuple(payload.get("expected_evidence") or []),
        stop_conditions=tuple(payload.get("stop_conditions") or []),
        created_at=payload["created_at"], expires_at=payload.get("expires_at"),
    )


def _decode_arguments(canonical: str) -> dict[str, Any]:
    import json

    try:
        value = json.loads(canonical)
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


def _proposable_catalog(capabilities: dict[str, Any]) -> list[Any]:
    from terminux_engine.llm.proposal_registry import ProposableCapability

    return [
        ProposableCapability(
            action_name=cid,
            description=c.get("description") or "",
            parameters=_schema_to_parameters(c.get("input_schema") or {}),
        )
        for cid, c in capabilities.items()
    ]


def _schema_to_parameters(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """JSON Schema → ProposalRegistry 的 manifest-style parameter mapping。"""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return {}
    required = set(schema.get("required") or [])
    return {name: {**(spec if isinstance(spec, dict) else {}),
                   "required": name in required}
            for name, spec in properties.items()}


def _context(member_id: Any, project_id: Any, session_id: Any) -> Any:
    from terminux_engine.ipc.request_context import CanonicalRequestContext

    return CanonicalRequestContext(session_id=str(session_id), member_id=str(member_id),
                                   project_id=str(project_id))


__all__ = [
    "PLANNER_VERSION",
    "VALIDATOR_VERSION",
    "BoundedExecutionDeps",
    "execute_step",
    "propose_step",
]
