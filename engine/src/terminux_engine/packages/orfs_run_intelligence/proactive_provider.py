# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS proactive provider。

**所有** domain 判斷都在這裡：observation 是否屬於 ORFS run、DQE evaluation
是否完整、blocker／readiness、baseline comparability、trigger／suppress 理由、
通知的 level／title／內容。`terminux_engine.proactive` 一律不知道這些。

Discovery 讀的是 Terminux **自己的** durable store（DQE DB ＋ artifact
manifest store），**不**監控 Docker／container process，**不**掃描 ORFS 目錄。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.dqe.db import DqeDbManager
from terminux_engine.package_runtime.result import NotificationRequest
from terminux_engine.proactive.observation import (
    ANALYSIS_INPUT_INCOMPLETE,
    ANALYSIS_INPUT_READY,
    ANALYSIS_INPUT_UNVERIFIABLE,
    BARRIER_NOT_OBSERVED,
    DISPATCH_NOT_OBSERVED,
    EXECUTION_UNVERIFIED,
    NO_DURABLE_RECEIPTS,
    OBSERVATION_SOURCE,
    PRODUCER_EXECUTION_OBSERVATION_POST_RETURN,
    AnalysisInputBasis,
    DECISION_INSUFFICIENT,
    DECISION_SUPPRESS,
    DECISION_TRIGGER,
    OBSERVATION_SCHEMA_VERSION,
    ProactiveObservation,
    TriggerDecision,
)
from terminux_engine.packages.orfs_run_intelligence import (
    situation_policy as policy,
    situation_service,
)
from terminux_engine.packages.orfs_run_intelligence.comparison import (
    STATUS_NOT_COMPARABLE,
)
from terminux_engine.packages.orfs_run_intelligence.dqe_reader import DQE_PACKAGE_ID

_LOG = logging.getLogger(__name__)

POLICY_ID = "orfs_proactive_run_intelligence"
POLICY_VERSION = "v1"
SOURCE_KIND = "dqe_evaluation_persisted"

#: v1 只處理「完整 evaluation」。ORFS DQE policy v1 有 14 個 check；不足代表
#: evaluation 還沒完整持久化，一律不觸發。
EXPECTED_DQE_ROW_COUNT = 14

#: analysis-input basis 的 Package 端取值（Common 只定義 status 軸本身）。
MANIFEST_VERIFIED = "verified"
MANIFEST_MISSING = "missing"
MANIFEST_UNVERIFIABLE = "unverifiable"
EVALUATION_KIND = "dqe_evaluation"
DQE_COMPLETE = "complete_and_persisted"
DQE_INCOMPLETE = "incomplete"
DQE_MISSING = "missing"

# ── stable reason codes（Package-owned；Common 只搬運）────────────────
REASON_TRIGGER_BLOCKERS = "current_run_not_ready_with_blockers"
REASON_SUPPRESS_NO_BLOCKER = "no_deterministic_blocker"
REASON_SUPPRESS_NOT_ORFS = "observation_is_not_an_orfs_dqe_run"
REASON_INSUFFICIENT_ROWS = "dqe_evaluation_incomplete"
REASON_INSUFFICIENT_INPUT = "analysis_input_not_ready"
REASON_INSUFFICIENT_MANIFEST = "artifact_manifest_unverifiable"
REASON_INSUFFICIENT_NO_BASELINE = "no_eligible_baseline_run"
REASON_INSUFFICIENT_NOT_COMPARABLE = "baseline_not_comparable"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _RunFacts:
    run_id: str
    step_id: str | None
    attempt: int | None
    manifest_reference: str | None
    manifest_id: str | None
    manifest_sha256: str | None
    manifest_capture_status: str | None
    row_count: int
    rows: list[dict[str, Any]]
    recorded_at: str | None


class OrfsProactiveProvider:
    """Package-owned discovery ＋ policy ＋ notification content。"""

    policy_id = POLICY_ID
    policy_version = POLICY_VERSION

    def __init__(
        self,
        *,
        dqe_db_path: Path | str | None = None,
        llm: Any = None,
        store: Any = None,
        activation_boundary: str | None = None,
    ) -> None:
        self._dqe_db_path = Path(dqe_db_path) if dqe_db_path else None
        self._llm = llm
        self._store = store
        #: activation boundary 之前的歷史資料預設**不** backfill。
        #: `None` 代表尚未設定 boundary——此時 discovery 回空清單。
        self._activation_boundary = activation_boundary
        #: 由 coordinator 在每輪開始時交進來的 per-cycle budget。
        #: `None` 代表不設限，行為與加入此欄位之前完全相同。
        self._cycle_budget = None

    def set_cycle_budget(self, budget) -> None:
        """Coordinator 的可選 hook。budget 只在這次有效。"""
        self._cycle_budget = budget

    # ── discovery ────────────────────────────────────────────────────

    def discover(self, *, limit: int) -> list[ProactiveObservation]:
        """回傳結構上已完成、evidence 就緒的 ORFS 候選觀察。

        No-backfill：沒有 activation boundary 就不回任何東西；有 boundary 時
        只看 boundary **之後**記錄的 evaluation。
        """
        if self._activation_boundary is None:
            return []
        runs = self._recent_runs(limit=limit)
        observations: list[ProactiveObservation] = []
        for facts in runs:
            manifest_ok = bool(facts.manifest_id and facts.manifest_sha256)
            dqe_complete = facts.row_count >= EXPECTED_DQE_ROW_COUNT
            if manifest_ok and dqe_complete:
                input_status = ANALYSIS_INPUT_READY
            elif manifest_ok or dqe_complete:
                input_status = ANALYSIS_INPUT_INCOMPLETE
            else:
                input_status = ANALYSIS_INPUT_UNVERIFIABLE
            observations.append(ProactiveObservation(
                schema_version=OBSERVATION_SCHEMA_VERSION,
                observation_id=f"obs_{_sha(facts.run_id + (facts.manifest_id or ''))[:32]}",
                observed_at=facts.recorded_at,
                observation_source=OBSERVATION_SOURCE,
                source_kind=SOURCE_KIND,
                producer_run_id=facts.run_id,
                producer_step_id=facts.step_id,
                producer_attempt=facts.attempt,
                artifact_manifest_id=facts.manifest_id,
                artifact_manifest_sha256=facts.manifest_sha256,
                # 這裡斷言的是「situation assessment 的分析輸入已備妥且可驗證」。
                analysis_input_status=input_status,
                analysis_input_basis=AnalysisInputBasis(
                    artifact_manifest_status=(
                        MANIFEST_VERIFIED if manifest_ok else MANIFEST_UNVERIFIABLE),
                    evaluation_kind=EVALUATION_KIND,
                    evaluation_status=(
                        DQE_COMPLETE if dqe_complete
                        else DQE_INCOMPLETE if facts.row_count else DQE_MISSING),
                    manifest_id=facts.manifest_id,
                    manifest_sha256=facts.manifest_sha256,
                    evaluation_id=facts.run_id,
                    row_count=facts.row_count,
                ),
                analysis_input_provenance=(
                    "persisted_dqe_evaluation", "verified_artifact_manifest"),
                # Lifecycle 三軸：ExecutionReceipt 不可 durable 查詢（G-1），
                # 且 DQE DB 寫入發生在 `orfs_dqe.after._emit()` **建構並回傳
                # PhaseResult 之前**——中間 crash 時 Barrier 根本沒看過結果。
                # 所以持久化的 DQE rows **不能**證明 dispatch 或 Barrier
                # 完成，這三軸只能是未觀察。
                execution_completion_status=EXECUTION_UNVERIFIED,
                dispatch_status=DISPATCH_NOT_OBSERVED,
                barrier_evidence_status=BARRIER_NOT_OBSERVED,
                lifecycle_evidence_basis=dict(NO_DURABLE_RECEIPTS),
                # lower bound：kernel 的順序（subprocess return →
                # discover_artifacts → capture）可由 source 直接證明，因此
                # manifest 存在即代表已越過 artifact-capture boundary。
                # 它不代表 exit code 0、dispatch 或 Barrier 完成，也**不是**
                # trigger 的必要條件。
                producer_execution_observation=(
                    PRODUCER_EXECUTION_OBSERVATION_POST_RETURN if manifest_ok else None),
                opaque_refs={"manifest_reference": facts.manifest_reference,
                             "dqe_row_count": facts.row_count},
            ))
        return observations

    def _recent_runs(self, *, limit: int) -> list[_RunFacts]:
        manager = DqeDbManager(self._dqe_db_path) if self._dqe_db_path else DqeDbManager()
        db_path = getattr(manager, "db_path", None) or self._dqe_db_path
        if db_path is None or not Path(db_path).is_file():
            return []
        try:
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    "SELECT orchestration_run_id, MAX(DATE_TIME) AS recorded_at "
                    "FROM Version_Dqe_Tab WHERE package_id = ? "
                    "AND orchestration_run_id IS NOT NULL "
                    "GROUP BY orchestration_run_id "
                    "HAVING recorded_at > ? ORDER BY recorded_at ASC LIMIT ?",
                    (DQE_PACKAGE_ID, self._activation_boundary, limit),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001 - 讀不到就當作沒有候選
            _LOG.warning("proactive discovery query failed: %r", exc)
            return []

        out: list[_RunFacts] = []
        for run_id, recorded_at in rows:
            records = manager.read_records_by_orchestration_run(
                run_id, package_id=DQE_PACKAGE_ID)
            if not records:
                continue
            reference = records[0].get("PATH")
            manifest_id = manifest_sha = capture_status = None
            if reference and Path(reference).is_file():
                raw = Path(reference).read_bytes()
                manifest_sha = hashlib.sha256(raw).hexdigest()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    manifest_id = payload.get("manifest_id")
                    capture_status = payload.get("capture_status")
                except Exception:  # noqa: BLE001
                    manifest_id = capture_status = None
            out.append(_RunFacts(
                run_id=run_id, step_id=records[0].get("step_id"),
                attempt=records[0].get("attempt"), manifest_reference=reference,
                manifest_id=manifest_id, manifest_sha256=manifest_sha,
                manifest_capture_status=capture_status, row_count=len(records),
                rows=records, recorded_at=recorded_at))
        return out

    # ── deterministic decision ───────────────────────────────────────

    def decide(self, observation: ProactiveObservation) -> TriggerDecision:
        """完全 deterministic。LLM 不參與。

        Eligibility 只看 **analysis input**：verified manifest ＋ 完整且
        已持久化的 DQE evaluation ＋ 足夠的 baseline ＋ deterministic
        readiness。刻意**不**要求也不宣稱 `dispatch_status == completed` 或
        `barrier_evidence_status == ready`——那兩件事沒有 durable receipt 可證。
        """
        if observation.source_kind != SOURCE_KIND:
            return self._decision(DECISION_SUPPRESS, observation,
                                  (REASON_SUPPRESS_NOT_ORFS,))

        basis = observation.analysis_input_basis
        if not observation.analysis_input_ready:
            return self._decision(
                DECISION_INSUFFICIENT, observation, (REASON_INSUFFICIENT_INPUT,),
                missing=({"evidence_type": "analysis_input",
                          "subject_id": observation.producer_run_id,
                          "status": observation.analysis_input_status},))
        row_count = basis.row_count or 0
        if basis.evaluation_status != DQE_COMPLETE:
            return self._decision(
                DECISION_INSUFFICIENT, observation, (REASON_INSUFFICIENT_ROWS,),
                missing=({"evidence_type": "dqe_evaluation",
                          "subject_id": observation.producer_run_id,
                          "status": "incomplete",
                          "detail": f"{row_count}/{EXPECTED_DQE_ROW_COUNT} checks"},))
        if basis.artifact_manifest_status != MANIFEST_VERIFIED:
            return self._decision(
                DECISION_INSUFFICIENT, observation, (REASON_INSUFFICIENT_MANIFEST,),
                missing=({"evidence_type": "artifact_manifest",
                          "subject_id": observation.producer_run_id,
                          "status": "unverifiable"},))

        current_rows = self._rows_for(observation.producer_run_id)
        readiness = policy.decide_readiness(
            current_rows=current_rows, required_sources_available=bool(current_rows))
        if readiness.status != policy.READINESS_NOT_READY or not readiness.blockers:
            return self._decision(DECISION_SUPPRESS, observation,
                                  (REASON_SUPPRESS_NO_BLOCKER,))

        baseline = self._select_baseline(observation)
        if baseline is None:
            # 找不到合格 baseline 就**不**降級成另一套分析，也不呼叫 LLM。
            return self._decision(
                DECISION_INSUFFICIENT, observation,
                (REASON_INSUFFICIENT_NO_BASELINE,),
                missing=({"evidence_type": "baseline_run",
                          "subject_id": observation.producer_run_id,
                          "status": "missing"},))

        return self._decision(
            DECISION_TRIGGER, observation, (REASON_TRIGGER_BLOCKERS,),
            baseline={"run_id": baseline.run_id, "manifest_id": baseline.manifest_id})

    def _decision(self, decision: str, observation: ProactiveObservation,
                  reasons: tuple[str, ...], *, baseline: dict[str, Any] | None = None,
                  missing: tuple[dict[str, Any], ...] = ()) -> TriggerDecision:
        return TriggerDecision(
            decision=decision, policy_id=POLICY_ID, policy_version=POLICY_VERSION,
            subject_identity={"run_id": observation.producer_run_id,
                              "manifest_id": observation.artifact_manifest_id},
            baseline_identity=baseline, reason_codes=reasons, missing_evidence=missing)

    def _rows_for(self, run_id: str) -> list[dict[str, Any]]:
        manager = DqeDbManager(self._dqe_db_path) if self._dqe_db_path else DqeDbManager()
        try:
            return manager.read_records_by_orchestration_run(
                run_id, package_id=DQE_PACKAGE_ID)
        except Exception:  # noqa: BLE001
            return []

    def _select_baseline(self, observation: ProactiveObservation) -> _RunFacts | None:
        """Baseline 由 policy 選，**不**由 LLM 選、**不** hard-code run id。

        必須早於 current、且不是同一個 manifest instance。
        """
        candidates = [r for r in self._all_runs()
                      if r.run_id != observation.producer_run_id
                      and r.manifest_id != observation.artifact_manifest_id
                      and r.row_count >= EXPECTED_DQE_ROW_COUNT
                      and (observation.observed_at is None or r.recorded_at is None
                           or r.recorded_at < observation.observed_at)]
        return candidates[-1] if candidates else None

    def _all_runs(self) -> list[_RunFacts]:
        saved, self._activation_boundary = self._activation_boundary, ""
        try:
            return self._recent_runs(limit=50)
        finally:
            self._activation_boundary = saved

    # ── assessment（重用 situation assessment，不經 ActionRegistry）───────────────────

    def assess(self, decision: TriggerDecision,
               observation: ProactiveObservation | None = None) -> dict[str, Any]:
        analysis_input: dict[str, Any] = {}
        if observation is not None:
            # Snapshot 必須保留 lifecycle unknowns：這裡原樣帶入
            # observation 的四個座標軸，不做任何升級。
            analysis_input = {
                "analysis_input_status": observation.analysis_input_status,
                "analysis_input_basis": observation.analysis_input_basis.to_json_dict(),
                "analysis_input_provenance": list(observation.analysis_input_provenance),
                "execution_completion_status": observation.execution_completion_status,
                "dispatch_status": observation.dispatch_status,
                "barrier_evidence_status": observation.barrier_evidence_status,
                "lifecycle_evidence_basis": dict(observation.lifecycle_evidence_basis),
            }
        request = situation_service.AssessmentRequest(
            current_run_id=str(decision.subject_identity.get("run_id")),
            baseline_run_id=str((decision.baseline_identity or {}).get("run_id")),
            question=None,
            dqe_db_path=str(self._dqe_db_path) if self._dqe_db_path else None,
            analysis_input=analysis_input,
        )
        llm = self._llm
        if self._cycle_budget is not None:
            llm = _BudgetedLlm(llm, self._cycle_budget)
        return situation_service.assess(request, llm=llm, store=self._store)

    # ── notification（內容只來自已持久化的 snapshot）──────────────────

    def build_notification(self, decision: TriggerDecision,
                           assessment: dict[str, Any]) -> NotificationRequest:
        snapshot = assessment.get("snapshot") or {}
        readiness = snapshot.get("readiness") or {}
        blockers = snapshot.get("blockers") or []
        improvements = snapshot.get("improvements") or []
        regressions = snapshot.get("regressions") or []
        unknowns = snapshot.get("unknowns") or []
        missing = snapshot.get("missing_evidence") or []
        checks = snapshot.get("recommended_checks") or []

        analysis_input = assessment.get("analysis_input") or {}
        lines = [
            f"Terminux detected analysis-ready evidence for run "
            f"{decision.subject_identity.get('run_id')} "
            f"(baseline {(decision.baseline_identity or {}).get('run_id')}).",
            # 明說 lifecycle 沒被 durably 觀察到。絕不寫
            # "the run completed successfully" / "barrier passed"。
            f"Execution/dispatch lifecycle completion was not durably observed "
            f"(execution {analysis_input.get('execution_completion_status', 'unverified')}, "
            f"dispatch {analysis_input.get('dispatch_status', 'not_observed')}, "
            f"barrier {analysis_input.get('barrier_evidence_status', 'not_observed')}).",
            f"The non-authoritative Situation assessment found {len(blockers)} blockers.",
            f"readiness {readiness.get('status')} "
            f"(non-authoritative; formal signoff {readiness.get('formal_signoff_gate')})",
            f"comparability {assessment.get('comparability')}",
            f"blockers {len(blockers)}  improvements {len(improvements)}  "
            f"regressions {len(regressions)}",
        ]
        for blocker in blockers[:3]:
            lines.append(f"blocker: {blocker.get('text')}")
        if missing:
            lines.append(f"missing evidence: {len(missing)} item(s)")
        lines.append("root cause is not established by this evidence")
        if unknowns:
            lines.append(f"unknowns recorded: {len(unknowns)}")
        if checks:
            lines.append(f"suggested read-only check: {checks[0].get('description')}")
        lines.append(f"snapshot {assessment.get('snapshot_id')}")
        lines.append("proactive analysis only — nothing was executed and no approval "
                     "was requested")

        return NotificationRequest(
            level="warning",
            title="Proactive run review (non-authoritative)",
            message="\n".join(_clean(line) for line in lines),
            # proactive intelligence 通知**不含任何 action**。
            actions=[],
        )


class _BudgetedLlm:
    """在每次 provider call **之前** reserve，回應後依 metadata 記錄實際用量。

    預算不足時直接回 `None`——`situation_service` 會把它轉成誠實的失敗，
    不會產生 snapshot、也不會發通知。initial 與 bounded repair 共用同一份
    預算，因為它們在同一個 cycle 內。
    """

    def __init__(self, inner, budget) -> None:
        self._inner, self._budget = inner, budget

    def ask(self, prompt, *, timeout=None, max_output_tokens=None):
        result = self.ask_detailed(prompt, timeout=timeout,
                                   max_output_tokens=max_output_tokens)
        return getattr(result, "text", None)

    def ask_detailed(self, prompt, *, timeout=None, max_output_tokens=None):
        from terminux_engine.package_runtime.services import LlmAskResult

        requested = int(max_output_tokens or 0)
        reservation = self._budget.reserve(requested_output_tokens=requested)
        if not reservation.granted:
            return LlmAskResult(None, None, reservation.reason)
        detailed = getattr(self._inner, "ask_detailed", None)
        if callable(detailed):
            result = detailed(prompt, timeout=timeout,
                              max_output_tokens=max_output_tokens)
        else:
            # 沒有 detailed channel 時仍必須把 output budget 傳下去，
            # 否則 reserve 的宣告值與實際請求量會不一致。
            text = self._inner.ask(prompt, timeout=timeout,
                                   max_output_tokens=max_output_tokens)
            result = LlmAskResult(text, None, None)
        self._budget.record_usage(result.metadata)
        return result


def _clean(text: str) -> str:
    """移除 control character，並確保單行——避免被誤讀成系統 header。"""
    return "".join(ch for ch in str(text) if ch == " " or ch.isprintable()).strip()


__all__ = [
    "EXPECTED_DQE_ROW_COUNT",
    "POLICY_ID",
    "POLICY_VERSION",
    "REASON_INSUFFICIENT_INPUT",
    "REASON_INSUFFICIENT_MANIFEST",
    "REASON_INSUFFICIENT_NOT_COMPARABLE",
    "REASON_INSUFFICIENT_NO_BASELINE",
    "REASON_INSUFFICIENT_ROWS",
    "REASON_SUPPRESS_NOT_ORFS",
    "REASON_SUPPRESS_NO_BLOCKER",
    "REASON_TRIGGER_BLOCKERS",
    "SOURCE_KIND",
    "OrfsProactiveProvider",
]
