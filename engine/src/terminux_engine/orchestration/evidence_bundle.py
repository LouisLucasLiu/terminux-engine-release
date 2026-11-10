# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""僅由已授權 orchestration run 衍生的唯讀 Evidence Bundle。

呼叫端永遠不提供 workspace、DQE DB 或 artifact 路徑。本模組只接受 child
workflow 已持久化的可信設定，並將輸出限制為固定、可 JSON 序列化且不含
filesystem path 的 schema。producer 以 sibling temporary file + replace
發佈；producer 缺失或來源不足時，reader 回傳明確 unavailable／partial，
不補造 evidence。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, NotRequired, TypedDict

#: eda_convergence 是 optional workflow；它的 state 模組只在建立該 workflow 的 bundle 時才載入。
_EDA_CONVERGENCE_STATE_MODULE = "terminux_engine.packages.eda_convergence.state"
_EDA_CONVERGENCE_PACKAGE = "terminux_engine.packages.eda_convergence"


_SCHEMA_VERSION = "terminux.evidence_bundle.v1"
_MAX_DQE_ITEMS = 128
_MAX_REPORT_ITEMS = 32
_MAX_LINEAGE_ITEMS = 16
_MAX_REPORT_BYTES = 1024 * 1024
_MAX_BUNDLE_BYTES = 256 * 1024

Availability = Literal["available", "partial", "unavailable"]


class EvidenceBundle(TypedDict):
    """對 JSON-RPC 與 Agent 共用的固定回應 schema。"""

    schema_version: str
    orchestration_run_id: str
    availability: Availability
    orchestration: dict[str, Any]
    dqe: dict[str, Any]
    reports: dict[str, Any]
    lineage: dict[str, Any]
    #: daemon 回覆時附加的 Evidence Resolver 四軸判定（不含路徑）；
    #: 沒有 Resolver 時為 `None`。producer 發布的檔案不含這個欄位。
    execution_evidence: NotRequired[dict[str, Any] | None]
    #: receipt 投影路徑上、由 tool adapter record 組出的單一 setup path 證據
    #: （`timing_evidence.projection` 的封閉 schema）。只有注入 timing path reader 的
    #: daemon 才有這個鍵；它受獨立的外送類別 `timing_path_evidence` 管制。
    timing_path: NotRequired[dict[str, Any]]


#: Evidence Resolver 結果裡**不進入 caller 可見投影**的鍵與 outcome reason：它們描述的是
#: 其他 member 寫入同一 run id 的列是否存在、有幾筆。caller 看到的 bundle 必須與那些列
#: 不存在時完全相同；它們只以 `ReceiptIntegrity` 的形式留在 daemon 行程內，供 LLM tool
#: 層在第一個可外送位元組之前拒絕整份 evidence。
CALLER_HIDDEN_EXECUTION_KEYS = frozenset({"foreign_user_receipt_count"})
CALLER_HIDDEN_OUTCOME_REASONS = frozenset({"foreign_user_receipt_ignored"})

INTEGRITY_FOREIGN_RECEIPT_ROWS = "foreign_receipt_rows"
INTEGRITY_UNTRUSTED_RECEIPT_ROWS = "untrusted_receipt_rows"


@dataclass(frozen=True)
class ReceiptIntegrity:
    """同一 run 的 receipt 投影在 daemon 內部的完整性判定。

    **不可序列化到 JSON-RPC、transcript、request 或 log**：它只在行程內由
    `OrchestrationService.evidence_bundle_request_with_integrity` 交給 LLM tool 層，後者以
    `task_scope.veto_run_evidence` 讓最終 Gate 拒送。`vetoed=True` 表示 Evidence Resolver
    看到外人寫入或 content hash 不自洽的列；原因碼只供 daemon 內部測試斷言。
    """

    vetoed: bool
    reason: str | None = field(default=None, repr=False)

    def __str__(self) -> str:
        return "ReceiptIntegrity(vetoed=%s)" % self.vetoed


def receipt_integrity_of(evidence: Any) -> ReceiptIntegrity:
    """由 Resolver 結果判定 integrity veto。沒有 Resolver 結果時不構成 veto（既有語意）。"""
    if evidence is None:
        return ReceiptIntegrity(vetoed=False)
    if getattr(evidence, "foreign_user_receipt_count", 0) != 0:
        return ReceiptIntegrity(vetoed=True, reason=INTEGRITY_FOREIGN_RECEIPT_ROWS)
    if getattr(evidence, "untrusted_receipt_count", 0) != 0:
        return ReceiptIntegrity(vetoed=True, reason=INTEGRITY_UNTRUSTED_RECEIPT_ROWS)
    return ReceiptIntegrity(vetoed=False)


def caller_execution_evidence(evidence: Any) -> dict[str, Any] | None:
    """Resolver 結果的 caller 可見投影：移除描述其他 member 列的鍵與 reason，其餘原樣。"""
    if evidence is None:
        return None
    payload = dict(evidence.to_json_dict())
    for key in CALLER_HIDDEN_EXECUTION_KEYS:
        payload.pop(key, None)
    reasons = payload.get("outcome_reasons")
    if isinstance(reasons, list):
        payload["outcome_reasons"] = [r for r in reasons if r not in CALLER_HIDDEN_OUTCOME_REASONS]
    return payload


def make_unavailable_evidence_bundle(
    orchestration_run_id: str,
    *,
    status: str,
    terminal_reason: str | None,
    reason: str,
) -> EvidenceBundle:
    """建立不含來源內容的固定 unavailable schema。"""
    return {
        "schema_version": _SCHEMA_VERSION,
        "orchestration_run_id": orchestration_run_id,
        "availability": "unavailable",
        "orchestration": {
            "status": status,
            "terminal_reason": terminal_reason,
            "evidence_reason": reason,
        },
        "dqe": {"status": "unavailable", "items": [], "truncated": False},
        "reports": {"status": "unavailable", "items": [], "truncated": False},
        "lineage": {"status": "unavailable", "items": [], "truncated": False},
    }


def make_receipt_projection_evidence_bundle(
    orchestration_run_id: str, *, receipts: list[dict],
) -> EvidenceBundle:
    """由 **durable execution receipt projection** 組出的 evidence bundle。

    這條路徑只在 orchestration ledger 沒有該 run 時使用（例如 run 是由 CLI 以
    拋棄式 ledger 執行的）。回傳的內容**明確標示來源是 immutable receipt
    projection，不是 ledger 的 lifecycle state**——ledger 仍然是 lifecycle
    authority，這裡不會、也不能改變它。
    """
    latest = receipts[-1] if receipts else {}
    return {
        "schema_version": _SCHEMA_VERSION,
        "orchestration_run_id": orchestration_run_id,
        "availability": "available_from_receipt_projection",
        "orchestration": {
            "status": latest.get("execution_lifecycle_status"),
            "terminal_reason": latest.get("termination_reason"),
            "evidence_reason": "durable_execution_receipt_projection",
            "evidence_source": "external_execution_receipt_projection",
            "lifecycle_authority": ("the orchestration ledger has no record of this "
                                    "run; these facts come from an immutable "
                                    "execution receipt projection"),
            "dispatch_status": latest.get("dispatch_status"),
            "evidence_status": latest.get("evidence_status"),
            "process_exit_code": latest.get("process_exit_code"),
            "timed_out": latest.get("timed_out"),
            "step_id": latest.get("step_id"),
            "attempt": latest.get("attempt"),
            "tool_id": latest.get("tool_id"),
            "adapter_id": latest.get("adapter_id"),
            "artifact_manifest_id": latest.get("artifact_manifest_id"),
            "artifact_manifest_sha256": latest.get("artifact_manifest_sha256"),
            "receipt_id": latest.get("receipt_id"),
            "receipt_content_hash": latest.get("content_hash"),
            "receipt_count": len(receipts),
        },
        "dqe": {"status": "unavailable", "items": [], "truncated": False},
        "reports": {"status": "unavailable", "items": [], "truncated": False},
        "lineage": {"status": "unavailable", "items": [], "truncated": False},
    }


def make_flow_task_run_evidence_bundle(
    orchestration_run_id: str,
    *,
    status: str,
    terminal_reason: str | None,
    agent_task: dict[str, Any],
    receipts: list[dict[str, Any]],
    dqe_items: dict[str, Any] | None = None,
) -> EvidenceBundle:
    """`orfs_single_run` 的 evidence bundle。

    這條 workflow 由 daemon 的正式 ledger 驅動，因此 lifecycle authority
    是 ledger；receipt／artifact manifest 由 durable receipt projection 提供，
    input manifest 由 task binding 提供。

    歷史缺值一律保留 `unknown`／`unavailable`，**不由 reader fallback 冒充
    run lifecycle authority**。
    """
    latest = receipts[-1] if receipts else {}
    return {
        "schema_version": _SCHEMA_VERSION,
        "orchestration_run_id": orchestration_run_id,
        "availability": "available" if receipts else "partial",
        "orchestration": {
            "status": status,
            "terminal_reason": terminal_reason,
            "evidence_reason": "flow_task_run",
            "evidence_source": "orchestration_ledger_and_receipt_projection",
            "lifecycle_authority": "orchestration_ledger",
            "workflow_type": "orfs_single_run",
            # task 關聯：由 host 在 run 建立時寫入 ledger metadata，caller
            # 不能覆寫。
            "task_id": agent_task.get("task_id"),
            "slot": agent_task.get("slot"),
            "grant_id": agent_task.get("grant_id"),
            "profile_digest": agent_task.get("profile_digest"),
            "input_manifest_hash": agent_task.get("input_manifest_hash"),
            "recovery_policy": agent_task.get("recovery_policy"),
            "dispatch_status": latest.get("dispatch_status"),
            "evidence_status": latest.get("evidence_status"),
            "process_exit_code": latest.get("process_exit_code"),
            "timed_out": latest.get("timed_out"),
            "step_id": latest.get("step_id"),
            "attempt": latest.get("attempt"),
            "tool_id": latest.get("tool_id"),
            "adapter_id": latest.get("adapter_id"),
            "artifact_manifest_id": latest.get("artifact_manifest_id"),
            "artifact_manifest_sha256": latest.get("artifact_manifest_sha256"),
            "receipt_id": latest.get("receipt_id"),
            "receipt_content_hash": latest.get("content_hash"),
            "receipt_count": len(receipts),
        },
        "dqe": dqe_items or {"status": "unavailable", "items": [], "truncated": False},
        "reports": {"status": "unavailable", "items": [], "truncated": False},
        "lineage": {"status": "unavailable", "items": [], "truncated": False},
    }


def _validated_run_root(workspace: Path, eco_run_id: str) -> Path:
    """只接受 package 產生的 eco id，並保證 root 留在已知 workspace 下。"""
    if not eco_run_id.startswith("eco-") or not eco_run_id[4:].isalnum():
        raise ValueError("invalid eco run identity")
    root_parent = (workspace / ".terminux" / "eda_convergence").resolve()
    root = (root_parent / eco_run_id).resolve()
    try:
        root.relative_to(root_parent)
    except ValueError as exc:
        raise ValueError("evidence root escapes workspace") from exc
    return root


def _status_from_sources(*, dqe_status: str, report_status: str, lineage_status: str) -> Availability:
    if dqe_status == "available" and report_status == "available" and lineage_status == "available":
        return "available"
    if dqe_status == "unavailable" and report_status == "unavailable" and lineage_status == "unavailable":
        return "unavailable"
    return "partial"


def _is_regular_file_within(path: Path, root: Path) -> bool:
    """拒絕 run root 內指向外部的 symlink，避免受控索引退化成任意檔案讀取。"""
    try:
        if not path.is_file():
            return False
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _valid_section(section: object, *, max_items: int, item_keys: tuple[frozenset[str], ...]) -> bool:
    """驗證已發布 section 仍符合固定 schema，拒絕 caller 寫入的任意 JSON。"""
    if type(section) is not dict or set(section) != {"status", "items", "truncated"}:
        return False
    if section["status"] not in {"available", "partial", "unavailable"}:
        return False
    if type(section["items"]) is not list or len(section["items"]) > max_items:
        return False
    if type(section["truncated"]) is not bool:
        return False
    return all(
        type(item) is dict and any(set(item) == allowed for allowed in item_keys)
        for item in section["items"]
    )


def _is_published_bundle_schema(payload: object, orchestration_run_id: str) -> bool:
    """只接受本模組寫出的 v1 shape；不讓 artifact 變成任意 JSON 輸出通道。"""
    if type(payload) is not dict or set(payload) != {
        "schema_version", "orchestration_run_id", "availability", "orchestration", "dqe", "reports", "lineage"
    }:
        return False
    if payload.get("schema_version") != _SCHEMA_VERSION or payload.get("orchestration_run_id") != orchestration_run_id:
        return False
    if payload.get("availability") not in {"available", "partial", "unavailable"}:
        return False
    orchestration = payload.get("orchestration")
    if type(orchestration) is not dict or set(orchestration) != {
        "status", "terminal_reason", "domain_outcome", "eco_run_id"
    }:
        return False
    return (
        _valid_section(
            payload.get("dqe"),
            max_items=_MAX_DQE_ITEMS,
            item_keys=(frozenset({"dqe_id", "value", "unit", "status", "iteration", "step_id", "attempt", "package_id", "dispatch_key", "source_event_id"}),),
        )
        and _valid_section(
            payload.get("reports"),
            max_items=_MAX_REPORT_ITEMS,
            item_keys=(frozenset({"report_id", "kind", "iteration", "sha256", "bytes"}),),
        )
        and _valid_section(
            payload.get("lineage"),
            max_items=_MAX_LINEAGE_ITEMS + 2,
            item_keys=(
                frozenset({"kind", "sha256"}),
                frozenset({"kind", "iteration", "base_sha256", "new_sha256", "recipe_sha256"}),
            ),
        )
    )


def _read_dqe_items(db_path: Path, orchestration_run_id: str) -> dict[str, Any]:
    """以 run identity 參數化讀取既有 DQE table；絕不讀 PATH/blob 欄位。"""
    if not db_path.is_file():
        return {"status": "unavailable", "items": [], "truncated": False}
    # bundle 的 DQE evidence 也預設排除已作廢的 row。
    from terminux_engine.evidence_integrity.invalidation import load_invalidation_set

    invalidated = load_invalidation_set()
    # 整個 run 被作廢時，bundle 的 DQE 段就是 `unavailable`——沿用固定 schema
    # 的三種狀態，不新增第四種讓下游必須認識的值。
    if invalidated.run_invalidated(orchestration_run_id):
        return {"status": "unavailable", "items": [], "truncated": False}
    try:
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                # `rowid`：見 dqe/db.py 的同一個理由（舊 schema 未必有 `id`）。
                "SELECT rowid AS _row_id, DQE_ID, VALUE, UNIT, STATUS, eco_iteration, step_id, attempt, "
                "package_id, dispatch_key, source_event_id FROM Version_Dqe_Tab "
                "WHERE orchestration_run_id = ? "
                "ORDER BY eco_iteration ASC, step_id ASC, attempt ASC, package_id ASC, DQE_ID ASC "
                "LIMIT ?",
                (orchestration_run_id, _MAX_DQE_ITEMS + 1),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return {"status": "unavailable", "items": [], "truncated": False}

    db_identity = str(db_path.resolve())
    rows = [r for r in rows
            if not invalidated.dqe_row_invalidated(
                db_identity=db_identity, row_id=r["_row_id"])]

    truncated = len(rows) > _MAX_DQE_ITEMS
    def _safe_value(row: sqlite3.Row) -> Any:
        """DQE row 的 PATH-like metric 不得把 workspace filesystem path 帶出 bundle。"""
        value = row["VALUE"]
        dqe_id = row["DQE_ID"]
        if type(value) is str and (
            dqe_id.lower().endswith("_path")
            or value.startswith("/")
            or value.startswith("~")
        ):
            return None
        return value

    items = [
        {
            "dqe_id": row["DQE_ID"],
            "value": _safe_value(row),
            "unit": row["UNIT"] or "",
            "status": row["STATUS"] or "UNKNOWN",
            "iteration": row["eco_iteration"],
            "step_id": row["step_id"],
            "attempt": row["attempt"],
            "package_id": row["package_id"],
            "dispatch_key": row["dispatch_key"],
            "source_event_id": row["source_event_id"],
        }
        for row in rows[:_MAX_DQE_ITEMS]
    ]
    return {
        "status": "available" if items else "partial",
        "items": items,
        "truncated": truncated,
    }


def read_bound_dqe_items(db_path: Path, orchestration_run_id: str) -> dict[str, Any]:
    """讀出已由 Evidence Resolver 判定綁得回這次執行（Q=bound）的 DQE rows。

    與 eda_convergence bundle 使用同一套固定 schema、作廢排除與路徑遮蔽規則。
    """
    return _read_dqe_items(db_path, orchestration_run_id)


def build_timing_path_section(rows: list[Any], *, orchestration_run_id: str,
                              latest_receipt: dict[str, Any], member_id: str,
                              execution_evidence: Any,
                              reparsers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """由 `timing_path_evidence` record 組出 bundle 的 `timing_path` section。

    只在下列條件**全部**成立時回傳 `available`：Evidence Resolver 已判定本人 receipt 可信且
    manifest 已驗證（沒有 content hash 不自洽的本人列）；record 由本人寫入；record 的 run／attempt／
    step／manifest id／manifest sha 與 receipt 投影逐項相同；同一身分的 record 只有一份內容；
    record 的證據能以封閉 schema 讀回；**record 內容能由 receipt 指向的已驗證 manifest 與其
    report bytes 重新解析並逐欄重現**（`timing_evidence.verification`）；對外投影通過封閉驗證。
    缺任一條件即 `unavailable`＋封閉原因碼。Record 只是指標：對外的 stage／特徵一律來自
    重解析結果，不信任 record 自述的 digest、stages 或 features。

    `rows` 可能含其他 member 寫入同 run id 的列（reader 不過濾）；它們只會被略過，不得在
    status、reason、計數或輸出形狀上留下任何痕跡。
    """
    from terminux_engine.timing_evidence.features import compute_features
    from terminux_engine.timing_evidence.model import (
        RECORD_TYPE,
        TimingEvidenceError,
        TimingPathEvidence,
    )
    from terminux_engine.timing_evidence.projection import (
        SectionInvalid,
        outbound_section,
        unavailable_section,
        validate_section,
    )
    from terminux_engine.timing_evidence.verification import verify_record_against_manifest

    if execution_evidence is None:
        return unavailable_section("execution_evidence_unavailable")
    # 只看 caller 本人列的可信度。其他 member 寫入同 run id 的列不得改變這個 section 的任何
    # 可觀測結果（它們也不在 `rows`／`latest_receipt` 內）；對 LLM 路徑的拒送由 tool 層以
    # `ReceiptIntegrity` 在 bundle 之外處理。
    if getattr(execution_evidence, "untrusted_receipt_count", 1) != 0:
        return unavailable_section("receipt_projection_untrusted")
    if getattr(execution_evidence, "artifact_manifest", None) != "verified":
        return unavailable_section("artifact_manifest_not_verified")
    own = [row for row in rows if getattr(row, "user_id", None) == member_id]
    if not own:
        # 其他 member 寫入同 run id 的 timing record 不是 caller 的資料：有沒有、有幾筆都不得
        # 改變這個 section 的任何可觀測結果，所以原因碼固定為 `no_record`，與 reader 完全沒有
        # 列時相同（JSON-RPC `evidence_bundle`／`evidence_set` 都經過這裡）。
        return unavailable_section("no_record")
    expected = {
        "run_id": orchestration_run_id,
        "attempt": latest_receipt.get("attempt"),
        "step_id": latest_receipt.get("step_id"),
        "artifact_manifest_id": latest_receipt.get("artifact_manifest_id"),
        "artifact_manifest_sha256": latest_receipt.get("artifact_manifest_sha256"),
    }
    if any(value is None for value in expected.values()):
        return unavailable_section("receipt_identity_incomplete")
    matching: list[dict[str, Any]] = []
    for row in own:
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict) or payload.get("record_type") != RECORD_TYPE:
            continue
        source = payload.get("source")
        if not isinstance(source, dict):
            continue
        if all(source.get(key) == value for key, value in expected.items()):
            matching.append(payload)
    if not matching:
        return unavailable_section("record_identity_mismatch")
    canonical = {json.dumps(p, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                 for p in matching}
    if len(canonical) > 1:
        return unavailable_section("ambiguous_records")
    payload = matching[0]
    if payload.get("status") != "available":
        # record 的 reason 只是提示：收斂到封閉詞彙，不原樣外送。
        return unavailable_section(payload.get("reason"))
    try:
        evidence = TimingPathEvidence.from_json_dict(payload.get("evidence"))
    except TimingEvidenceError as exc:
        return unavailable_section(f"record_unreadable:{exc.code}")
    source = evidence.source
    if (source.run_id, source.attempt, source.step_id, source.artifact_manifest_id,
            source.artifact_manifest_sha256) != (
            orchestration_run_id, expected["attempt"], expected["step_id"],
            expected["artifact_manifest_id"], expected["artifact_manifest_sha256"]):
        return unavailable_section("record_identity_mismatch")
    verified = verify_record_against_manifest(
        evidence, manifest_reference=latest_receipt.get("artifact_manifest_reference"),
        manifest_id=str(expected["artifact_manifest_id"]),
        manifest_sha256=str(expected["artifact_manifest_sha256"]),
        reparsers=reparsers or {},
    )
    if verified.evidence is None:
        return unavailable_section(verified.reason or "record_unavailable")
    section = outbound_section(verified.evidence, compute_features(verified.evidence))
    try:
        validate_section(section)
    except SectionInvalid as exc:
        return unavailable_section(f"projection_invalid:{exc.code}")
    return section


def _sha256_limited(path: Path) -> str | None:
    """只雜湊 bounded regular report，避免把 bundle 變成任意檔案 reader。"""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_REPORT_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _report_index(root: Path) -> dict[str, Any]:
    """索引 known run root 內的 report，不輸出或接受任何 path。"""
    reports_root = root / "iterations"
    if not reports_root.is_dir():
        return {"status": "unavailable", "items": [], "truncated": False}
    candidates = sorted(
        (
            path
            for path in reports_root.rglob("*.rpt")
            if _is_regular_file_within(path, root)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    truncated = len(candidates) > _MAX_REPORT_ITEMS
    items: list[dict[str, Any]] = []
    for path in candidates[:_MAX_REPORT_ITEMS]:
        digest = _sha256_limited(path)
        if digest is None:
            continue
        relative = path.relative_to(root).as_posix()
        iteration = relative.split("/", 2)[1] if relative.startswith("iterations/") else None
        items.append(
            {
                "report_id": hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16],
                "kind": "eda_report",
                "iteration": iteration,
                "sha256": digest,
                "bytes": path.stat().st_size,
            }
        )
    return {
        "status": "available" if items else "partial",
        "items": items,
        "truncated": truncated,
    }


def _lineage_index(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """從 durable state/materialization records 產生僅含 hash 的 lineage。"""
    state_scripts: list[dict[str, Any]] = []
    for role in ("best_script", "current_script"):
        script = state.get(role)
        if type(script) is dict and type(script.get("sha256")) is str:
            state_scripts.append({"kind": role, "sha256": script["sha256"]})

    records = sorted((root / "iterations").glob("iter_*/scripts/materialization.json"))
    materializations: list[dict[str, Any]] = []
    for record_path in records[:_MAX_LINEAGE_ITEMS]:
        if not _is_regular_file_within(record_path, root):
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            base = record.get("base_script")
            new = record.get("new_script")
            recipe_sha = record.get("materialized_recipe_sha256")
            if not (
                type(base) is dict
                and type(new) is dict
                and type(base.get("sha256")) is str
                and type(new.get("sha256")) is str
                and type(recipe_sha) is str
            ):
                continue
            materializations.append(
                {
                    "kind": "materialization",
                    "iteration": record_path.parents[1].name,
                    "base_sha256": base["sha256"],
                    "new_sha256": new["sha256"],
                    "recipe_sha256": recipe_sha,
                }
            )
        except (OSError, ValueError, TypeError):
            continue
    items = state_scripts + materializations
    return {
        "status": "available" if items else "partial",
        "items": items,
        "truncated": len(records) > _MAX_LINEAGE_ITEMS,
    }


def build_eda_convergence_bundle(
    *,
    workspace: Path,
    dqe_db_path: Path,
    eco_run_id: str,
    orchestration_run_id: str,
    status: str,
    terminal_reason: str | None,
) -> EvidenceBundle:
    """從 EDA convergence 的既有 durable sources 建立一份 bounded bundle。"""
    try:
        state_mod = importlib.import_module(_EDA_CONVERGENCE_STATE_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name not in {_EDA_CONVERGENCE_STATE_MODULE, _EDA_CONVERGENCE_PACKAGE}:
            raise
        return make_unavailable_evidence_bundle(
            orchestration_run_id,
            status=status,
            terminal_reason=terminal_reason,
            reason="workflow_not_installed",
        )
    try:
        root = _validated_run_root(workspace, eco_run_id)
        state = state_mod.read_state(workspace, eco_run_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return make_unavailable_evidence_bundle(
            orchestration_run_id,
            status=status,
            terminal_reason=terminal_reason,
            reason="state_unavailable",
        )
    if state.get("orchestration_run_id") != orchestration_run_id:
        return make_unavailable_evidence_bundle(
            orchestration_run_id,
            status=status,
            terminal_reason=terminal_reason,
            reason="state_correlation_mismatch",
        )

    dqe = _read_dqe_items(dqe_db_path, orchestration_run_id)
    reports = _report_index(root)
    lineage = _lineage_index(root, state)
    availability = _status_from_sources(
        dqe_status=dqe["status"],
        report_status=reports["status"],
        lineage_status=lineage["status"],
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "orchestration_run_id": orchestration_run_id,
        "availability": availability,
        "orchestration": {
            "status": status,
            "terminal_reason": terminal_reason,
            "domain_outcome": state.get("domain_outcome"),
            "eco_run_id": eco_run_id,
        },
        "dqe": dqe,
        "reports": reports,
        "lineage": lineage,
    }


def evidence_bundle_path(workspace: Path, eco_run_id: str) -> Path:
    """回傳固定 producer artifact path，不接受 caller 任意 path。"""
    return _validated_run_root(workspace, eco_run_id) / "evidence_bundle.v1.json"


def publish_eda_convergence_bundle(
    *,
    workspace: Path,
    dqe_db_path: Path,
    eco_run_id: str,
    orchestration_run_id: str,
    status: str,
    terminal_reason: str | None,
) -> EvidenceBundle:
    """原子發布 current bundle；失敗由呼叫端記錄但不改 workflow lifecycle。"""
    bundle = build_eda_convergence_bundle(
        workspace=workspace,
        dqe_db_path=dqe_db_path,
        eco_run_id=eco_run_id,
        orchestration_run_id=orchestration_run_id,
        status=status,
        terminal_reason=terminal_reason,
    )
    target = evidence_bundle_path(workspace, eco_run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(bundle, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return bundle


def load_published_eda_convergence_bundle(
    *,
    workspace: Path,
    eco_run_id: str,
    orchestration_run_id: str,
    status: str,
    terminal_reason: str | None,
) -> EvidenceBundle:
    """讀取已發布 bundle，驗證 correlation 後只覆寫 live orchestration state。"""
    try:
        root = _validated_run_root(workspace, eco_run_id)
        target = root / "evidence_bundle.v1.json"
        if not _is_regular_file_within(target, root) or target.stat().st_size > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle is not a safe regular file")
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return make_unavailable_evidence_bundle(
            orchestration_run_id,
            status=status,
            terminal_reason=terminal_reason,
            reason="bundle_unavailable",
        )
    if not _is_published_bundle_schema(payload, orchestration_run_id):
        return make_unavailable_evidence_bundle(
            orchestration_run_id,
            status=status,
            terminal_reason=terminal_reason,
            reason="bundle_correlation_mismatch",
        )
    payload["orchestration"] = dict(payload["orchestration"])
    payload["orchestration"]["status"] = status
    payload["orchestration"]["terminal_reason"] = terminal_reason
    return payload  # type: ignore[return-value]
