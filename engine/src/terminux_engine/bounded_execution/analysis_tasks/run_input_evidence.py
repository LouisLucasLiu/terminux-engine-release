# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""正式的 scoped run input evidence reader。

**為什麼需要它**：flow task 的比較永遠停在 `conditionally_comparable`，因為
`build_run_identity()` 只從 execution receipt 取 RTL／design config／SDC／
scripts 身分，而 receipt 進 content hash 的 schema 不帶這些欄位。

**本檔的做法**：不動共用 receipt 的 schema、序列化、content hash 或既有 bytes。
改為由 host 端用**已經持久化的引用**把輸入身分重建出來，再交給 Package。

驗證順序固定，任一層不成立即 `RunInputEvidenceError`，
**不**降級成「取最新一筆」「檔名相符」或「兩邊都空所以相同」：

1. **ledger**：同 scope 取得 run，核對 `metadata.agent_task` 的
   `task_id`／`slot`／`grant_id`／`task_digest`／`profile_digest`／
   `input_manifest_hash`。不靠 DQE 最新一筆、檔名、mtime 或自填字面值。
2. **receipt ＋ task history**：同一 run 必須有 durable receipt；task 歷史裡
   必須有把這個 step／slot 綁到這個 run 的 `flow_task.run_link.v1`，以及該步
   已持久化的 result envelope。歷史授權只供來源查證，**不恢復執行權**。
3. **manifest**：由上述引用綁定的 input manifest 必須讀得到、sha256 相符、
   `tree_hash` 與 result envelope 相符。實際使用的輸入檔案由**執行當下的有效
   設定**（ORFS 自己寫出的 `print-%` 結果）指出，再回 manifest 取其 sha256——
   不依副檔名或 variant 名稱猜測分類。
4. 回傳唯讀 `VerifiedRunInputs`。

Reader 只驗身分與關聯；領域分類（哪個是 RTL、哪個是 SDC）沿用原實際輸入
記錄，不做工程判斷。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks import errors as E
from terminux_engine.bounded_execution.analysis_tasks import flow_models as F

#: container 內固定的 flow 路徑前綴。有效設定記的是 container 視角的絕對
#: 路徑，manifest 記的是相對 snapshot root 的路徑，兩者靠這個前綴對應。
_CONTAINER_FLOW_PREFIX = "/OpenROAD-flow-scripts/"

#: 有效設定裡指出「實際輸入」的變數名稱 → 我們給的角色名。
#: 角色名沿用既有 comparison policy 的維度語彙，不另創一套。
_INPUT_ROLE_VARIABLES: tuple[tuple[str, str], ...] = (
    ("VERILOG_FILES", "rtl"),
    ("SDC_FILE", "sdc"),
    ("DESIGN_CONFIG", "design_config"),
)

MAX_HISTORY_EVENTS = 512


class RunInputEvidenceError(E.TaskError):
    """輸入證據無法由既有持久化紀錄重建。**不**回傳部分結果。"""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(E.TASK_EVIDENCE_INVALID, message, **details)


@dataclass(frozen=True)
class InputFileIdentity:
    """一個實際輸入檔案或檔案集合的身分。`sha256` 來自 manifest，不是現場重算。"""

    role: str
    #: 執行當下有效設定記錄的路徑（container 視角）。若為多檔案集合，為所有宣告路徑以空格連接。
    declared_path: str
    #: 對應到 input manifest 的相對路徑。若為多檔案集合，為所有相對路徑清單（以逗號分隔）。
    manifest_path: str
    #: 單檔案的 sha256，或多檔案集合的 canonical digest（依 manifest_path 排序後的 canonical hash）。
    sha256: str
    #: 單檔案大小，或多檔案集合的總大小。
    size: int
    #: 若為多檔案集合，保存集合內各個檔案的子 InputFileIdentity。
    files: tuple[InputFileIdentity, ...] = ()
    #: 涵蓋的檔案數量。單一檔案為 1。
    file_count: int = 1
    #: 若能從有效設定識別 top module（例如 DESIGN_NAME / TOP_MODULE），記錄在此。
    top_module: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "declared_path": self.declared_path,
            "manifest_path": self.manifest_path,
            "sha256": self.sha256,
            "size": self.size,
        }
        if self.file_count > 1 or self.files:
            result["file_count"] = self.file_count
            result["files"] = [f.to_json_dict() for f in self.files]
        if self.top_module is not None:
            result["top_module"] = self.top_module
        return result


@dataclass(frozen=True)
class VerifiedRunInputs:
    """唯讀。只有四層全部通過才會被建立。"""

    member_id: str
    project_id: str
    run_id: str
    attempt: int
    task_id: str
    slot: str
    grant_id: str
    task_digest: str
    profile_digest: str
    receipt_id: str
    receipt_content_hash: str
    #: 直接取自同一份 durable receipt，供 comparability 的 flow_completion 使用。
    execution_lifecycle_status: str | None
    process_exit_code: int | None
    input_manifest_path: str
    input_manifest_sha256: str
    input_tree_hash: str
    #: snapshot 來源 revision（ORFS scripts 身分），來自 manifest 本身。
    source_revision: str | None
    source_dirty: bool | None
    #: **執行當下**實際使用的 image。由 run-bound 的兩個來源交叉核對得到。
    #: host-native run 沒有 image（`None`），工具身分見 `native_tool_binding_sha256`。
    docker_image_id: str | None
    #: snapshot 準備當時**宣告**的 image。它是 snapshot metadata，不是執行
    #: 事實；與 `docker_image_id` 不同時列入 `unresolved`，不當成矛盾而整筆
    #: 拒絕（兩者本來就是不同的事實）。
    snapshot_declared_image_id: str | None
    design_id: str
    platform_id: str
    #: 實際輸入檔案身分，key 是角色名。
    files: dict[str, InputFileIdentity] = field(default_factory=dict)
    #: 有效設定的來源標記（例如 `make_print`）。
    effective_settings_source: str = "unavailable"
    #: ORFS 腳本的**內容** digest，由已驗證 manifest 裡 `flow/scripts/`
    #: 底下的條目推得。它與 `source_revision`（git commit）是不同欄位：
    #: commit 是來源資訊，這個才是「兩次 run 是不是跑同一份腳本」的答案。
    scripts_digest: str | None = None
    #: 上面那個 digest 實際涵蓋幾個檔案。0 代表涵蓋不足，維持 unknown。
    scripts_file_count: int = 0
    #: 這次未能證實的項目。**空 dict 不代表已證明相同。**
    unresolved: tuple[str, ...] = ()
    #: RC-4：執行當下實際 EDA binary 身分，取自 durable step result。
    tool_binaries: dict[str, Any] = field(default_factory=dict)
    #: RC-4：同一份 durable receipt 綁定的 artifact manifest 身分。
    artifact_manifest_id: str | None = None
    artifact_manifest_sha256: str | None = None
    #: host-native run 的工具身分；同樣由 step result 與 receipt 兩個來源交叉核對。
    native_tool_binding_sha256: str | None = None

    def identity_of(self, role: str) -> str | None:
        entry = self.files.get(role)
        return entry.sha256 if entry is not None else None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id, "project_id": self.project_id,
            "run_id": self.run_id, "attempt": self.attempt,
            "task_id": self.task_id, "slot": self.slot,
            "grant_id": self.grant_id, "task_digest": self.task_digest,
            "profile_digest": self.profile_digest,
            "receipt_id": self.receipt_id,
            "receipt_content_hash": self.receipt_content_hash,
            "execution_lifecycle_status": self.execution_lifecycle_status,
            "process_exit_code": self.process_exit_code,
            "input_manifest_path": self.input_manifest_path,
            "input_manifest_sha256": self.input_manifest_sha256,
            "input_tree_hash": self.input_tree_hash,
            "source_revision": self.source_revision,
            "scripts_digest": self.scripts_digest,
            "scripts_file_count": self.scripts_file_count,
            "source_dirty": self.source_dirty,
            "docker_image_id": self.docker_image_id,
            "snapshot_declared_image_id": self.snapshot_declared_image_id,
            "design_id": self.design_id, "platform_id": self.platform_id,
            "files": {k: v.to_json_dict() for k, v in sorted(self.files.items())},
            "effective_settings_source": self.effective_settings_source,
            "unresolved": list(self.unresolved),
            "tool_binaries": dict(self.tool_binaries),
            "artifact_manifest_id": self.artifact_manifest_id,
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            **({"native_tool_binding_sha256": self.native_tool_binding_sha256}
               if self.native_tool_binding_sha256 else {}),
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


SCRIPTS_PREFIX = "flow/scripts/"


def scripts_content_identity(entries: Any) -> tuple[str | None, int]:
    """由已驗證的 manifest 條目推出腳本內容 digest 與涵蓋檔數。

    只取 `flow/scripts/` 底下的檔案，依路徑排序後把 (path, sha256) 逐項
    餵進 SHA-256。因此它只回答「腳本內容是否相同」，不會被 design config
    或 RTL 的差異帶著跑。

    一個檔案都沒涵蓋到就回 `(None, 0)`——那代表我們並不知道兩次 run 是不是
    同一份腳本，必須維持 unknown，不得當成已建立。
    """
    import hashlib

    rows: list[tuple[str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("kind") != "file":
            continue
        path = entry.get("path")
        digest = entry.get("sha256")
        if (not isinstance(path, str) or not path.startswith(SCRIPTS_PREFIX)
                or not isinstance(digest, str) or not digest):
            continue
        rows.append((path, digest))
    if not rows:
        return None, 0
    accumulator = hashlib.sha256()
    for path, digest in sorted(rows):
        accumulator.update(path.encode("utf-8"))
        accumulator.update(b"\0")
        accumulator.update(digest.encode("utf-8"))
        accumulator.update(b"\0")
    return accumulator.hexdigest(), len(rows)


class RunInputEvidenceReader:
    """由既有 ledger／task 事件／manifest 重建一次 run 的輸入身分。

    只讀。不寫任何 recovery event、不建第二份 receipt、不建平行 ledger 或
    新投影資料庫。
    """

    def __init__(
        self, *, ledger: Any, event_store: Any, receipt_reader: Any,
        baseline_manifest_provider: Any = None,
        settings_reader: Any = None,
    ) -> None:
        """`baseline_manifest_provider(task_id, manifest_sha256) -> str | None`
        回傳 baseline slot 的 manifest 絕對路徑；它來自 server profile 的
        `input_manifest_path`，並且必須與 ledger 綁定的 hash 相符。
        candidate slot 的 manifest 路徑則來自 `flow_task.input_reference.v1`，
        不需要這個 provider。
        """
        if ledger is None or event_store is None:
            raise ValueError("ledger and event_store are required")
        self._ledger = ledger
        self._event_store = event_store
        self._receipt_reader = receipt_reader
        self._baseline_manifest_provider = baseline_manifest_provider
        self._settings_reader = settings_reader

    # ── public ───────────────────────────────────────────────────────

    def read_for_scope(
        self, context: Any, run_id: str, attempt_id: int | None = None,
    ) -> VerifiedRunInputs:
        member_id = getattr(context, "member_id", None)
        project_id = getattr(context, "project_id", None)
        if not member_id or not project_id:
            raise RunInputEvidenceError("the request context carries no canonical scope")

        agent_task, ledger_scope = self._ledger_layer(run_id, member_id, project_id)
        receipt = self._receipt_layer(run_id)
        history = self._history_layer(
            agent_task["task_id"], member_id, project_id, run_id,
            slot=agent_task["slot"])
        return self._manifest_layer(
            member_id=member_id, project_id=project_id, run_id=run_id,
            attempt=int(attempt_id if attempt_id is not None
                        else receipt.get("attempt") or 1),
            agent_task=agent_task, receipt=receipt, history=history,
            ledger_scope=ledger_scope)

    # ── layer 1：ledger ──────────────────────────────────────────────

    def _ledger_layer(
        self, run_id: str, member_id: str, project_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self._ledger.get_run(run_id)
        if record is None:
            raise RunInputEvidenceError("run not found", run_id=run_id)
        metadata = getattr(record, "metadata", None) or {}
        scope = (metadata.get("orchestration") or {}).get("scope") or {}
        # scope 必須是 canonical 的那一組；不接受 client 自報。
        if scope.get("project_id") != project_id:
            raise RunInputEvidenceError(
                "the run does not belong to this project scope", run_id=run_id)
        owner = getattr(record, "owner", None)
        if owner is not None and owner != member_id:
            raise RunInputEvidenceError(
                "the run does not belong to this member scope", run_id=run_id)
        agent_task = metadata.get("agent_task")
        if not isinstance(agent_task, dict):
            raise RunInputEvidenceError(
                "the run carries no agent_task binding; it was not started by a "
                "flow task", run_id=run_id)
        for key in ("task_id", "slot", "grant_id", "task_digest",
                    "profile_digest", "input_manifest_hash"):
            if not agent_task.get(key):
                raise RunInputEvidenceError(
                    f"the agent_task binding is missing {key!r}", run_id=run_id)
        return dict(agent_task), dict(scope)

    # ── layer 2a：durable receipt ────────────────────────────────────

    def _receipt_layer(self, run_id: str) -> dict[str, Any]:
        if self._receipt_reader is None:
            raise RunInputEvidenceError(
                "no execution receipt reader is available", run_id=run_id)
        try:
            receipts = list(self._receipt_reader(run_id))
        except Exception as exc:  # noqa: BLE001
            raise RunInputEvidenceError(
                f"the execution receipts could not be read: {exc!r}",
                run_id=run_id) from exc
        if not receipts:
            raise RunInputEvidenceError(
                "the run has no durable execution receipt", run_id=run_id)
        latest = receipts[-1]
        if not latest.get("receipt_id") or not latest.get("content_hash"):
            raise RunInputEvidenceError(
                "the execution receipt carries no identity", run_id=run_id)
        return dict(latest)

    # ── layer 2b：task history ───────────────────────────────────────

    def _history_layer(
        self, task_id: str, member_id: str, project_id: str, run_id: str,
        *, slot: str,
    ) -> dict[str, Any]:
        events, truncated = self._event_store.query_task_events(
            task_id, user_id=member_id, project_id=project_id,
            event_types=F.TASK_EVENT_TYPES, limit=MAX_HISTORY_EVENTS)
        if truncated:
            raise RunInputEvidenceError(
                "the task history is truncated; evidence is incomplete",
                task_id=task_id)
        if not events:
            raise RunInputEvidenceError("no task history in this scope",
                                        task_id=task_id)

        run_link = None
        input_reference = None
        result_full: dict[str, Any] | None = None
        for event in events:
            payload = event.payload or {}
            if event.event_type == F.EVENT_RUN_LINK:
                run = payload.get("run") or {}
                if run.get("run_id") == run_id:
                    if run.get("slot") != slot:
                        raise RunInputEvidenceError(
                            "the persisted run link disagrees with the ledger slot",
                            run_id=run_id)
                    run_link = dict(payload)
            elif event.event_type == F.EVENT_INPUT_REFERENCE:
                if payload.get("slot") == slot:
                    input_reference = dict(payload.get("input_reference") or {})
            elif event.event_type == F.EVENT_STEP_RESULT:
                envelope = payload.get("envelope") or {}
                full = envelope.get("full_result")
                if not isinstance(full, dict):
                    continue
                evidence = full.get("completed_run_evidence") or {}
                if evidence.get("run_id") == run_id:
                    result_full = dict(full)

        if run_link is None:
            raise RunInputEvidenceError(
                "no persisted run link binds this run to the task", run_id=run_id)
        if result_full is None:
            raise RunInputEvidenceError(
                "no persisted step result references this run", run_id=run_id)
        tool_key = ("native_tool_binding_sha256"
                    if result_full.get("native_tool_binding_sha256") else "docker_image_id")
        for key in ("input_manifest_sha256", "input_tree_hash", "output_root", tool_key):
            if not result_full.get(key):
                raise RunInputEvidenceError(
                    f"the persisted step result is missing {key!r}", run_id=run_id)
        return {"run_link": run_link, "input_reference": input_reference,
                "result": result_full}

    # ── layer 3：manifest ＋ 實際輸入 ────────────────────────────────

    def _manifest_layer(
        self, *, member_id: str, project_id: str, run_id: str, attempt: int,
        agent_task: dict[str, Any], receipt: dict[str, Any],
        history: dict[str, Any], ledger_scope: dict[str, Any],
    ) -> VerifiedRunInputs:
        result = history["result"]
        declared_hash = str(agent_task["input_manifest_hash"])
        if result["input_manifest_sha256"] != declared_hash:
            # 兩個來源都存在但矛盾 → 拒絕取信，不挑方便的那個。
            raise RunInputEvidenceError(
                "the ledger binding and the persisted step result disagree on the "
                "input manifest hash", run_id=run_id)

        manifest_path = self._resolve_manifest_path(
            agent_task, history["input_reference"], declared_hash)
        path = Path(manifest_path)
        if not path.is_file():
            raise RunInputEvidenceError(
                f"the bound input manifest is not readable: {manifest_path}",
                run_id=run_id)
        actual_hash = _sha256_file(path)
        if actual_hash != declared_hash:
            raise RunInputEvidenceError(
                "the input manifest content does not match the bound hash",
                run_id=run_id)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunInputEvidenceError(
                f"the input manifest is unreadable: {exc}", run_id=run_id) from exc
        if manifest.get("tree_hash") != result["input_tree_hash"]:
            raise RunInputEvidenceError(
                "the manifest tree hash does not match the persisted step result",
                run_id=run_id)

        entries = {e.get("path"): e for e in manifest.get("entries") or []
                   if isinstance(e, dict)}
        if not entries:
            raise RunInputEvidenceError("the input manifest has no entries",
                                        run_id=run_id)

        settings, settings_source = self._effective_settings(result["output_root"])
        files: dict[str, InputFileIdentity] = {}
        unresolved: list[str] = []
        for variable, role in _INPUT_ROLE_VARIABLES:
            declared = settings.get(variable)
            if not declared:
                unresolved.append(f"{role}: the run recorded no {variable} value")
                continue
            parts = [p for p in str(declared).split() if p]
            if not parts:
                unresolved.append(f"{role}: the run recorded empty {variable} value")
                continue

            top_module = (settings.get("DESIGN_NAME") or settings.get("TOP_MODULE")
                          if role == "rtl" else None)

            if len(parts) == 1:
                relpath = self._to_manifest_path(parts[0])
                entry = entries.get(relpath)
                if entry is None or not entry.get("sha256"):
                    raise RunInputEvidenceError(
                        f"{role}: declared input file {parts[0]!r} (manifest path {relpath!r}) "
                        "is not covered by the input manifest",
                        run_id=run_id, role=role, path=parts[0], manifest_path=relpath)
                self._verify_file_content_if_present(
                    path.parent, relpath, str(entry["sha256"]), role=role, run_id=run_id)
                files[role] = InputFileIdentity(
                    role=role, declared_path=parts[0], manifest_path=relpath,
                    sha256=str(entry["sha256"]), size=int(entry.get("size") or 0),
                    files=(), file_count=1,
                    top_module=str(top_module) if top_module else None)
            else:
                # ── 多 RTL / 多檔案集合身分重建 ──
                sub_identities: list[InputFileIdentity] = []
                for idx, p in enumerate(parts):
                    relpath = self._to_manifest_path(p)
                    entry = entries.get(relpath)
                    if entry is None or not entry.get("sha256"):
                        raise RunInputEvidenceError(
                            f"{role}: declared input file {p!r} (manifest path {relpath!r}) "
                            "is not covered by the input manifest",
                            run_id=run_id, role=role, path=p, manifest_path=relpath)
                    self._verify_file_content_if_present(
                        path.parent, relpath, str(entry["sha256"]), role=role, run_id=run_id)
                    sub_identities.append(InputFileIdentity(
                        role=f"{role}[{idx}]", declared_path=p, manifest_path=relpath,
                        sha256=str(entry["sha256"]), size=int(entry.get("size") or 0)))

                sorted_subs = sorted(sub_identities, key=lambda f: f.manifest_path)

                # 集合完整性檢查：
                if len({s.manifest_path for s in sorted_subs}) != len(sorted_subs):
                    raise RunInputEvidenceError(
                        f"{role}: duplicate files detected in declared file set",
                        run_id=run_id, role=role)

                if top_module and role == "rtl":
                    top_filename = f"{top_module}.v"
                    if not any(Path(s.manifest_path).name == top_filename for s in sorted_subs):
                        raise RunInputEvidenceError(
                            f"{role}: collection integrity check failed: top module {top_module!r} "
                            f"has no corresponding {top_filename!r} in declared files",
                            run_id=run_id, role=role, top_module=top_module)

                acc = hashlib.sha256()
                total_size = 0
                for sub in sorted_subs:
                    acc.update(sub.manifest_path.encode("utf-8"))
                    acc.update(b"\0")
                    acc.update(sub.sha256.encode("utf-8"))
                    acc.update(b"\0")
                    total_size += sub.size
                collection_digest = acc.hexdigest()

                files[role] = InputFileIdentity(
                    role=role,
                    declared_path=" ".join(parts),
                    manifest_path=", ".join(s.manifest_path for s in sorted_subs),
                    sha256=collection_digest,
                    size=total_size,
                    files=tuple(sorted_subs),
                    file_count=len(sorted_subs),
                    top_module=str(top_module) if top_module else None,
                )

        # 檢查可選的 constraints（如 FLOORPLAN_DEF）
        fp_declared = settings.get("FLOORPLAN_DEF")
        if fp_declared:
            fp_parts = [p for p in str(fp_declared).split() if p]
            if fp_parts:
                fp_rel = self._to_manifest_path(fp_parts[0])
                fp_entry = entries.get(fp_rel)
                if fp_entry and fp_entry.get("sha256"):
                    self._verify_file_content_if_present(
                        path.parent, fp_rel, str(fp_entry["sha256"]),
                        role="floorplan_def", run_id=run_id)
                    files["floorplan_def"] = InputFileIdentity(
                        role="floorplan_def", declared_path=fp_parts[0],
                        manifest_path=fp_rel, sha256=str(fp_entry["sha256"]),
                        size=int(fp_entry.get("size") or 0))

        # 執行用的 image 身分只由 **run-bound** 的兩個來源決定：host 綁進
        # binding 的 profile image（step result）與 adapter 在執行當下查到並寫
        # 進 receipt 的 image。兩者必須一致；不一致就是真的矛盾，拒絕取信。
        native_binding = result.get("native_tool_binding_sha256")
        image: str | None
        if native_binding:
            # host-native：沒有 image；binding digest 必須在兩個 run-bound 來源一致。
            receipt_binding = ((receipt.get("tool_version_evidence") or {})
                               .get("native_tool_binding_sha256"))
            if not receipt_binding:
                raise RunInputEvidenceError(
                    "the execution receipt records no native tool binding", run_id=run_id)
            if str(receipt_binding) != str(native_binding):
                raise RunInputEvidenceError(
                    "the persisted step result and the execution receipt disagree on "
                    "the native tool binding", run_id=run_id)
            image = None
        else:
            image = str(result["docker_image_id"])
            receipt_image = ((receipt.get("tool_version_evidence") or {})
                             .get("docker_image_id"))
            if not receipt_image:
                raise RunInputEvidenceError(
                    "the execution receipt records no tool image identity", run_id=run_id)
            if str(receipt_image) != image:
                raise RunInputEvidenceError(
                    "the persisted step result and the execution receipt disagree on "
                    "the tool image identity", run_id=run_id)
        # manifest 的 `docker_image_id` 是 **snapshot 準備當時的宣告**，不是
        # 執行事實（實際執行中曾遇到它停留在較早設定的 image）。不當成同一件事
        # 比對，但也不丟掉——不一致就明列出來。
        manifest_image = manifest.get("docker_image_id")
        if manifest_image and image is None:
            unresolved.append(
                "tool_image_provenance: the input manifest declares "
                f"{manifest_image!r} at snapshot time, but the run executed host-native "
                "tools; the image declaration does not describe this run")
        if manifest_image and image is not None and str(manifest_image) != image:
            unresolved.append(
                "tool_image_provenance: the input manifest declares "
                f"{manifest_image!r} at snapshot time, but the run executed with "
                f"{image!r}; the run-bound sources agree and are used")

        tool_binaries = result.get("tool_binaries")
        if not isinstance(tool_binaries, dict) or tool_binaries.get(
                "source") in (None, "", "unavailable"):
            tool_binaries = {}
            unresolved.append(
                "tool_binaries: the run recorded no executed EDA binary identity")
        elif tool_binaries.get("incomplete"):
            unresolved.append(
                "tool_binaries: incomplete identity for "
                f"{sorted(tool_binaries.get('incomplete') or [])}")

        return VerifiedRunInputs(
            member_id=member_id, project_id=project_id, run_id=run_id,
            attempt=attempt, task_id=str(agent_task["task_id"]),
            slot=str(agent_task["slot"]), grant_id=str(agent_task["grant_id"]),
            task_digest=str(agent_task["task_digest"]),
            profile_digest=str(agent_task["profile_digest"]),
            receipt_id=str(receipt["receipt_id"]),
            receipt_content_hash=str(receipt["content_hash"]),
            execution_lifecycle_status=receipt.get("execution_lifecycle_status"),
            process_exit_code=receipt.get("process_exit_code"),
            input_manifest_path=str(path), input_manifest_sha256=declared_hash,
            input_tree_hash=str(result["input_tree_hash"]),
            source_revision=manifest.get("source_revision"),
            source_dirty=manifest.get("source_dirty"),
            **dict(zip(("scripts_digest", "scripts_file_count"),
                       scripts_content_identity(manifest.get("entries")))),
            docker_image_id=image,
            snapshot_declared_image_id=(str(manifest_image) if manifest_image
                                        else None),
            design_id=str(manifest.get("design_id") or ""),
            platform_id=str(manifest.get("platform_id") or ""),
            files=files, effective_settings_source=settings_source,
            unresolved=tuple(unresolved), tool_binaries=dict(tool_binaries),
            artifact_manifest_id=receipt.get("artifact_manifest_id"),
            artifact_manifest_sha256=receipt.get("artifact_manifest_sha256"),
            native_tool_binding_sha256=(str(native_binding) if native_binding else None))

    def _resolve_manifest_path(
        self, agent_task: dict[str, Any], input_reference: dict[str, Any] | None,
        declared_hash: str,
    ) -> str:
        """manifest 路徑只能來自**已持久化的引用**。"""
        if agent_task["slot"] != F.SLOT_BASELINE:
            if not input_reference:
                raise RunInputEvidenceError(
                    "no persisted input reference for this candidate slot",
                    slot=agent_task["slot"])
            if input_reference.get("manifest_sha256") != declared_hash:
                raise RunInputEvidenceError(
                    "the persisted input reference disagrees with the ledger "
                    "binding on the manifest hash", slot=agent_task["slot"])
            path = input_reference.get("manifest_path")
            if not path:
                raise RunInputEvidenceError(
                    "the persisted input reference has no manifest path",
                    slot=agent_task["slot"])
            return str(path)
        if self._baseline_manifest_provider is None:
            raise RunInputEvidenceError(
                "no baseline input manifest reference is available")
        path = self._baseline_manifest_provider(
            agent_task["task_id"], declared_hash)
        if not path:
            raise RunInputEvidenceError(
                "the baseline input manifest reference does not match the ledger "
                "binding")
        return str(path)

    def _effective_settings(self, output_root: str) -> tuple[dict[str, str], str]:
        """實際輸入由**執行當下**寫下的有效設定指出，不由我們猜。"""
        if self._settings_reader is None:
            from terminux_engine.packages.orfs_run_intelligence import (
                flow_evidence as FE,
            )
            reader = FE.read_effective_settings
        else:
            reader = self._settings_reader
        try:
            settings = reader(output_root, platform="", design="", variant="",
                              required=())
        except Exception as exc:  # noqa: BLE001
            raise RunInputEvidenceError(
                f"the effective settings for this run are unreadable: {exc!r}"
            ) from exc
        source = getattr(settings, "source", "unavailable")
        if source in (None, "", "unavailable"):
            raise RunInputEvidenceError(
                "the run recorded no identifiable effective settings source")
        values = dict(getattr(settings, "values", {}) or {})
        return values, str(source)

    @staticmethod
    def _to_manifest_path(declared: str) -> str:
        text = str(declared)
        if text.startswith(_CONTAINER_FLOW_PREFIX):
            return text[len(_CONTAINER_FLOW_PREFIX):]
        # 相對路徑是相對 `flow/` 目錄（ORFS 的 make 在那裡執行）。
        return text if text.startswith("flow/") else f"flow/{text.lstrip('./')}"

    @staticmethod
    def _verify_file_content_if_present(
        manifest_dir: Path, relpath: str, expected_sha256: str, *, role: str, run_id: str,
    ) -> None:
        """若實體檔案存在於磁碟（例如 snapshot 目錄），核對內容 digest；不符則明確拒絕。"""
        candidates = [
            manifest_dir / relpath,
            manifest_dir / "baseline_source" / relpath,
        ]
        for candidate in candidates:
            if candidate.is_file():
                actual_hash = _sha256_file(candidate)
                if actual_hash != expected_sha256:
                    raise RunInputEvidenceError(
                        f"{role}: content hash mismatch for {relpath} "
                        f"(disk {actual_hash} != manifest {expected_sha256})",
                        run_id=run_id, role=role, path=str(candidate),
                        actual_sha256=actual_hash, expected_sha256=expected_sha256)
                break


__all__ = [
    "InputFileIdentity",
    "RunInputEvidenceError",
    "RunInputEvidenceReader",
    "VerifiedRunInputs",
]
