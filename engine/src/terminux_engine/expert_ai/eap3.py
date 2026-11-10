# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""持久化工程工作與有界多輪協作。

共同層只管理 work、角色決策、parent authorization 與總預算。Package
負責驗證／編譯方案；Runtime 負責每輪建立新的 task／grant／binding 及執行。
本模組不認識 ORFS、特定參數或工具命令。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from terminux_engine.expert_ai.batch_ledger import configured_ledger
from terminux_engine.store.event_store import SessionEvent

_LOG = logging.getLogger(__name__)

SCHEMA_VERSION = "engineering_work.v1"
EVENT_WORK = "engineering_work.event.v1"

TERMINAL_STATUSES = frozenset({"COMPLETED", "STOPPED", "CANCELLED", "UNCONFIRMED"})

#: 只在確定沒有 child 被建立時才可 RELEASED 的外部動作額度。child 一旦啟動，
#: timeout 與 tool error 都是已發生的用量，一律 CONSUMED。
_CHILD_LAUNCH_BUDGET_KEYS = frozenset({
    "tool_launches", "formal_proof_launches", "candidate_materializations"})
#: Runtime 明確表示 child 從未建立的結果碼；其他失敗一律保留給對帳，不猜測。
_CHILD_NEVER_STARTED = frozenset({"CHILD_DRAFT_FAILED", "CHILD_GRANT_FAILED"})
ACTIVE_STATUSES = frozenset({
    "PREPARING", "ANALYZING", "COLLECTING_EVIDENCE", "EXECUTING",
    "ANALYZING_RESULT", "WAITING_APPROVAL", "WAITING_USER", "PAUSED",
    "STOPPING", "RECONCILIATION_REQUIRED",
})
GLOBAL_DECISIONS = frozenset({
    "DELEGATE", "COLLECT_EVIDENCE", "REQUEST_REVISION", "SUBMIT_PROPOSAL",
    "EXECUTE", "WAIT_USER", "STOP",
})
#: Phase A：手動 proposal 的 origin 與核准狀態。這條路徑**不會**派送
#: provider、不會產生 Expert response 或 Global decision，也不會建立
#: authorization、grant 或 task——它只把一筆使用者送出的 proposal 正式
#: 驗證、canonicalize 並持久化，等待獨立的核准。
PROPOSAL_ORIGIN_USER = "USER_SUBMITTED"
PROPOSAL_PENDING_APPROVAL = "PENDING_APPROVAL"
PROPOSAL_APPROVED = "APPROVED"

DOMAIN_KINDS = frozenset({
    "PROPOSAL", "NEED_EVIDENCE", "NO_PROPOSAL", "UNSUPPORTED",
    "EXECUTION_ANALYSIS",
})

#: Global `STOP(criteria_met=true)` 的達標核驗。Global 的宣稱只是判斷，
#: Work 能否收為 `COMPLETED` 取決於實際支撐它的 durable 工程證據。
CRITERIA_VERIFICATION_SCHEMA = "terminux.work_criteria_verification.v1"
CRITERIA_MET_EVIDENCE_UNVERIFIED = "GLOBAL_CRITERIA_MET_EVIDENCE_UNVERIFIED"
CRITERIA_MET_CONTRADICTS_EVIDENCE = "GLOBAL_CRITERIA_MET_CONTRADICTS_EVIDENCE"
#: 結果階段的 REQUEST_REVISION 同時宣稱 `criteria_met=true`；非 CORE 方法不放行。
REVISION_CLAIMS_CRITERIA_MET = "STOP_REVISION_CLAIMS_CRITERIA_MET"
#: FlowTask runtime 回報 `COMPLETED`；isolated fixture runtime 回報 `SUCCEEDED`。
_ATTEMPT_SUCCESS_STATUSES = frozenset({"COMPLETED", "SUCCEEDED"})


def _qualifies_from_engineering_result(result: dict[str, Any]) -> bool | None:
    """`result['available']` 已成立時，判定這次候選是否已達到可判定為
    「工程目標已達成」的品質門檻。

    規則與 `verify_criteria_met_claim()` 判定 `repair_status`／`target_slack_met`／
    `timing_repaired`／`selection_eligible`／`protected_metric_regressions` 的
    contradicted 條件同源——這裡只是把它翻成單一布林，供**任一輪** attempt
    （不限最後一輪）使用。`None` 代表證據不足以判定，不得默認任一方向。
    """
    repair_status = result.get("repair_status")
    if repair_status == "NOT_REPAIRED":
        return False
    if repair_status != "REPAIRED":
        return None  # 涵蓋 UNVERIFIABLE 與未知值。
    if result.get("target_slack_ns") is not None and result.get("target_slack_met") is None:
        return None
    if result.get("target_slack_met") is False:
        return False
    if result.get("timing_repaired") is False:
        return False
    if result.get("selection_eligible") is False:
        return False
    regressions = result.get("protected_metric_regressions")
    if isinstance(regressions, list) and regressions:
        return False
    return True


def _attempt_candidate_qualifies(attempt: dict[str, Any]) -> bool | None:
    """一個 attempt 的候選是否有**獨立於 Global 宣稱**的證據支持它已達標。

    只認 Package／工具算出來的判定欄位，不看 Global 的文字宣稱。因此這裡
    刻意與 `verify_criteria_met_claim()` 在同一種「Package 沒有算出判定
    欄位」情形下的行為不同：那裡核對的是「這次宣稱有沒有被證據推翻」（沒有
    可推翻的證據就當作不矛盾），這裡回答的是「這個候選有沒有被證據**證實**」
    （沒有可證實的證據就是 `None`，不得當作已達標）。兩者是不同問題，答案
    也不強求一致。
    """
    outcome = attempt.get("outcome") if isinstance(attempt.get("outcome"), dict) else None
    if outcome is None or attempt.get("status") not in _ATTEMPT_SUCCESS_STATUSES:
        return None
    engineering = attempt.get("engineering_result")
    if isinstance(engineering, dict):
        if engineering.get("terminal_status") != "COMPLETED":
            return None
        steps = engineering.get("steps") if isinstance(engineering.get("steps"), dict) else {}
        if any(status != "SUCCEEDED" for status in steps.values()):
            return None
        if not engineering.get("available"):
            return None
        return _qualifies_from_engineering_result(engineering)
    acceptance = outcome.get("acceptance") if isinstance(outcome.get("acceptance"), dict) else None
    if acceptance is None or acceptance.get("complete") is not True:
        return None
    if acceptance.get("passed") is False or acceptance.get("criteria_met") is False:
        return False
    if acceptance.get("passed") is True and acceptance.get("criteria_met") is True:
        return True
    return None


#: 尚在執行、還沒有終局結果可供判定的 attempt 狀態。
_ATTEMPT_ACTIVE_STATUSES = frozenset(
    {"DISPATCHING", "EXECUTING", "ANALYZING_RESULT", "RECONCILIATION_REQUIRED"})

CANDIDATE_QUALIFICATION_SCHEMA = "terminux.work_candidate_qualification_history.v1"


def candidate_qualification_history(work: dict[str, Any]) -> dict[str, Any]:
    """逐 attempt 摘要哪個候選已具備獨立證據、哪個仍在執行、有沒有被保留。

    純函式，只讀 Work snapshot 已保存的欄位，因此獨立 reader 能用同一份
    snapshot 重建相同結論。判定規則與 `verify_criteria_met_claim()` 同源
    （`_attempt_candidate_qualifies()`），差別是這裡檢視**每一輪** attempt，
    不是只看最後一輪。

    這裡**不**做任何跨輪指標比較或加權排序——那是既有 per-run selection
    policy（`selection.py`）與 comparability policy（`comparison.py`）的職責，
    這裡不重複、也不覆蓋它們的結論。已保留的候選（`retained_attempt_id`）
    固定是**第一個**被獨立證據判定為合格的 attempt；之後即使又出現合格的
    attempt，也只會被列進 `additional_qualified_attempt_ids`，不會自動頂替
    retained——沒有既有規則可以在兩個都合格的候選之間評分選優，就不發明一個。
    """
    entries: list[dict[str, Any]] = []
    retained_attempt_id: str | None = None
    additional_qualified: list[str] = []
    active_attempt_id: str | None = None
    for attempt in work.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        attempt_id = attempt.get("attempt_id")
        if attempt.get("status") in _ATTEMPT_ACTIVE_STATUSES:
            active_attempt_id = attempt_id
            entries.append({"attempt_id": attempt_id, "state": "IN_PROGRESS",
                            "qualified": None})
            continue
        qualifies = _attempt_candidate_qualifies(attempt)
        if qualifies is True:
            state = "QUALIFIED"
            if retained_attempt_id is None:
                retained_attempt_id = attempt_id
            else:
                additional_qualified.append(attempt_id)
        elif qualifies is False:
            state = "NOT_QUALIFIED"
        else:
            state = "EVIDENCE_INSUFFICIENT"
        entries.append({"attempt_id": attempt_id, "state": state, "qualified": qualifies})

    closed_entries = [e for e in entries if e["state"] != "IN_PROGRESS"]
    no_eligible_candidate = (bool(closed_entries) and retained_attempt_id is None
                             and active_attempt_id is None)
    return {
        "schema_version": CANDIDATE_QUALIFICATION_SCHEMA,
        "attempts": entries,
        "retained_attempt_id": retained_attempt_id,
        "additional_qualified_attempt_ids": additional_qualified,
        "active_attempt_id": active_attempt_id,
        "no_eligible_candidate": no_eligible_candidate,
    }


#: Global 在終局決策明確指名 `selected_attempt_id`，但那個 attempt 找不到
#: （不存在、或屬於另一個 Work——兩者在只查本 Work `attempts` 時是同一種
#: 觀測結果）。
SELECTED_ATTEMPT_NOT_FOUND = "SELECTED_ATTEMPT_NOT_FOUND"
#: 指名的 attempt 仍在執行中，還沒有終局結果可供核驗。
SELECTED_ATTEMPT_STILL_EXECUTING = "SELECTED_ATTEMPT_STILL_EXECUTING"
#: 指名的 attempt 身分有效、已關閉，但候選本身不具備採用資格（proof 不
#: 成立、品質退步，或必要證據不足以判定）。這與「達不達標」是不同問題：
#: 即使這次是誠實的 `criteria_met=false` STOP，一個真正不合格或資格無法
#: 驗證的候選也不能被正式採用——見 `_consume_global()`。
SELECTED_ATTEMPT_NOT_QUALIFIED = "SELECTED_ATTEMPT_NOT_QUALIFIED"


def _resolve_selected_attempt(
    work: dict[str, Any], decision: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """依 `decision.get('selected_attempt_id')` 解析 Global 明確選定的 attempt。

    回傳 `(attempt, denial_code)`：
    - 沒有提供 `selected_attempt_id` → `(None, None)`。呼叫端沿用既有「核對
      最後一輪」的行為；這裡刻意不預設任何 fallback，避免「沒有明確選擇」
      與「明確選了最後一輪」在這個函式裡被混成同一種結果。
    - 提供了但在本 Work 找不到、或屬於另一個 Work → `(None,
      SELECTED_ATTEMPT_NOT_FOUND)`。
    - 找到了但仍在執行中 → `(None, SELECTED_ATTEMPT_STILL_EXECUTING)`。
    - 找到且已關閉 → `(attempt, None)`——**只解析身分**，不在這裡判定它
      合不合格；合不合格是 `_verify_attempt_criteria_claim()` 的職責，兩者
      刻意分開（選擇一個候選不等於它就達標）。

    找不到／仍在執行時一律回傳明確的拒絕理由，呼叫端不得改用其他 attempt
    （例如靜默退回 `attempts[-1]`）頂替。
    """
    selected_id = decision.get("selected_attempt_id")
    if selected_id is None:
        return None, None
    if not isinstance(selected_id, str) or not selected_id:
        return None, SELECTED_ATTEMPT_NOT_FOUND
    for attempt in work.get("attempts") or []:
        if isinstance(attempt, dict) and attempt.get("attempt_id") == selected_id:
            if attempt.get("status") in _ATTEMPT_ACTIVE_STATUSES:
                return None, SELECTED_ATTEMPT_STILL_EXECUTING
            return attempt, None
    return None, SELECTED_ATTEMPT_NOT_FOUND


def _verify_attempt_criteria_claim(
    work: dict[str, Any], decision: dict[str, Any], attempt: dict[str, Any] | None,
) -> dict[str, Any]:
    """`verify_criteria_met_claim()` 的核心判定，核驗對象由呼叫端直接指定。

    抽出成參數化版本，讓「核對最後一輪」（`verify_criteria_met_claim()`）與
    「核對 Global 明確選定的某一輪」（`_resolve_selected_attempt()` 之後）
    共用同一套規則，不重複維護兩份判定邏輯。這個函式本身不對外公開：它的
    輸出形狀會隨 `attempt` 不同而不同，不適合單獨當成穩定介面。
    """
    unverified: list[str] = []
    contradicted: list[str] = []
    engineering = attempt.get("engineering_result") if attempt else None
    if attempt is None:
        unverified.append("NO_SUPPORTING_ATTEMPT")
    else:
        outcome = attempt.get("outcome") if isinstance(attempt.get("outcome"), dict) else None
        if outcome is None:
            unverified.append("SUPPORTING_ATTEMPT_OUTCOME_MISSING")
        if attempt.get("status") not in _ATTEMPT_SUCCESS_STATUSES:
            unverified.append("SUPPORTING_ATTEMPT_NOT_SUCCEEDED")
        if isinstance(engineering, dict):
            if engineering.get("terminal_status") != "COMPLETED":
                unverified.append("SUPPORTING_TASK_NOT_COMPLETED")
            steps = engineering.get("steps") if isinstance(engineering.get("steps"), dict) else {}
            if any(status != "SUCCEEDED" for status in steps.values()):
                unverified.append("SUPPORTING_TASK_STEP_NOT_SUCCEEDED")
            if engineering.get("available"):
                # 只有 Package acceptance 存在時才有判定欄位可核對。
                # Package 的正式驗收結論優先：`timing_repaired=True` 只代表 setup
                # 違規消除，不能蓋過 NOT_REPAIRED／UNVERIFIABLE。
                repair_status = engineering.get("repair_status")
                if repair_status == "UNVERIFIABLE":
                    unverified.append("PACKAGE_ACCEPTANCE_UNVERIFIABLE")
                elif repair_status == "NOT_REPAIRED":
                    contradicted.append("PACKAGE_ACCEPTANCE_NOT_REPAIRED")
                elif repair_status != "REPAIRED":
                    unverified.append("PACKAGE_ACCEPTANCE_STATUS_UNKNOWN")
                # Profile 宣告了 target 時，REPAIRED 也必須有明確的達標事實。
                if (engineering.get("target_slack_ns") is not None
                        and engineering.get("target_slack_met") is None):
                    unverified.append("TARGET_SLACK_EVIDENCE_MISSING")
                if engineering.get("target_slack_met") is False:
                    contradicted.append("TARGET_SLACK_NOT_MET")
                if engineering.get("timing_repaired") is False:
                    contradicted.append("TIMING_NOT_REPAIRED")
                if engineering.get("selection_eligible") is False:
                    contradicted.append("CANDIDATE_NOT_SELECTION_ELIGIBLE")
                regressions = engineering.get("protected_metric_regressions")
                if isinstance(regressions, list) and regressions:
                    contradicted.append("PROTECTED_METRIC_REGRESSION")
        elif outcome is not None and isinstance(outcome.get("acceptance"), dict):
            # 沒有 FlowTask 工程摘要的 runtime（isolated fixture）以其 acceptance 為證據。
            acceptance = outcome["acceptance"]
            if acceptance.get("complete") is not True:
                unverified.append("SUPPORTING_ACCEPTANCE_INCOMPLETE")
            if acceptance.get("passed") is False:
                contradicted.append("ACCEPTANCE_NOT_PASSED")
            if acceptance.get("criteria_met") is False:
                contradicted.append("ACCEPTANCE_CRITERIA_NOT_MET")
        else:
            unverified.append("SUPPORTING_ENGINEERING_EVIDENCE_MISSING")
    # 證據本身不可用時，其中的判定欄位也不足以作為「矛盾」的依據。
    if unverified:
        result, codes = "UNVERIFIED", unverified
    elif contradicted:
        result, codes = "CONTRADICTED", contradicted
    else:
        result, codes = "VERIFIED", []
    return {
        "schema_version": CRITERIA_VERIFICATION_SCHEMA,
        "decision_id": decision.get("decision_id"),
        "claimed_criteria_met": bool(decision.get("criteria_met", False)),
        "outcome": result,
        "reason_codes": codes,
        "attempt_id": attempt.get("attempt_id") if attempt else None,
        "attempt_status": attempt.get("status") if attempt else None,
        "task_id": attempt.get("task_id") if attempt else None,
        "task_digest": attempt.get("task_digest") if attempt else None,
        "outcome_digest": attempt.get("outcome_digest") if attempt else None,
        "engineering_result_digest": attempt.get("engineering_result_digest") if attempt else None,
        "engineering_result_available": (bool(engineering.get("available"))
                                         if isinstance(engineering, dict) else None),
        "task_terminal_status": (engineering.get("terminal_status")
                                 if isinstance(engineering, dict) else None),
    }


def verify_criteria_met_claim(work: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """核對 Global 的達標宣稱是否有可用、不矛盾的工程證據支撐。

    純函式，只讀 Work snapshot 已保存的欄位，因此獨立 reader 可由同一份
    snapshot 重建相同結論。核驗對象是最近一次 attempt——Global 結果決策
    所依據的那一輪；不要求歷史 attempt 都成功。

    只使用證據**已經說出口**的事實：Task／attempt 終態、step 狀態，以及
    Package 已算好的判定欄位（Work 層不重新解讀 Package 語意）。Package
    acceptance 存在時，`repair_status` 必須是 `REPAIRED`；Profile 宣告的
    target 必須有 `target_slack_met=True`。欄位缺席不等於通過；Work 的文字型
    criteria 仍屬 Global 的判斷，不在這裡評分。

    回傳 `outcome`：`VERIFIED`、`UNVERIFIED`（證據不足或不可用）或
    `CONTRADICTED`（證據明確表示未達標）。

    這個函式**固定**核對最後一輪 attempt，簽章與輸出形狀維持不變（既有
    installed qualification 腳本會拿它與歷史 persisted 結果逐位元組比較）。要核對
    Global 明確選定的某一輪，改用 `_resolve_selected_attempt()` 取得該
    attempt 後呼叫 `_verify_attempt_criteria_claim()`。
    """
    attempts = work.get("attempts") or []
    attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else None
    return _verify_attempt_criteria_claim(work, decision, attempt)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class WorkError(ValueError):
    """Engineering Work 穩定錯誤。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkScope:
    member_id: str
    project_id: str
    origin_session_id: str
    project_source: str
    block: str

    def to_json_dict(self) -> dict[str, str]:
        return {
            "member_id": self.member_id,
            "project_id": self.project_id,
            "origin_session_id": self.origin_session_id,
            "project_source": self.project_source,
            "block": self.block,
        }


@dataclass(frozen=True)
class PackageCapability:
    package_id: str
    capability: str
    version: str
    proposal_schema_version: str
    validator_version: str
    validate: Callable[[dict[str, Any]], dict[str, Any]]
    compile_plan: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    baseline_requirements: Callable[[dict[str, Any]], dict[str, Any]]
    authorization_check: Callable[
        [dict[str, Any], dict[str, Any]], dict[str, Any] | None]
    proposal_contract: dict[str, Any] | None = None
    execution_kind: str | None = None

    def descriptor(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "package_id": self.package_id,
            "capability": self.capability,
            "version": self.version,
            "proposal_schema_version": self.proposal_schema_version,
            "validator_version": self.validator_version,
        }
        if self.proposal_contract is not None:
            result["proposal_contract"] = self.proposal_contract
        if self.execution_kind is not None:
            result["execution_kind"] = self.execution_kind
        return result


class WorkPackageRegistry:
    """Engineering Work 的 Package 擴充目錄；共同層只依 identity 查找。"""

    def __init__(self, packages: tuple[PackageCapability, ...] = ()) -> None:
        self._packages: dict[str, PackageCapability] = {}
        for package in packages:
            self.register(package)

    def register(self, package: PackageCapability) -> None:
        if package.package_id in self._packages:
            raise WorkError("PACKAGE_ID_DUPLICATE", package.package_id)
        self._packages[package.package_id] = package

    def get(self, package_id: str) -> PackageCapability:
        package = self._packages.get(package_id)
        if package is None:
            raise WorkError("PACKAGE_NOT_AVAILABLE", package_id)
        return package

    def descriptors(self) -> list[dict[str, Any]]:
        return [self._packages[key].descriptor() for key in sorted(self._packages)]


class WorkExpertRouter(Protocol):
    def call(
        self, *, role: str, capability: str, purpose: str,
        request: dict[str, Any], request_id: str,
    ) -> dict[str, Any]: ...


class WorkRuntime(Protocol):
    def prepare_input(
        self, *, work: dict[str, Any], request_id: str,
    ) -> dict[str, Any]: ...

    def validate_baseline(
        self, *, work: dict[str, Any], reference: dict[str, Any],
        requirements: dict[str, Any],
    ) -> dict[str, Any]: ...

    def collect_evidence(
        self, *, work: dict[str, Any], request: dict[str, Any], request_id: str,
    ) -> dict[str, Any]: ...

    def poll_evidence(
        self, *, work: dict[str, Any], collection: dict[str, Any],
    ) -> dict[str, Any]: ...

    def execute(
        self, *, work: dict[str, Any], proposal: dict[str, Any],
        execution_plan: dict[str, Any], authorization: dict[str, Any],
        attempt_id: str, request_id: str,
    ) -> dict[str, Any]: ...

    def poll(self, *, work: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]: ...

    def cancel(self, *, work: dict[str, Any]) -> dict[str, Any]: ...


class WorkReader:
    """從 EventStore 重建 work；跨 session 唯讀仍綁定 member/project 規則。"""

    def __init__(self, event_store: Any) -> None:
        self._store = event_store

    def load(self, work_id: str, context: Any, *, allow_profile_fallback: bool = True) -> dict[str, Any] | None:
        events = self._store.query(
            user_id=str(context.member_id), event_type=EVENT_WORK, order="asc")
        selected = [event for event in events if event.payload.get("work_id") == work_id]
        if not selected:
            return None
        # EventStore 的時間排序可能因 daemon 重啟／時鐘回撥與寫入順序不同；
        # 同一 Work 以持久化 sequence 為準，避免舊用量覆蓋新的對帳結果。
        snapshot = max(enumerate(selected), key=lambda pair: (
            self._sequence(pair[1].payload), pair[0]))[1].payload.get("snapshot")
        if not isinstance(snapshot, dict):
            return None
        scope = snapshot.get("scope")
        if not isinstance(scope, dict):
            return None
        if scope.get("member_id") != context.member_id:
            return None
        declared = bool(getattr(context, "project_declared", True))
        if declared and scope.get("project_id") != context.project_id:
            return None
        if not declared and not (allow_profile_fallback and scope.get("project_source") == "profile"):
            return None
        return json.loads(json.dumps(snapshot))

    def list(self, context: Any) -> list[dict[str, Any]]:
        events = self._store.query(
            user_id=str(context.member_id), event_type=EVENT_WORK, order="asc")
        latest: dict[str, tuple[int, int, dict[str, Any]]] = {}
        for index, event in enumerate(events):
            payload = event.payload
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, dict) and isinstance(payload.get("work_id"), str):
                candidate = (self._sequence(payload), index, snapshot)
                if candidate[:2] > latest.get(payload["work_id"], (-1, -1, {}))[:2]:
                    latest[payload["work_id"]] = candidate
        rows = []
        for stored_id, (_sequence, _index, work) in latest.items():
            scope = work.get("scope")
            if not isinstance(scope, dict):
                continue
            declared = bool(getattr(context, "project_declared", True))
            readable = (
                scope.get("member_id") == context.member_id
                and ((declared and scope.get("project_id") == context.project_id)
                     or (not declared and scope.get("project_source") == "profile"))
            )
            if readable and work.get("schema_version") != "engineering_work.v1":
                # 未知 schema 僅提供受 scope 保護的識別資訊；不得解讀其狀態或授權。
                rows.append({
                    "work_id": stored_id, "status": "UNSUPPORTED_SCHEMA",
                    "objective": "", "revision": None,
                    "project_id": scope.get("project_id"), "block": scope.get("block"),
                    "updated_at": 0,
                    "stored_schema_version": work.get("schema_version"),
                })
            elif readable:
                rows.append({
                    "work_id": stored_id, "status": work.get("status"),
                    "objective": work.get("objective"), "revision": work.get("revision"),
                    "project_id": scope.get("project_id"), "block": scope.get("block"),
                    "updated_at": work.get("updated_at"),
                })
        return sorted(rows, key=lambda row: (row.get("updated_at") or 0, row["work_id"]), reverse=True)

    @staticmethod
    def _sequence(payload: dict[str, Any]) -> int:
        value = payload.get("sequence")
        if not isinstance(value, int) or isinstance(value, bool):
            snapshot = payload.get("snapshot")
            value = snapshot.get("sequence") if isinstance(snapshot, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else 0


class EngineeringWorkService:
    """一件工程目標的唯一狀態機與事件寫入者。"""

    def __init__(
        self, *, event_store: Any, scope_provider: Callable[[], Any],
        profile_resolver: Callable[[str], dict[str, Any]],
        packages: WorkPackageRegistry, experts: WorkExpertRouter,
        runtime: WorkRuntime, daemon_instance_id: str,
        submit: Callable[[Callable[[], None]], None] | None = None,
        notify: Callable[[dict[str, Any]], None] | None = None,
        poll_interval_sec: float = 0.1,
    ) -> None:
        self._store = event_store
        self._scope_provider = scope_provider
        self._profile_resolver = profile_resolver
        self._packages = packages
        self._experts = experts
        self._runtime = runtime
        self._daemon_instance_id = daemon_instance_id
        self._reader = WorkReader(event_store)
        self._lock = threading.RLock()
        self._submit = submit
        self._notify = notify
        self._poll_interval_sec = max(0.01, float(poll_interval_sec))
        self._scheduled: set[str] = set()

    def create(
        self, *, objective: str, profile_id: str, package_id: str,
        constraints: list[str] | None = None, criteria: list[str] | None = None,
        block: str | None = None, method: str | None = None,
        source_root: str | None = None,
        baseline_reference: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(objective, str) or not objective.strip():
            return self._error("WORK_OBJECTIVE_INVALID", "objective must be non-empty")
        if not isinstance(profile_id, str) or not profile_id:
            return self._error("WORK_PROFILE_INVALID", "profile_id must be non-empty")
        try:
            package = self._packages.get(package_id)
        except WorkError as exc:
            return self._error(exc.code, exc.message)
        profile = self._profile_resolver(profile_id)
        if not isinstance(profile, dict) or profile.get("status") != "ok":
            return self._error("WORK_PROFILE_UNAVAILABLE", "profile is unavailable or out of scope")
        context = self._scope_provider()
        project_id = str(profile.get("project_id") or "")
        if not project_id:
            return self._error("WORK_PROJECT_INVALID", "profile has no project")
        if getattr(context, "project_declared", True) and context.project_id != project_id:
            return self._error("WORK_PROJECT_MISMATCH", "profile project differs from the declared session project")
        effective_block = block or str((profile.get("environment") or {}).get("block") or "")
        if not effective_block:
            return self._error("WORK_BLOCK_REQUIRED", "one work must be bound to one block")
        if source_root is not None:
            if not isinstance(source_root, str) or not Path(source_root).is_absolute():
                return self._error("WORK_SOURCE_INVALID", "source_root must be an absolute path")
            fixed_source = str((profile.get("environment") or {}).get("source_root") or "")
            if not fixed_source or Path(source_root).resolve() != Path(fixed_source).resolve():
                return self._error(
                    "WORK_SOURCE_OUT_OF_SCOPE",
                    "source_root must match the selected engineering environment")
        stable_request = request_id or "work-create:" + digest_json({
            "session": context.session_id, "profile_id": profile_id,
            "package_id": package_id, "objective": objective,
            "source_root": source_root,
        })
        existing = self._find_request(stable_request, context)
        if existing is not None:
            return {"status": "ok", "idempotent": True, "work": self._public(existing)}
        now = time.time()
        work_id = "work_" + uuid.uuid4().hex
        scope = WorkScope(
            member_id=str(context.member_id), project_id=project_id,
            origin_session_id=str(context.session_id), project_source=(
                "session" if getattr(context, "project_declared", True) else "profile"),
            block=effective_block,
        )
        work: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "work_id": work_id,
            "daemon_instance_id": self._daemon_instance_id,
            "scope": scope.to_json_dict(), "profile_id": profile_id,
            "requested_source_root": source_root,
            "package": package.descriptor(), "objective": objective.strip(),
            "constraints": list(constraints or []), "criteria": list(criteria or []),
            "requested_method": method, "status": "PREPARING", "revision": 1,
            "control_generation": 1, "created_at": now, "updated_at": now,
            "time": {"active_elapsed_sec": 0.0, "active_started_at": None,
                     "calendar_deadline_at": None},
            "revisions": [{"revision": 1, "kind": "USER_GOAL", "reason": "CREATE",
                           "objective": objective.strip(), "method": method,
                           "created_at": now}],
            "environment": dict(profile.get("environment") or {}),
            "profile_source": (dict(profile["profile_source"])
                               if isinstance(profile.get("profile_source"), dict) else None),
            "input": None, "baseline": {"status": "UNRESOLVED",
                "reference": baseline_reference}, "evidence_sets": [],
            "proposals": [], "attempts": [], "role_calls": [], "decisions": [],
            "pending": None, "stop_reason": None, "authorization": None,
            "authorization_preview": None,
            "budget": {"limits": {}, "used": self._zero_usage(), "reserved": self._zero_usage()},
            "no_progress": {"proposal_digests": [], "evidence_digests": [], "count": 0},
            "request_ids": [stable_request], "late_responses": [],
        }
        self._append(work, "CREATED", {"request_id": stable_request})
        return {"status": "ok", "idempotent": False, "work": self._public(work)}

    def create_from_saved_proposal(
        self, *, source_work_id: str, source_proposal_id: str,
        expected_proposal_digest: str, profile_id: str, objective: str,
        constraints: list[str] | None = None, criteria: list[str] | None = None,
        source_root: str | None = None, request_id: str | None = None,
    ) -> dict[str, Any]:
        """以已由 Global 提交的 PLAN_ONLY proposal 建立新 execution work。"""
        required = {
            "source_work_id": source_work_id,
            "source_proposal_id": source_proposal_id,
            "expected_proposal_digest": expected_proposal_digest,
            "profile_id": profile_id,
            "objective": objective,
        }
        invalid = next((key for key, value in required.items()
                        if not isinstance(value, str) or not value.strip()), None)
        if invalid is not None:
            return self._error(
                "SAVED_PROPOSAL_INPUT_INVALID", f"{invalid} must be non-empty")
        for name, values in (("constraints", constraints), ("criteria", criteria)):
            if values is not None and (
                    not isinstance(values, list)
                    or any(not isinstance(item, str) for item in values)):
                return self._error(
                    "SAVED_PROPOSAL_INPUT_INVALID", f"{name} must be strings")

        with self._lock:
            source_work = self._load_owned(source_work_id)
            if isinstance(source_work, WorkError):
                return self._error(source_work.code, source_work.message)
            checked = self._validate_saved_proposal_source(
                source_work, source_proposal_id=source_proposal_id,
                expected_proposal_digest=expected_proposal_digest)
            if isinstance(checked, WorkError):
                return self._error(checked.code, checked.message)
            source_record, global_decision, validation = checked

            target_profile = self._profile_resolver(profile_id)
            if not isinstance(target_profile, dict) or target_profile.get("status") != "ok":
                return self._error(
                    "WORK_PROFILE_UNAVAILABLE", "profile is unavailable or out of scope")
            source_scope = source_work.get("scope") or {}
            target_environment = dict(target_profile.get("environment") or {})
            target_project = str(target_profile.get("project_id") or "")
            target_block = str(target_environment.get("block") or "")
            if (target_project != source_scope.get("project_id")
                    or target_block != source_scope.get("block")):
                return self._error(
                    "SAVED_PROPOSAL_SCOPE_MISMATCH",
                    "execution Profile project/block differs from the proposal scope")
            source_environment = source_work.get("environment") or {}
            identity_keys = (
                "design_id", "platform_id", "block", "source_root",
                "tool_identity", "design_config_digest", "constraints_digest",
            )
            mismatched = [key for key in identity_keys
                          if source_environment.get(key) is not None
                          and target_environment.get(key) != source_environment.get(key)]
            if mismatched:
                return self._error(
                    "SAVED_PROPOSAL_ENVIRONMENT_MISMATCH",
                    "execution Profile changed fixed engineering identity: "
                    + ", ".join(mismatched))
            fixed_source = source_root
            if fixed_source is None and isinstance(
                    target_environment.get("source_root"), str):
                fixed_source = str(target_environment["source_root"])

            context = self._scope_provider()
            create_request_digest = digest_json({
                "source_work_id": source_work_id,
                "source_proposal_id": source_proposal_id,
                "proposal_digest": expected_proposal_digest,
                "profile_id": profile_id,
                "objective": objective.strip(),
                "constraints": list(constraints or []),
                "criteria": list(criteria or []),
                "source_root": fixed_source,
            })
            stable_request = request_id or "work-create-saved:" + digest_json({
                "member_id": context.member_id,
                "project_id": target_project,
                "create_request_digest": create_request_digest,
            })
            created = self.create(
                objective=objective, profile_id=profile_id,
                package_id=str(source_work["package"]["package_id"]),
                constraints=constraints, criteria=criteria,
                block=str(source_scope["block"]),
                method=source_work.get("requested_method"),
                source_root=fixed_source, baseline_reference=None,
                request_id=stable_request)
            if created.get("status") != "ok":
                return created
            work = self._load_controlled(created["work"]["work_id"])
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            existing_seed = work.get("execution_seed")
            if isinstance(existing_seed, dict):
                if not self._saved_seed_matches(
                        existing_seed, source_work_id, source_proposal_id,
                        expected_proposal_digest, create_request_digest):
                    return self._error(
                        "SAVED_PROPOSAL_REQUEST_CONFLICT",
                        "request_id already belongs to another saved proposal")
                return {"status": "ok", "idempotent": True,
                        "work": self._public(work)}
            if work.get("proposals") or work.get("attempts") \
                    or work.get("authorization") is not None:
                return self._error(
                    "SAVED_PROPOSAL_REQUEST_CONFLICT",
                    "request_id already belongs to a non-empty work")

            now = time.time()
            source_input = source_work.get("input") or {}
            source_reference = (source_work.get("baseline") or {}).get("reference") or {}
            source_import = source_reference.get("execution_import") or {}
            source_tree_hash = (
                source_input.get("tree_hash")
                or source_input.get("historical_input_tree_hash")
                or source_import.get("input_tree_hash"))
            seed = {
                "schema_version": "saved_proposal_execution.v1",
                "create_request_digest": create_request_digest,
                "source_work_id": source_work_id,
                "source_proposal_id": source_proposal_id,
                "source_proposal_digest": source_record["source_proposal_digest"],
                "canonical_proposal_digest": source_record["proposal_digest"],
                "source_response_digest": source_record["source_response_digest"],
                "domain_response_digest": source_record["response_digest"],
                "global_decision_id": global_decision["decision_id"],
                "global_decision_digest": digest_json(global_decision),
                "package_id": source_work["package"]["package_id"],
                "baseline_strategy": "RERUN",
                "source_scope": {
                    key: source_scope.get(key)
                    for key in ("member_id", "project_id", "block")},
                "source_input_digest": source_input.get("input_digest"),
                "source_input_tree_hash": source_tree_hash,
                "source_environment": {
                    key: source_environment.get(key) for key in identity_keys
                    if source_environment.get(key) is not None},
                "validation": validation,
            }
            imported_record = json.loads(json.dumps(source_record))
            imported_record.update({
                "proposal_id": "wprop_" + uuid.uuid4().hex,
                "revision": 1,
                "source_work_id": source_work_id,
                "source_proposal_id": source_proposal_id,
                "imported_at": now,
                "validation": validation,
            })
            imported_decision = {
                **json.loads(json.dumps(global_decision)),
                "imported_from_work_id": source_work_id,
                "imported_for_execution": True,
            }
            work["execution_seed"] = seed
            work["proposals"] = [imported_record]
            work["decisions"] = [imported_decision]
            work["no_progress"]["proposal_digests"] = [
                source_record["proposal_digest"]]
            work["revisions"] = [{
                "revision": 1,
                "kind": "SAVED_PROPOSAL_EXECUTION",
                "reason": "CREATE_FROM_SAVED_PROPOSAL",
                "objective": objective.strip(),
                "method": source_work.get("requested_method"),
                "source_work_id": source_work_id,
                "source_proposal_id": source_proposal_id,
                "proposal_digest": source_record["proposal_digest"],
                "created_at": now,
            }]
            work["pending"] = {
                "kind": "SAVED_PROPOSAL_EXECUTION",
                "proposal_digest": source_record["proposal_digest"],
                "baseline_strategy": "RERUN",
            }
            self._append(work, "SAVED_PROPOSAL_IMPORTED", {
                "source_work_id": source_work_id,
                "source_proposal_id": source_proposal_id,
                "proposal_id": imported_record["proposal_id"],
                "proposal_digest": imported_record["proposal_digest"],
                "global_decision_id": global_decision["decision_id"],
                "baseline_strategy": "RERUN",
            })
            return {"status": "ok", "idempotent": False,
                    "work": self._public(work)}

    def preview_authorization(
        self, work_id: str, *, mode: str = "BOUNDED_EXPLORATION",
        limits: dict[str, int] | None = None,
        allowed_capabilities: list[str] | None = None,
        allowed_methods: list[str] | None = None,
        allowed_changes: dict[str, Any] | None = None,
        writable_roots: list[str] | None = None,
        stop_conditions: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if mode not in {"PLAN_ONLY", "PER_PROPOSAL", "BOUNDED_EXPLORATION"}:
                return self._error("WORK_AUTH_MODE_INVALID", mode)
            package = work["package"]
            try:
                normalized_limits = self._normalize_limits(limits or {})
                normalized_changes = dict(allowed_changes or {
                    "kind": "ANY_PACKAGE_VALIDATED_CHANGE"})
            except (WorkError, TypeError, ValueError) as exc:
                return self._error(getattr(exc, "code", "WORK_AUTH_SCOPE_INVALID"),
                                   str(exc))
            preview = {
                "schema_version": "work_authorization.v1", "work_id": work_id,
                "revision": work["revision"], "mode": mode,
                "scope": dict(work["scope"]), "objective": work["objective"],
                "criteria": list(work["criteria"]), "environment": dict(work["environment"]),
                "package_id": package["package_id"],
                "allowed_capabilities": list(allowed_capabilities or [package["capability"]]),
                "allowed_methods": list(allowed_methods or ([work["requested_method"]]
                                         if work.get("requested_method") else ["*"])),
                "allowed_changes": normalized_changes,
                "writable_roots": list(writable_roots or ([work["environment"]["execution_root"]]
                                         if work["environment"].get("execution_root") else [])),
                "limits": normalized_limits,
                "stop_conditions": list(stop_conditions or ["criteria_met", "no_progress", "budget_exhausted"]),
            }
            if isinstance(work.get("execution_seed"), dict):
                preview["saved_proposal"] = self._saved_seed_binding(
                    work["execution_seed"])
                preview["baseline_strategy"] = "RERUN"
            preview["authorization_digest"] = digest_json(preview)
            work["authorization_preview"] = preview
            work["status"] = "WAITING_APPROVAL"
            self._append(work, "AUTHORIZATION_PREVIEWED", {"authorization_digest": preview["authorization_digest"]})
            return {"status": "ok", "authorization": preview, "execution_allowed": False}

    def approve(self, work_id: str) -> dict[str, Any]:
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            # PER_PROPOSAL 的後續核准只增加指定 proposal digest，不改寫
            # parent scope，也不重置任何 budget。
            existing = work.get("authorization")
            pending = work.get("pending") or {}
            if isinstance(existing, dict) and existing.get("valid") \
                    and pending.get("kind") == "PROPOSAL_APPROVAL":
                proposal_digest = pending.get("proposal_digest")
                approved = existing.setdefault("approved_proposal_digests", [])
                if proposal_digest not in approved:
                    approved.append(proposal_digest)
                work["pending"] = {"kind": "EXECUTE_APPROVED",
                                   "proposal_digest": proposal_digest}
                work["status"] = "ANALYZING"
                self._append(work, "PROPOSAL_APPROVED", {
                    "proposal_digest": proposal_digest,
                    "authorization_id": existing["authorization_id"]})
                self._schedule_autonomous_drive(work_id)
                return {"status": "ok", "authorization": existing,
                        "work": self._public(work), "execution_allowed": True}
            preview = work.get("authorization_preview")
            if not isinstance(preview, dict):
                return self._error("WORK_AUTH_PREVIEW_REQUIRED", "preview the authorization first")
            if preview.get("revision") != work["revision"]:
                return self._error("WORK_AUTH_REVISION_STALE", "authorization preview is stale")
            authorization = {**preview, "authorization_id": "wauth_" + uuid.uuid4().hex,
                             "approved_at": time.time(), "approved_by_session_id": self._scope_provider().session_id,
                             "valid": True, "approved_proposal_digests": []}
            work["authorization"] = authorization
            work["budget"]["limits"] = dict(preview["limits"])
            work["status"] = "PREPARING" if work.get("input") is None else "ANALYZING"
            work["time"]["active_started_at"] = time.time()
            self._append(work, "AUTHORIZED", {
                "authorization_id": authorization["authorization_id"],
                "authorization_digest": authorization["authorization_digest"],
            })
            self._schedule_autonomous_drive(work_id)
            return {"status": "ok", "authorization": authorization,
                    "work": self._public(work), "execution_allowed": preview["mode"] != "PLAN_ONLY"}

    # ── Phase A：手動 proposal 收件與獨立核准 ─────────────────────────

    @staticmethod
    def _public_proposal(record: dict[str, Any]) -> dict[str, Any]:
        """手動 proposal 的對外形狀。不含 grant／task／授權欄位。"""
        return {
            "proposal_id": record["proposal_id"],
            "origin": record["origin"],
            "revision": record["revision"],
            "method_identity": record["method_identity"],
            "source_proposal_digest": record["source_proposal_digest"],
            "proposal_digest": record["proposal_digest"],
            "proposal": json.loads(json.dumps(record["proposal"])),
            "validation": json.loads(json.dumps(record["validation"])),
            "approval": json.loads(json.dumps(record["approval"])),
            "created_at": record["created_at"],
        }

    @staticmethod
    def _manual_proposals(work: dict[str, Any]) -> list[dict[str, Any]]:
        return [row for row in work.get("proposals") or []
                if isinstance(row, dict) and row.get("origin") == PROPOSAL_ORIGIN_USER]

    def _manual_intake(self, work: dict[str, Any]) -> dict[str, Any] | WorkError:
        """重新解析 Profile 並取得目前的 intake 交集。

        每次 submit 都重算——Profile 或 policy 之後收回宣告，後續 submit 就
        立刻被拒，不吃「第一次通過」的舊結論。
        """
        scope_info = self._profile_resolver(work["profile_id"])
        if not isinstance(scope_info, dict) or scope_info.get("status") != "ok":
            return WorkError("WORK_PROFILE_UNAVAILABLE",
                             "profile is unavailable or out of scope")
        if str(scope_info.get("project_id") or "") != work["scope"]["project_id"]:
            return WorkError("WORK_PROPOSAL_SCOPE_MISMATCH",
                             "the profile project differs from the work project")
        intake = scope_info.get("proposal_intake")
        if not isinstance(intake, dict) or not intake.get("allowed"):
            reason = ((intake or {}).get("reason_code")
                      if isinstance(intake, dict) else None)
            return WorkError(
                reason or "MANUAL_PROPOSAL_NOT_DECLARED_BY_PROFILE",
                "manual proposal intake is not allowed for this profile")
        return intake

    def submit_proposal(
        self, work_id: str, *, proposal: dict[str, Any], method_identity: str,
        origin: str = PROPOSAL_ORIGIN_USER, request_id: str | None = None,
    ) -> dict[str, Any]:
        """收下一筆使用者送出的 proposal，驗證、canonicalize 並持久化。

        沒有 provider dispatch、沒有 Expert response、沒有 Global decision，
        也不改動 provider state machine。回傳的 proposal 一律是
        `PENDING_APPROVAL`，且 `execution_allowed` 永遠是 False。
        """
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if work["status"] in TERMINAL_STATUSES:
                return self._error("WORK_TERMINAL",
                                   "terminal work cannot accept proposals")
            if not isinstance(proposal, dict) or not proposal:
                return self._error("WORK_PROPOSAL_INVALID",
                                   "proposal must be a non-empty object")
            if not isinstance(method_identity, str) or not method_identity.strip():
                return self._error("WORK_PROPOSAL_METHOD_INVALID",
                                   "method_identity must be a non-empty string")
            if not isinstance(origin, str) or not origin.strip():
                return self._error("WORK_PROPOSAL_ORIGIN_INVALID",
                                   "origin must be a non-empty string")
            if request_id is not None and (not isinstance(request_id, str)
                                           or not request_id.strip()):
                return self._error("WORK_PROPOSAL_REQUEST_INVALID",
                                   "request_id must be a non-empty string")
            intake = self._manual_intake(work)
            if isinstance(intake, WorkError):
                return self._error(intake.code, intake.message)
            if origin not in (intake.get("allowed_origins") or []):
                return self._error(
                    "MANUAL_PROPOSAL_ORIGIN_NOT_ALLOWED",
                    "this origin is not in the profile/policy intake intersection")
            if method_identity not in (intake.get("allowed_method_identities") or []):
                return self._error(
                    "MANUAL_PROPOSAL_METHOD_NOT_ALLOWED",
                    "this method identity is not in the profile/policy intake "
                    "intersection")

            source_digest = digest_json(proposal)
            stable_request = request_id or "work-proposal:" + digest_json({
                "work_id": work["work_id"], "origin": origin,
                "method_identity": method_identity, "proposal": proposal})
            existing = next((row for row in self._manual_proposals(work)
                             if row.get("request_id") == stable_request), None)
            if existing is not None:
                if (existing["source_proposal_digest"] != source_digest
                        or existing["method_identity"] != method_identity
                        or existing["origin"] != origin):
                    return self._error(
                        "WORK_PROPOSAL_REPLAY_MISMATCH",
                        "this idempotency key was already used for a different "
                        "proposal")
                return {"status": "ok", "idempotent": True,
                        "proposal": self._public_proposal(existing),
                        "work": self._public(work), "execution_allowed": False}

            try:
                package = self._packages.get(work["package"]["package_id"])
            except WorkError as exc:
                return self._error(exc.code, exc.message)
            try:
                validation = package.validate(proposal)
            except Exception as exc:  # noqa: BLE001 - Package 失敗不是 daemon crash
                validation = {"status": "REJECTED",
                              "reason_code": "PACKAGE_VALIDATOR_FAILED",
                              "message": type(exc).__name__}
            if not isinstance(validation, dict):
                validation = {"status": "REJECTED",
                              "reason_code": "PACKAGE_VALIDATOR_RESULT_INVALID",
                              "message": "validator must return an object"}
            canonical = validation.pop("normalized_proposal", proposal)
            if not isinstance(canonical, dict):
                validation = {"status": "REJECTED",
                              "reason_code": "PACKAGE_ADAPTER_RESULT_INVALID",
                              "message": "normalized proposal must be an object"}
                canonical = proposal
            if validation.get("status") != "PASSED":
                self._append(work, "USER_PROPOSAL_REJECTED", {
                    "origin": origin, "method_identity": method_identity,
                    "source_proposal_digest": source_digest,
                    "request_id": stable_request, "validation": dict(validation)})
                return {"status": "error", "error_code": "WORK_PROPOSAL_REJECTED",
                        "message": str(validation.get("message")
                                       or "the package rejected this proposal"),
                        "validation": dict(validation),
                        "execution_allowed": False}

            context = self._scope_provider()
            record = {
                "proposal_id": "uprop_" + uuid.uuid4().hex,
                "origin": origin, "revision": work["revision"],
                "method_identity": method_identity,
                "request_id": stable_request,
                "source_proposal": json.loads(json.dumps(proposal)),
                "source_proposal_digest": source_digest,
                "proposal": json.loads(json.dumps(canonical)),
                "proposal_digest": digest_json(canonical),
                "validation": dict(validation),
                "package": dict(work["package"]),
                "intake": {
                    "allowed_origins": list(intake.get("allowed_origins") or []),
                    "allowed_method_identities": list(
                        intake.get("allowed_method_identities") or [])},
                "approval": {"status": PROPOSAL_PENDING_APPROVAL,
                             "approved_at": None,
                             "approved_by_session_id": None,
                             "approved_proposal_digest": None,
                             "request_id": None},
                "created_at": time.time(),
                "created_by_session_id": str(context.session_id),
            }
            work["proposals"].append(record)
            work["request_ids"].append(stable_request)
            self._append(work, "USER_PROPOSAL_SUBMITTED", {
                "proposal_id": record["proposal_id"], "origin": origin,
                "method_identity": method_identity,
                "source_proposal_digest": source_digest,
                "proposal_digest": record["proposal_digest"],
                "request_id": stable_request,
                "validation": dict(validation)})
            return {"status": "ok", "idempotent": False,
                    "proposal": self._public_proposal(record),
                    "work": self._public(work), "execution_allowed": False}

    def approve_proposal(
        self, work_id: str, *, proposal_id: str, proposal_digest: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """核准一筆已保存的手動 proposal。

        核准**只是**核准：不建立 authorization、不建立 grant、不 materialize、
        不執行。要執行仍必須走既有的 Work authorization preview／approve。
        """
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if work["status"] in TERMINAL_STATUSES:
                return self._error("WORK_TERMINAL",
                                   "terminal work cannot approve proposals")
            if not isinstance(proposal_id, str) or not proposal_id.strip():
                return self._error("WORK_PROPOSAL_ID_INVALID",
                                   "proposal_id must be a non-empty string")
            if not isinstance(proposal_digest, str) or not proposal_digest.strip():
                return self._error("WORK_PROPOSAL_DIGEST_INVALID",
                                   "proposal_digest must be a non-empty string")
            if request_id is not None and (not isinstance(request_id, str)
                                           or not request_id.strip()):
                return self._error("WORK_PROPOSAL_REQUEST_INVALID",
                                   "request_id must be a non-empty string")
            record = next((row for row in self._manual_proposals(work)
                           if row.get("proposal_id") == proposal_id), None)
            if record is None:
                return self._error(
                    "WORK_PROPOSAL_NOT_FOUND",
                    "no such user-submitted proposal in this work")
            if record["proposal_digest"] != proposal_digest:
                return self._error(
                    "WORK_PROPOSAL_DIGEST_MISMATCH",
                    "approval must carry the canonical proposal digest")
            approval = record["approval"]
            if approval.get("status") == PROPOSAL_APPROVED:
                return {"status": "ok", "idempotent": True,
                        "proposal": self._public_proposal(record),
                        "work": self._public(work), "execution_allowed": False}
            context = self._scope_provider()
            stable_request = request_id or "work-proposal-approve:" + digest_json({
                "work_id": work["work_id"], "proposal_id": proposal_id,
                "proposal_digest": proposal_digest})
            record["approval"] = {
                "status": PROPOSAL_APPROVED, "approved_at": time.time(),
                "approved_by_session_id": str(context.session_id),
                "approved_proposal_digest": proposal_digest,
                "request_id": stable_request}
            if stable_request not in work["request_ids"]:
                work["request_ids"].append(stable_request)
            self._append(work, "USER_PROPOSAL_APPROVED", {
                "proposal_id": proposal_id, "origin": record["origin"],
                "method_identity": record["method_identity"],
                "proposal_digest": proposal_digest,
                "request_id": stable_request})
            # 這個 Work 可能正卡在「需要一次 Global 規劃 dispatch」上。核准一份
            # manual proposal 之後那次規劃已無必要，讓它重新可推進；**不**改變
            # 執行授權語意——要執行仍必須另有有效的 execution authorization。
            if (work["status"] == "WAITING_USER"
                    and (work.get("pending") or {}).get("kind") == "BUDGET_DELTA"
                    and self._manual_zero_provider(work, record,
                                                   work.get("authorization"))):
                work["pending"] = None
                work["status"] = "ANALYZING"
                self._append(work, "MANUAL_PROPOSAL_READY_FOR_AUTHORIZATION", {
                    "proposal_id": proposal_id,
                    "proposal_digest": proposal_digest})
            return {"status": "ok", "idempotent": False,
                    "proposal": self._public_proposal(record),
                    "work": self._public(work), "execution_allowed": False}

    def continue_work(self, work_id: str, *, max_transitions: int = 64) -> dict[str, Any]:
        """依保存的 Global／Domain 決策推進，直到 terminal 或需要人介入。"""
        if not isinstance(max_transitions, int) or not 1 <= max_transitions <= 256:
            return self._error("WORK_TRANSITION_LIMIT_INVALID", "max_transitions must be 1..256")
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if work["status"] in TERMINAL_STATUSES | {"PAUSED", "WAITING_USER", "WAITING_APPROVAL", "RECONCILIATION_REQUIRED"}:
                return {"status": "ok", "work": self._public(work)}
            for _ in range(max_transitions):
                if self._active_time_exhausted(work):
                    try:
                        stopped = self._runtime.cancel(work=work)
                    except Exception as exc:  # noqa: BLE001 - 無法確認 child 就不猜
                        stopped = {"children_terminal": False,
                                   "error_type": type(exc).__name__}
                    work["status"] = ("STOPPED" if stopped.get("children_terminal")
                                      else "UNCONFIRMED")
                    work["stop_reason"] = "WORK_ACTIVE_TIME_EXHAUSTED"
                    self._append(work, "TERMINAL", {
                        "reason": work["stop_reason"], "runtime": stopped})
                    break
                before = (work["status"], work["revision"], len(work["role_calls"]), len(work["attempts"]))
                self._advance_one(work)
                after = (work["status"], work["revision"], len(work["role_calls"]), len(work["attempts"]))
                if work["status"] in TERMINAL_STATUSES | {"PAUSED", "WAITING_USER", "WAITING_APPROVAL", "RECONCILIATION_REQUIRED", "EXECUTING", "COLLECTING_EVIDENCE"}:
                    break
                if before == after:
                    work["status"] = "UNCONFIRMED"
                    work["stop_reason"] = "STATE_MACHINE_NO_PROGRESS"
                    self._append(work, "STOPPED", {"reason": work["stop_reason"]})
                    break
            return {"status": "ok", "work": self._public(work)}

    def get(self, work_id: str) -> dict[str, Any]:
        work = self._load_owned(work_id)
        if isinstance(work, WorkError):
            return self._error(work.code, work.message)
        return {"status": "ok", "work": self._public(work)}

    def list(self) -> dict[str, Any]:
        return {"status": "ok", "works": self._reader.list(self._scope_provider())}

    def answer(
        self, work_id: str, *, message: str, objective: str | None = None,
        method: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if not isinstance(message, str) or not message.strip():
                return self._error("WORK_ANSWER_INVALID", "message must be non-empty")
            if work["status"] in TERMINAL_STATUSES:
                return self._error("WORK_TERMINAL", "terminal work cannot be revised")
            prior_revision = work["revision"]
            work["revision"] += 1
            if objective is not None:
                if not objective.strip():
                    return self._error("WORK_OBJECTIVE_INVALID", "objective must be non-empty")
                work["objective"] = objective.strip()
            if method is not None:
                work["requested_method"] = method
            work["revisions"].append({
                "revision": work["revision"], "previous_revision": prior_revision,
                "kind": "USER_INPUT", "message": message.strip(), "objective": objective,
                "method": method, "created_at": time.time(),
            })
            work["pending"] = None
            authorization = work.get("authorization") or {}
            if authorization and not self._revision_covered(work, authorization):
                work["status"] = "WAITING_APPROVAL"
                work["pending"] = {"kind": "AUTHORIZATION_DELTA", "message": "objective or method exceeds authorization"}
            else:
                work["status"] = "ANALYZING"
            self._append(work, "REVISED", {"previous_revision": prior_revision, "message": message.strip()})
            self._schedule_autonomous_drive(work_id)
            return {"status": "ok", "work": self._public(work)}

    def pause(self, work_id: str) -> dict[str, Any]:
        return self._control(work_id, "PAUSED", "PAUSED_BY_USER")

    def resume(self, work_id: str) -> dict[str, Any]:
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if work["status"] != "PAUSED":
                return self._error("WORK_NOT_PAUSED", "work is not paused")
            work["control_generation"] += 1
            paused = work.get("pending") or {}
            if paused.get("previous_status") in {"PREPARING", "WAITING_APPROVAL", "WAITING_USER"}:
                work["status"] = paused["previous_status"]
                work["pending"] = paused.get("previous_pending")
            elif (work.get("baseline") or {}).get("status") in {"DISPATCHING", "COLLECTING"}:
                collection = work["baseline"].get("collection") or {}
                work["pending"] = {"kind": "BASELINE_COLLECTION",
                                   "request_id": collection.get("request_id"),
                                   "collection": collection,
                                   "reserved": {"tool_launches": 1}}
                work["status"] = "COLLECTING_EVIDENCE"
            else:
                work["status"] = (
                    "EXECUTING" if self._running_attempt(work) else "ANALYZING")
            self._append(work, "RESUMED", {"control_generation": work["control_generation"]})
            if (work["status"] not in {"WAITING_APPROVAL", "WAITING_USER"}
                    and isinstance(work.get("authorization"), dict)):
                self._schedule_autonomous_drive(work_id)
            return {"status": "ok", "work": self._public(work)}

    def cancel(self, work_id: str) -> dict[str, Any]:
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if work["status"] in TERMINAL_STATUSES:
                return {"status": "ok", "idempotent": True, "work": self._public(work)}
            work["control_generation"] += 1
            work["status"] = "STOPPING"
            self._append(work, "CANCEL_REQUESTED", {"control_generation": work["control_generation"]})
            try:
                outcome = self._runtime.cancel(work=work)
            except Exception as exc:  # noqa: BLE001 - cancel failure must remain queryable
                outcome = {"children_terminal": False,
                           "error_type": type(exc).__name__}
            work["status"] = "CANCELLED" if outcome.get("children_terminal", False) else "UNCONFIRMED"
            work["stop_reason"] = "CANCELLED_BY_USER" if work["status"] == "CANCELLED" else "CANCEL_CHILD_UNCONFIRMED"
            self._append(work, "CANCELLED", {"runtime": outcome, "reason": work["stop_reason"]})
            return {"status": "ok", "work": self._public(work)}

    def takeover(self, work_id: str) -> dict[str, Any]:
        """重啟後合法 owner 接管；舊 child binding 永不復活。"""
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            context = self._scope_provider()
            active: dict[str, Any] | None = None
            if ((work.get("baseline") or {}).get("status") in {"DISPATCHING", "COLLECTING"}
                    and (work.get("pending") or {}).get("kind") in {
                        "BASELINE_COLLECTION_DISPATCH", "BASELINE_COLLECTION"}):
                collection = (work.get("baseline") or {}).get("collection") or {}
                unknown_launches = min(1, int(work["budget"]["reserved"].get(
                    "tool_launches", 0)))
                if unknown_launches:
                    self._complete_reservation(work, {"tool_launches": unknown_launches})
                work["baseline"] = {**work["baseline"], "status": "UNCONFIRMED"}
                work["status"] = "RECONCILIATION_REQUIRED"
                work["pending"] = {
                    "kind": "BASELINE_RECONCILIATION",
                    "task_id": collection.get("task_id"),
                    "task_digest": collection.get("task_digest"),
                    "message": "old daemon baseline child state must be reconciled",
                    "unknown_consumption": {"tool_launches": unknown_launches},
                }
            elif work["status"] == "EXECUTING":
                work["status"] = "RECONCILIATION_REQUIRED"
                work["pending"] = {"kind": "CHILD_RECONCILIATION", "message": "old daemon child state must be reconciled"}
                active = self._running_attempt(work)
                if active is not None:
                    active["status"] = "UNCONFIRMED"
                    active["replacement_reason"] = "DAEMON_RESTART_CHILD_UNCONFIRMED"
            elif work["status"] == "RECONCILIATION_REQUIRED":
                active = next((row for row in reversed(work["attempts"])
                               if row.get("status") == "UNCONFIRMED"), None)
            elif (work["status"] == "WAITING_USER"
                  and (work.get("pending") or {}).get("kind")
                  in {"INPUT_PREPARATION", "AUTHORIZATION_DELTA"}
                  and ((work.get("input") is None)
                       or ((work.get("baseline") or {}).get("status") == "REJECTED"
                           and isinstance((work.get("baseline") or {}).get(
                               "reference"), dict)))):
                # 新 daemon 的部署設定／唯讀證據可能已補齊；接管
                # 後重跑 PREPARING gate，不能直接讓 provider 在舊的
                # REJECTED baseline 上提案。
                work["pending"] = None
                work["status"] = "PREPARING"
            elif work["status"] not in TERMINAL_STATUSES:
                work["status"] = "ANALYZING"
            if isinstance(active, dict) and any((active.get("reserved") or {}).values()):
                reserved = active.get("reserved") or {}
                self._complete_reservation(work, reserved)
                active["unknown_consumption"] = dict(reserved)
                active["reserved"] = self._zero_usage()
            work["daemon_instance_id"] = self._daemon_instance_id
            work["control_generation"] += 1
            self._append(work, "TAKEN_OVER", {
                "session_id": context.session_id,
                "control_generation": work["control_generation"],
            })
            self._schedule_autonomous_drive(work_id)
            return {"status": "ok", "work": self._public(work)}

    def _schedule_autonomous_drive(self, work_id: str) -> None:
        """一次核准後由 daemon 持續推進；查詢與 ``continue`` 都不是時鐘。"""
        if self._submit is None or work_id in self._scheduled:
            return
        self._scheduled.add(work_id)

        def drive() -> None:
            final: dict[str, Any] | None = None
            try:
                while True:
                    result = self.continue_work(work_id)
                    work = result.get("work") if isinstance(result, dict) else None
                    if not isinstance(work, dict):
                        final = result
                        break
                    final = work
                    status = str(work.get("status"))
                    if status in TERMINAL_STATUSES | {
                            "PAUSED", "WAITING_USER", "WAITING_APPROVAL",
                            "RECONCILIATION_REQUIRED"}:
                        break
                    # FlowTask 與 baseline child 非同步執行；只有 daemon worker
                    # 依固定間隔讀取保存狀態，外部 get/continue 不負責推進。
                    if status in {"EXECUTING", "COLLECTING_EVIDENCE"}:
                        time.sleep(self._poll_interval_sec)
            except Exception as exc:  # noqa: BLE001 - worker failure must be queryable
                with self._lock:
                    work = self._load_controlled(work_id)
                    if isinstance(work, dict) and work.get("status") not in TERMINAL_STATUSES:
                        work["status"] = "UNCONFIRMED"
                        work["stop_reason"] = "AUTONOMOUS_DRIVER_FAILED"
                        self._append(work, "AUTONOMOUS_DRIVER_FAILED", {
                            "error_type": type(exc).__name__})
                        final = self._public(work)
            finally:
                with self._lock:
                    self._scheduled.discard(work_id)
                if self._notify is not None and isinstance(final, dict):
                    try:
                        self._notify({
                            "type": "notification",
                            "trigger": "eap3.work",
                            "work_id": work_id,
                            "status": final.get("status"),
                            "stop_reason": final.get("stop_reason"),
                            "pending": final.get("pending"),
                            "answer": (f"工程工作 {work_id} 目前狀態："
                                       f"{final.get('status')}"),
                        })
                    except Exception as exc:  # noqa: BLE001 - 通知失敗不改寫 work 結果
                        _LOG.warning("Engineering Work notification failed: %r", exc)

        try:
            self._submit(drive)
        except Exception:
            self._scheduled.discard(work_id)
            raise

    def record_late_response(
        self, work_id: str, *, request_id: str, response: dict[str, Any],
        observed_generation: int,
    ) -> dict[str, Any]:
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            ignored = (work["status"] in TERMINAL_STATUSES
                       or observed_generation != work["control_generation"])
            work["late_responses"].append({
                "request_id": request_id, "response_digest": digest_json(response),
                "ignored": ignored, "observed_generation": observed_generation,
                "received_at": time.time(),
            })
            self._append(work, "LATE_RESPONSE", work["late_responses"][-1])
            return {"status": "ok", "ignored": ignored, "work": self._public(work)}

    def _advance_one(self, work: dict[str, Any]) -> None:
        status = work["status"]
        if status == "PREPARING":
            self._prepare(work)
            return
        if status == "COLLECTING_EVIDENCE":
            self._poll_evidence(work)
            return
        if status == "EXECUTING":
            self._poll_attempt(work)
            return
        if status == "ANALYZING_RESULT":
            self._analyze_result(work)
            return
        if status == "ANALYZING":
            self._plan_or_delegate(work)
            return
        raise WorkError("WORK_STATE_INVALID", status)

    def _prepare(self, work: dict[str, Any]) -> None:
        authorization = work.get("authorization")
        if not isinstance(authorization, dict):
            self.preview_authorization(work["work_id"])
            return
        if work.get("input") is None:
            request_id = self._new_request(work, "input_prepare")
            prepared = self._runtime.prepare_input(work=work, request_id=request_id)
            if prepared.get("status") != "READY":
                work["status"] = "WAITING_USER" if prepared.get("recoverable", True) else "STOPPED"
                work["pending"] = {"kind": "INPUT_PREPARATION", "detail": prepared}
                self._append(work, "INPUT_PREPARATION_FAILED", prepared)
                return
            work["input"] = prepared
            if isinstance(prepared.get("profile_id"), str):
                work["execution_profile_id"] = prepared["profile_id"]
            if isinstance(prepared.get("environment_patch"), dict):
                work["environment"].update(prepared["environment_patch"])
            self._append(work, "INPUT_PREPARED", {"input_digest": prepared.get("input_digest")})
        applicability = self._saved_proposal_input_check(work)
        if applicability is not None:
            work["status"] = "STOPPED"
            work["stop_reason"] = "SAVED_PROPOSAL_APPLICABILITY_MISMATCH"
            work["pending"] = {"kind": "SAVED_PROPOSAL_REJECTED", **applicability}
            self._append(work, "SAVED_PROPOSAL_REJECTED", work["pending"])
            return
        reference = (work.get("baseline") or {}).get("reference")
        if not isinstance(reference, dict):
            # Profile 釘住了 durable source ⇒ 由 durable 紀錄取回既有 Work 的
            # baseline reference，而不是再收集一次 baseline。取不回來就照常
            # 走收集流程（不靜默降級成「沒有 baseline」）。
            seed = getattr(self._runtime, "durable_baseline_reference", None)
            durable = seed(work) if callable(seed) else None
            if isinstance(durable, dict):
                reference = durable
                work["baseline"] = {"status": "UNRESOLVED", "reference": durable,
                                    "pinned_durable_source": True}
                self._append(work, "BASELINE_DURABLE_SOURCE_RESOLVED", {
                    "source_task_id": (durable.get("execution_import") or {}).get(
                        "source_task_id")})
        if isinstance(reference, dict):
            package = self._packages.get(work["package"]["package_id"])
            validated = self._runtime.validate_baseline(
                work=work, reference=reference,
                requirements=package.baseline_requirements(work))
            if validated.get("status") == "REUSED_VALIDATED":
                work["baseline"] = validated
                self._append(work, "BASELINE_REUSED", {"reference_digest": digest_json(validated)})
            else:
                work["baseline"] = {
                    "status": "REJECTED", "reference": reference,
                    "reason_code": validated.get("reason_code"),
                    # 兩側各自的 digest 等診斷資訊原樣保留，不再只剩 reason_code。
                    "rejection": validated,
                    "pinned_durable_source": bool(
                        (work.get("baseline") or {}).get("pinned_durable_source"))}
                self._append(work, "BASELINE_REJECTED", validated)
        requirements = self._packages.get(
            work["package"]["package_id"]).baseline_requirements(work)
        if (work["baseline"]["status"] not in {"REUSED_VALIDATED", "COMPLETE"}
                and requirements.get("baseline_mode") == "NOT_REQUIRED"
                and requirements.get("required") == []):
            work["baseline"] = {
                "status": "COMPLETE", "mode": "NOT_REQUIRED",
                "reference": {"schema_version": "eap3.baseline_not_required.v1",
                              "input_digest": work["input"]["input_digest"],
                              "requirements_digest": digest_json(requirements)}}
            self._append(work, "BASELINE_NOT_REQUIRED", {
                "requirements_digest": digest_json(requirements)})
        if work["baseline"]["status"] not in {"REUSED_VALIDATED", "COMPLETE"}:
            if authorization.get("mode") == "PLAN_ONLY":
                work["status"] = "WAITING_USER"
                work["pending"] = {
                    "kind": "AUTHORIZATION_DELTA",
                    "message": "baseline collection requires execution authorization"}
                self._append(work, "BASELINE_AUTHORIZATION_REQUIRED", work["pending"])
                return
            # 明確釘選的重用被拒時，**不得**自行改成重新收集 baseline：那會
            # 把「重用這一份」變成「再跑一次」，並吃掉未授權的工具額度。
            if work["baseline"].get("pinned_durable_source"):
                work["status"] = "STOPPED"
                work["stop_reason"] = "PINNED_BASELINE_REUSE_REJECTED"
                work["pending"] = {
                    "kind": "PINNED_BASELINE_REUSE_REJECTED",
                    "reason_code": work["baseline"].get("reason_code"),
                    "detail": work["baseline"].get("rejection"),
                    "message": ("the pinned durable baseline was refused; this Work "
                                "requires reuse and must not collect a new baseline")}
                self._append(work, "PINNED_BASELINE_REUSE_REJECTED", work["pending"])
                return
            request = {"kind": "BASELINE_ONLY", "requirements": self._packages.get(
                work["package"]["package_id"]).baseline_requirements(work)}
            # 用途額度先於總額度；未授權 baseline collection 就不得保留、
            # 更不得派送 child。
            if not self._purpose_allows(work, self.PURPOSE_BASELINE_TOOL_LAUNCHES):
                work["status"] = "WAITING_USER"
                work["pending"] = {
                    "kind": "PURPOSE_BUDGET_DELTA",
                    "purpose": self.PURPOSE_BASELINE_TOOL_LAUNCHES,
                    "message": ("baseline collection is not authorized for this "
                                "Work; the total tool budget cannot be borrowed")}
                self._append(work, "PURPOSE_BUDGET_INSUFFICIENT", work["pending"])
                return
            if not self._can_spend(work, {"tool_launches": 1}):
                self._budget_wait(work, "baseline requires one tool launch")
                return
            self._purpose_consume(work, self.PURPOSE_BASELINE_TOOL_LAUNCHES)
            self._reserve(work, {"tool_launches": 1})
            request_id = self._new_request(work, "baseline_collect")
            work["baseline"] = {"status": "DISPATCHING", "request_id": request_id}
            work["pending"] = {"kind": "BASELINE_COLLECTION_DISPATCH",
                               "request_id": request_id,
                               "reserved": {"tool_launches": 1}}
            work["status"] = "COLLECTING_EVIDENCE"
            # 任何 child side effect 前先把狀態與 reservation 落盤；若 daemon
            # 在 runtime 回傳前中斷，重啟只能對帳，不能重 launch。
            self._append(work, "EVIDENCE_BUDGET_RESERVED", {
                "request_id": request_id, "cost": {"tool_launches": 1},
                "dispatch_state": "BASELINE_COLLECTION_DISPATCH"})
            try:
                result = self._runtime.collect_evidence(
                    work=work, request=request, request_id=request_id)
            except Exception as exc:  # noqa: BLE001 - 已派送視為未知消耗
                self._complete_reservation(work, {"tool_launches": 1})
                work["baseline"] = {"status": "UNCONFIRMED",
                                    "request_id": request_id}
                work["status"] = "RECONCILIATION_REQUIRED"
                work["stop_reason"] = "BASELINE_DISPATCH_UNCONFIRMED"
                work["pending"] = {"kind": "BASELINE_RECONCILIATION",
                                   "request_id": request_id,
                                   "unknown_consumption": {"tool_launches": 1}}
                self._append(work, "EVIDENCE_DISPATCH_FAILED", {
                    "request_id": request_id, "error_type": type(exc).__name__})
                return
            if result.get("status") in {"RUNNING", "QUEUED"}:
                work["baseline"] = {"status": "COLLECTING", "collection": result}
                work["pending"] = {"kind": "BASELINE_COLLECTION",
                                   "request_id": request_id, "collection": result,
                                   "reserved": {"tool_launches": 1}}
                work["status"] = "COLLECTING_EVIDENCE"
                self._append(work, "BASELINE_COLLECTION_DISPATCHED", {
                    "request_id": request_id, "task_id": result.get("task_id"),
                    "task_digest": result.get("task_digest")})
                return
            self._complete_reservation(work, {"tool_launches": 1})
            work["evidence_sets"].append(result)
            if result.get("status") != "COMPLETE":
                work["status"] = "UNCONFIRMED"
                work["stop_reason"] = "BASELINE_EVIDENCE_INCOMPLETE"
                self._append(work, "EVIDENCE_INCOMPLETE", result)
                return
            work["baseline"] = {"status": "COMPLETE", "reference": result.get("reference"),
                                "evidence_digest": digest_json(result)}
            self._append(work, "BASELINE_COLLECTED", {"evidence_digest": digest_json(result)})
        work["status"] = "ANALYZING"
        self._append(work, "READY_FOR_ANALYSIS", {})

    def _poll_evidence(self, work: dict[str, Any]) -> None:
        """收束正式 baseline-only child；只有可信終態才建立 baseline reference。"""
        pending = work.get("pending") or {}
        collection = pending.get("collection")
        if pending.get("kind") != "BASELINE_COLLECTION" or not isinstance(collection, dict):
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "BASELINE_COLLECTION_STATE_INVALID"
            self._append(work, "EVIDENCE_INCOMPLETE", {
                "reason_code": work["stop_reason"]})
            return
        try:
            result = self._runtime.poll_evidence(work=work, collection=collection)
        except Exception as exc:  # noqa: BLE001 - child 狀態未知時不得重送
            work["status"] = "RECONCILIATION_REQUIRED"
            work["stop_reason"] = "BASELINE_POLL_UNCONFIRMED"
            self._append(work, "EVIDENCE_POLL_FAILED", {
                "task_id": collection.get("task_id"),
                "error_type": type(exc).__name__})
            return
        if result.get("status") in {"RUNNING", "QUEUED"}:
            work["baseline"]["collection"] = result
            work["pending"]["collection"] = result
            return
        reserved = pending.get("reserved") or {"tool_launches": 1}
        self._complete_reservation(work, reserved)
        work["pending"] = None
        work["evidence_sets"].append(result)
        if result.get("status") != "COMPLETE":
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "BASELINE_EVIDENCE_INCOMPLETE"
            self._append(work, "EVIDENCE_INCOMPLETE", result)
            return
        work["baseline"] = {"status": "COMPLETE", "reference": result.get("reference"),
                            "evidence_digest": digest_json(result)}
        work["status"] = "ANALYZING"
        self._append(work, "BASELINE_COLLECTED", {
            "evidence_digest": digest_json(result),
            "task_id": result.get("task_id")})
        self._append(work, "READY_FOR_ANALYSIS", {})

    def _plan_or_delegate(self, work: dict[str, Any]) -> None:
        pending = work.get("pending") or {}
        if pending.get("kind") == "EXECUTE_APPROVED":
            work["pending"] = None
            self._submit_latest(work)
            return
        if isinstance(work.get("execution_seed"), dict) and not work["attempts"]:
            self._submit_latest(work)
            return
        # 已核准的 manual proposal 不需要 Global 再規劃一次——使用者已經把
        # 要執行的內容寫清楚並核准了。這條路由與 `execution_seed` 同性質：
        # 計畫已經存在，缺的只是執行授權。
        manual = self._latest_approved_manual_proposal(work)
        # `_submit_latest()` 取的是 `proposals[-1]`。只有當那一份就是已核准的
        # manual proposal 時才走這條路由——否則後送的一份未核准 proposal 會
        # 搭上前一份的核准。要提交的必須是被核准的那個 digest，不是最新的。
        latest = (work.get("proposals") or [None])[-1]
        if (manual is not None and not work["attempts"]
                and isinstance(latest, dict)
                and latest.get("proposal_digest") == manual.get("proposal_digest")
                and self._manual_zero_provider(work, manual,
                                               work.get("authorization"))):
            self._submit_latest(work)
            return
        if pending.get("kind") == "DOMAIN_REVISION":
            prior_attempt = work["attempts"][-1] if work.get("attempts") else None
            domain = self._call_role(work, "domain", "proposal_revision", {
                "revision_request": pending, "prior_proposal": pending.get("prior_proposal"),
                # 修訂必須綁在**先前真的量到什麼**上，不能只看前一份 proposal。
                "prior_attempt": ({"attempt_id": prior_attempt["attempt_id"],
                                   "status": prior_attempt["status"],
                                   "outcome_digest": prior_attempt.get("outcome_digest"),
                                   "engineering_result": prior_attempt.get(
                                       "engineering_result")}
                                  if isinstance(prior_attempt, dict) else None),
                "history": self._history_context(work),
                "package": work["package"], "requested_method": work.get("requested_method"),
                "authorization_scope": {
                    "allowed_methods": list((work.get("authorization") or {}).get(
                        "allowed_methods") or ()),
                    "allowed_changes": (work.get("authorization") or {}).get(
                        "allowed_changes"),
                },
            })
            if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
                return
            work["pending"] = None
            self._consume_domain(work, domain)
            return
        if pending.get("kind") == "GLOBAL_AFTER_DOMAIN":
            decision = self._call_role(work, "global", "domain_response", {
                "domain_response": pending["domain_response"],
                "domain_response_digest": pending["domain_response_digest"],
                "history": self._history_context(work),
            })
            if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
                return
            work["pending"] = None
            self._consume_global(work, decision, pending["domain_response_digest"])
            return
        decision = self._call_role(work, "global", "plan", {
            "objective": work["objective"], "criteria": work["criteria"],
            "constraints": work["constraints"], "requested_method": work.get("requested_method"),
            "package": work["package"], "baseline": work["baseline"],
            "history": self._history_context(work),
        })
        if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
            return
        self._consume_global(work, decision, None)

    def _consume_global(
        self, work: dict[str, Any], decision: dict[str, Any],
        based_on_domain_digest: str | None,
    ) -> None:
        kind = decision.get("kind")
        if kind not in GLOBAL_DECISIONS:
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "GLOBAL_DECISION_INVALID"
            self._append(work, "GLOBAL_INVALID", {"response_digest": digest_json(decision)})
            return
        stored = {**decision, "decision_id": "gdec_" + uuid.uuid4().hex,
                  "revision": work["revision"],
                  "based_on_domain_response_digest": based_on_domain_digest}
        work["decisions"].append(stored)
        self._append(work, "GLOBAL_DECISION", stored)
        if (isinstance(work.get("execution_seed"), dict)
                and work.get("attempts") and based_on_domain_digest is not None
                and kind in {"REQUEST_REVISION", "COLLECT_EVIDENCE"}):
            work["status"] = "STOPPED"
            work["stop_reason"] = "SAVED_PROPOSAL_SINGLE_ATTEMPT_COMPLETE"
            work["pending"] = {
                "kind": "FOLLOW_UP_NOT_AUTHORIZED",
                "global_decision_id": stored["decision_id"],
                "requested_action": kind,
            }
            self._append(work, "TERMINAL", {
                "reason": work["stop_reason"],
                "global_decision_id": stored["decision_id"],
                "requested_action": kind,
            })
            return
        if kind == "DELEGATE":
            domain = self._call_role(work, "domain", "proposal", {
                "question": decision.get("question"), "objective": work["objective"],
                "evidence": self._evidence_context(work), "history": self._history_context(work),
                "package": work["package"], "requested_method": work.get("requested_method"),
                "authorization_scope": {
                    "allowed_methods": list((work.get("authorization") or {}).get(
                        "allowed_methods") or ()),
                    "allowed_changes": (work.get("authorization") or {}).get(
                        "allowed_changes"),
                },
            })
            if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
                return
            self._consume_domain(work, domain)
        elif kind == "COLLECT_EVIDENCE":
            self._collect(work, decision.get("request") or {})
        elif kind == "REQUEST_REVISION":
            core_denial = self._core_utilization_revision_denial(work, decision)
            if core_denial is not None:
                work["status"] = "STOPPED"
                work["stop_reason"] = core_denial
                self._append(work, "CORE_UTILIZATION_REVISION_STOPPED", {
                    "reason_code": core_denial,
                    "based_on_domain_response_digest": based_on_domain_digest,
                })
                return
            claim_denial = self._revision_claim_denial(work, stored, based_on_domain_digest)
            if claim_denial is not None:
                work["status"] = "STOPPED"
                work["stop_reason"] = claim_denial["reason_code"]
                self._append(work, "REVISION_CLAIM_REJECTED", claim_denial)
                return
            prior = work["proposals"][-1] if work["proposals"] else None
            work["pending"] = {"kind": "DOMAIN_REVISION", "message": decision.get("message"),
                               "prior_proposal": prior, "new_evidence": self._evidence_context(work)}
            work["status"] = "ANALYZING"
            self._append(work, "DOMAIN_REVISION_REQUIRED", {
                "based_on_domain_response_digest": based_on_domain_digest,
                "message": decision.get("message")})
        elif kind in {"SUBMIT_PROPOSAL", "EXECUTE"}:
            self._submit_latest(work)
        elif kind == "WAIT_USER":
            work["status"] = "WAITING_USER"
            work["pending"] = {"kind": "USER_QUESTION", "message": decision.get("message")}
            self._append(work, "WAITING_USER", dict(work["pending"]))
        else:
            reason = str(decision.get("reason_code") or "GLOBAL_STOP")
            # 每次終局都重新算一次候選處置摘要並落盤：不管這次是不是宣稱
            # criteria_met，之前哪一輪合格、哪一輪被保留都不得因為這次終局
            # 而從快照裡消失——見 `candidate_qualification_history()`。
            disposition = candidate_qualification_history(work)
            work["candidate_disposition"] = disposition
            # 身分／完成狀態的候選資格檢查不因 `criteria_met` 是 True 或
            # False 而被略過：Global 只要明確指名 `selected_attempt_id`，
            # 這裡一律先解析；找不到或仍在執行一律明確拒絕，不得靜默改
            # 核對其他 attempt（見 `_resolve_selected_attempt()`）。
            selected_attempt, selection_denial = _resolve_selected_attempt(work, stored)
            if selection_denial is not None:
                work["status"] = "STOPPED"
                work["stop_reason"] = selection_denial
                self._append(work, "TERMINAL", {
                    "reason": selection_denial, "criteria_met": False,
                    "criteria_met_claimed": bool(decision.get("criteria_met", False)),
                    "global_reason_code": reason,
                    "selected_attempt_id": decision.get("selected_attempt_id"),
                    "candidate_disposition": disposition})
                return
            if selected_attempt is not None and not bool(
                    decision.get("criteria_met", False)):
                # `criteria_met=false` 時不會走到下面的 `_verify_attempt_
                # criteria_claim()`，但「身分有效」不等於「具備採用資格」：
                # 真正不合格或資格無法驗證的候選，不能因為這是一次誠實的
                # 未達標 STOP 就被正式採用。這裡直接讀 `disposition`（已對
                # 這次重算過，見上）裡這個 attempt 的資格狀態，不重新呼叫
                # 一次判定——同一份事實只算一次。
                selected_state = next(
                    (row["state"] for row in disposition["attempts"]
                     if row["attempt_id"] == selected_attempt.get("attempt_id")), None)
                if selected_state in {"NOT_QUALIFIED", "EVIDENCE_INSUFFICIENT"}:
                    work["status"] = "STOPPED"
                    work["stop_reason"] = SELECTED_ATTEMPT_NOT_QUALIFIED
                    self._append(work, "TERMINAL", {
                        "reason": SELECTED_ATTEMPT_NOT_QUALIFIED, "criteria_met": False,
                        "criteria_met_claimed": False, "global_reason_code": reason,
                        # 原始決策指向誰仍保留在 `work["decisions"]` 供追溯；
                        # 這裡不落盤 `work["selected_attempt_id"]`——它是「已
                        # 接受的正式選擇」，不是「Global 提過的選擇」。
                        "selected_attempt_id": selected_attempt.get("attempt_id"),
                        "candidate_disposition": disposition})
                    return
            if selected_attempt is not None:
                # 「選了哪個候選」與「它有沒有達標」是兩件事：即使這是一次
                # 誠實的未達標 STOP，只要候選本身具備採用資格（上面已核對
                # 過，或這裡是 `criteria_met=true` 路徑、資格隨後一併核驗），
                # Global 明確選過這個 attempt 就是需要保留的事實。
                work["selected_attempt_id"] = selected_attempt.get("attempt_id")
            if not bool(decision.get("criteria_met", False)):
                # 合法的未達標 STOP：候選身分與採用資格都已核對過，但不對
                # 一個本來就沒宣稱達標的候選套用「criteria_met」核驗規則——
                # 可採用不等於已達標，這裡不宣稱、也不誤拒。
                work["status"] = "STOPPED"
                work["stop_reason"] = reason
                self._append(work, "TERMINAL", {
                    "reason": reason, "criteria_met": False,
                    **({"selected_attempt_id": selected_attempt.get("attempt_id")}
                       if selected_attempt is not None else {}),
                    "candidate_disposition": disposition})
                return
            # Global 原始決策已原樣保存於 GLOBAL_DECISION；這裡只決定 Work 終態。
            verification = (
                _verify_attempt_criteria_claim(work, stored, selected_attempt)
                if selected_attempt is not None
                else verify_criteria_met_claim(work, stored))
            work["criteria_verification"] = verification
            if verification["outcome"] == "VERIFIED":
                work["status"] = "COMPLETED"
                work["stop_reason"] = reason
            elif verification["outcome"] == "CONTRADICTED":
                work["status"] = "STOPPED"
                work["stop_reason"] = CRITERIA_MET_CONTRADICTS_EVIDENCE
            else:
                work["status"] = "UNCONFIRMED"
                work["stop_reason"] = CRITERIA_MET_EVIDENCE_UNVERIFIED
            self._append(work, "TERMINAL", {
                "reason": work["stop_reason"],
                "criteria_met": verification["outcome"] == "VERIFIED",
                "criteria_met_claimed": True, "global_reason_code": reason,
                "criteria_verification": verification,
                **({"selected_attempt_id": selected_attempt.get("attempt_id")}
                   if selected_attempt is not None else {}),
                "candidate_disposition": disposition})

    def _revision_claim_denial(
        self, work: dict[str, Any], decision: dict[str, Any],
        based_on_domain_digest: str | None,
    ) -> dict[str, Any] | None:
        """非 CORE 方法的結果階段續作 guard；有矛盾時回傳 STOP 紀錄。

        CORE_UTILIZATION 由 `_core_utilization_revision_denial` 核對結構化續作
        依據。其他方法（如 SETUP_SLACK_MARGIN）的正式協定沒有續作依據欄位，
        Global 唯一可機器核對的宣稱是 `criteria_met`：一邊宣稱已達標、一邊要求
        修訂，等同 CORE 的 `objective_unmet` 不成立，不得放行第二輪。未帶此宣稱
        或宣稱未達標的合法 revision 不受影響；核驗結果只作稽核紀錄。
        """
        if decision.get("criteria_met") is not True or based_on_domain_digest is None:
            return None
        if not work.get("attempts"):
            return None
        attempt = work["attempts"][-1]
        if based_on_domain_digest not in {attempt.get("domain_analysis_digest"),
                                          attempt.get("expert_analysis_collection_digest")}:
            return None  # 不是針對執行結果的修訂（例如提案審查階段）。
        plan = attempt.get("execution_plan") or {}
        changes = (plan.get("canonical_proposal") or {}).get("changes") or {}
        if changes.get("kind") == "core_utilization":
            return None
        return {"reason_code": REVISION_CLAIMS_CRITERIA_MET,
                "decision_id": decision.get("decision_id"),
                "attempt_id": attempt.get("attempt_id"),
                "based_on_domain_response_digest": based_on_domain_digest,
                "engineering_result_digest": attempt.get("engineering_result_digest"),
                "criteria_verification": verify_criteria_met_claim(work, decision)}

    def _core_utilization_revision_denial(
        self, work: dict[str, Any], decision: dict[str, Any],
    ) -> str | None:
        """CORE_UTILIZATION 的 Global 續作 guard；缺一項事實即 STOP。"""
        if not work.get("attempts"):
            return None
        plan = work["attempts"][-1].get("execution_plan") or {}
        changes = (plan.get("canonical_proposal") or {}).get("changes") or {}
        if changes.get("kind") != "core_utilization":
            return None
        from terminux_engine.packages.orfs_run_intelligence.core_utilization import (
            CoreUtilizationError, candidate_window, continuation_decision,
        )
        executed: list[dict[str, Any]] = []
        for attempt in work["attempts"]:
            row = ((attempt.get("execution_plan") or {}).get("canonical_proposal")
                   or {}).get("changes") or {}
            if row.get("kind") == "core_utilization":
                executed.append({"attempt_id": attempt.get("attempt_id"),
                                 "candidate": row.get("candidate")})
        try:
            window = candidate_window(executed)
        except CoreUtilizationError:
            return "STOP_CORE_UTILIZATION_HISTORY_INVALID"
        basis = decision.get("core_utilization_revision_basis")
        if not isinstance(basis, dict):
            return "STOP_CORE_UTILIZATION_REVISION_BASIS_MISSING"
        # Global 宣稱的續作依據**不是**事實來源。凡是 durable 證據自己說得出口
        # 的欄位，一律以證據為準；宣稱與證據不符就停，不讓一次幻覺解鎖第二輪。
        contradiction = self._revision_basis_contradiction(work, basis)
        if contradiction is not None:
            return contradiction
        remaining = self._prepare_public_budget(work).get("remaining") or {}
        allowed, reason = continuation_decision(basis, window=window, remaining=remaining)
        return None if allowed else reason

    #: 續作依據中**可由 durable 工程結果核對**的欄位 → 證據裡對應的判定。
    #: 其餘欄位（Expert 是否支持、目標是否仍未達成）屬於判斷，不在這裡覆寫。
    _VERIFIABLE_REVISION_BASIS = ("comparable", "selection_eligible",
                                  "no_new_protected_violations", "setup_improved",
                                  "target_slack_met", "timing_repaired")

    @staticmethod
    def _evidence_revision_facts(result: dict[str, Any]) -> dict[str, Any]:
        """把 durable 工程結果翻成與續作依據同名的可核對事實。

        只回傳**證據真的說得出口**的欄位；證據沒講的不放進來，也不猜。
        """
        facts: dict[str, Any] = {}
        if result.get("comparable") is not None:
            facts["comparable"] = bool(result["comparable"])
        if result.get("selection_eligible") is not None:
            facts["selection_eligible"] = bool(result["selection_eligible"])
        regressions = result.get("protected_metric_regressions")
        if isinstance(regressions, list):
            facts["no_new_protected_violations"] = not regressions
        verdict = result.get("engineering_verdict")
        if isinstance(verdict, str) and verdict:
            facts["setup_improved"] = verdict == "IMPROVED"
        if result.get("target_slack_met") is not None:
            facts["target_slack_met"] = bool(result["target_slack_met"])
        if result.get("timing_repaired") is not None:
            facts["timing_repaired"] = bool(result["timing_repaired"])
        return facts

    def _revision_basis_contradiction(
        self, work: dict[str, Any], basis: dict[str, Any],
    ) -> str | None:
        """續作依據與 durable 證據對不起來時回傳 STOP 理由；一致則回 `None`。

        沒有工程結果摘要時**不**放行：第二輪必須由第一輪的真實結果驅動，
        「查無紀錄」不等於「可以續作」。
        """
        attempt = work["attempts"][-1]
        result = attempt.get("engineering_result")
        if not isinstance(result, dict) or not result.get("available"):
            return "STOP_CORE_UTILIZATION_REVISION_EVIDENCE_MISSING"
        facts = self._evidence_revision_facts(result)
        for field in self._VERIFIABLE_REVISION_BASIS:
            if field not in facts:
                # 證據沒有這一項，但續作依據卻宣稱它成立 ⇒ 無法核對，不放行。
                if basis.get(field) is True:
                    return "STOP_CORE_UTILIZATION_REVISION_EVIDENCE_MISSING"
                continue
            if basis.get(field) != facts[field]:
                self._append(work, "REVISION_BASIS_CONTRADICTS_EVIDENCE", {
                    "attempt_id": attempt["attempt_id"],
                    "field": field, "claimed": basis.get(field),
                    "evidence": facts[field],
                    "engineering_result_digest": attempt.get(
                        "engineering_result_digest")})
                return "STOP_CORE_UTILIZATION_REVISION_BASIS_CONTRADICTS_EVIDENCE"
        return None

    def _consume_domain(self, work: dict[str, Any], response: dict[str, Any]) -> None:
        kind = response.get("kind")
        if kind not in DOMAIN_KINDS:
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "DOMAIN_RESPONSE_INVALID"
            self._append(work, "DOMAIN_INVALID", {"response_digest": digest_json(response)})
            return
        response_digest = digest_json(response)
        if kind == "PROPOSAL":
            source_proposal = response.get("proposal")
            if not isinstance(source_proposal, dict):
                work["status"] = "UNCONFIRMED"
                work["stop_reason"] = "DOMAIN_PROPOSAL_MISSING"
                self._append(work, "DOMAIN_INVALID", {"response_digest": response_digest})
                return
            package = self._packages.get(work["package"]["package_id"])
            try:
                validation = package.validate(source_proposal)
            except Exception as exc:  # noqa: BLE001 - Package failure is not a daemon crash
                validation = {"status": "REJECTED",
                              "reason_code": "PACKAGE_VALIDATOR_FAILED",
                              "message": type(exc).__name__}
            if not isinstance(validation, dict):
                validation = {"status": "REJECTED",
                              "reason_code": "PACKAGE_VALIDATOR_RESULT_INVALID",
                              "message": "validator must return an object"}
            proposal = validation.pop("normalized_proposal", source_proposal)
            if not isinstance(proposal, dict):
                validation = {"status": "REJECTED",
                              "reason_code": "PACKAGE_ADAPTER_RESULT_INVALID",
                              "message": "normalized proposal must be an object"}
                proposal = source_proposal
            source_response_digest = response_digest
            if proposal is not source_proposal and proposal != source_proposal:
                adaptation = {
                    key: validation.get(key) for key in (
                        "proposal_adapter_version", "adaptation_status",
                        "adaptation_reason") if validation.get(key) is not None}
                response = {**response, "proposal": proposal,
                            "package_adaptation": adaptation,
                            "source_response_digest": source_response_digest}
                response_digest = digest_json(response)
            record = {"proposal_id": "wprop_" + uuid.uuid4().hex,
                      "revision": work["revision"], "response": response,
                      "response_digest": response_digest, "proposal": proposal,
                      "proposal_digest": digest_json(proposal), "validation": validation,
                      "source_proposal": source_proposal,
                      "source_proposal_digest": digest_json(source_proposal),
                      "source_response_digest": source_response_digest,
                      "created_at": time.time()}
            work["proposals"].append(record)
            self._append(work, "DOMAIN_PROPOSAL", {
                "proposal_id": record["proposal_id"], "proposal_digest": record["proposal_digest"],
                "response_digest": response_digest, "validation": validation})
            if validation.get("status") != "PASSED":
                work["pending"] = {"kind": "DOMAIN_REVISION", "message": validation.get("message"),
                                   "reason_code": validation.get("reason_code"),
                                   "validation": dict(validation),
                                   "prior_proposal": record}
                work["status"] = "ANALYZING"
                self._append(work, "DOMAIN_REVISION_REQUIRED", {
                    "proposal_id": record["proposal_id"],
                    "proposal_digest": record["proposal_digest"],
                    "reason_code": validation.get("reason_code"),
                    "message": validation.get("message")})
                return
            seen = work["no_progress"]["proposal_digests"]
            if record["proposal_digest"] in seen:
                work["no_progress"]["count"] += 1
            seen.append(record["proposal_digest"])
            if work["no_progress"]["count"] >= 2:
                work["status"] = "STOPPED"
                work["stop_reason"] = "REPEATED_PROPOSAL_NO_PROGRESS"
                self._append(work, "TERMINAL", {"reason": work["stop_reason"]})
                return
        elif kind == "NEED_EVIDENCE":
            self._collect(work, response.get("request") or {})
            if work["status"] == "ANALYZING":
                enriched = {**response, "collected_evidence": work["evidence_sets"][-1]}
                enriched_digest = digest_json(enriched)
                work["pending"] = {"kind": "GLOBAL_AFTER_DOMAIN",
                                   "domain_response": enriched,
                                   "domain_response_digest": enriched_digest}
                self._append(work, "DOMAIN_EVIDENCE_READY", {
                    "original_domain_response_digest": response_digest,
                    "domain_response_digest": enriched_digest})
            return
        work["pending"] = {"kind": "GLOBAL_AFTER_DOMAIN", "domain_response": response,
                           "domain_response_digest": response_digest}
        work["status"] = "ANALYZING"
        self._append(work, "GLOBAL_REVIEW_REQUIRED", {
            "domain_response_digest": response_digest})

    def _collect(self, work: dict[str, Any], request: dict[str, Any]) -> None:
        request_digest = digest_json(request)
        seen = work["no_progress"]["evidence_digests"]
        if request_digest in seen:
            work["no_progress"]["count"] += 1
        seen.append(request_digest)
        if work["no_progress"]["count"] >= 2:
            work["status"] = "STOPPED"
            work["stop_reason"] = "REPEATED_EVIDENCE_REQUEST"
            self._append(work, "TERMINAL", {"reason": work["stop_reason"]})
            return
        needs_tool = bool(request.get("requires_tool", False))
        cost = {"tool_launches": 1} if needs_tool else {}
        if not self._can_spend(work, cost):
            self._budget_wait(work, "evidence request exceeds remaining budget")
            return
        self._reserve(work, cost)
        request_id = self._new_request(work, "evidence_collect")
        self._append(work, "EVIDENCE_BUDGET_RESERVED", {
            "request_id": request_id, "cost": cost})
        try:
            result = self._runtime.collect_evidence(
                work=work, request=request, request_id=request_id)
        except Exception as exc:  # noqa: BLE001 - 已派送視為未知消耗
            self._complete_reservation(work, cost)
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "EVIDENCE_DISPATCH_UNCONFIRMED"
            self._append(work, "EVIDENCE_DISPATCH_FAILED", {
                "request_id": request_id, "error_type": type(exc).__name__})
            return
        self._complete_reservation(work, cost)
        work["evidence_sets"].append(result)
        self._append(work, "EVIDENCE_COLLECTED", {"request_digest": request_digest,
                                                    "result_digest": digest_json(result)})
        if result.get("status") != "COMPLETE":
            work["status"] = "WAITING_USER" if result.get("needs_user") else "UNCONFIRMED"
            work["pending"] = {"kind": "EVIDENCE_INCOMPLETE", "detail": result}
        else:
            work["status"] = "ANALYZING"

    def _submit_latest(self, work: dict[str, Any]) -> None:
        if not work["proposals"]:
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "NO_VALIDATED_PROPOSAL"
            return
        authorization = work.get("authorization")
        if not isinstance(authorization, dict) or not authorization.get("valid"):
            work["status"] = "WAITING_APPROVAL"
            return
        if authorization["mode"] == "PLAN_ONLY":
            work["status"] = "STOPPED"
            work["stop_reason"] = "PLAN_ONLY_COMPLETE"
            self._append(work, "TERMINAL", {"reason": work["stop_reason"]})
            return
        record = work["proposals"][-1]
        if authorization["mode"] == "PER_PROPOSAL" and record["proposal_digest"] not in \
                authorization.get("approved_proposal_digests", []):
            work["status"] = "WAITING_APPROVAL"
            work["pending"] = {"kind": "PROPOSAL_APPROVAL",
                               "proposal_id": record["proposal_id"],
                               "proposal_digest": record["proposal_digest"],
                               "message": "this concrete proposal requires approval"}
            self._append(work, "PROPOSAL_APPROVAL_REQUIRED", work["pending"])
            return
        proposal = record["proposal"]
        package = self._packages.get(work["package"]["package_id"])
        if isinstance(work.get("execution_seed"), dict):
            checked = self._revalidate_execution_seed(work, record)
            if isinstance(checked, WorkError):
                work["status"] = "STOPPED"
                work["stop_reason"] = checked.code
                work["pending"] = {
                    "kind": "SAVED_PROPOSAL_REJECTED", "message": checked.message}
                self._append(work, "SAVED_PROPOSAL_REJECTED", {
                    "reason_code": checked.code, "message": checked.message})
                return
            work["execution_seed"]["last_revalidated_at"] = time.time()
            work["execution_seed"]["validation"] = checked
            self._append(work, "SAVED_PROPOSAL_REVALIDATED", {
                "proposal_digest": record["proposal_digest"],
                "validator_version": checked.get("validator_version"),
                "baseline_strategy": "RERUN",
            })
        try:
            plan = package.compile_plan(proposal, {
                "work": self._public(work), "baseline": work["baseline"],
                "input": work["input"], "authorization": authorization,
            })
            if not isinstance(plan, dict):
                raise WorkError("PLAN_COMPILE_RESULT_INVALID",
                                "Package compiler must return an object")
        except Exception as exc:  # noqa: BLE001 - Package 邊界收斂為可查詢狀態
            if isinstance(work.get("execution_seed"), dict):
                work["status"] = "STOPPED"
                work["stop_reason"] = "SAVED_PROPOSAL_PLAN_REJECTED"
                work["pending"] = {
                    "kind": "SAVED_PROPOSAL_REJECTED",
                    "reason_code": work["stop_reason"],
                    "message": str(exc),
                }
                self._append(work, "PLAN_REJECTED", {
                    "reason": type(exc).__name__,
                    "reason_code": work["stop_reason"]})
                return
            work["pending"] = {"kind": "DOMAIN_REVISION", "reason_code": "PLAN_COMPILE_FAILED",
                               "message": str(exc), "prior_proposal": record}
            work["status"] = "ANALYZING"
            self._append(work, "PLAN_REJECTED", {"reason": type(exc).__name__})
            return
        denial = self._authorization_denial(work, proposal, plan, authorization)
        if denial is not None:
            work["status"] = "WAITING_USER"
            work["pending"] = {"kind": "AUTHORIZATION_DELTA", **denial}
            self._append(work, "AUTHORIZATION_DELTA_REQUIRED", denial)
            return
        supports_plan = getattr(self._runtime, "supports_plan", None)
        if callable(supports_plan) and not supports_plan(plan):
            # 編譯器有資料格式不等於正式 Runtime 已接線；在保留嘗試／工具
            # 用量前拒絕，不能把缺少 adapter 誤報成 child dispatch 未確認。
            work["status"] = "WAITING_USER"
            work["pending"] = {
                "kind": "PACKAGE_RUNTIME_BINDING_UNAVAILABLE",
                "reason_code": "PACKAGE_RUNTIME_BINDING_UNAVAILABLE",
                "package_id": work["package"]["package_id"],
                "runtime": plan.get("runtime"),
                "message": "Package plan has no formal FlowTask runtime binding",
            }
            self._append(work, "RUNTIME_BINDING_UNAVAILABLE", dict(work["pending"]))
            return
        cost = self._attempt_cost(plan)
        if not self._can_spend(work, cost):
            self._budget_wait(work, "attempt and required verification exceed remaining budget")
            return
        manual_zero_provider = self._manual_zero_provider(work, record, authorization)
        if not manual_zero_provider and not self._can_spend(
                work, {"provider_dispatches": 2}):
            self._budget_wait(work, "attempt requires Domain and Global result feedback budget")
            return
        self._reserve(work, cost)
        attempt_id = "attempt_" + uuid.uuid4().hex
        request_id = self._new_request(work, "attempt_execute")
        attempt = {
            "attempt_id": attempt_id, "revision": work["revision"],
            "proposal_id": record["proposal_id"], "proposal_digest": record["proposal_digest"],
            "authorization_id": authorization["authorization_id"],
            "execution_plan": plan, "execution_plan_digest": digest_json(plan),
            "status": "DISPATCHING", "request_id": request_id,
            # 如實記錄這一次 attempt 走的是哪一條預算規則。
            "provider_feedback": ("NOT_REQUIRED_MANUAL_PROPOSAL"
                                  if manual_zero_provider else "REQUIRED"),
            "control_generation": work["control_generation"], "created_at": time.time(),
            "reserved": cost,
        }
        if isinstance(work.get("execution_seed"), dict):
            attempt["saved_proposal"] = self._saved_seed_binding(
                work["execution_seed"])
        previous = work["attempts"][-1] if work["attempts"] else None
        if isinstance(previous, dict) and previous.get("status") == "UNCONFIRMED":
            attempt["replacement"] = {
                "replaces_attempt_id": previous["attempt_id"],
                "replaces_task_id": previous.get("task_id"),
                "reason_code": previous.get("replacement_reason", "UNCONFIRMED_CHILD"),
            }
        work["attempts"].append(attempt)
        # 在任何 child side effect 之前持久化 EXECUTING／reservation。若 daemon
        # 在 execute 回傳前中斷，重啟只能進對帳，不會把同一 proposal 再 launch。
        work["status"] = "EXECUTING"
        self._append(work, "ATTEMPT_RESERVED", {"attempt_id": attempt_id, "cost": cost,
                                                  "execution_plan_digest": attempt["execution_plan_digest"]})
        try:
            outcome = self._runtime.execute(
                work=work, proposal=proposal, execution_plan=plan,
                authorization=authorization, attempt_id=attempt_id, request_id=request_id)
            if not isinstance(outcome, dict):
                raise WorkError("CHILD_DISPATCH_RESULT_INVALID",
                                "Runtime execute must return an object")
        except Exception as exc:  # noqa: BLE001 - 派送結果未知，保留 reservation 對帳
            attempt["status"] = "UNCONFIRMED"
            attempt["replacement_reason"] = "CHILD_DISPATCH_UNCONFIRMED"
            work["status"] = "RECONCILIATION_REQUIRED"
            work["pending"] = {"kind": "CHILD_RECONCILIATION",
                               "attempt_id": attempt_id,
                               "error_type": type(exc).__name__}
            self._append(work, "ATTEMPT_DISPATCH_UNCONFIRMED", work["pending"])
            return
        attempt["runtime"] = outcome
        attempt["task_id"] = outcome.get("task_id")
        attempt["task_digest"] = outcome.get("task_digest")
        attempt["grant_id"] = outcome.get("grant_id")
        if outcome.get("status") in {"RUNNING", "QUEUED", "WAITING_DEPENDENCY"}:
            attempt["status"] = outcome["status"]
            work["status"] = "EXECUTING"
            self._append(work, "ATTEMPT_DISPATCHED", {
                "attempt_id": attempt_id, "task_id": attempt.get("task_id"),
                "task_digest": attempt.get("task_digest"), "grant_id": attempt.get("grant_id")})
            return
        self._finish_attempt(work, attempt, outcome)

    def _poll_attempt(self, work: dict[str, Any]) -> None:
        attempt = self._running_attempt(work)
        if attempt is None:
            work["status"] = "RECONCILIATION_REQUIRED"
            work["pending"] = {"kind": "MISSING_ACTIVE_ATTEMPT"}
            return
        outcome = self._runtime.poll(work=work, attempt=attempt)
        if outcome.get("status") in {"RUNNING", "QUEUED", "WAITING_DEPENDENCY"}:
            # WAITING_DEPENDENCY：子 FlowTask 正在等待 dependent proof；
            # 依賴仍在進行中，attempt 繼續等待，不呼叫 _finish_attempt。
            return
        self._finish_attempt(work, attempt, outcome)

    #: flow_task 帳本 kind ←→ Work 預算 key。formal proof 啟動會同時記一筆
    #: `tool_launch` 與一筆 `formal_proof_launch`，兩者各自對應自己的額度。
    _LEDGER_KIND_BY_BUDGET_KEY = {
        "tool_launches": "tool_launch",
        "formal_proof_launches": "formal_proof_launch",
        "candidate_materializations": "candidate_materialization",
    }

    @staticmethod
    def _ledger_child_usage(
        work: dict[str, Any], attempt: dict[str, Any],
    ) -> dict[str, dict[str, int]] | None:
        """由 durable 帳本取本 attempt 跨**所有** FlowTask 的外部動作用量。

        來源必須是帳本而不是單一 child 的 in-memory counter：dependent formal
        proof 是**另一個** FlowTask，它的 launch 只會出現在帳本裡。CORE child
        的 counter 看不到它，拿來結算就會把已經消耗的額度誤放回去。

        每個預算鍵回傳三個**分開**的數字，不得合併成一個：

        - ``launched``：已結算為 CONSUMED，代表工具確實啟動過。
        - ``outstanding``：已 PREPARED 但尚未結算，代表額度已被占用、
          但**還不能斷定**工具是否真的啟動。
        - ``held``：``launched + outstanding``，結算時實際不予釋放的量。

        `held` 只用來決定「釋放多少」；報告與證據必須保留前兩者的區別，
        不可以把 outstanding 說成已經啟動或已經消耗。

        回傳 `None` 代表**沒有可用的帳本證據**：帳本未設定、讀不到，或它根本
        沒觀察到這個 attempt 的 FlowTask。呼叫端必須 fail closed（全額
        CONSUMED）。「查無紀錄」不等於「沒有發生」——child 已經啟動但帳本沒
        記到時，額度必須照算，不得退回。
        """
        from terminux_engine.expert_ai import batch_ledger as BL

        try:
            ledger = BL.configured_ledger()
            if ledger is None:
                return None
            rows = BL.read_rows(ledger.path)
        except Exception:  # noqa: BLE001 - 讀不到帳本一律 fail closed，不猜 0
            return None

        work_id = str(work.get("work_id"))
        attempt_id = str(attempt.get("attempt_id") or "")
        by_kind = EngineeringWorkService._LEDGER_KIND_BY_BUDGET_KEY
        keys = {kind: key for key, kind in by_kind.items()}
        consumed = {key: 0 for key in by_kind}
        outstanding = {key: 0 for key in by_kind}
        observed = False
        for row in rows:
            if row.get("source") != "flow_task" or str(row.get("work_id")) != work_id:
                continue
            if str(row.get("attempt_id") or "") != attempt_id:
                continue
            # 這個 attempt 的 FlowTask 確實在帳本裡（含 create／start 生命週期列），
            # 帳本對它的 0 才有意義。
            observed = True
            key = keys.get(str(row.get("kind")))
            if key is None:
                continue
            quantity = int(row.get("quantity") or 0)
            operation = str(row.get("operation") or "")
            if operation in {"reserve", "prepared"}:
                outstanding[key] += quantity
            elif operation in {"consume", "consumed"}:
                consumed[key] += quantity
                outstanding[key] = max(0, outstanding[key] - quantity)
            elif operation in {"release", "released"}:
                outstanding[key] = max(0, outstanding[key] - quantity)
        if not observed:
            return None
        # 仍然 outstanding 的 reservation ＝ 額度已被占用、但工具是否真的啟動
        # 尚未確認。它**不予釋放**（fail closed），但也**不算**已啟動。
        return {key: {"launched": consumed[key],
                      "outstanding": outstanding[key],
                      "held": consumed[key] + outstanding[key]}
                for key in by_kind}

    @staticmethod
    def _actual_child_consumption(
        outcome: dict[str, Any], reserved_cost: dict[str, int],
        ledger_usage: dict[str, dict[str, int]] | None,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """依 durable 執行事實區分 CONSUMED 與 RELEASED。

        - engineering_attempts: attempt 已經發生，一律 CONSUMED。
        - tool_launches / candidate_materializations / formal_proof_launches:
          只有在 runtime 明確回報 child 從未建立（`_CHILD_NEVER_STARTED`），
          或帳本證明該次外部動作從未落盤時，才 RELEASED。
        - `ledger_usage is None`（帳本不可用）時不釋放任何額度：缺少紀錄不是
          「沒有發生」。

        這裡用的是 `held`（launched ＋ outstanding）——它決定**不釋放**多少，
        不代表那些額度都已經啟動過工具。兩者的區別由
        `_ledger_child_usage()` 保留，呼叫端報告時不得合併。
        """
        consumed = dict(reserved_cost)
        released: dict[str, int] = {}

        if str(outcome.get("reason_code") or "") in _CHILD_NEVER_STARTED:
            for key in _CHILD_LAUNCH_BUDGET_KEYS:
                if key in consumed and consumed[key] > 0:
                    released[key] = consumed.pop(key)
            return consumed, released

        if ledger_usage is None:
            return consumed, released

        for key in _CHILD_LAUNCH_BUDGET_KEYS:
            if key not in consumed:
                continue
            reserved = int(consumed[key])
            # 實際用量不得超過預扣：超額本身是對帳問題，但在這裡只能不釋放。
            held = int((ledger_usage.get(key) or {}).get("held", 0))
            actual = min(reserved, held)
            if actual < reserved:
                released[key] = reserved - actual
                consumed[key] = actual
        return consumed, released

    def _finish_attempt(
        self, work: dict[str, Any], attempt: dict[str, Any], outcome: dict[str, Any],
    ) -> None:
        attempt["outcome"] = outcome
        attempt["outcome_digest"] = digest_json(outcome)
        attempt["status"] = str(outcome.get("status") or "UNCONFIRMED")
        attempt["finished_at"] = time.time()
        reserved_cost = dict(attempt.get("reserved") or {})
        ledger_usage = self._ledger_child_usage(work, attempt)
        consumed, released = self._actual_child_consumption(
            outcome, reserved_cost, ledger_usage)
        consumed_active = {k: v for k, v in consumed.items() if v > 0}
        if consumed_active:
            self._complete_reservation(work, consumed_active)
        if released:
            self._release_reservation(work, released)
            attempt["released_reservations"] = dict(released)
        # terminal child 與後續分析狀態必須同一筆 durable snapshot 保存。
        # 否則 transition 邊界或 daemon 重啟會讀回 EXECUTING，但已沒有
        # running attempt，錯誤進入 MISSING_ACTIVE_ATTEMPT reconciliation。
        # Runtime 交回的有界工程結果摘要。它是 Expert／Global 結果回饋與續作
        # guard 的唯一工程事實來源；缺席就是缺席，不在這裡補值。
        engineering = outcome.get("engineering_result")
        if isinstance(engineering, dict):
            attempt["engineering_result"] = engineering
            attempt["engineering_result_digest"] = digest_json(engineering)
        work["status"] = "ANALYZING_RESULT"
        self._append(work, "ATTEMPT_FINISHED", {
            "attempt_id": attempt["attempt_id"], "status": attempt["status"],
            "outcome_digest": attempt["outcome_digest"],
            "engineering_result": attempt.get("engineering_result"),
            "engineering_result_digest": attempt.get("engineering_result_digest"),
            "evidence": outcome.get("evidence"),
        })

    #: manual 免 provider 路徑在 attempt 派送當下就記下的標記。
    MANUAL_PROVIDER_FEEDBACK = "NOT_REQUIRED_MANUAL_PROPOSAL"

    def _close_manual_attempt(self, work: dict[str, Any],
                              attempt: dict[str, Any]) -> None:
        """manual proposal 的 attempt 收束：沒有 Domain 分析，也沒有 Global 決策。

        使用者自己送出、自己核准的 typed proposal 這條路上**不存在** Domain
        專家與 Global 決策——`_manual_zero_provider()` 的既有說明就是這樣寫的。
        因此這裡不呼叫那兩個 role，也**不捏造** Domain analysis：
        `domain_analysis` 留空並如實記錄豁免原因。

        判定所需的內容全部取自正式來源：strict typed proposal 的
        `proposal_digest`、使用者核准紀錄、Profile／policy 的 intake 宣告，
        以及有效的 execution authorization。資格在這裡**重新驗證一次**，
        不只相信派送當下的標記；任一項不成立就退回既有 provider 規則。

        Work 的 `WAITING_USER`／`UNCONFIRMED` 語意不變：attempt 自己回報
        `UNCONFIRMED` 時仍然是 `UNCONFIRMED`，不因為免了 provider 就改判。
        """
        record = self._latest_approved_manual_proposal(work)
        if (record is None
                or record.get("proposal_digest") != attempt.get("proposal_digest")
                or not self._manual_zero_provider(
                    work, record, work.get("authorization"))):
            return None
        if attempt.get("result_analysis") == self.MANUAL_PROVIDER_FEEDBACK:
            # 已收束過：重複推進不得再寫一筆事件，也不改動既有判定。
            return True
        attempt["domain_analysis"] = None
        attempt["domain_analysis_digest"] = None
        attempt["result_analysis"] = self.MANUAL_PROVIDER_FEEDBACK
        # **Work 不自行下結論**：免掉的只是兩次不存在的 provider feedback，
        # 不是工程判定。attempt 自報 UNCONFIRMED 仍是 UNCONFIRMED；其餘一律
        # 交回使用者裁決，不產生 terminal verdict。
        if attempt.get("status") == "UNCONFIRMED":
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "MANUAL_ATTEMPT_UNCONFIRMED"
        else:
            work["status"] = "WAITING_USER"
        work["pending"] = {
            "kind": "MANUAL_RESULT_REVIEW",
            "attempt_id": attempt["attempt_id"],
            "attempt_status": attempt.get("status"),
            "provider_feedback": self.MANUAL_PROVIDER_FEEDBACK,
            "message": ("this attempt ran a user-submitted, user-approved typed "
                        "proposal; no Domain analysis or Global decision exists "
                        "for it and none was fabricated; the engineering verdict "
                        "is the user's")}
        # 狀態與 pending 先落定，再寫事件——否則快照帶不到這次的判定，
        # 重新讀回時會退回 ANALYZING_RESULT 並一再重跑。
        self._append(work, "MANUAL_RESULT_ANALYSIS_NOT_REQUIRED", {
            "attempt_id": attempt["attempt_id"],
            "proposal_id": record.get("proposal_id"),
            "proposal_digest": record.get("proposal_digest"),
            "provider_feedback": self.MANUAL_PROVIDER_FEEDBACK,
            "attempt_status": attempt.get("status"),
            "outcome_digest": attempt.get("outcome_digest")})
        return True

    def _analyze_result(self, work: dict[str, Any]) -> None:
        attempt = work["attempts"][-1]
        # 派送當下已判定為 manual 免 provider 的 attempt，收束時不得又回頭
        # 要求那兩次不存在的 provider feedback。
        if attempt.get("provider_feedback") == self.MANUAL_PROVIDER_FEEDBACK \
                and self._close_manual_attempt(work, attempt):
            return
        domain = self._call_role(work, "domain", "execution_analysis", {
            "attempt": attempt, "history": self._history_context(work),
            "criteria": work["criteria"],
        })
        if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
            return
        if domain.get("kind") != "EXECUTION_ANALYSIS":
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "DOMAIN_RESULT_ANALYSIS_INVALID"
            self._append(work, "DOMAIN_INVALID", {"response_digest": digest_json(domain)})
            return
        domain_digest = digest_json(domain)
        attempt["domain_analysis"] = domain
        attempt["domain_analysis_digest"] = domain_digest
        self._append(work, "DOMAIN_RESULT_ANALYSIS", {
            "attempt_id": attempt["attempt_id"], "domain_response_digest": domain_digest})
        decision = self._call_role(work, "global", "result_decision", {
            "domain_response": domain, "domain_response_digest": domain_digest,
            "attempt": attempt, "history": self._history_context(work),
        })
        if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
            return
        self._consume_global(work, decision, domain_digest)

    def _call_role(
        self, work: dict[str, Any], role: str, purpose: str, payload: dict[str, Any],
        *, dispatch_class: str | None = None,
    ) -> dict[str, Any]:
        if not self._can_spend(work, {"provider_dispatches": 1}):
            self._budget_wait(work, f"one {role} {purpose} dispatch is required")
            return {"kind": "STOP", "reason_code": "PROVIDER_BUDGET_EXHAUSTED",
                    "message": "provider budget exhausted"} if role == "global" else {
                        "kind": "NO_PROPOSAL", "message": "provider budget exhausted"}
        self._reserve(work, {"provider_dispatches": 1})
        request_id = self._new_request(work, f"{role}_{purpose}")
        generation = work["control_generation"]
        call = {
            "request_id": request_id, "role": role, "purpose": purpose,
            "revision": work["revision"], "control_generation": generation,
            "request_digest": digest_json(payload), "status": "DISPATCHED",
            "started_at": time.time(),
        }
        if dispatch_class is not None:
            call["dispatch_class"] = dispatch_class
        work["role_calls"].append(call)
        self._append(work, "ROLE_DISPATCHED", call)
        try:
            response = self._experts.call(
                role=role, capability=work["package"]["capability"], purpose=purpose,
                request=payload, request_id=request_id)
        except Exception as exc:  # noqa: BLE001 - provider failure is work state
            error_code = str(getattr(exc, "code", type(exc).__name__))
            rejected = getattr(exc, "rejected_response", None)
            provider_failure = getattr(exc, "provider_metadata", None)
            response_diagnostics = getattr(exc, "response_diagnostics", None)
            call.update({"status": "FAILED", "error_type": type(exc).__name__,
                         "error_code": error_code,
                         "error_message": str(getattr(exc, "message", str(exc))),
                         "finished_at": time.time()})
            if isinstance(provider_failure, dict):
                call["provider"] = provider_failure
            if isinstance(response_diagnostics, dict):
                call["response_diagnostics"] = response_diagnostics
            if isinstance(rejected, dict):
                call["rejected_response_digest"] = digest_json(rejected)
                call["rejected_response_fields"] = sorted(
                    str(key) for key in rejected)
                call["rejected_response_kind"] = rejected.get("kind")
            self._complete_reservation(work, {"provider_dispatches": 1})
            corrected = self._maybe_correct_role_format(
                work, role, purpose, payload, call, exc)
            if corrected is not None:
                return corrected
            if work["status"] != "UNCONFIRMED":
                work["status"] = "UNCONFIRMED"
                work["stop_reason"] = "PROVIDER_UNAVAILABLE"
            self._append(work, "ROLE_FAILED", {"request_id": request_id,
                                                "error_type": type(exc).__name__,
                                                "error_code": error_code})
            return {"kind": "STOP", "reason_code": "PROVIDER_UNAVAILABLE",
                    "message": "provider call failed"} if role == "global" else {
                        "kind": "NO_PROPOSAL", "message": "provider call failed"}
        if not isinstance(response, dict):
            self._complete_reservation(work, {"provider_dispatches": 1})
            call.update({"status": "FAILED", "error_type": "ProviderResultTypeError",
                         "error_code": "PROVIDER_RESPONSE_INVALID",
                         "finished_at": time.time()})
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "PROVIDER_RESPONSE_INVALID"
            self._append(work, "ROLE_FAILED", {"request_id": request_id,
                                                "error_type": "ProviderResultTypeError",
                                                "error_code": "PROVIDER_RESPONSE_INVALID"})
            return {"kind": "STOP", "reason_code": "PROVIDER_RESPONSE_INVALID",
                    "message": "provider result must be an object"}
        provider_call = response.pop("_terminux_provider_call", None)
        self._complete_reservation(work, {"provider_dispatches": 1})
        call.update({"status": "COMPLETED", "response_digest": digest_json(response),
                     "finished_at": time.time()})
        if isinstance(provider_call, dict):
            call["provider"] = provider_call
        if work["control_generation"] != generation or work["status"] in TERMINAL_STATUSES:
            work["late_responses"].append({"request_id": request_id,
                                            "response_digest": call["response_digest"],
                                            "ignored": True, "received_at": time.time()})
            self._append(work, "ROLE_RESPONSE_IGNORED", work["late_responses"][-1])
            return {"kind": "STOP", "reason_code": "STALE_RESPONSE", "message": "stale response"}
        self._append(work, "ROLE_COMPLETED", {"request_id": request_id,
                                               "response_digest": call["response_digest"]})
        return response

    def _maybe_correct_role_format(
        self, work: dict[str, Any], role: str, purpose: str,
        payload: dict[str, Any], call: dict[str, Any], error: Exception,
    ) -> dict[str, Any] | None:
        return None

    def _authorization_denial(
        self, work: dict[str, Any], proposal: dict[str, Any],
        plan: dict[str, Any], authorization: dict[str, Any],
    ) -> dict[str, Any] | None:
        if work["package"]["capability"] not in authorization["allowed_capabilities"]:
            return {"reason_code": "CAPABILITY_OUT_OF_SCOPE",
                    "required": work["package"]["capability"]}
        method = str(proposal.get("method") or work.get("requested_method") or "")
        allowed = authorization["allowed_methods"]
        if "*" not in allowed and method not in allowed:
            return {"reason_code": "METHOD_OUT_OF_SCOPE", "required": method}
        roots = set(plan.get("writable_roots") or [])
        allowed_roots = set(authorization.get("writable_roots") or [])
        if roots and not roots.issubset(allowed_roots):
            return {"reason_code": "WRITABLE_ROOT_OUT_OF_SCOPE",
                    "required": sorted(roots - allowed_roots)}
        package = self._packages.get(work["package"]["package_id"])
        boundary = authorization.get("allowed_changes") or {}
        # CORE_UTILIZATION 的相對範圍只能由本 Work 的已執行 lineage 推導。
        # 這裡不信任模型帶回的 previous 值，且把同一份 context 固定到 plan、
        # authorization decision 與後續 task digest 的輸入。
        changes = proposal.get("changes") if isinstance(proposal.get("changes"), dict) else {}
        if (changes.get("kind") == "core_utilization"
                and changes.get("parameter") == "CORE_UTILIZATION"):
            executed: list[dict[str, Any]] = []
            for prior in work.get("attempts") or []:
                canonical = (prior.get("execution_plan") or {}).get("canonical_proposal")
                prior_changes = canonical.get("changes") if isinstance(canonical, dict) else None
                if (isinstance(prior_changes, dict)
                        and prior_changes.get("kind") == "core_utilization"):
                    executed.append({"attempt_id": prior.get("attempt_id"),
                                     "candidate": prior_changes.get("candidate")})
            from terminux_engine.packages.orfs_run_intelligence.core_utilization import candidate_window
            try:
                window = candidate_window(executed).to_json_dict()
            except ValueError as exc:
                return {"reason_code": "CORE_UTILIZATION_HISTORY_INVALID",
                        "required": str(exc)}
            boundary = {**boundary, "executed_attempts": executed}
            plan["authorization_context"] = window
            plan["core_utilization_revision_context"] = {
                key: value for key, value in window.items()
                if key in {"previous_executed_value", "previous_attempt_id",
                           "executed_candidates", "allowed_candidate_minimum",
                           "allowed_candidate_maximum"}}
            authorization.setdefault("decision_contexts", []).append({
                "attempt_proposal_digest": digest_json(proposal),
                "core_utilization": dict(plan["authorization_context"]),
            })
        elif changes.get("kind") == "timing_parameter":
            # SETUP_SLACK_MARGIN 沒有 CORE_UTILIZATION 那種相對範圍，但同樣不能
            # 讓一次 REQUEST_REVISION 重新提出先前**已經真的跑過**的候選值——
            # 那不是修訂，是重花一次真實 EDA 額度重播已知結果。history 只由本
            # Work 的已執行 lineage 推導，不信任模型宣稱的「這是新方案」。
            executed = []
            for prior in work.get("attempts") or []:
                canonical = (prior.get("execution_plan") or {}).get("canonical_proposal")
                prior_changes = canonical.get("changes") if isinstance(canonical, dict) else None
                if (isinstance(prior_changes, dict)
                        and prior_changes.get("kind") == "timing_parameter"
                        and prior_changes.get("parameter") == changes.get("parameter")):
                    executed.append({"attempt_id": prior.get("attempt_id"),
                                     "candidate": prior_changes.get("candidate")})
            if executed:
                boundary = {**boundary, "executed_attempts": executed}
        package_denial = package.authorization_check(proposal, boundary)
        if package_denial is not None:
            return package_denial
        return None

    def _revision_covered(self, work: dict[str, Any], authorization: dict[str, Any]) -> bool:
        if authorization.get("objective") != work["objective"]:
            return False
        method = work.get("requested_method")
        allowed = authorization.get("allowed_methods") or []
        return method is None or "*" in allowed or method in allowed

    def _prepare_public_budget(self, work: dict[str, Any]) -> dict[str, Any]:
        limits = work["budget"]["limits"]
        # 歷史 Work snapshot 沒有後來才加入的 sub-budget 欄位。這裡**唯讀**
        # 補成 0：不回寫事件、不改動已簽出的 authorization digest，也不因為
        # 欄位缺席就放寬新 Work 的額度檢查（缺 limit ⇒ remaining 為 0）。
        used = {**self._zero_usage(), **work["budget"]["used"]}
        reserved = {**self._zero_usage(), **work["budget"]["reserved"]}
        # 用途用量必須跟著 snapshot 落盤：只留在 runtime dict 的話，重啟讀回
        # 會歸零，等於同一個 Work 每重啟一次就能再收一次 baseline。
        purpose_used = dict(work["budget"].get("purpose_used") or {})
        return {"limits": dict(limits), "used": used, "reserved": reserved,
                "purpose_used": purpose_used,
                "remaining": {key: limits.get(key, 0) - used.get(key, 0) - reserved.get(key, 0)
                              for key in limits}}

    @staticmethod
    def _zero_usage() -> dict[str, int]:
        return {"engineering_attempts": 0, "provider_dispatches": 0,
                "tool_launches": 0, "formal_proof_launches": 0,
                "candidate_materializations": 0}

    @classmethod
    def _normalize_limits(cls, limits: dict[str, int]) -> dict[str, int]:
        defaults = {"engineering_attempts": 2, "provider_dispatches": 12,
                    "tool_launches": 5, "candidate_materializations": 2}
        result = dict(defaults)
        # baseline collection 的用途額度：不宣告就是 0（不得自行收集 baseline）。
        optional = {"work_active_time_sec", "formal_proof_launches",
                    cls.PURPOSE_BASELINE_TOOL_LAUNCHES}
        for key, value in limits.items():
            if (key not in result and key not in optional) or not isinstance(
                    value, int) or isinstance(value, bool) or value < 0:
                raise WorkError("WORK_BUDGET_INVALID", f"invalid budget {key}")
            result[key] = value
        return result

    @staticmethod
    def _active_time_exhausted(work: dict[str, Any]) -> bool:
        """以 parent authorization 累計的 active time 收束工作。

        PAUSED／WAITING 期間 ``active_started_at`` 為 None，不會消耗此額度。
        """
        limit = (work.get("budget") or {}).get("limits", {}).get(
            "work_active_time_sec")
        if not isinstance(limit, int):
            return False
        timing = work.get("time") or {}
        elapsed = float(timing.get("active_elapsed_sec") or 0.0)
        started = timing.get("active_started_at")
        if isinstance(started, (int, float)):
            elapsed += max(0.0, time.time() - float(started))
        return elapsed >= limit

    #: manual zero-provider 路徑必須在 durable event log 裡看到的兩筆動作。
    MANUAL_PROPOSAL_ACTIONS = ("USER_PROPOSAL_SUBMITTED", "USER_PROPOSAL_APPROVED")

    @staticmethod
    def _latest_approved_manual_proposal(work: dict[str, Any]) -> dict[str, Any] | None:
        """最後一筆**已核准**的 USER_SUBMITTED proposal；沒有就回 `None`。"""
        for record in reversed(work.get("proposals") or []):
            if not isinstance(record, dict):
                continue
            if record.get("origin") != PROPOSAL_ORIGIN_USER:
                continue
            approval = record.get("approval")
            if isinstance(approval, dict) and approval.get("status") == PROPOSAL_APPROVED:
                return record
        return None

    def _manual_zero_provider(self, work: dict[str, Any], record: dict[str, Any],
                              authorization: dict[str, Any] | None) -> bool:
        """這一次 attempt 是否走**嚴格限定**的 manual 免 provider 路徑。

        Domain／Global 的 result feedback 預算是留給「AI 提出的 proposal」用的：
        那條路上真的會有 Domain 專家與 Global 決策要回饋。使用者自己送出、
        自己核准的 typed proposal 沒有這兩者，卻仍被要求保留兩次 provider
        額度——在 provider 額度為 0 的授權下，manual 路徑因此完全走不通。

        這裡只豁免**沒有發生的**那兩次 provider feedback。materialization、
        tool launch、formal proof launch 與 engineering attempt 一律照算。

        只有下列條件同時成立才回 `True`；任何一項不成立都退回既有規則：

        1. proposal 的 durable origin 是 `USER_SUBMITTED`（不接受 request 宣告）。
        2. 同一份 proposal digest 已被使用者核准。
        3. Profile／policy 兩側都允許 `USER_SUBMITTED` 與這個 method。
        4. 另有有效的 execution authorization。
        5. durable event log 裡確實有 submit 與 approve 兩筆，且指向同一 digest。
        """
        if not isinstance(authorization, dict) or not authorization.get(
                "authorization_id"):
            return False
        if record.get("origin") != PROPOSAL_ORIGIN_USER:
            return False
        approval = record.get("approval")
        if not isinstance(approval, dict) or approval.get("status") != "APPROVED":
            return False
        digest = record.get("proposal_digest")
        if not isinstance(digest, str) or not digest:
            return False

        scope_info = self._profile_resolver(work["profile_id"])
        intake = (scope_info or {}).get("proposal_intake") \
            if isinstance(scope_info, dict) else None
        if not isinstance(intake, dict) or not intake.get("allowed"):
            return False
        origins = intake.get("allowed_origins")
        methods = intake.get("allowed_method_identities")
        if not isinstance(origins, list) or PROPOSAL_ORIGIN_USER not in origins:
            return False
        if (isinstance(methods, list)
                and record.get("method_identity") not in methods):
            return False

        # durable event log 是唯一依據：重啟之後也必須看得到這兩筆。
        observed = {action: set() for action in self.MANUAL_PROPOSAL_ACTIONS}
        for event in self._reader._store.query(
                user_id=work["scope"]["member_id"], event_type=EVENT_WORK,
                order="asc"):
            payload = event.payload or {}
            if payload.get("work_id") != work["work_id"]:
                continue
            action = payload.get("action")
            if action in observed:
                detail = payload.get("detail") or {}
                if (detail.get("origin") == PROPOSAL_ORIGIN_USER
                        and detail.get("proposal_id") == record.get("proposal_id")):
                    observed[action].add(detail.get("proposal_digest"))
        return all(digest in observed[action]
                   for action in self.MANUAL_PROPOSAL_ACTIONS)

    @staticmethod
    def _attempt_cost(plan: dict[str, Any]) -> dict[str, int]:
        """一次 attempt 的外部動作成本。

        `formal_proof_launches` 是 opt-in sub-budget：沒宣告的 plan 成本一位元
        都不變。宣告了就必須同時佔用工具啟動額度——formal proof 本身就是一次
        tool launch，兩種額度任一不足都會讓 `_can_spend()` 在 child 建立前
        拒絕（fail closed），不會只扣掉其中一種就派送。
        """
        cost = {"engineering_attempts": int(plan.get("engineering_attempts", 1)),
                "tool_launches": int(plan.get("tool_launches", 0)),
                "candidate_materializations": int(plan.get("candidate_materializations", 0))}
        formal_proofs = int(plan.get("formal_proof_launches", 0))
        if formal_proofs:
            cost["formal_proof_launches"] = formal_proofs
            cost["tool_launches"] = max(cost["tool_launches"], formal_proofs)
        return cost

    #: 用途額度鍵。candidate 與 formal 本來就各有一把；baseline collection
    #: 先前只受總 `tool_launches` 限制，於是「baseline 上限 0」在執行期沒有
    #: 對應的 gate，durable 重用被拒時會借用總額度跑出真實 baseline ORFS。
    PURPOSE_BASELINE_TOOL_LAUNCHES = "baseline_tool_launches"

    def _purpose_allows(self, work: dict[str, Any], purpose: str,
                        amount: int = 1) -> bool:
        """用途額度檢查。**未宣告該用途 ⇒ 視為 0**（fail closed）。

        用途額度與總額度是兩道獨立的門，兩道都要過；其他用途的剩餘額度不能
        被借用。呼叫點必須在 reservation 與 child dispatch **之前**。
        """
        budget = work.get("budget") or {}
        allowed = (budget.get("limits") or {}).get(purpose, 0)
        used = (budget.get("purpose_used") or {}).get(purpose, 0)
        return used + amount <= allowed

    def _purpose_consume(self, work: dict[str, Any], purpose: str,
                         amount: int = 1) -> None:
        """記錄用途已耗用量。與 ledger 的總額度各自獨立累計。"""
        budget = work.setdefault("budget", {})
        used = budget.setdefault("purpose_used", {})
        used[purpose] = used.get(purpose, 0) + amount

    def _can_spend(self, work: dict[str, Any], cost: dict[str, int]) -> bool:
        limits = work["budget"]["limits"]
        if not all(work["budget"]["used"].get(key, 0)
                   + work["budget"]["reserved"].get(key, 0) + value
                   <= limits.get(key, 0) for key, value in cost.items()):
            return False
        ledger = configured_ledger()
        if ledger is None:
            return True
        # 即時帳本是跨重啟的權威累計：Runtime counter 被重置或替換時仍不得超額。
        recorded = ledger.work_totals(str(work["work_id"]))
        return all(recorded.get(key, {}).get("consumed", 0)
                   + recorded.get(key, {}).get("reserved", 0) + value
                   <= limits.get(key, 0) for key, value in cost.items()
                   if key in recorded)

    def _reserve(self, work: dict[str, Any], cost: dict[str, int]) -> None:
        """先保留帳本，再讓 Runtime 取得可派送的預算。

        EventStore 與 JSONL 是兩個 storage；這裡只建立可恢復的
        PREPARED→CONSUMED／RELEASED 協定，並不宣稱跨 storage 原子提交。
        """
        ledger = configured_ledger()
        reservations = work.setdefault("_ledger_reservations", [])
        if ledger is not None:
            attempt = (work.get("attempts") or [])[-1] if work.get("attempts") else {}
            ordinal = len(work.get("request_ids") or []) + 1
            for key, value in cost.items():
                if value <= 0:
                    continue
                kind = {"provider_dispatches": "provider_dispatch",
                        "tool_launches": "tool_launch",
                        "formal_proof_launches": "formal_proof_launch",
                        "candidate_materializations": "candidate_materialization",
                        "engineering_attempts": "engineering_attempt"}[key]
                idem = f"work:{work['work_id']}:{work.get('sequence', 0) + 1}:{ordinal}:{key}"
                row = ledger.prepare_reservation(
                    source="work_runtime", work_id=str(work["work_id"]), task_id=None,
                    kind=kind, quantity=value, idempotency_key=idem,
                    identity={
                        "attempt_id": attempt.get("attempt_id"),
                        "authorization_digest": (work.get("authorization") or {}).get(
                            "authorization_digest"),
                        "session_id": (work.get("scope") or {}).get("origin_session_id"),
                    })
                if not any(item.get("reservation_id") == row["reservation_id"]
                           for item in reservations):
                    reservations.append({
                        "reservation_id": row["reservation_id"], "kind": kind,
                        "budget_key": key, "quantity": value,
                        "idempotency_key": idem, "state": "PREPARED"})
        for key, value in cost.items():
            # 歷史 snapshot 可能沒有這個 sub-budget 鍵；讀成 0 後再累加。
            work["budget"]["reserved"][key] = work["budget"]["reserved"].get(key, 0) + value

    def _complete_reservation(self, work: dict[str, Any], cost: dict[str, int]) -> None:
        """外部動作已送出或完成；待同一 Work event 落盤後才結算。"""
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in work.get("_ledger_reservations") or []:
            if row.get("state") == "PREPARED":
                by_key.setdefault(str(row.get("budget_key")), []).append(row)
        for key, value in cost.items():
            reserved_now = work["budget"]["reserved"].get(key, 0)
            work["budget"]["reserved"][key] = max(0, reserved_now - value)
            work["budget"]["used"][key] = work["budget"]["used"].get(key, 0) + value
            remaining = value
            for row in by_key.get(key, []):
                if remaining <= 0:
                    break
                if int(row.get("quantity", 0)) > remaining:
                    raise WorkError("LEDGER_RESERVATION_SPLIT_UNSUPPORTED",
                                    "a reservation cannot be partially consumed")
                row["state"] = "CONSUME_PENDING"
                remaining -= int(row["quantity"])
            if configured_ledger() is not None and remaining:
                raise WorkError("LEDGER_RESERVATION_LINEAGE_MISSING",
                                f"missing prepared reservation for {key}")

    def _release_reservation(self, work: dict[str, Any], cost: dict[str, int]) -> None:
        """確定未派送時釋放 reservation；同樣需由後續 Work event 引用。"""
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in work.get("_ledger_reservations") or []:
            if row.get("state") == "PREPARED":
                by_key.setdefault(str(row.get("budget_key")), []).append(row)
        for key, value in cost.items():
            work["budget"]["reserved"][key] = max(
                0, work["budget"]["reserved"].get(key, 0) - value)
            remaining = value
            for row in by_key.get(key, []):
                if remaining <= 0:
                    break
                if int(row.get("quantity", 0)) > remaining:
                    raise WorkError("LEDGER_RESERVATION_SPLIT_UNSUPPORTED",
                                    "a reservation cannot be partially released")
                row["state"] = "RELEASE_PENDING"
                remaining -= int(row["quantity"])
            if configured_ledger() is not None and remaining:
                raise WorkError("LEDGER_RESERVATION_LINEAGE_MISSING",
                                f"missing prepared reservation for {key}")

    def _budget_wait(self, work: dict[str, Any], message: str) -> None:
        work["status"] = "WAITING_USER"
        work["pending"] = {"kind": "BUDGET_DELTA", "message": message}
        self._append(work, "BUDGET_INSUFFICIENT", work["pending"])

    def _new_request(self, work: dict[str, Any], purpose: str) -> str:
        request_id = f"{work['work_id']}:{purpose}:{len(work['request_ids']) + 1}"
        work["request_ids"].append(request_id)
        return request_id

    @staticmethod
    def _evidence_context(work: dict[str, Any]) -> dict[str, Any]:
        return {"baseline": work["baseline"], "sets": work["evidence_sets"]}

    @staticmethod
    def _history_context(work: dict[str, Any]) -> dict[str, Any]:
        return {
            "revision": work["revision"],
            "proposals": [{"proposal_digest": row["proposal_digest"],
                           "validation": row["validation"]} for row in work["proposals"]],
            "attempts": [{"attempt_id": row["attempt_id"], "proposal_digest": row["proposal_digest"],
                          "status": row["status"], "outcome_digest": row.get("outcome_digest"),
                          # 有界摘要：沒有它，Expert 只看得到 digest，無從判斷
                          # 先前到底量到什麼。
                          "engineering_result": row.get("engineering_result"),
                          "engineering_result_digest": row.get("engineering_result_digest")}
                         for row in work["attempts"]],
            "revisions": list(work["revisions"]),
            "evidence_set_digests": [digest_json(row) for row in work["evidence_sets"]],
            "pending": work.get("pending"), "budget": work["budget"],
            # Global 在終局決策**之前**就看得到跨輪候選資格摘要（哪個已保留、
            # 哪個仍在執行、有沒有候選合格），不必等到 TERMINAL 才知道；不
            # 混入「最佳」的判斷，也不在這裡替 Global 指定必選哪一個。
            "candidate_disposition": candidate_qualification_history(work),
        }

    @staticmethod
    def _running_attempt(work: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in reversed(work["attempts"]):
            if attempt["status"] in {"RUNNING", "QUEUED", "DISPATCHING", "WAITING_DEPENDENCY"}:
                # WAITING_DEPENDENCY：attempt 正等待 dependent proof 完成，仍視為活躍。
                return attempt
        return None

    def _control(self, work_id: str, status: str, reason: str) -> dict[str, Any]:
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            if work["status"] in TERMINAL_STATUSES:
                return self._error("WORK_TERMINAL", "terminal work cannot be controlled")
            previous_status = work["status"]
            previous_pending = work.get("pending")
            work["control_generation"] += 1
            work["status"] = status
            work["pending"] = {"kind": reason, "previous_status": previous_status,
                               "previous_pending": previous_pending}
            self._append(work, reason, {"control_generation": work["control_generation"]})
            return {"status": "ok", "work": self._public(work)}

    def _append(self, work: dict[str, Any], action: str, detail: dict[str, Any]) -> None:
        detail = dict(detail)
        pending_reservations = [row for row in work.get("_ledger_reservations") or []
                                if row.get("state") in {
                                    "PREPARED", "CONSUME_PENDING", "RELEASE_PENDING"}]
        if pending_reservations:
            detail["reservation_identities"] = [
                {"reservation_id": row["reservation_id"],
                 "settlement": ("CONSUMED" if row["state"] == "CONSUME_PENDING"
                                else "RELEASED" if row["state"] == "RELEASE_PENDING"
                                else "PREPARED")}
                for row in pending_reservations]
        now = time.time()
        timing = work.setdefault("time", {"active_elapsed_sec": 0.0,
                                           "active_started_at": None,
                                           "calendar_deadline_at": None})
        active_started = timing.get("active_started_at")
        if isinstance(active_started, (int, float)):
            timing["active_elapsed_sec"] += max(0.0, now - float(active_started))
        inactive = work["status"] in TERMINAL_STATUSES | {
            "PAUSED", "WAITING_USER", "WAITING_APPROVAL", "RECONCILIATION_REQUIRED"}
        timing["active_started_at"] = None if inactive else now
        work["updated_at"] = now
        sequence = int(work.get("sequence", 0)) + 1
        work["sequence"] = sequence
        scope = work["scope"]
        event_id = uuid.uuid4().hex
        self._store.emit(SessionEvent(
            event_id=event_id, session_id=scope["origin_session_id"],
            terminal_id="default", user_id=scope["member_id"],
            project_id=scope["project_id"], package_id=work["package"]["package_id"],
            timestamp=work["updated_at"], event_type=EVENT_WORK,
            block_name=scope["block"], payload={
                "schema_version": SCHEMA_VERSION, "work_id": work["work_id"],
                "sequence": sequence, "action": action, "detail": detail,
                "snapshot": json.loads(json.dumps(work)),
            }))
        # 權威事件落盤後才入帳；帳本寫入失敗必須中止後續派送（fail closed）。
        ledger = configured_ledger()
        if ledger is not None:
            ledger.record_work_event(work, action=action, detail=detail, event_id=event_id)
            for reservation in pending_reservations:
                if reservation.get("state") == "CONSUME_PENDING":
                    reservation["state"] = "CONSUMED"
                elif reservation.get("state") == "RELEASE_PENDING":
                    reservation["state"] = "RELEASED"

    def _load_owned(self, work_id: str) -> dict[str, Any] | WorkError:
        if not isinstance(work_id, str) or not work_id:
            return WorkError("WORK_ID_INVALID", "work_id must be non-empty")
        work = self._reader.load(work_id, self._scope_provider())
        if work is None:
            return WorkError(
                "WORK_NOT_FOUND_OR_OUT_OF_SCOPE", "work is unavailable in this scope")
        if work.get("schema_version") != SCHEMA_VERSION:
            return WorkError(
                "WORK_SCHEMA_UNSUPPORTED",
                "stored Work schema is not supported by this daemon; do not control or rewrite it")
        try:
            self._reconcile_pending_ledger_reservations(work)
        except Exception as exc:  # noqa: BLE001 - 壞帳本／lineage 不可控制 work
            return WorkError("LEDGER_RECONCILIATION_FAILED", str(exc))
        return work

    def _reconcile_pending_ledger_reservations(self, work: dict[str, Any]) -> None:
        """重啟後立即依 reservation 與同一 Work 的 durable event 結算。

        不會等下一件 Work 的事件來補帳；沒有可驗證 event lineage 的 reservation
        則保持 outstanding，讓 `_can_spend()` fail closed。child／provider 狀態未知
        時也不猜測 RELEASED。
        """
        ledger = configured_ledger()
        pending = [row for row in work.get("_ledger_reservations") or []
                   if row.get("state") in {"CONSUME_PENDING", "RELEASE_PENDING"}]
        if ledger is None or not pending:
            return
        scope = work["scope"]
        events = self._store.query(user_id=scope["member_id"], event_type=EVENT_WORK,
                                   order="asc")
        evidence: dict[str, tuple[str, str]] = {}
        for event in events:
            payload = event.payload or {}
            if payload.get("work_id") != work["work_id"]:
                continue
            for identity in (payload.get("detail") or {}).get("reservation_identities") or []:
                if not isinstance(identity, dict):
                    raise WorkError("LEDGER_RESERVATION_LINEAGE_INVALID",
                                    "stored reservation identity is not an object")
                reservation_id = identity.get("reservation_id")
                settlement = identity.get("settlement")
                if settlement in {"CONSUMED", "RELEASED"} and isinstance(reservation_id, str):
                    previous = evidence.get(reservation_id)
                    candidate = (settlement, event.event_id)
                    if previous is not None and previous != candidate:
                        raise WorkError("LEDGER_RESERVATION_LINEAGE_INVALID",
                                        "reservation is linked to conflicting work events")
                    evidence[reservation_id] = candidate
        for row in pending:
            reference = evidence.get(str(row.get("reservation_id")))
            expected = "CONSUMED" if row["state"] == "CONSUME_PENDING" else "RELEASED"
            if reference is None or reference[0] != expected:
                raise WorkError("LEDGER_RESERVATION_LINEAGE_INVALID",
                                "pending reservation has no matching durable work event")
            ledger.settle_reservation(
                str(row["reservation_id"]), operation=expected.lower(),
                evidence_ref={"event_type": EVENT_WORK, "event_id": reference[1],
                              "reservation_id": row["reservation_id"], "reconciled": True})
            row["state"] = expected

    def _load_controlled(self, work_id: str) -> dict[str, Any] | WorkError:
        """控制權比歷史唯讀更窄；fallback 重啟必須重新選到固定 Profile。"""
        work = self._load_owned(work_id)
        if isinstance(work, WorkError):
            return work
        context = self._scope_provider()
        scope = work["scope"]
        if scope["origin_session_id"] == context.session_id:
            return work
        if getattr(context, "project_declared", True):
            return work if scope["project_id"] == context.project_id else WorkError(
                "WORK_CONTROL_OUT_OF_SCOPE", "declared project does not own this work")
        profile = self._profile_resolver(work["profile_id"])
        if isinstance(profile, dict) and profile.get("status") == "ok" \
                and profile.get("project_id") == scope["project_id"] \
                and scope.get("project_source") == "profile":
            return work
        return WorkError(
            "WORK_CONTROL_REATTACH_REQUIRED",
            "history is readable, but control requires selecting the work's fixed Profile")

    def _find_request(self, request_id: str, context: Any) -> dict[str, Any] | None:
        for row in self._reader.list(context):
            work = self._reader.load(row["work_id"], context)
            if work is not None and request_id in work.get("request_ids", []):
                return work
        return None

    def _validate_saved_proposal_source(
        self, source_work: dict[str, Any], *, source_proposal_id: str,
        expected_proposal_digest: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | WorkError:
        authorization = source_work.get("authorization") or {}
        if (source_work.get("status") != "STOPPED"
                or source_work.get("stop_reason") != "PLAN_ONLY_COMPLETE"
                or authorization.get("mode") != "PLAN_ONLY"
                or not authorization.get("valid")):
            return WorkError(
                "SAVED_PROPOSAL_SOURCE_NOT_FINAL",
                "source work is not an authorized PLAN_ONLY_COMPLETE work")
        records = [row for row in source_work.get("proposals") or []
                   if row.get("proposal_id") == source_proposal_id]
        if len(records) != 1:
            return WorkError(
                "SAVED_PROPOSAL_NOT_FOUND", "source proposal is unavailable or ambiguous")
        record = records[0]
        proposal = record.get("proposal")
        source_proposal = record.get("source_proposal")
        response = record.get("response")
        if not all(isinstance(value, dict)
                   for value in (proposal, source_proposal, response)):
            return WorkError(
                "SAVED_PROPOSAL_RECORD_INVALID", "source proposal record is incomplete")
        if (record.get("proposal_digest") != expected_proposal_digest
                or digest_json(proposal) != expected_proposal_digest):
            return WorkError(
                "SAVED_PROPOSAL_DIGEST_MISMATCH",
                "expected digest does not match the stored canonical proposal")
        if (record.get("source_proposal_digest") != digest_json(source_proposal)
                or record.get("response_digest") != digest_json(response)):
            return WorkError(
                "SAVED_PROPOSAL_LINK_INVALID",
                "stored source/canonical proposal linkage is inconsistent")
        original_response = json.loads(json.dumps(response))
        if source_proposal != proposal:
            original_response["proposal"] = source_proposal
            original_response.pop("package_adaptation", None)
            original_response.pop("source_response_digest", None)
        if record.get("source_response_digest") != digest_json(original_response):
            return WorkError(
                "SAVED_PROPOSAL_SOURCE_RESPONSE_MISMATCH",
                "stored Domain source response digest is inconsistent")
        stored_validation = record.get("validation") or {}
        if stored_validation.get("status") != "PASSED":
            return WorkError(
                "SAVED_PROPOSAL_NOT_VALIDATED",
                "source proposal did not pass its recorded Package validation")
        package_id = str((source_work.get("package") or {}).get("package_id") or "")
        try:
            package = self._packages.get(package_id)
            current = package.validate(json.loads(json.dumps(source_proposal)))
        except Exception as exc:  # noqa: BLE001 - Package boundary 必須 fail closed
            return WorkError(
                "SAVED_PROPOSAL_VALIDATION_FAILED",
                f"current Package validator failed: {type(exc).__name__}")
        if not isinstance(current, dict):
            return WorkError(
                "SAVED_PROPOSAL_VALIDATION_FAILED",
                "current Package validator returned a non-object")
        normalized = current.get("normalized_proposal", source_proposal)
        if (current.get("status") != "PASSED" or not isinstance(normalized, dict)):
            return WorkError(
                "SAVED_PROPOSAL_VALIDATION_REJECTED",
                str(current.get("reason_code") or "Package rejected saved proposal"))
        if digest_json(normalized) != expected_proposal_digest or normalized != proposal:
            return WorkError(
                "SAVED_PROPOSAL_CANONICAL_MISMATCH",
                "current Package normalization differs from the submitted proposal")
        current_validation = {
            key: json.loads(json.dumps(value))
            for key, value in current.items() if key != "normalized_proposal"}
        decisions = [row for row in source_work.get("decisions") or []
                     if row.get("kind") == "SUBMIT_PROPOSAL"
                     and row.get("revision") == record.get("revision")
                     and row.get("based_on_domain_response_digest")
                     == record.get("response_digest")]
        if len(decisions) != 1 or not isinstance(decisions[0].get("decision_id"), str):
            return WorkError(
                "SAVED_PROPOSAL_GLOBAL_SUBMISSION_MISSING",
                "no unique Global SUBMIT_PROPOSAL links to the Domain response")
        return (json.loads(json.dumps(record)),
                json.loads(json.dumps(decisions[0])), current_validation)

    def _revalidate_execution_seed(
        self, work: dict[str, Any], record: dict[str, Any],
    ) -> dict[str, Any] | WorkError:
        seed = work.get("execution_seed") or {}
        source_work = self._load_owned(str(seed.get("source_work_id") or ""))
        if isinstance(source_work, WorkError):
            return WorkError(
                "SAVED_PROPOSAL_SOURCE_UNAVAILABLE", source_work.message)
        checked = self._validate_saved_proposal_source(
            source_work,
            source_proposal_id=str(seed.get("source_proposal_id") or ""),
            expected_proposal_digest=str(
                seed.get("canonical_proposal_digest") or ""))
        if isinstance(checked, WorkError):
            return checked
        source_record, global_decision, validation = checked
        if (seed.get("source_proposal_digest")
                != source_record.get("source_proposal_digest")
                or seed.get("domain_response_digest")
                != source_record.get("response_digest")
                or seed.get("global_decision_id")
                != global_decision.get("decision_id")
                or seed.get("global_decision_digest")
                != digest_json(global_decision)):
            return WorkError(
                "SAVED_PROPOSAL_LINEAGE_CHANGED",
                "source proposal or Global submission no longer matches the bound lineage")
        if (record.get("source_work_id") != seed.get("source_work_id")
                or record.get("source_proposal_id") != seed.get("source_proposal_id")
                or record.get("proposal_digest")
                != seed.get("canonical_proposal_digest")
                or digest_json(record.get("proposal"))
                != seed.get("canonical_proposal_digest")):
            return WorkError(
                "SAVED_PROPOSAL_EXECUTION_RECORD_MISMATCH",
                "execution work proposal differs from its saved source")
        if (work.get("baseline") or {}).get("status") not in {
                "COMPLETE", "REUSED_VALIDATED"}:
            return WorkError(
                "SAVED_PROPOSAL_BASELINE_NOT_READY",
                "a complete current-work baseline is required")
        return validation

    @staticmethod
    def _saved_proposal_input_check(
        work: dict[str, Any],
    ) -> dict[str, Any] | None:
        seed = work.get("execution_seed")
        if not isinstance(seed, dict):
            return None
        expected_scope = seed.get("source_scope") or {}
        scope = work.get("scope") or {}
        scope_mismatch = [key for key in ("member_id", "project_id", "block")
                          if expected_scope.get(key) != scope.get(key)]
        if scope_mismatch:
            return {"reason_code": "SAVED_PROPOSAL_SCOPE_MISMATCH",
                    "mismatched_fields": scope_mismatch}
        expected_environment = seed.get("source_environment") or {}
        environment = work.get("environment") or {}
        environment_mismatch = [
            key for key, value in expected_environment.items()
            if environment.get(key) != value]
        if environment_mismatch:
            return {"reason_code": "SAVED_PROPOSAL_ENVIRONMENT_MISMATCH",
                    "mismatched_fields": sorted(environment_mismatch)}
        current_input = work.get("input") or {}
        expected_tree = seed.get("source_input_tree_hash")
        current_tree = (current_input.get("tree_hash")
                        or current_input.get("historical_input_tree_hash"))
        if isinstance(expected_tree, str) and expected_tree:
            if current_tree != expected_tree:
                return {"reason_code": "SAVED_PROPOSAL_INPUT_TREE_MISMATCH",
                        "expected_digest": expected_tree,
                        "actual_digest": current_tree}
        else:
            expected_input = seed.get("source_input_digest")
            if (isinstance(expected_input, str) and expected_input
                    and current_input.get("input_digest") != expected_input):
                return {"reason_code": "SAVED_PROPOSAL_INPUT_MISMATCH",
                        "expected_digest": expected_input,
                        "actual_digest": current_input.get("input_digest")}
        return None

    @staticmethod
    def _saved_seed_binding(seed: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "schema_version", "create_request_digest", "source_work_id",
            "source_proposal_id",
            "source_proposal_digest", "canonical_proposal_digest",
            "source_response_digest", "domain_response_digest",
            "global_decision_id", "global_decision_digest", "package_id",
            "baseline_strategy", "source_scope", "source_input_digest",
            "source_input_tree_hash", "source_environment", "validation",
        )
        return {key: json.loads(json.dumps(seed[key]))
                for key in keys if key in seed}

    @staticmethod
    def _saved_seed_matches(
        seed: dict[str, Any], source_work_id: str, source_proposal_id: str,
        expected_proposal_digest: str, create_request_digest: str,
    ) -> bool:
        return bool(
            seed.get("source_work_id") == source_work_id
            and seed.get("source_proposal_id") == source_proposal_id
            and seed.get("canonical_proposal_digest")
            == expected_proposal_digest
            and seed.get("create_request_digest") == create_request_digest)

    def _public(self, work: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(work))
        result["budget"] = self._prepare_public_budget(work)
        result["execution_allowed"] = bool(
            isinstance(work.get("authorization"), dict)
            and work["authorization"].get("valid")
            and work["authorization"].get("mode") != "PLAN_ONLY"
            and work["status"] not in TERMINAL_STATUSES | {"PAUSED", "WAITING_USER"})
        return result

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"status": "error", "error_code": code, "message": message,
                "execution_allowed": False}


__all__ = [
    "ACTIVE_STATUSES", "DOMAIN_KINDS", "EVENT_WORK", "EngineeringWorkService",
    "GLOBAL_DECISIONS", "PackageCapability", "PROPOSAL_APPROVED",
    "PROPOSAL_ORIGIN_USER", "PROPOSAL_PENDING_APPROVAL",
    "SCHEMA_VERSION", "TERMINAL_STATUSES",
    "WorkError", "WorkExpertRouter", "WorkPackageRegistry", "WorkReader", "WorkRuntime",
    "WorkScope", "canonical_json", "digest_json",
]
