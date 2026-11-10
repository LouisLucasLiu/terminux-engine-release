# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""B1：method 宣告的 formal-proof 相依的編排者。

分工
----

* `FlowController` 只做兩件事：把 durable dependency intent 落盤，然後把
  CORE task 停在 `WAITING_DEPENDENCY`。它不知道 proof task 怎麼建立。
* 這支 coordinator 讀那份 intent，**透過既有的正式 API** 建立相依的
  formal-proof FlowTask（一樣走 draft／grant／reservation／Kernel），
  等它在操作層完成並產生 durable envelope 後恢復 CORE 的 compare。coordinator
  不讀 envelope 的 formal classification。
* harness 與 driver 只能送出 Work 並等結果：它們不建立 proof task，
  也不注入 envelope 或 proof 結果。

刻意保持窄
----------

這不是通用的 workflow DAG scheduler，也不接受任意 task 相依。它只認
`F.DEPENDENCY_FORMAL_PAIR_PROOF` 這一種、由已釘住的 method extension 宣告的
相依；其餘 template（含 setup-margin 與 legacy）完全不經過這裡。

recovery 採「事件驅動 ＋ 啟動時對帳」兩段，不做輪詢排程。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks import flow_models as F

_LOG = logging.getLogger(__name__)

#: intent 內必須齊備的欄位；缺任何一個都不建立 proof task。
REQUIRED_INTENT_FIELDS: tuple[str, ...] = (
    "work_id", "attempt_id", "core_task_id", "method_extension",
    "proof_descriptor_sha256", "baseline", "candidate", "input_manifest")

#: coordinator 對一次相依的處置。
ACTION_CREATED = "PROOF_TASK_CREATED"
ACTION_REUSED = "PROOF_TASK_REUSED"
ACTION_RESUMED = "CORE_RESUMED"
ACTION_FAILED = "DEPENDENCY_FAILED"
ACTION_NOOP = "NO_ACTION"


class DependencyCoordinatorError(RuntimeError):
    """相依無法被編排。CORE 以 UNVERIFIABLE 收束，不永久停在等待。"""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


@dataclass(frozen=True)
class DependencyOutcome:
    action: str
    core_task_id: str
    proof_task_id: str | None = None
    reason: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {"action": self.action, "core_task_id": self.core_task_id,
                "proof_task_id": self.proof_task_id, "reason": self.reason}


def _valid_intent(intent: Any) -> dict[str, Any]:
    if not isinstance(intent, dict):
        raise DependencyCoordinatorError("the dependency intent is not a document")
    if intent.get("kind") != F.DEPENDENCY_FORMAL_PAIR_PROOF:
        raise DependencyCoordinatorError(
            "only a method-declared formal pair proof is coordinated here",
            kind=intent.get("kind"))
    missing = sorted(name for name in REQUIRED_INTENT_FIELDS if not intent.get(name))
    if missing:
        raise DependencyCoordinatorError(
            "the dependency intent is incomplete", missing=missing)
    for side in ("baseline", "candidate"):
        row = intent.get(side)
        if not isinstance(row, dict) or not row.get("sha256") or not row.get("path"):
            raise DependencyCoordinatorError(
                f"the {side} proof input is not fully identified")
    return intent


class DependencyCoordinator:
    """把 dependency intent 變成一個正式的 proof FlowTask，再恢復 CORE。"""

    def __init__(self, *, flow_service: Any, controller: Any) -> None:
        self._flow = flow_service
        self._controller = controller

    # ── 建立 proof task ──────────────────────────────────────────────

    def ensure_proof_task(self, intent: dict[str, Any]) -> DependencyOutcome:
        """為這份 intent 建立（或取回）相依的 proof task。

        identity 由 Work／attempt／descriptor／兩側 netlist hash 推導，並交給
        既有的 durable draft idempotency——同一份 intent 重送只會得到同一個
        task，不會出現第二個 child 或第二次 reservation。
        """
        intent = _valid_intent(intent)
        with self._coordination_lock():
            if not self._core_accepts_dependency(intent["core_task_id"]):
                return DependencyOutcome(ACTION_NOOP, intent["core_task_id"],
                                         reason="the CORE task was cancelled or settled")
            proposal = self._proof_proposal(intent)
            existing = self._find_proof_task(intent, proposal)
            if existing is not None:
                return DependencyOutcome(ACTION_REUSED, intent["core_task_id"],
                                         existing["task_id"])
            created = self._flow.create_package_flow_task(
                question=(f"formal equivalence for {intent['work_id']}"
                          f"/{intent['attempt_id']}"),
                profile_id=self._profile_id(intent), proposal=proposal)
            if created.get("status") != "ok":
                raise DependencyCoordinatorError(
                    "the dependent proof task could not be created: "
                    + str(created.get("message") or created.get("error_code") or ""),
                    detail=created)
            task = created.get("task") or {}
            task_id = str(task.get("task_id") or "")
            if not task_id:
                raise DependencyCoordinatorError(
                    "the dependent proof task carries no identity")
            self._register_proof_task(intent, task_id, task.get("task_digest"))
            if not self._core_accepts_dependency(intent["core_task_id"]):
                self._cancel_proof_task(task_id, task.get("task_digest"))
                return DependencyOutcome(ACTION_NOOP, intent["core_task_id"], task_id,
                                         "the CORE task was cancelled before proof start")
            if created.get("idempotent"):
                return DependencyOutcome(ACTION_REUSED, intent["core_task_id"], task_id)
            started = self._flow.start_flow_task(task_id, task.get("task_digest"))
            if started.get("status") not in {"ok", "queued", "started"}:
                raise DependencyCoordinatorError(
                    "the dependent proof task did not start", detail=started)
            return DependencyOutcome(ACTION_CREATED, intent["core_task_id"], task_id)

    def _coordination_lock(self) -> Any:
        """取得 service 的既有 dependency 接線鎖；單元測試可沒有它。"""
        import contextlib

        lock = getattr(self._flow, "_dependency_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    def _core_accepts_dependency(self, core_task_id: str) -> bool:
        checker = getattr(self._flow, "_dependency_core_accepts", None)
        return bool(checker(core_task_id)) if callable(checker) else True

    def _find_proof_task(self, intent: dict[str, Any], proposal: dict[str, Any]
                         ) -> dict[str, Any] | None:
        finder = getattr(self._flow, "_find_dependency_proof_task", None)
        if not callable(finder):
            return None
        return finder(intent["core_task_id"], proposal["source"]["draft_id"])

    def _register_proof_task(self, intent: dict[str, Any], task_id: str,
                             task_digest: Any) -> None:
        register = getattr(self._flow, "_register_dependency_proof_task", None)
        if callable(register):
            register(intent["core_task_id"], task_id, str(task_digest or ""))

    def _cancel_proof_task(self, task_id: str, task_digest: Any) -> None:
        cancel = getattr(self._flow, "cancel_flow_task", None)
        if callable(cancel) and task_digest:
            cancel(task_id, str(task_digest))

    def _profile_id(self, intent: dict[str, Any]) -> str:
        profile = self._flow.profile_for_task(intent["core_task_id"])
        return str(getattr(profile, "profile_id", ""))

    def _native_tool(self, intent: dict[str, Any]) -> dict[str, str] | None:
        """host-native Profile 授權的 formal Yosys pin；image Profile 回 `None`。

        只取 CORE 同一份已核准 Profile 的宣告，沒有預設值或 fallback——工具
        邊界從不由 coordinator 自行挑選。host-native Profile 未授權 `FORMAL_YOSYS` 即
        fail closed。
        """
        profile = self._flow.profile_for_task(intent["core_task_id"])
        if not getattr(profile, "is_native", False):
            return None
        binding = getattr(profile, "native_tool_binding", None)
        pin = (getattr(binding, "tools", None) or {}).get("FORMAL_YOSYS")
        if pin is None:
            raise DependencyCoordinatorError(
                "the host-native Profile authorizes no FORMAL_YOSYS binary for the "
                "dependent proof", core_task_id=intent.get("core_task_id"))
        return pin.to_json_dict()

    def _proof_proposal(self, intent: dict[str, Any]) -> dict[str, Any]:
        """由 intent 組出 package tool 的 formal_pair 提案。

        這**不是**第二份 manual proposal：它沒有 `origin`，也沒有
        `method_identity`，因此不可能被 Work 當成使用者提案。
        """
        from terminux_engine.expert_ai.eap3 import digest_json

        native_tool = self._native_tool(intent)
        if native_tool is None:
            # container 契約的 proof 不再建立；不改走 host-native、也不以
            # Docker 代跑，CORE 依既有規則收束為 UNVERIFIABLE。
            from terminux_engine.orchestration import legacy_docker_execution as LDE

            raise DependencyCoordinatorError(
                LDE.disabled_message(LDE.PATH_FORMAL_PAIR),
                core_task_id=intent.get("core_task_id"),
                error_code=LDE.LEGACY_DOCKER_EXECUTION_DISABLED)
        formal = {
            "work_id": intent["work_id"], "attempt_id": intent["attempt_id"],
            "package_identity": {"package_id": "orfs_run_intelligence",
                                 "version": "eap3.orfs.v1"},
            "method_extension": intent["method_extension"],
            # `FP._artifact()` 要求欄位恰好是 {role, path, sha256}；role 由
            # intent 帶來，不得在這裡丟掉或自行改寫。
            "baseline": {"role": intent["baseline"]["role"],
                         "path": intent["baseline"]["path"],
                         "sha256": intent["baseline"]["sha256"]},
            "candidate": {"role": intent["candidate"]["role"],
                          "path": intent["candidate"]["path"],
                          "sha256": intent["candidate"]["sha256"]},
            "input_manifest": dict(intent["input_manifest"]),
            "top_module": intent.get("top_module", "gcd"),
            # 元件庫由 intent 帶來，coordinator 不自行挑檔案。
            "cell_libraries": list(intent.get("cell_libraries") or []),
            "native_tool": native_tool,
            "production_proof": True,
        }
        canonical = {"formal_pair": formal}
        # proof task root 必須落在 CORE attempt 已授權的 writable root 底下；
        # 這不是新授權，是把同一份授權帶給相依工作。
        writable_roots = [str(item) for item in (
            (intent.get("authorization") or {}).get("writable_roots") or [])
            if isinstance(item, str) and item]
        if not writable_roots:
            raise DependencyCoordinatorError(
                "the dependency intent declares no authorized writable root")
        authorization = dict(intent.get("authorization") or {})
        return {
            "package": {"package_id": "orfs_run_intelligence",
                        "method": "CORE_UTILIZATION"},
            "authorization_writable_roots": writable_roots,
            # Work 身分必須跟著 proof task 走：`batch_ledger.task_identity()`
            # 由此把這個相依工作的 reservation 結算到**同一個** attempt 上。
            # 少了它，proof 的 tool launch 與 formal proof launch 會掛在
            # work_id=None 底下，CORE 的 attempt 結算就看不到它們。
            "work_source": {
                "work_id": intent["work_id"],
                "attempt_id": intent["attempt_id"],
                "parent_authorization_digest": authorization.get(
                    "authorization_digest"),
            },
            "package_tool": {"formal_pair": formal},
            "canonical_proposal": canonical,
            "source": {"draft_id": "dep:" + digest_json({
                "work_id": intent["work_id"],
                "attempt_id": intent["attempt_id"],
                "descriptor": intent["proof_descriptor_sha256"],
                "baseline": intent["baseline"]["sha256"],
                "candidate": intent["candidate"]["sha256"]})[:32],
                       "proposal_digest": digest_json(canonical)},
            "input": intent.get("input") or {},
        }

    # ── proof 完成之後 ───────────────────────────────────────────────

    def settle(self, core_task_id: str, intent: dict[str, Any]) -> DependencyOutcome:
        """proof 結束後：讓 controller 讀 durable envelope，再恢復或 fail closed。

        `resume_after_dependency()` 自己會再解析一次 envelope，因此這裡不傳
        任何 proof 結果進去——coordinator 不是證據來源。
        """
        context = self._flow._deps.request_context()
        live = self._flow._lookup_flow(core_task_id, context)
        if live is None:
            return DependencyOutcome(ACTION_NOOP, core_task_id,
                                     reason="the CORE task is not available")
        if (getattr(live, "status", None) != F.STATUS_WAITING_DEPENDENCY
                or getattr(live, "stop", False)):
            # 已經恢復過或已終結：重覆通知不得產生第二次 compare。
            return DependencyOutcome(ACTION_NOOP, core_task_id,
                                     reason=f"task is {live.status}")
        result = self._controller.resume_after_dependency(self._flow, live)
        if result.get("status") == "resumed":
            return DependencyOutcome(ACTION_RESUMED, core_task_id)
        if result.get("status") == "still_waiting":
            return self._fail(live, intent,
                              "the dependent proof completed without a durable envelope")
        if result.get("status") == "prerequisite_failed":
            # 必要的相依前置條件不成立：同樣不得永久停在 waiting。
            return self._fail(live, intent, str(
                result.get("reason")
                or "the formal proof prerequisite is not satisfied"))
        return DependencyOutcome(ACTION_NOOP, core_task_id,
                                 reason=result.get("status"))

    def _fail(self, live: Any, intent: dict[str, Any],
              reason: str) -> DependencyOutcome:
        """相依確定不成立：落盤並讓 CORE 以 UNVERIFIABLE 收束。

        **不**讓它永久停在 waiting——那會讓 Work 看起來還在進行。
        """
        from terminux_engine.bounded_execution.analysis_tasks import (
            flow_worker as W,
        )

        W.persist_fact_event(
            self._flow._deps.step_deps, live, live.task,
            event_type=F.EVENT_DEPENDENCY_FAILED,
            body={"body": {**dict(intent), "reason": reason}})
        with live.lock:
            live.dependency_wait = None
            self._flow._force_flow_terminal_locked(
                live, "OUTCOME_UNCONFIRMED", reason)
        return DependencyOutcome(ACTION_FAILED, live.task.task_id, reason=reason)

    # ── 啟動時對帳 ───────────────────────────────────────────────────

    def reconcile(self, waiting: list[dict[str, Any]]) -> list[DependencyOutcome]:
        """新 service 啟動時，把每一個仍在等待的 CORE task 推進一步。

        `waiting` 是由 durable events 重建出來的 `[{core_task_id, intent}]`。
        每個 task 只做**一次**動作：建立 proof task、等待既有 child、或在 child
        已終結後恢復／fail closed。
        已恢復或已終結的一律不動。
        """
        outcomes: list[DependencyOutcome] = []
        for row in waiting:
            core_task_id = str(row.get("core_task_id") or "")
            intent = row.get("intent")
            if not core_task_id or not isinstance(intent, dict):
                continue
            try:
                proposal = self._proof_proposal(_valid_intent(intent))
                proof = self._find_proof_task(intent, proposal)
                if proof is None:
                    created = self.ensure_proof_task(intent)
                    # durable create idempotency 可能在 restart 後才把既有 child
                    # 放回 live cache；若它其實已終結，這次就立刻依 envelope
                    # 收束，不等下一個無關事件來觸發 reconcile。
                    recovered = self._find_proof_task(intent, proposal)
                    if (created.action == ACTION_REUSED and recovered is not None
                            and recovered.get("status") in {
                                "COMPLETED", "FAILED", "BLOCKED", "CANCELLED",
                                "OUTCOME_UNCONFIRMED", "INTERRUPTED", "EXPIRED"}):
                        outcomes.append(self.settle(core_task_id, intent))
                    else:
                        outcomes.append(created)
                    continue
                if proof.get("status") not in {
                        "COMPLETED", "FAILED", "BLOCKED", "CANCELLED",
                        "OUTCOME_UNCONFIRMED", "INTERRUPTED", "EXPIRED"}:
                    outcomes.append(DependencyOutcome(
                        ACTION_NOOP, core_task_id, proof.get("task_id"),
                        "the dependent proof task is still active"))
                    continue
                outcomes.append(self.settle(core_task_id, intent))
            except DependencyCoordinatorError as exc:
                _LOG.warning("dependency reconciliation failed for %s: %s",
                             core_task_id, exc.reason)
                outcomes.append(DependencyOutcome(
                    ACTION_FAILED, core_task_id, reason=exc.reason))
        return outcomes


def waiting_dependencies(event_store: Any, *, member_id: str) -> list[dict[str, Any]]:
    """由 durable events 重建「還在等相依」的 CORE task。

    規則：看到 `DEPENDENCY_WAIT` 就記下；看到同一個 task 的
    `DEPENDENCY_SATISFIED`／`DEPENDENCY_FAILED`／`TERMINAL` 就移除。
    重啟後的狀態因此完全由事件決定，不靠任何記憶體。
    """
    waiting: dict[str, dict[str, Any]] = {}
    for event_type in (F.EVENT_DEPENDENCY_WAIT, F.EVENT_DEPENDENCY_SATISFIED,
                       F.EVENT_DEPENDENCY_FAILED, F.EVENT_TERMINAL):
        for event in event_store.query(user_id=member_id, event_type=event_type,
                                       order="asc"):
            payload = event.payload or {}
            task_id = str(payload.get("task_id") or "")
            if not task_id:
                continue
            if event_type == F.EVENT_DEPENDENCY_WAIT:
                body = payload.get("body") or payload
                waiting[task_id] = {"core_task_id": task_id, "intent": body}
            else:
                waiting.pop(task_id, None)
    return list(waiting.values())


__all__ = [
    "ACTION_CREATED", "ACTION_FAILED", "ACTION_NOOP", "ACTION_RESUMED",
    "ACTION_REUSED", "REQUIRED_INTENT_FIELDS", "DependencyCoordinator",
    "DependencyCoordinatorError", "DependencyOutcome", "waiting_dependencies",
]
