# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""跨 daemon instance 的 durable baseline import 重建。

為什麼需要它
------------

`export_eap_baseline_import()` 原本只接受**目前 daemon instance 裡仍然 live**
的 baseline task。那條規則本身是對的：它擋掉「呼叫端自己捏一份 import 文件」
這個形狀。但 `daemon_instance_id` 每次 daemon 啟動都重新產生，於是一份已經
qualified 的 baseline 只能在產生它的那個 process 存活期間被重用——跨日、重啟
或正式批次都不行，這與 durable Work／fresh reader 的設計自相矛盾。

這支模組補上另一條路：**同樣不信任呼叫端**，但改由 server 自己從 durable 紀錄
重建同一份 canonical import document，再與 target task 帶進來的內容逐位元組比對。

刻意保持窄
----------

* 只適用於 **terminal**（`COMPLETED` ＋ `persistence_status=durable`）的
  baseline-only task。active／未完成 task 的 instance gate **不放寬**。
* source store 的位置只能來自 **Profile 釘住的宣告**（operator 撰寫、唯讀、
  digest 進 task digest），不能由 RPC 或 client 指定。
* source store 全程唯讀（`mode=ro`），不新增、不修改、不回填任何事件。
* 不是 task migration、不是 cross-project import、不是 task cloning。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks import flow_models as F

#: Profile 內宣告 durable source 的欄位名。
PROFILE_FIELD = "durable_baseline_source"
#: 宣告必須齊備的欄位。少一個就不成立——不補預設值。
REQUIRED_FIELDS: tuple[str, ...] = (
    "source_task_id", "source_task_digest", "source_profile_id",
    "source_profile_digest", "reference_digest",
    "event_store_path", "artifact_root", "reference_path",
    # 重用既有 run 時，它的 DQE 仍屬於來源。位置只能由 Profile 釘住——
    # 宣告進 profile digest 再進 task digest，RPC 不得提供任意 DB 路徑。
    "dqe_db_path")

#: target Profile 與 source Profile 必須相同的**工程身分**。
#:
#: 刻意不比 `profile_digest`：target Profile 本身要帶 durable source 釘選，
#: 所以它的 digest 在構造上不可能等於 source 的。要求相等只會讓這條路徑
#: 永遠不成立。真正該一致的是「同一個設計、同一份輸入、同一個 image、
#: 同一個方法」——那些逐項比對，而且來源是 durable profile snapshot，
#: 不是呼叫端宣告。
ENGINEERING_IDENTITY_FIELDS: tuple[str, ...] = (
    "design_config", "design_id", "platform_id", "docker_image_id",
    "input_manifest_sha256", "template_id", "declared_method_identity")

#: scope 文件已知的欄位。出現未知欄位一律拒絕，不忽略。
KNOWN_SCOPE_FIELDS: frozenset[str] = frozenset({
    "member_id", "project_id", "origin_session_id", "workspace",
    "project_source"})
#: 跨 durable 重用時**必須完全相同**的 scope 欄位。
SCOPE_MUST_MATCH: tuple[str, ...] = ("member_id", "project_id", "workspace",
                                     "project_source")
#: 唯一允許不同的欄位——來源與本次使用本來就不是同一個 session。
SCOPE_MAY_DIFFER: tuple[str, ...] = ("origin_session_id",)

_WORK_EVENT = "engineering_work.event.v1"
_SNAPSHOT = "flow_task.snapshot.v1"
_PROFILE_SNAPSHOT = "flow_task.profile_snapshot.v1"
_TERMINAL = "flow_task.terminal.v1"
_RUN_LINK = "flow_task.run_link.v1"
_STEP_RESULT = "flow_task.step_result.v1"
_STEP_RECEIPT = "flow_task.step_receipt.v1"


class DurableBaselineError(RuntimeError):
    """無法由 durable 紀錄重建這份 baseline import。"""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


@dataclass(frozen=True)
class DurableBaselineSource:
    """Profile 釘住的 source 宣告。只有 operator 能寫，digest 進 task digest。"""

    source_task_id: str
    source_task_digest: str
    source_profile_id: str
    source_profile_digest: str
    reference_digest: str
    event_store_path: str
    artifact_root: str
    reference_path: str
    dqe_db_path: str

    @classmethod
    def from_profile(cls, profile: Any) -> "DurableBaselineSource | None":
        execution = getattr(profile, "execution", None)
        declared = getattr(execution, PROFILE_FIELD, None)
        if declared is None and isinstance(execution, dict):
            declared = execution.get(PROFILE_FIELD)
        if declared is None:
            return None
        if hasattr(declared, "to_json_dict"):
            declared = declared.to_json_dict()
        if not isinstance(declared, dict):
            raise DurableBaselineError(
                "the pinned durable baseline source is not a document")
        missing = sorted(name for name in REQUIRED_FIELDS if not declared.get(name))
        if missing:
            raise DurableBaselineError(
                "the pinned durable baseline source is incomplete", missing=missing)
        unknown = sorted(set(declared) - set(REQUIRED_FIELDS))
        if unknown:
            raise DurableBaselineError(
                "the pinned durable baseline source has unknown fields",
                unknown=unknown)
        return cls(**{name: str(declared[name]) for name in REQUIRED_FIELDS})


def _readable_path(value: str, *, label: str, kind: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise DurableBaselineError(f"{label} must be an absolute path", path=value)
    if path.is_symlink():
        raise DurableBaselineError(
            f"{label} must not be a symlink; the target can be repointed after "
            f"the check", path=value)
    if kind == "file" and not path.is_file():
        raise DurableBaselineError(f"{label} is not a readable file", path=value)
    if kind == "dir" and not path.is_dir():
        raise DurableBaselineError(f"{label} is not a readable directory", path=value)
    resolved = path.resolve()
    if str(resolved) != str(path):
        raise DurableBaselineError(
            f"{label} resolves elsewhere; the declared path must be canonical",
            declared=value, resolved=str(resolved))
    return resolved


def _events(store_path: Path, task_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    """唯讀讀回這個 task 的全部事件。**不寫入、不 migrate、不建立檔案。**"""
    uri = f"file:{store_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DurableBaselineError("the source event store cannot be opened "
                                   "read-only", error=str(exc)) from None
    try:
        rows = conn.execute(
            "SELECT event_id, event_type, payload FROM session_events "
            "ORDER BY rowid ASC").fetchall()
    except sqlite3.Error as exc:
        raise DurableBaselineError("the source event store is unreadable",
                                   error=str(exc)) from None
    finally:
        conn.close()
    out: list[tuple[str, str, dict[str, Any]]] = []
    for event_id, event_type, payload in rows:
        try:
            doc = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("task_id") == task_id:
            out.append((str(event_id), str(event_type), doc))
    return out


def _one(events: list[tuple[str, str, dict[str, Any]]], event_type: str,
         *, label: str) -> tuple[str, dict[str, Any]]:
    matched = [(event_id, doc) for event_id, kind, doc in events if kind == event_type]
    if not matched:
        raise DurableBaselineError(f"the source task has no {label} event")
    if len(matched) > 1:
        raise DurableBaselineError(
            f"the source task has more than one {label} event; the durable "
            f"record is ambiguous", count=len(matched))
    return matched[0]


def _sha256_payload(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _work_events(store_path: Path) -> list[dict[str, Any]]:
    """唯讀讀回 source store 的 Work 事件。"""
    uri = f"file:{store_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute(
            "SELECT payload FROM session_events WHERE event_type = ? "
            "ORDER BY rowid ASC", (_WORK_EVENT,)).fetchall()
    except sqlite3.Error as exc:
        raise DurableBaselineError("the source Work events are unreadable",
                                   error=str(exc)) from None
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close 失敗不影響唯讀語意
            pass
    out = []
    for (payload,) in rows:
        try:
            out.append(json.loads(payload))
        except (TypeError, ValueError):
            continue
    return out



#: 來源 DQE 必須由這個 package 寫入；換一個 evaluator 就不是同一份證據。
SOURCE_DQE_PACKAGE_ID = "orfs_dqe"


def verify_source_dqe(
    source: DurableBaselineSource, *, run_evidence: dict[str, Any],
) -> dict[str, Any]:
    """唯讀核對來源 DQE，並回傳可在 fresh reader 重驗的引用。

    重用的是**既有 run**，它的 DQE 仍保有原 run 身分：這裡不 migrate、不回填、
    不重跑 evaluator、不寫入來源 DB，也不產生任何冒充 target run 的 row。
    目標 Work 只保存引用與核對結果。

    位置只由 `source.dqe_db_path`（Profile 釘選）決定，不接受呼叫端提供。

    **讀得到不等於判定通過**：這裡只確立「這份 DQE 就是那一次 run 的」。
    row 本身的 STATUS／semantic_status 原樣回傳，交既有判定邏輯處理。

    任一必要關聯缺失、對不上或被改寫即拒絕；不補造欄位，也不以「有幾筆 row」
    代替身分核對。
    """
    run_id = run_evidence.get("run_id")
    dispatch_key = run_evidence.get("dqe_dispatch_key")
    row_count = run_evidence.get("dqe_row_count")
    manifest_id = run_evidence.get("artifact_manifest_id")
    if not (isinstance(run_id, str) and run_id
            and isinstance(dispatch_key, str) and dispatch_key
            and isinstance(row_count, int)):
        raise DurableBaselineError(
            "the durable run evidence does not carry the DQE identity needed to "
            "verify the source rows",
            field="completed_run_evidence",
            have=sorted(k for k in ("run_id", "dqe_dispatch_key", "dqe_row_count")
                        if run_evidence.get(k) is not None))

    db = _readable_path(source.dqe_db_path, label="the source DQE database",
                        kind="file")
    rows: list[dict[str, Any]] = []
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT * FROM Version_Dqe_Tab WHERE orchestration_run_id = ? "
            "AND package_id = ? ORDER BY id", (run_id, SOURCE_DQE_PACKAGE_ID))
        rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        raise DurableBaselineError(
            "the source DQE database could not be read",
            source_dqe_db_path=str(db), error=str(exc)) from exc
    finally:
        connection.close()

    if not rows:
        raise DurableBaselineError(
            "the source DQE holds no rows for the pinned baseline run",
            field="orchestration_run_id", source=run_id,
            source_dqe_db_path=str(db), package_id=SOURCE_DQE_PACKAGE_ID)
    if len(rows) != row_count:
        raise DurableBaselineError(
            "the source DQE row count differs from the durable record",
            field="dqe_row_count", source=len(rows), target=row_count,
            run_id=run_id)
    keys = {str(row.get("dispatch_key") or "") for row in rows}
    if keys != {dispatch_key}:
        raise DurableBaselineError(
            "the source DQE rows do not all carry the recorded dispatch key",
            field="dispatch_key", source=sorted(keys), target=dispatch_key,
            run_id=run_id)
    runs = {str(row.get("orchestration_run_id") or "") for row in rows}
    if runs != {run_id}:
        raise DurableBaselineError(
            "the source DQE rows do not all belong to the pinned run",
            field="orchestration_run_id", source=sorted(runs), target=run_id)
    packages = {str(row.get("package_id") or "") for row in rows}
    if packages != {SOURCE_DQE_PACKAGE_ID}:
        raise DurableBaselineError(
            "the source DQE rows were not all written by the recorded evaluator",
            field="package_id", source=sorted(packages),
            target=SOURCE_DQE_PACKAGE_ID)

    # 與既有 artifact manifest 的關聯：row 的 PATH 必須指向被釘住的那一份。
    if isinstance(manifest_id, str) and manifest_id:
        referenced = {str(row.get("PATH") or "") for row in rows
                      if row.get("PATH")}
        if not referenced or not all(manifest_id in path for path in referenced):
            raise DurableBaselineError(
                "the source DQE rows do not reference the recorded artifact "
                "manifest", field="PATH", source=sorted(referenced),
                target=manifest_id)

    return {
        "schema_version": "durable_baseline.source_dqe.v1",
        "source_dqe_db_path": str(db),
        "run_id": run_id,
        "dispatch_key": dispatch_key,
        "row_count": len(rows),
        "package_id": SOURCE_DQE_PACKAGE_ID,
        "step_ids": sorted({str(row.get("step_id") or "") for row in rows}),
        "artifact_manifest_id": manifest_id,
        # 原始判定結果照實帶出，不在這裡改寫或過濾。
        "statuses": sorted({str(row.get("STATUS") or "") for row in rows}),
        "semantic_statuses": sorted({str(row.get("semantic_status") or "")
                                     for row in rows}),
        "read_only": True,
    }


def reconstruct_work_baseline_reference(
    source: DurableBaselineSource, *, expected_import: dict[str, Any],
) -> dict[str, Any]:
    """由 source Work 的 durable snapshot 取回 **Work 層** 的 baseline reference。

    為什麼不自己合成一份：`validate_baseline()` 要的 reference 帶著 receipt／
    manifest／DQE 的完成度、`content` 與 `content_digest`、tool／config／
    constraints identity。那份文件在 baseline 被收集當下就已經寫進 Work 事件
    了，重新合成只會是「我們相信它長這樣」，而不是「它就是這樣」。

    找法不依賴額外釘選：挑出 `execution_import.source_task_id` 等於釘住的
    source task 的那一份。出現多份互相矛盾的就拒絕，不挑最新。
    """
    candidates: list[dict[str, Any]] = []
    for event in _work_events(_readable_path(
            source.event_store_path, label="the source event store", kind="file")):
        snapshot = event.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        baseline = snapshot.get("baseline")
        if not isinstance(baseline, dict) or baseline.get("status") not in (
                "COMPLETE", "REUSED_VALIDATED"):
            continue
        reference = baseline.get("reference")
        if not isinstance(reference, dict):
            continue
        imported = reference.get("execution_import")
        if (not isinstance(imported, dict)
                or imported.get("source_task_id") != source.source_task_id):
            continue
        candidates.append(reference)

    if not candidates:
        raise DurableBaselineError(
            "no durable Work snapshot carries a completed baseline reference for "
            "the pinned source task", source_task_id=source.source_task_id)
    digests = {_sha256_payload(row) for row in candidates}
    if len(digests) != 1:
        raise DurableBaselineError(
            "the durable Work snapshots disagree on the baseline reference",
            variants=len(digests))

    reference = candidates[-1]
    if reference.get("content_digest") != _sha256_payload(
            reference.get("content") or {}):
        raise DurableBaselineError(
            "the durable baseline reference content no longer matches its digest")
    imported = dict(reference.get("execution_import") or {})
    if imported.get("source_task_digest") != source.source_task_digest:
        raise DurableBaselineError(
            "the durable Work reference points at another task digest")
    return reference


def reconstruct_import(
    source: DurableBaselineSource, *, profile: Any, member_id: str,
    project_id: str, reference_policy_state: str,
    target_origin_session_id: str | None = None,
    target_workspace: str = "", target_project_source: str = "profile",
    work_id: str | None = None,
) -> dict[str, Any]:
    """由 durable 紀錄重建 canonical baseline import document。

    兩個 digest，語意不同：

    * `source_import_digest` 只描述**來源** durable baseline。同一份 baseline
      不論被哪一個 target session 重用，這個值都一樣——它是「重用的是同一份
      baseline」的證明。
    * `target_binding_digest` 把來源綁到**本次**的 member／project／target
      session／Work。它讓跨 session 不被誤判成 scope drift，同時擋掉別的
      member 或 project 拿同一份 reference 越權重用。

    `source_origin_session_id` 由 source EventStore 讀出，
    `target_origin_session_id` 由已驗證的 context 傳入。**兩者都不接受
    client import document 自行宣告。**
    """
    store_path = _readable_path(source.event_store_path,
                                label="the source event store", kind="file")
    artifact_root = _readable_path(source.artifact_root,
                                   label="the source artifact root", kind="dir")
    reference_path = _readable_path(source.reference_path,
                                    label="the baseline reference", kind="file")

    events = _events(store_path, source.source_task_id)
    if not events:
        raise DurableBaselineError("the source task is absent from the source "
                                   "event store", task_id=source.source_task_id)

    _, snapshot = _one(events, _SNAPSHOT, label="task snapshot")
    task = dict(snapshot.get("task") or {})
    if snapshot.get("task_digest") != source.source_task_digest:
        raise DurableBaselineError(
            "the source task digest does not match the pinned declaration",
            durable=snapshot.get("task_digest"), pinned=source.source_task_digest)
    if task.get("template_id") != F.BASELINE_ONLY_TEMPLATE_ID:
        raise DurableBaselineError(
            "the source task is not a baseline-only collection",
            template_id=task.get("template_id"))
    if (task.get("profile_id") != source.source_profile_id
            or task.get("profile_digest") != source.source_profile_digest):
        raise DurableBaselineError(
            "the source baseline was not collected under the pinned Profile",
            durable=task.get("profile_id"), pinned=source.source_profile_id)

    # target Profile 的工程身分必須與 source Profile 相同。比對對象是 durable
    # profile snapshot，不是呼叫端提供的任何東西。
    _, profile_event = _one(events, _PROFILE_SNAPSHOT, label="profile snapshot")
    source_profile = dict(profile_event.get("profile") or {})
    if source_profile.get("profile_digest") != source.source_profile_digest:
        raise DurableBaselineError(
            "the durable profile snapshot does not match the source task")
    drifted = sorted(
        name for name in ENGINEERING_IDENTITY_FIELDS
        if source_profile.get(name) != getattr(profile, name, None))
    # host-native Profile 沒有 image；工具身分是 execution 內的 binding，
    # 必須同樣一致（舊 Profile 兩側都沒有 binding，結果不變）。
    source_binding = (source_profile.get("execution") or {}).get("native_tool_binding")
    target_binding = getattr(profile, "native_tool_binding", None)
    target_binding = (target_binding.to_json_dict()
                      if hasattr(target_binding, "to_json_dict") else target_binding)
    if source_binding != target_binding:
        drifted = sorted([*drifted, "native_tool_binding"])
    if drifted:
        raise DurableBaselineError(
            "the target Profile's engineering identity differs from the "
            "baseline's; the candidate would not be comparable",
            differing=drifted,
            source={name: source_profile.get(name) for name in drifted})
    source_parameter = dict(source_profile.get("parameter") or {})
    target_parameter = getattr(profile, "parameter", None)
    target_parameter = (target_parameter.to_json_dict()
                        if hasattr(target_parameter, "to_json_dict")
                        else dict(target_parameter or {})
                        if isinstance(target_parameter, dict)
                        else {"name": getattr(target_parameter, "name", None),
                              "baseline": getattr(target_parameter, "baseline", None)})
    if (source_parameter.get("name") != target_parameter.get("name")
            or source_parameter.get("baseline") != target_parameter.get("baseline")):
        raise DurableBaselineError(
            "the target Profile's parameter identity differs from the baseline's",
            source=source_parameter, target=target_parameter)
    scope = dict(task.get("scope") or {})
    unknown_scope = sorted(set(scope) - KNOWN_SCOPE_FIELDS)
    if unknown_scope:
        raise DurableBaselineError(
            "the source scope carries unknown fields; an unrecognised authority "
            "field is refused, never ignored", unknown=unknown_scope)
    target_scope = {"member_id": member_id, "project_id": project_id,
                    "workspace": target_workspace,
                    "project_source": target_project_source}
    differing = sorted(name for name in SCOPE_MUST_MATCH
                       if scope.get(name) != target_scope[name])
    if differing:
        raise DurableBaselineError(
            "the source baseline scope differs from this Work's scope; only the "
            "originating session may differ across a durable reuse",
            differing=differing,
            source={name: scope.get(name) for name in differing},
            target={name: target_scope[name] for name in differing})

    _, terminal = _one(events, _TERMINAL, label="terminal")
    if terminal.get("status") != "COMPLETED":
        raise DurableBaselineError(
            "the source baseline task did not complete",
            status=terminal.get("status"))

    _, run_link = _one(events, _RUN_LINK, label="run link")
    run = dict(run_link.get("run") or {})
    if (run.get("slot") != F.SLOT_BASELINE
            or run.get("task_id") != source.source_task_id or not run.get("run_id")):
        raise DurableBaselineError("the source run identity is inconsistent",
                                   run=run)

    result_event_id, result = _one(events, _STEP_RESULT, label="step result")
    envelope = dict(result.get("envelope") or {})
    full = dict(envelope.get("full_result") or {})
    _, receipt_event = _one(events, _STEP_RECEIPT, label="step receipt")
    if receipt_event.get("step_status") != "SUCCEEDED":
        raise DurableBaselineError("the source baseline step did not succeed",
                                   step_status=receipt_event.get("step_status"))
    if receipt_event.get("persistence_status") != "durable":
        raise DurableBaselineError(
            "the source baseline evidence was never persisted durably",
            persistence_status=receipt_event.get("persistence_status"))
    if receipt_event.get("result_event_id") != result_event_id:
        raise DurableBaselineError(
            "the source receipt points at another result event")
    if receipt_event.get("result_sha256") != envelope.get("result_sha256"):
        raise DurableBaselineError(
            "the source receipt and result disagree on the result hash")

    evidence = dict(full.get("completed_run_evidence") or {})
    if evidence.get("state") != "ready" or evidence.get("run_id") != run.get("run_id"):
        raise DurableBaselineError("the source run evidence is not ready",
                                   state=evidence.get("state"))

    # ── baseline reference：digest 必須等於授權前釘住的值 ──────────
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if not isinstance(reference, dict):
        raise DurableBaselineError("the baseline reference is not a document")
    if reference.get("reference_digest") != source.reference_digest:
        raise DurableBaselineError(
            "the baseline reference digest does not match the pinned value",
            found=reference.get("reference_digest"))

    # `verification_reference` 是 host 當初寫下的**包裝**（reference_dir／
    # reference_path／sha256 ＋ production_reference），不是 reference 文件
    # 本身。它存在 durable Work reference 裡，所以由那裡取回，再與釘選的
    # 檔案互相核對——兩邊對不上就不是同一份 baseline。
    work_reference = reconstruct_work_baseline_reference(source, expected_import={})
    stored_import = dict(work_reference.get("execution_import") or {})
    verification_reference = stored_import.get("verification_reference")
    if not isinstance(verification_reference, dict):
        raise DurableBaselineError(
            "the durable Work reference carries no verification reference")
    produced = verification_reference.get("production_reference")
    if not isinstance(produced, dict):
        raise DurableBaselineError(
            "the durable verification reference carries no production reference")
    if produced.get("reference_digest") != source.reference_digest:
        raise DurableBaselineError(
            "the durable verification reference is for another baseline",
            found=produced.get("reference_digest"))
    # `record_path` 是持久化時加上的**位置**欄位，不是身分的一部分：同一份
    # reference 從不同路徑讀回仍是同一份。其餘欄位必須完全相同。
    if {k: v for k, v in produced.items() if k != "record_path"} != {
            k: v for k, v in reference.items() if k != "record_path"}:
        raise DurableBaselineError(
            "the durable verification reference and the pinned reference file "
            "are not the same document")

    # 不進身分比對**不等於**不檢查。位置欄位仍須是絕對、canonical、非 symlink，
    # 且落在釘選 reference 所在的那個目錄樹之內——否則一份看似相同的 reference
    # 可以從任意位置被指進來。
    reference_dir = _readable_path(
        str(verification_reference.get("reference_dir") or ""),
        label="the verification reference directory", kind="dir")
    if reference_dir != reference_path.parent:
        raise DurableBaselineError(
            "the verification reference directory is not the directory holding "
            "the pinned reference", declared=str(reference_dir),
            pinned_parent=str(reference_path.parent))
    for label, value in (("verification reference path",
                          verification_reference.get("reference_path")),
                         ("production reference record path",
                          produced.get("record_path"))):
        if value is None:
            continue
        located = _readable_path(str(value), label=label, kind="file")
        if reference_dir != located.parent and reference_dir not in located.parents:
            raise DurableBaselineError(
                f"the {label} resolves outside the verification reference "
                f"directory", path=str(located), root=str(reference_dir))
    provenance = dict(reference.get("provenance") or {})
    if provenance.get("fixture") or provenance.get("qualification_only"):
        raise DurableBaselineError(
            "a fixture or qualification-only baseline is never a production "
            "import", provenance=provenance)
    if provenance.get("evidence_origin") != "ORFS" or not provenance.get(
            "openroad_launched"):
        raise DurableBaselineError(
            "the baseline reference does not record a real ORFS run",
            provenance=provenance)

    # ── artifact manifest：以 id 載入原始 bytes 並重算 digest ──────
    from terminux_engine.lineage.artifact_manifest_store import ArtifactManifestStore

    manifest_id = evidence.get("artifact_manifest_id")
    if not manifest_id:
        raise DurableBaselineError("the source run evidence has no manifest id")
    store = ArtifactManifestStore(str(artifact_root))
    try:
        recomputed = store.sha256_of(str(manifest_id))
    except (OSError, ValueError) as exc:
        raise DurableBaselineError("the source artifact manifest cannot be read",
                                   manifest_id=str(manifest_id),
                                   error=str(exc)) from None
    if recomputed != evidence.get("artifact_manifest_sha256"):
        raise DurableBaselineError(
            "the artifact manifest bytes no longer hash to the recorded digest",
            manifest_id=str(manifest_id), recomputed=recomputed)

    source_session = scope.get("origin_session_id")
    if not source_session:
        raise DurableBaselineError(
            "the source scope records no originating session")

    body = {
        "schema_version": "flow_task.baseline_import.v1",
        "source_task_id": source.source_task_id,
        "source_task_digest": source.source_task_digest,
        "source_profile_id": source.source_profile_id,
        "source_profile_digest": source.source_profile_digest,
        "source_scope": scope,
        "input_manifest_sha256": task.get("baseline_input_manifest_sha256"),
        "input_tree_hash": task.get("baseline_input_tree_hash"),
        "run": run,
        "baseline_functional_reference": reference_policy_state,
        "output_root": str(full.get("output_root") or ""),
        "variant": f"terminux_{source.source_task_id[-8:]}_{F.SLOT_BASELINE}",
        "verification_reference": verification_reference,
        "result_event_id": result_event_id,
        "result_sha256": envelope.get("result_sha256"),
    }
    # 來源身分：跨 target session 穩定。
    source_import_digest = _sha256_payload(body)
    binding = {
        "schema_version": "flow_task.durable_baseline_binding.v1",
        "source_import_digest": source_import_digest,
        "source_origin_session_id": source_session,
        "target_origin_session_id": target_origin_session_id,
        "member_id": member_id, "project_id": project_id,
        "workspace": target_workspace, "project_source": target_project_source,
        "work_id": work_id,
    }
    # `baseline_import` **只含 canonical body**，形狀與 live export 完全相同。
    # 這一點是刻意的：既有 Work 裡存著的 `execution_import` 是由舊的 live
    # export 寫下的，重用時要能逐位元組比對，所以重建不得在文件裡多塞欄位。
    # durable 專屬的身分以 sibling metadata 回傳，不污染被比對的文件。
    return {
        "baseline_import": {**body, "import_digest": source_import_digest},
        "durable_binding": {**binding,
                            "target_binding_digest": _sha256_payload(binding)},
    }


__all__ = [
    "ENGINEERING_IDENTITY_FIELDS", "KNOWN_SCOPE_FIELDS", "PROFILE_FIELD",
    "REQUIRED_FIELDS",
    "SCOPE_MAY_DIFFER", "SCOPE_MUST_MATCH", "DurableBaselineError",
    "DurableBaselineSource", "reconstruct_import",
    "reconstruct_work_baseline_reference",
]
