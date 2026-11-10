# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""唯讀 Evidence Resolver：把一次執行的證據拆成四個獨立的軸來判定。

四個軸各自只看自己的來源，**任何一軸都不由其他軸推論**：

* **L（Terminal Outcome）**：只由 orchestration ledger 的終態，或 content hash
  重算成立的 execution receipt 決定。DQE、artifact manifest 存在都不會讓結果
  往 `SUCCEEDED` 移動。
* **M（artifact manifest）**：manifest 檔重算的 sha256 與 producer 身分，對照
  receipt 記錄的值。沒有 receipt 時只能說 manifest「自稱」屬於這次執行。
* **Q（DQE）**：DQE rows 的執行身分與 receipt 是否一致、是否已作廢。
* **V（可見性）**：目前使用者的 ledger 或 receipt 投影查不查得到這次執行。

Terminal Outcome 依下列順序判斷，第一個成立者即為結果；矛盾一律最優先，
不會被任何成功條件覆蓋：

1. `CONFLICT`：ledger 與 receipt 終態不一致、同一身分有內容不同的 receipt、
   receipt 與 manifest／DQE 身分不一致、manifest sha256 不符。
2. `IN_PROGRESS`：ledger 的 run 尚未進入終態，且有存活證據（見下方）。
3. `INTERRUPTED`：ledger 或 receipt 記錄了中斷（`cancelled`）。
4. `TIMED_OUT`：ledger 或 receipt 記錄了逾時。
5. `EXECUTION_UNVERIFIED`：沒有可信的終態紀錄、receipt 缺必要欄位、或 ledger
   為 `abandoned`（daemon 失去 child，結束未經觀察）。
6. `SUCCEEDED`：可信 receipt 記錄 lifecycle `completed`、process exit code 0、
   沒有逾時，且沒有任何矛盾。只有 ledger `completed`、沒有可信的 process exit
   結果時是 `EXECUTION_UNVERIFIED`，不是成功。
7. `FAILED`：其餘有可信終態的情況。

ledger 的 `pending`／`running` 只有在 child pid 存在且 heartbeat 新鮮時才是
`IN_PROGRESS`；否則（例如重啟後殘留）是 `EXECUTION_UNVERIFIED`。

注意：receipt 的 `execution_lifecycle_status="completed"` 只代表 DQE dispatch
完成且 evidence 就緒，**不代表** process 成功；成功還需要 exit code 0。

隔離邊界：一個 User Runtime Scope 只對應一個 Project，因此這裡只做既有的
per-user 隔離——receipt 的 `user_id` 必須是目前的 runtime user，不做 project
過濾，也不因缺少 `project_id` 拒絕或警告。

本模組只讀：不寫 EventStore、ledger、DQE DB 或 manifest，也不保存判定結果；
每次查詢都從持久化來源重新判定，因此 process 重啟後結果相同。
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import pwd
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from terminux_engine.execution_receipts.projection import (
    RECEIPT_RECORD_TYPE,
    content_hash_of,
)

# ── Terminal Outcome（依判斷順序排列）──────────────────────────────────
CONFLICT = "CONFLICT"
IN_PROGRESS = "IN_PROGRESS"
INTERRUPTED = "INTERRUPTED"
TIMED_OUT = "TIMED_OUT"
EXECUTION_UNVERIFIED = "EXECUTION_UNVERIFIED"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"

TERMINAL_OUTCOMES: tuple[str, ...] = (
    CONFLICT, IN_PROGRESS, INTERRUPTED, TIMED_OUT, EXECUTION_UNVERIFIED,
    SUCCEEDED, FAILED,
)

# ── M：artifact manifest ──────────────────────────────────────────────
MANIFEST_VERIFIED = "verified"
MANIFEST_UNBOUND = "unbound"
MANIFEST_MISMATCH = "mismatch"
MANIFEST_MISSING = "missing"

# ── Q：DQE ─────────────────────────────────────────────────────────────
DQE_BOUND = "bound"
DQE_PRESENT_UNBOUND = "present_unbound"
DQE_ABSENT = "absent"
DQE_INVALIDATED = "invalidated"

# ── V：可見性 ─────────────────────────────────────────────────────────
VISIBLE = "visible"
NOT_PROJECTED = "not_projected"

#: ledger 的終態集合（與 `orchestration.ledger._TERMINAL_RUN_STATES` 相同）。
_LEDGER_TERMINAL = frozenset({"completed", "failed", "timeout", "cancelled", "abandoned"})
#: 需要 process 存活證據才能稱為執行中的 ledger 狀態。`paused` 是 daemon 記錄的
#: 控制狀態，本來就沒有執行中的 process，不需要 liveness。
_LEDGER_NEEDS_LIVENESS = frozenset({"pending", "running"})
#: heartbeat 新鮮度門檻，與 daemon heartbeat／recovery scanner 的預設值相同。
LIVENESS_STALE_SECONDS = 30.0
#: receipt 判定 Terminal Outcome 必備的欄位；歷史 receipt 缺任一欄即無法判定。
_REQUIRED_RECEIPT_FIELDS = ("execution_lifecycle_status", "process_exit_code", "timed_out")
_KNOWN_LIFECYCLES = frozenset({"completed", "failed", "timeout", "cancelled"})
_MAX_RECEIPT_ROWS = 10000
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024

#: 對外摘要只保留這些 receipt 欄位；`output_roots`、manifest reference 等路徑不輸出。
_RECEIPT_SUMMARY_KEYS = (
    "receipt_id", "content_hash", "step_id", "attempt", "execution_lifecycle_status",
    "process_exit_code", "timed_out", "termination_reason", "dispatch_status",
    "evidence_status", "artifact_manifest_id", "artifact_manifest_sha256",
    "artifact_capture_status",
)

_UNSET: Any = object()


@dataclass(frozen=True)
class ExecutionEvidence:
    """一次執行的四軸判定結果。只描述證據，不保存、不改寫任何來源。"""

    run_id: str
    terminal_outcome: str
    outcome_reasons: tuple[str, ...]
    lifecycle_source: str
    artifact_manifest: str
    dqe: str
    visibility: str
    conflicts: tuple[str, ...] = ()
    ledger_status: str | None = None
    ledger_terminal_reason: str | None = None
    receipt: dict[str, Any] | None = None
    receipt_count: int = 0
    untrusted_receipt_count: int = 0
    foreign_user_receipt_count: int = 0
    dqe_row_count: int = 0
    dqe_source: str = "unavailable"
    artifact_manifest_reasons: tuple[str, ...] = ()
    dqe_reasons: tuple[str, ...] = ()
    #: 實際讀取的 DQE DB 路徑。只供 host 內部使用，不進入序列化結果。
    dqe_db_path: str | None = field(default=None, compare=False, repr=False)

    @property
    def succeeded(self) -> bool:
        """只有第 6 列條件全部成立、且沒有任何矛盾時才是 True。"""
        return self.terminal_outcome == SUCCEEDED

    def to_json_dict(self) -> dict[str, Any]:
        """固定、可 JSON 序列化且不含 filesystem path 的形狀。"""
        return {
            "run_id": self.run_id,
            "terminal_outcome": self.terminal_outcome,
            "outcome_reasons": list(self.outcome_reasons),
            "lifecycle_source": self.lifecycle_source,
            "conflicts": list(self.conflicts),
            "ledger_status": self.ledger_status,
            "ledger_terminal_reason": self.ledger_terminal_reason,
            "receipt": dict(self.receipt) if self.receipt is not None else None,
            "receipt_count": self.receipt_count,
            "untrusted_receipt_count": self.untrusted_receipt_count,
            "foreign_user_receipt_count": self.foreign_user_receipt_count,
            "axes": {
                "artifact_manifest": {"status": self.artifact_manifest,
                                      "reasons": list(self.artifact_manifest_reasons)},
                "dqe": {"status": self.dqe, "reasons": list(self.dqe_reasons),
                        "row_count": self.dqe_row_count, "source": self.dqe_source},
                "visibility": {"status": self.visibility},
            },
        }


def _home_of_current_uid() -> Path:
    """與 daemon 共用目前 UID 的基底目錄解析，不讀 `$HOME`。"""
    try:
        from terminux_engine.ipc.agent_socket import home_for_uid
        return home_for_uid(os.geteuid())
    except KeyError:
        return Path.home()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _receipt_outcome(receipt: dict[str, Any]) -> tuple[str, str]:
    """單一可信 receipt 的結果（不含 ledger 與矛盾）。回傳 (outcome, reason)。"""
    for name in _REQUIRED_RECEIPT_FIELDS:
        if receipt.get(name) is None:
            return EXECUTION_UNVERIFIED, f"receipt_missing_{name}"
    lifecycle = receipt["execution_lifecycle_status"]
    if lifecycle not in _KNOWN_LIFECYCLES:
        return EXECUTION_UNVERIFIED, "receipt_lifecycle_unknown"
    if lifecycle == "cancelled":
        return INTERRUPTED, "receipt_cancelled"
    if receipt["timed_out"] is True or lifecycle == "timeout":
        return TIMED_OUT, "receipt_timed_out"
    if lifecycle == "completed" and receipt["process_exit_code"] == 0 \
            and receipt["timed_out"] is False:
        return SUCCEEDED, "receipt_exit_zero_no_timeout"
    if lifecycle == "completed":
        return FAILED, "receipt_process_exit_code_not_zero"
    return FAILED, f"receipt_lifecycle_{lifecycle}"


def _ledger_receipt_conflict(ledger_status: str, receipt: dict[str, Any]) -> bool:
    """ledger 終態與單一 receipt 是否互相矛盾。

    single-tool workflow 的 ledger `completed` 等於 process exit 0；receipt 的
    lifecycle `failed`（例如 evidence 未就緒）與它並不矛盾，只是讓結果落在 FAILED。
    """
    exit_code = receipt.get("process_exit_code")
    timed_out = receipt.get("timed_out")
    lifecycle = receipt.get("execution_lifecycle_status")
    if ledger_status == "completed":
        return ((exit_code is not None and exit_code != 0) or timed_out is True
                or lifecycle in {"timeout", "cancelled"})
    if ledger_status == "timeout":
        return timed_out is False and lifecycle not in {None, "timeout"}
    if ledger_status == "failed":
        return lifecycle == "completed" and exit_code == 0 and timed_out is False
    return False


def _identity(receipt: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (receipt.get("run_id"), receipt.get("step_id"), receipt.get("attempt"))


class ExecutionEvidenceResolver:
    """以目前使用者的持久化證據判定一次執行。只讀，可安全重複建立。

    `ledger_lookup(run_id)` 由 host 提供，回傳**已套用 caller 範圍**的 ledger
    run（或 `None`）；未提供時只依 receipt 判定。`ledger_recent(limit)` 是可選的
    latest-run 來源。兩者都只讀。
    """

    def __init__(
        self,
        *,
        event_store: Any,
        current_user: str | None = None,
        dqe_db_path: str | Path | None = None,
        ledger_lookup: Callable[[str], Any] | None = None,
        ledger_recent: Callable[[int], Iterable[Any]] | None = None,
        trusted_dqe_roots: Iterable[str | Path] | None = None,
        invalidation_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._event_store = event_store
        self._current_user = current_user or getpass.getuser()
        self._dqe_db_path = Path(dqe_db_path) if dqe_db_path is not None else None
        self._ledger_lookup = ledger_lookup
        self._ledger_recent = ledger_recent
        self._trusted_dqe_roots = tuple(
            Path(p) for p in (trusted_dqe_roots
                              if trusted_dqe_roots is not None
                              else (_home_of_current_uid(),)))
        self._invalidation_path = invalidation_path
        self._clock = clock

    # ── public ──────────────────────────────────────────────────────────

    def resolve(self, run_id: str, *, dqe_db_path: str | Path | None = None,
                ledger_run: Any = _UNSET) -> ExecutionEvidence:
        """判定一次執行。`ledger_run` 由已做過範圍檢查的 host 直接提供時，不再查詢。"""
        invalidated = self._invalidation_set()
        db_path = Path(dqe_db_path) if dqe_db_path is not None else self._dqe_db_path
        if invalidated.run_invalidated(run_id):
            return ExecutionEvidence(
                run_id=run_id, terminal_outcome=EXECUTION_UNVERIFIED,
                outcome_reasons=("run_invalidated",), lifecycle_source="none",
                artifact_manifest=MANIFEST_MISSING, dqe=DQE_INVALIDATED,
                visibility=NOT_PROJECTED,
                artifact_manifest_reasons=("run_invalidated",),
                dqe_reasons=("run_invalidated",),
                dqe_source=self._dqe_source(db_path),
                dqe_db_path=str(db_path) if db_path is not None else None)

        reasons: list[str] = []
        conflicts: list[str] = []

        ledger = self._ledger_lookup(run_id) if ledger_run is _UNSET \
            and self._ledger_lookup is not None else (
                None if ledger_run is _UNSET else ledger_run)
        ledger_status = getattr(ledger, "status", None) if ledger is not None else None
        ledger_reason = getattr(ledger, "terminal_reason", None) if ledger is not None else None

        trusted, untrusted, foreign = self._receipts_for(run_id, invalidated)

        # 同一 (run, step, attempt) 有兩份內容不同的 receipt ⇒ 矛盾。
        by_identity: dict[tuple[Any, Any, Any], dict[str, dict[str, Any]]] = {}
        for receipt in trusted:
            by_identity.setdefault(_identity(receipt), {})[receipt["content_hash"]] = receipt
        if any(len(versions) > 1 for versions in by_identity.values()):
            conflicts.append("duplicate_receipt_identity")
        terminal = self._terminal_receipts(by_identity)

        # ── M ──
        manifest_status, manifest_reasons, manifest_conflicts = self._manifest_axis(
            run_id, terminal, db_path, invalidated)
        conflicts.extend(manifest_conflicts)

        # ── Q ──
        rows, rows_invalidated = self._dqe_rows(db_path, run_id, invalidated)
        dqe_status, dqe_reasons, dqe_conflicts = self._dqe_axis(
            rows, rows_invalidated, terminal, db_path)
        conflicts.extend(dqe_conflicts)

        # ── ledger 與 receipt 的終態 ──
        if ledger_status in _LEDGER_TERMINAL and ledger_status != "abandoned":
            if any(_ledger_receipt_conflict(ledger_status, r) for r in terminal):
                conflicts.append("ledger_receipt_terminal_mismatch")

        # ── V ──
        visibility = VISIBLE if ledger is not None or trusted else NOT_PROJECTED

        if ledger is not None and terminal:
            source = "ledger_and_receipt"
        elif ledger is not None:
            source = "ledger"
        elif terminal:
            source = "receipt"
        else:
            source = "none"

        receipt_results = [_receipt_outcome(r) for r in terminal]
        outcome = self._terminal_outcome(
            conflicts=conflicts, ledger=ledger, ledger_status=ledger_status,
            ledger_live=self._ledger_live(ledger),
            receipt_results=receipt_results, reasons=reasons)
        if untrusted:
            reasons.append("untrusted_receipt_ignored")
        if foreign:
            reasons.append("foreign_user_receipt_ignored")

        summary = None
        if terminal:
            latest = max(terminal, key=lambda r: (r.get("finished_at") or 0.0))
            summary = {k: latest.get(k) for k in _RECEIPT_SUMMARY_KEYS}

        return ExecutionEvidence(
            run_id=run_id, terminal_outcome=outcome,
            outcome_reasons=tuple(dict.fromkeys(reasons)),
            lifecycle_source=source, artifact_manifest=manifest_status,
            dqe=dqe_status, visibility=visibility,
            conflicts=tuple(dict.fromkeys(conflicts)),
            ledger_status=ledger_status, ledger_terminal_reason=ledger_reason,
            receipt=summary, receipt_count=len(trusted),
            untrusted_receipt_count=untrusted, foreign_user_receipt_count=foreign,
            dqe_row_count=len(rows), dqe_source=self._dqe_source(db_path),
            artifact_manifest_reasons=tuple(manifest_reasons),
            dqe_reasons=tuple(dqe_reasons),
            dqe_db_path=str(db_path) if db_path is not None else None,
        )

    def latest_run_id(self, *, dqe_db_path: str | Path | None = None,
                      require_dqe: bool = True, limit: int = 200) -> str | None:
        """目前使用者自己的執行證據中最新的一次執行。

        只看目前使用者的可信 receipt 與 host 提供的 ledger runs；只有 DQE rows、
        沒有執行證據的 run 不會被選中。`require_dqe=True` 時再要求該 run 有未作廢的
        DQE rows。
        """
        invalidated = self._invalidation_set()
        candidates: dict[str, float] = {}
        for receipt, timestamp in self._own_receipts(invalidated):
            run_id = receipt.get("run_id")
            if isinstance(run_id, str) and not invalidated.run_invalidated(run_id):
                candidates[run_id] = max(candidates.get(run_id, float("-inf")), timestamp)
        if self._ledger_recent is not None:
            for run in self._ledger_recent(limit) or []:
                run_id = getattr(run, "run_id", None)
                when = getattr(run, "finished_at", None) or getattr(run, "started_at", None)
                if isinstance(run_id, str) and not invalidated.run_invalidated(run_id):
                    candidates[run_id] = max(candidates.get(run_id, float("-inf")),
                                             float(when or 0.0))
        db_path = Path(dqe_db_path) if dqe_db_path is not None else self._dqe_db_path
        for run_id, _when in sorted(candidates.items(), key=lambda kv: (kv[1], kv[0]),
                                    reverse=True):
            if not require_dqe:
                return run_id
            rows, _ = self._dqe_rows(db_path, run_id, invalidated)
            if rows:
                return run_id
        return None

    # ── 判定 ────────────────────────────────────────────────────────────

    def _ledger_live(self, ledger: Any) -> bool:
        """ledger run 是否有可靠的存活證據：有 child pid，且 heartbeat 仍新鮮。

        daemon 重啟後殘留的 `running`，若沒有新鮮 heartbeat，就不能宣稱仍在執行
        （recovery scanner 用的是同一個門檻）。這裡不送任何 signal 給 process。
        """
        if ledger is None:
            return False
        heartbeat = getattr(ledger, "last_heartbeat_at", None)
        if getattr(ledger, "child_pid", None) is None or heartbeat is None:
            return False
        return self._clock() - float(heartbeat) < LIVENESS_STALE_SECONDS

    @staticmethod
    def _terminal_outcome(*, conflicts: list[str], ledger: Any, ledger_status: str | None,
                          ledger_live: bool, receipt_results: list[tuple[str, str]],
                          reasons: list[str]) -> str:
        receipt_outcomes = {outcome for outcome, _ in receipt_results}
        reasons.extend(reason for _, reason in receipt_results)
        if conflicts:
            reasons.extend(conflicts)
            return CONFLICT
        if ledger is not None and ledger_status not in _LEDGER_TERMINAL:
            if ledger_status in _LEDGER_NEEDS_LIVENESS and not ledger_live:
                reasons.append("ledger_running_without_liveness")
                return EXECUTION_UNVERIFIED
            reasons.append(f"ledger_status_{ledger_status}")
            return IN_PROGRESS
        if ledger_status == "cancelled" or INTERRUPTED in receipt_outcomes:
            if ledger_status == "cancelled":
                reasons.append("ledger_cancelled")
            return INTERRUPTED
        if ledger_status == "timeout" or TIMED_OUT in receipt_outcomes:
            if ledger_status == "timeout":
                reasons.append("ledger_timeout")
            return TIMED_OUT
        if ledger_status == "abandoned":
            reasons.append("ledger_abandoned")
            return EXECUTION_UNVERIFIED
        if not receipt_results and ledger is None:
            reasons.append("no_terminal_execution_record")
            return EXECUTION_UNVERIFIED
        if EXECUTION_UNVERIFIED in receipt_outcomes:
            return EXECUTION_UNVERIFIED
        if not receipt_results:
            # 只有 ledger：明確記錄的失敗（failed；timeout／cancelled 已在上面處理）
            # 可以採信；`completed` 沒有可信的 process exit 結果，不能推論成功。
            if ledger_status == "completed":
                reasons.append("ledger_completed_without_process_exit_evidence")
                return EXECUTION_UNVERIFIED
            reasons.append(f"ledger_{ledger_status}")
            return FAILED
        if receipt_outcomes == {SUCCEEDED} and ledger_status in {None, "completed"}:
            return SUCCEEDED
        return FAILED

    @staticmethod
    def _terminal_receipts(
        by_identity: dict[tuple[Any, Any, Any], dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """每個 step 取 attempt 最大的 receipt（retry 以最後一次為準）。"""
        per_step: dict[Any, tuple[Any, list[dict[str, Any]]]] = {}
        for (_, step_id, attempt), versions in by_identity.items():
            current = per_step.get(step_id)
            key = attempt if isinstance(attempt, int) else -1
            if current is None or key > current[0]:
                per_step[step_id] = (key, list(versions.values()))
        terminal: list[dict[str, Any]] = []
        for _, receipts in per_step.values():
            terminal.extend(receipts)
        return terminal

    def _manifest_axis(
        self, run_id: str, terminal: list[dict[str, Any]], db_path: Path | None,
        invalidated: Any,
    ) -> tuple[str, list[str], list[str]]:
        if terminal:
            statuses: list[str] = []
            reasons: list[str] = []
            conflicts: list[str] = []
            for receipt in terminal:
                status, reason, conflict = self._verify_receipt_manifest(receipt)
                statuses.append(status)
                if reason:
                    reasons.append(reason)
                if conflict:
                    conflicts.append(conflict)
            for status in (MANIFEST_MISMATCH, MANIFEST_MISSING):
                if status in statuses:
                    return status, reasons, conflicts
            return MANIFEST_VERIFIED, reasons, conflicts

        # 沒有 receipt：只能看 DQE 指向的 manifest 是否自稱屬於這次執行。
        rows, _ = self._dqe_rows(db_path, run_id, invalidated)
        references = sorted({str(r["PATH"]) for r in rows if r.get("PATH")})
        if not references:
            return MANIFEST_MISSING, ["no_manifest_reference"], []
        for reference in references:
            payload, _sha = self._read_manifest(reference)
            if payload is None:
                return MANIFEST_MISSING, ["manifest_unreadable"], []
            producer = payload.get("producer") if isinstance(payload, dict) else None
            if not isinstance(producer, dict) or producer.get("run_id") != run_id:
                return MANIFEST_MISMATCH, ["manifest_producer_mismatch"], \
                    ["manifest_producer_mismatch"]
        return MANIFEST_UNBOUND, ["no_execution_receipt"], []

    def _verify_receipt_manifest(
        self, receipt: dict[str, Any],
    ) -> tuple[str, str | None, str | None]:
        reference = receipt.get("artifact_manifest_reference")
        manifest_id = receipt.get("artifact_manifest_id")
        recorded_sha = receipt.get("artifact_manifest_sha256")
        if not reference:
            return MANIFEST_MISSING, "receipt_has_no_manifest_reference", None
        payload, actual_sha = self._read_manifest(str(reference))
        if payload is None:
            return MANIFEST_MISSING, "manifest_unreadable", None
        if recorded_sha and actual_sha != recorded_sha:
            return MANIFEST_MISMATCH, "manifest_sha256_mismatch", "manifest_sha256_mismatch"
        if manifest_id and payload.get("manifest_id") != manifest_id:
            return MANIFEST_MISMATCH, "manifest_id_mismatch", "manifest_id_mismatch"
        producer = payload.get("producer")
        if not isinstance(producer, dict) or (
                producer.get("run_id"), producer.get("step_id"), producer.get("attempt")
        ) != _identity(receipt):
            return MANIFEST_MISMATCH, "manifest_producer_mismatch", "manifest_producer_mismatch"
        return MANIFEST_VERIFIED, None, None

    def _dqe_axis(
        self, rows: list[dict[str, Any]], rows_invalidated: int,
        terminal: list[dict[str, Any]], db_path: Path | None,
    ) -> tuple[str, list[str], list[str]]:
        if not rows:
            if rows_invalidated:
                return DQE_INVALIDATED, ["dqe_rows_invalidated"], []
            return DQE_ABSENT, [], []
        if not terminal:
            return DQE_PRESENT_UNBOUND, ["no_execution_receipt"], []
        identities = {(r.get("step_id"), r.get("attempt")) for r in terminal}
        conflicts: list[str] = []
        for row in rows:
            step_id, attempt = row.get("step_id"), row.get("attempt")
            if step_id is None and attempt is None:
                continue
            if (step_id, attempt) not in identities:
                conflicts.append("dqe_identity_mismatch")
                break
        references = {r.get("artifact_manifest_reference") for r in terminal} - {None}
        row_refs = {row.get("PATH") for row in rows} - {None, ""}
        if references and row_refs and not row_refs <= {str(r) for r in references}:
            conflicts.append("dqe_manifest_reference_mismatch")
        if conflicts:
            return DQE_PRESENT_UNBOUND, list(conflicts), conflicts
        if self._dqe_source(db_path) != "user_scope":
            return DQE_PRESENT_UNBOUND, ["dqe_db_outside_user_scope"], []
        return DQE_BOUND, [], []

    # ── 來源讀取（全部唯讀）────────────────────────────────────────────

    def _invalidation_set(self) -> Any:
        from terminux_engine.evidence_integrity.invalidation import load_invalidation_set

        return load_invalidation_set(self._invalidation_path)

    def _receipt_events(self) -> list[Any]:
        if self._event_store is None:
            return []
        return list(self._event_store.query(
            event_type=RECEIPT_RECORD_TYPE, limit=_MAX_RECEIPT_ROWS) or [])

    def _receipts_for(self, run_id: str, invalidated: Any,
                      ) -> tuple[list[dict[str, Any]], int, int]:
        trusted: list[dict[str, Any]] = []
        untrusted = 0
        foreign = 0
        for event in self._receipt_events():
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict) or payload.get("run_id") != run_id:
                continue
            if invalidated.event_invalidated(getattr(event, "event_id", None)):
                continue
            if getattr(event, "user_id", None) != self._current_user:
                foreign += 1
                continue
            recomputed = content_hash_of(payload)
            if recomputed is None or recomputed != payload.get("content_hash"):
                untrusted += 1
                continue
            trusted.append(payload)
        return trusted, untrusted, foreign

    def _own_receipts(self, invalidated: Any) -> Iterable[tuple[dict[str, Any], float]]:
        for event in self._receipt_events():
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                continue
            if invalidated.event_invalidated(getattr(event, "event_id", None)):
                continue
            if getattr(event, "user_id", None) != self._current_user:
                continue
            if content_hash_of(payload) != payload.get("content_hash"):
                continue
            when = payload.get("finished_at") or payload.get("started_at") \
                or getattr(event, "timestamp", 0.0)
            yield payload, float(when or 0.0)

    def _dqe_rows(self, db_path: Path | None, run_id: str, invalidated: Any,
                  ) -> tuple[list[dict[str, Any]], int]:
        """這次執行未作廢的 DQE rows，以及被作廢排除的筆數。"""
        if db_path is None or not db_path.is_file():
            return [], 0
        try:
            connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        except sqlite3.Error:
            return [], 0
        try:
            connection.row_factory = sqlite3.Row
            rows = [dict(r) for r in connection.execute(
                "SELECT rowid AS _row_id, * FROM Version_Dqe_Tab "
                "WHERE orchestration_run_id = ?", (run_id,)).fetchall()]
        except sqlite3.Error:
            return [], 0
        finally:
            connection.close()
        identity = str(db_path.resolve())
        kept = [r for r in rows if not invalidated.dqe_row_invalidated(
            db_identity=identity, row_id=r.get("_row_id"))]
        return kept, len(rows) - len(kept)

    def _dqe_source(self, db_path: Path | None) -> str:
        if db_path is None or not db_path.is_file():
            return "unavailable"
        if any(_within(db_path, root) for root in self._trusted_dqe_roots):
            return "user_scope"
        return "outside_user_scope"

    @staticmethod
    def _read_manifest(reference: str) -> tuple[dict[str, Any] | None, str | None]:
        path = Path(reference)
        try:
            if not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
                return None, None
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        return payload, hashlib.sha256(raw).hexdigest()


__all__ = [
    "LIVENESS_STALE_SECONDS",
    "CONFLICT", "DQE_ABSENT", "DQE_BOUND", "DQE_INVALIDATED", "DQE_PRESENT_UNBOUND",
    "EXECUTION_UNVERIFIED", "FAILED", "IN_PROGRESS", "INTERRUPTED",
    "MANIFEST_MISMATCH", "MANIFEST_MISSING", "MANIFEST_UNBOUND", "MANIFEST_VERIFIED",
    "NOT_PROJECTED", "SUCCEEDED", "TERMINAL_OUTCOMES", "TIMED_OUT", "VISIBLE",
    "ExecutionEvidence", "ExecutionEvidenceResolver",
]
