# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Bounded proactive coordinator。

固定順序，中間不插入任何 action、proposal、tool 或 shell：

    activation gate
    → package discovery（Package-owned；Common 不解讀）
    → package trigger decision（deterministic；LLM 不參與）
    → deterministic trigger id
    → durable claim（same-process lock ＋ history 重查）
    → situation assessment read-only assessment（Package-owned use case，一次）
    → snapshot durable／idempotent
    → 一次 notification
    → completed／failed lifecycle record

**By construction 不可能執行任何東西**：建構子沒有 ActionRegistry、
ProposalRegistry、PlanRegistry、ToolRegistry executor、Kernel、
SubprocessExecutor 或 shell runner 的位置。

Coordinator 不認識任何 EDA 工具或 metric。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from terminux_engine.proactive.activation import ProactiveActivationGate
from terminux_engine.proactive.lifecycle import (
    CLAIM_OK,
    LIFECYCLE_SEMANTICS,
    RECORD_CLAIMED,
    RECORD_COMPLETED,
    RECORD_FAILED,
    SUPPRESSING_RECORD_TYPES,
    ClaimResult,
    LifecycleRecord,
    claim_trigger,
)
from terminux_engine.proactive.observation import (
    ProactiveObservation,
    TriggerDecision,
    canonical_json,
    compute_trigger_id,
    sha256_text,
)

_LOG = logging.getLogger(__name__)

FAILURE_ASSESSMENT = "PROACTIVE_ASSESSMENT_FAILED"
FAILURE_SNAPSHOT = "PROACTIVE_SNAPSHOT_NOT_DURABLE"
FAILURE_NOTIFICATION = "PROACTIVE_NOTIFICATION_DELIVERY_FAILED"
#: 預算在呼叫 provider **之前**就用盡。這**不是**分析成功，
#: 因此不寫 snapshot、也不發通知。
FAILURE_BUDGET = "PROACTIVE_PROVIDER_BUDGET_EXHAUSTED"


class ProactiveProvider(Protocol):
    """Package-owned。**所有** domain 判斷都在這裡。"""

    policy_id: str
    policy_version: str

    def discover(self, *, limit: int) -> list[ProactiveObservation]:
        """回傳結構上已完成、evidence 就緒的候選觀察。"""

    def decide(self, observation: ProactiveObservation) -> TriggerDecision:
        """deterministic trigger／suppress／insufficient_evidence。"""

    def assess(self, decision: TriggerDecision,
               observation: ProactiveObservation) -> dict[str, Any]:
        """重用 situation assessment read-only assessment。回傳其原本的結構化結果。

        `observation` 一併傳入，讓 Package 能把 analysis-input 與 lifecycle
        狀態原樣寫進 snapshot；Common 不解讀其內容。
        """

    def build_notification(self, decision: TriggerDecision,
                           assessment: dict[str, Any]) -> Any:
        """由**已持久化**的 snapshot 內容組出通知。不得重新呼叫 LLM。"""


@dataclass(frozen=True)
class ProactiveDeps:
    """Coordinator 的**完整**依賴集合。

    刻意不是通用 container：只有這四樣，executor 類的東西無處可放。
    """

    provider: ProactiveProvider
    activation_gate: ProactiveActivationGate
    #: `put(record_type, key, payload) -> bool`／`query(record_type, **filters)`
    record_store: Any
    #: `emit(NotificationRequest) -> None`
    notifier: Any
    clock: Callable[[], str] | None = None
    #: 可選的 per-cycle provider budget。`None` 代表不設限，
    #: 行為與加入此欄位之前完全相同（backward compatible）。
    budget_limits: Any | None = None
    #: 可選的 `(input_tokens, output_tokens) -> float` 估價策略。Common 不含
    #: 任何 provider 價目；未提供時 cost estimate 一律 unavailable。
    pricing_policy: Any | None = None

    def assert_no_execution_capability(self) -> None:
        forbidden = ("action_registry", "proposal_registry", "plan_registry",
                     "tool_registry", "executor", "kernel", "dispatch_engine",
                     "subprocess_executor", "shell", "run_command")
        present = [n for n in forbidden if hasattr(self, n)]
        if present:
            raise RuntimeError(
                f"proactive deps must not carry execution capability: {present}")


@dataclass
class _Counters:
    candidates_examined: int = 0
    triggers: int = 0
    suppressed: int = 0
    insufficient: int = 0
    claims_written: int = 0
    claims_suppressed: int = 0
    assessments: int = 0
    snapshots_written: int = 0
    notifications_sent: int = 0
    failures: int = 0
    #: 因 per-cycle 預算耗盡而**延後**（未 claim、未寫記錄）的候選數。
    #: 刻意與 `failures` 分開——延後不是失敗，候選仍可在下個 cycle 被處理。
    budget_deferred: int = 0

    def to_json_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class ProactiveCoordinator:
    """一輪 bounded 掃描。每個 candidate 最多一次嘗試。"""

    #: 明確列出永遠不得持有的東西；每次 run 都檢查一次。
    FORBIDDEN_ATTRIBUTES: tuple[str, ...] = (
        "_action_registry", "_proposal_registry", "_plan_registry", "_tool_registry",
        "_executor", "_kernel", "_dispatch_engine", "_subprocess_executor", "_shell",
    )

    def __init__(self, deps: ProactiveDeps, *, max_candidates: int = 5) -> None:
        self._budget = None
        self._deps = deps
        self._max_candidates = max_candidates

    def assert_no_execution_capability(self) -> None:
        present = [a for a in self.FORBIDDEN_ATTRIBUTES if hasattr(self, a)]
        if present:
            raise RuntimeError(
                f"ProactiveCoordinator must not hold execution capability: {present}")
        self._deps.assert_no_execution_capability()

    # ── public ───────────────────────────────────────────────────────

    def run_once(self) -> dict[str, Any]:
        """執行一輪。永不拋出——所有失敗收斂成結構化結果。"""
        deps = self._deps

        # ── activation gate：在**任何** side effect 之前 ────────
        if not deps.activation_gate.enabled:
            return deps.activation_gate.blocked_response()

        self.assert_no_execution_capability()
        counters = _Counters()
        results: list[dict[str, Any]] = []
        # 每一輪各自建立預算。沒有跨 cycle 累計、沒有持久化、
        # 沒有全域 scheduler／store／ledger。
        self._budget = None
        if deps.budget_limits is not None:
            from terminux_engine.proactive.budget import CycleBudget

            self._budget = CycleBudget(limits=deps.budget_limits,
                                       pricing_policy=deps.pricing_policy)
            # provider 若支援，就把這個 cycle 的預算交給它——這樣 reserve 會發生在
            # 真正呼叫 provider 之前，而不是事後補記。Protocol 不變：
            # 沒有這個 hook 的 provider 完全不受影響。
            hook = getattr(deps.provider, "set_cycle_budget", None)
            if callable(hook):
                hook(self._budget)

        try:
            observations = deps.provider.discover(limit=self._max_candidates)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("proactive discovery failed: %r", exc)
            return {"status": "error", "error_code": "PROACTIVE_DISCOVERY_FAILED",
                    "message": f"{exc!r}", **counters.to_json_dict()}

        for observation in observations[: self._max_candidates]:
            counters.candidates_examined += 1
            results.append(self._handle(observation, counters))

        payload = {"status": "ok", "lifecycle_semantics": LIFECYCLE_SEMANTICS,
                   "candidates": results, **counters.to_json_dict()}
        if self._budget is not None:
            payload["budget"] = self._budget.to_json_dict()
        return payload

    # ── internals ────────────────────────────────────────────────────

    def _now(self) -> str | None:
        return self._deps.clock() if self._deps.clock is not None else None

    def _handle(self, observation: ProactiveObservation,
                counters: _Counters) -> dict[str, Any]:
        deps = self._deps
        try:
            decision = deps.provider.decide(observation)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("proactive decision failed: %r", exc)
            return {"observation_id": observation.observation_id,
                    "outcome": "decision_error", "message": f"{exc!r}"}

        if not decision.should_trigger:
            if decision.decision == "suppress":
                counters.suppressed += 1
            else:
                counters.insufficient += 1
            return {"observation_id": observation.observation_id,
                    "outcome": decision.decision,
                    "decision": decision.to_json_dict()}

        counters.triggers += 1
        trigger_id = compute_trigger_id(observation, decision)

        # ── budget 在 claim **之前**檢查────────────────────
        # budget 一律在呼叫 provider 之前檢查；被 dedup／not_claimed
        # 的 candidate 不會消耗預算。
        #
        # 順序很重要：`RECORD_FAILED` 屬於 `SUPPRESSING_RECORD_TYPES`，而本版
        # 不自動重跑既有 trigger。若先 claim 再檢查預算，預算耗盡就會寫下一筆
        # FAILED 記錄，使這個 trigger **永久**不再被分析——等於靜默丟掉一筆
        # 真實發現。因此預算耗盡時完全不 claim、不寫任何 lifecycle 記錄，
        # 讓下一個 cycle 重新發現它。
        budget = self._budget
        if budget is not None and budget.exhausted:
            counters.budget_deferred += 1
            return {"observation_id": observation.observation_id,
                    "trigger_id": trigger_id, "outcome": "budget_exhausted",
                    "failure_code": FAILURE_BUDGET,
                    "budget": budget.to_json_dict(),
                    "claimed": False,
                    "notification_sent": False,
                    "deferred_to_next_cycle": True,
                    "message": ("the per-cycle provider budget was exhausted before "
                                "this candidate was claimed; nothing was analysed, "
                                "no lifecycle record was written and no notification "
                                "was sent — the candidate remains eligible for a "
                                "later cycle")}

        claim = self._claim(trigger_id, observation, decision)
        if not claim.may_proceed:
            counters.claims_suppressed += 1
            return {"observation_id": observation.observation_id,
                    "trigger_id": trigger_id, "outcome": "not_claimed",
                    "claim": claim.to_json_dict()}
        counters.claims_written += 1

        # ── 一次 assessment（situation assessment read-only use case）────────────────
        try:
            assessment = deps.provider.assess(decision, observation)
            counters.assessments += 1
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("proactive assessment failed: %r", exc)
            self._finalize(RECORD_FAILED, trigger_id, observation, decision,
                           failure_code=FAILURE_ASSESSMENT)
            counters.failures += 1
            return {"observation_id": observation.observation_id,
                    "trigger_id": trigger_id, "outcome": "assessment_failed",
                    "failure_code": FAILURE_ASSESSMENT, "message": f"{exc!r}"}

        if assessment.get("status") != "ok":
            self._finalize(RECORD_FAILED, trigger_id, observation, decision,
                           failure_code=FAILURE_ASSESSMENT)
            counters.failures += 1
            return {"observation_id": observation.observation_id,
                    "trigger_id": trigger_id, "outcome": "assessment_not_ok",
                    "failure_code": FAILURE_ASSESSMENT,
                    "assessment_status": assessment.get("status"),
                    "assessment_error": assessment.get("error_code")}

        # ── snapshot 必須先 durable／idempotent，才允許通知 ───
        persistence = assessment.get("snapshot_persistence") or {}
        if persistence.get("outcome") not in ("append", "idempotent"):
            self._finalize(RECORD_FAILED, trigger_id, observation, decision,
                           failure_code=FAILURE_SNAPSHOT)
            counters.failures += 1
            return {"observation_id": observation.observation_id,
                    "trigger_id": trigger_id, "outcome": "snapshot_not_durable",
                    "failure_code": FAILURE_SNAPSHOT,
                    "snapshot_persistence": persistence}
        counters.snapshots_written += 1

        # ── 一次 notification（內容只來自已持久化的 snapshot）────────
        notification = None
        try:
            notification = deps.provider.build_notification(decision, assessment)
            deps.notifier.emit(notification)
            counters.notifications_sent += 1
            delivered = True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("proactive notification delivery failed: %r", exc)
            delivered = False

        summary_hash = (sha256_text(canonical_json({
            "level": getattr(notification, "level", None),
            "title": getattr(notification, "title", None),
            "message": getattr(notification, "message", None),
        })) if notification is not None else None)

        if not delivered:
            # 不重新分析、不自動 retry；只誠實記錄投遞失敗。
            self._finalize(RECORD_FAILED, trigger_id, observation, decision,
                           failure_code=FAILURE_NOTIFICATION,
                           snapshot_id=assessment.get("snapshot_id"),
                           notification_summary_sha256=summary_hash)
            counters.failures += 1
            return {"observation_id": observation.observation_id,
                    "trigger_id": trigger_id,
                    "outcome": "notification_delivery_failed",
                    "failure_code": FAILURE_NOTIFICATION,
                    "snapshot_id": assessment.get("snapshot_id"),
                    "automatic_retry_allowed": False}

        self._finalize(RECORD_COMPLETED, trigger_id, observation, decision,
                       snapshot_id=assessment.get("snapshot_id"),
                       notification_summary_sha256=summary_hash)
        return {"observation_id": observation.observation_id,
                "trigger_id": trigger_id, "outcome": "completed",
                "snapshot_id": assessment.get("snapshot_id"),
                "readiness": (assessment.get("readiness") or {}).get("status"),
                "notification_summary_sha256": summary_hash}

    def _claim(self, trigger_id: str, observation: ProactiveObservation,
               decision: TriggerDecision) -> ClaimResult:
        deps = self._deps

        def read_history() -> list[dict[str, Any]]:
            history: list[dict[str, Any]] = []
            for record_type in SUPPRESSING_RECORD_TYPES:
                try:
                    history.extend(deps.record_store.query(record_type,
                                                           trigger_id=trigger_id))
                except Exception as exc:  # noqa: BLE001 - 讀不到就不能 claim
                    raise RuntimeError(f"lifecycle history unreadable: {exc!r}") from exc
            return history

        def write_claim() -> bool:
            record = LifecycleRecord(
                record_type=RECORD_CLAIMED, trigger_id=trigger_id,
                policy_id=decision.policy_id, policy_version=decision.policy_version,
                observation_identity=observation.identity(),
                subject_identity=decision.subject_identity,
                baseline_identity=decision.baseline_identity,
                recorded_at=self._now())
            return bool(deps.record_store.put(RECORD_CLAIMED, trigger_id,
                                              record.to_json_dict()))

        try:
            return claim_trigger(trigger_id=trigger_id, read_history=read_history,
                                 write_claim=write_claim)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("proactive claim failed: %r", exc)
            return ClaimResult("claim_write_failed", (), f"{exc!r}")

    def _finalize(self, record_type: str, trigger_id: str,
                  observation: ProactiveObservation, decision: TriggerDecision,
                  **extra: Any) -> None:
        # `proactive_trigger_completed` 指的是**這次 proactive cycle**
        # 完成，不是被觀察的 run 完成。記錄一併帶上 lifecycle 三軸的實際
        # 狀態，讓後續讀者不會把兩者混為一談。
        observed_lifecycle = {
            "analysis_input_status": observation.analysis_input_status,
            "execution_completion_status": observation.execution_completion_status,
            "dispatch_status": observation.dispatch_status,
            "barrier_evidence_status": observation.barrier_evidence_status,
            "lifecycle_evidence_basis": dict(observation.lifecycle_evidence_basis),
        }
        record = LifecycleRecord(
            observed_lifecycle=observed_lifecycle,
            record_type=record_type, trigger_id=trigger_id,
            policy_id=decision.policy_id, policy_version=decision.policy_version,
            observation_identity=observation.identity(),
            subject_identity=decision.subject_identity,
            baseline_identity=decision.baseline_identity,
            recorded_at=self._now(), **extra)
        try:
            self._deps.record_store.put(record_type, trigger_id, record.to_json_dict())
        except Exception as exc:  # noqa: BLE001 - 記不下來也不改變已發生的事
            _LOG.error("proactive lifecycle record write failed (%s): %r",
                       record_type, exc)


__all__ = [
    "FAILURE_ASSESSMENT",
    "FAILURE_NOTIFICATION",
    "FAILURE_SNAPSHOT",
    "ProactiveCoordinator",
    "ProactiveDeps",
    "ProactiveProvider",
]
