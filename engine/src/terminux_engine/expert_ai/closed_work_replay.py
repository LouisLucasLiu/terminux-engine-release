# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""已關閉 EAP Work 的唯讀重播。

在**私有副本**上開啟 EventStore／orchestration ledger，因此不會對原始證據新增事件、
改寫 snapshot、執行 schema migration 或碰觸 WAL。只重建與驗證，不回寫任何結果。

判定：

- `VERIFIED`：Work 終態、Global 結果決策與 Expert digest 鏈、task result hash、
  repair evidence hash 與重算 verdict 全部一致。
- `UNVERIFIABLE`：任一必要欄位缺失、digest 或 hash 不符、重算與保存結果不同。
- `WORK_SCHEMA_UNSUPPORTED`：Work snapshot 不是本版支援的 schema，或任一 attempt
  的 method extension identity／version 不在 Package registry。

每個 attempt 各自由其 task snapshot（digest 已驗）的
`eap_proposal.method_extension` 經 Package method extension registry 選 decoder；
不從 Work objective、parameter、design、檔名或 candidate 值推論 method，也不把
某一 attempt 的 extension 或 evidence 套用到另一 attempt。無 pin 的歷史
setup-margin task 與 FlowController 共用同一條受限 legacy 規則。

setup-margin 比較事實同時支援兩種已發生的格式：解說失敗時 action 把
`metric_comparisons` 提到頂層；解說成功時它位於 `insight` 內。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

VERIFIED = "VERIFIED"
UNVERIFIABLE = "UNVERIFIABLE"
WORK_SCHEMA_UNSUPPORTED = "WORK_SCHEMA_UNSUPPORTED"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_facts_from_result(full_result: dict[str, Any]) -> dict[str, Any]:
    """compare step 的 deterministic 比較事實；兩種已知格式都接受。"""
    if isinstance(full_result.get("metric_comparisons"), list):
        return full_result
    insight = full_result.get("insight")
    if isinstance(insight, dict) and isinstance(insight.get("metric_comparisons"), list):
        return insight
    return {}


def _copy_store(source: Path, target_dir: Path) -> Path:
    target = target_dir / source.name
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, target_dir / sidecar.name)
    return target


def replay_closed_work(*, terminux_home: str | Path, member_id: str, project_id: str,
                       work_id: str) -> dict[str, Any]:
    home = Path(terminux_home)
    event_store_path = home / "ecc" / "event_store.db"
    with tempfile.TemporaryDirectory(prefix="terminux-closed-work-") as private:
        store_copy = _copy_store(event_store_path, Path(private))
        from terminux_engine.store.event_store import EventStore

        store = EventStore(str(store_copy))
        try:
            return _replay(store, member_id=member_id, project_id=project_id,
                           work_id=work_id)
        finally:
            store.close()


def _replay(store: Any, *, member_id: str, project_id: str, work_id: str) -> dict[str, Any]:
    from terminux_engine.bounded_execution.analysis_tasks import flow_models as F
    from terminux_engine.bounded_execution.analysis_tasks.reader import TaskHistoryReader
    from terminux_engine.expert_ai.eap3 import SCHEMA_VERSION, WorkReader, digest_json

    reasons: list[str] = []
    scope = SimpleNamespace(member_id=member_id, project_id=project_id,
                            project_declared=True)
    work = WorkReader(store).load(work_id, scope)
    if work is None:
        return {"status": UNVERIFIABLE, "reasons": ["work not found in scope"]}
    if work.get("schema_version") != SCHEMA_VERSION:
        return {"status": WORK_SCHEMA_UNSUPPORTED, "work_id": work_id,
                "stored_schema_version": work.get("schema_version"),
                "reasons": ["unsupported work schema"]}
    for field in ("status", "decisions", "attempts", "role_calls", "budget", "authorization"):
        if field not in work:
            reasons.append(f"work field missing: {field}")
    attempts = work.get("attempts") or []
    decisions = [row for row in work.get("decisions") or []
                 if row.get("kind") in {"STOP", "REQUEST_REVISION", "COLLECT_EVIDENCE",
                                        "WAIT_USER"}]
    chains = []
    for index, attempt in enumerate(attempts):
        analyses = attempt.get("expert_analyses") or []
        digests = attempt.get("expert_analysis_digests") or []
        recomputed = [digest_json(row.get("response")) for row in analyses]
        stored = [row.get("response_digest") for row in analyses]
        decision = decisions[index] if index < len(decisions) else {}
        chain = {"attempt_id": attempt.get("attempt_id"), "expert_digests": digests,
                 "recomputed_expert_digests": recomputed,
                 "decision_id": decision.get("decision_id"), "decision_kind": decision.get("kind"),
                 "decision_based_on": decision.get("based_on_expert_response_digests")}
        chain["verified"] = bool(digests) and recomputed == stored == digests \
            and decision.get("based_on_expert_response_digests") == digests
        if not chain["verified"]:
            reasons.append(f"expert digest chain mismatch for {attempt.get('attempt_id')}")
        chains.append(chain)
    if not attempts:
        reasons.append("the work has no engineering attempt")

    usage = {"by_class": {}, "completed": 0, "failed": 0}
    for call in work.get("role_calls") or []:
        name = call.get("dispatch_class") or "mainline"
        usage["by_class"][name] = usage["by_class"].get(name, 0) + 1
        if call.get("status") == "COMPLETED":
            usage["completed"] += 1
            if not call.get("response_digest"):
                reasons.append(f"completed role call lacks response digest: {call.get('request_id')}")
        else:
            usage["failed"] += 1
    used = (work.get("budget") or {}).get("used") or {}
    if used.get("provider_dispatches") != len(work.get("role_calls") or []):
        reasons.append("provider dispatch counter does not match role calls")
    if used.get("engineering_attempts") != len(attempts):
        reasons.append("attempt counter does not match attempts")

    histories = TaskHistoryReader(store, vocabulary=F)
    loaded: dict[str, dict[str, Any]] = {}

    def load_task(task_id: Any) -> dict[str, Any]:
        """每個 task 只讀一次；結果只屬於該 task。"""
        if not isinstance(task_id, str) or not task_id:
            return {"history": None, "reasons": ["attempt has no task id"]}
        if task_id not in loaded:
            loaded[task_id] = _load_task(histories, F, task_id,
                                         member_id=member_id, project_id=project_id)
        return loaded[task_id]

    baseline_ref = ((work.get("baseline") or {}).get("reference") or {}).get("execution_import") or {}
    if isinstance(baseline_ref.get("source_task_id"), str):
        reasons.extend(load_task(baseline_ref["source_task_id"])["reasons"])
    attempt_views: list[dict[str, Any]] = []
    for attempt in attempts:
        view = _replay_attempt(attempt, load_task, F)
        attempt_views.append(view)
        reasons.extend(f"{view['attempt_id']}: {reason}" for reason in view["reasons"])
    tasks = {task_id: row["summary"] for task_id, row in loaded.items()
             if row.get("summary") is not None}
    # 相容既有 installed 檢查：`engineering` 是最後一個 attempt 的判讀。
    engineering: dict[str, Any] = (attempt_views[-1]["engineering"] if attempt_views
                                   else {"status": None})

    final = decisions[-1] if decisions else {}
    if any(view["status"] == WORK_SCHEMA_UNSUPPORTED for view in attempt_views):
        status = WORK_SCHEMA_UNSUPPORTED
    else:
        status = VERIFIED if not reasons else UNVERIFIABLE
    return {
        "status": status, "reasons": reasons, "work_id": work_id,
        "work_status": work.get("status"), "stop_reason": work.get("stop_reason"),
        "authorization_digest": (work.get("authorization") or {}).get("authorization_digest"),
        "final_decision": {key: final.get(key) for key in (
            "decision_id", "kind", "reason_code", "criteria_met",
            "based_on_expert_response_digests")},
        "chains": chains, "usage": usage, "budget": work.get("budget"),
        "tasks": tasks, "engineering": engineering, "attempts": attempt_views,
    }


def _load_task(histories: Any, F: Any, task_id: str, *, member_id: str,
               project_id: str) -> dict[str, Any]:
    """讀回並驗證單一 task 歷史（snapshot digest、sequence、scope、result hash）。"""
    reasons: list[str] = []
    try:
        history = histories.load(task_id, member_id=member_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - 歷史不一致即不可驗證
        return {"history": None, "summary": None,
                "reasons": [f"task history not verifiable for {task_id}: {exc}"]}
    if history is None or history.truncated:
        return {"history": None, "summary": None,
                "reasons": [f"task history unavailable or truncated: {task_id}"]}
    grant_id = (history.grant or {}).get("grant_id")
    started_id = (history.started or {}).get("grant_id")
    if not grant_id or grant_id != started_id:
        reasons.append(f"grant/started linkage mismatch for {task_id}")
    run_links: dict[str, list[str]] = {}
    for row in history.informational.get(F.EVENT_RUN_LINK, []):
        run = row.get("run") or {}
        run_links.setdefault(str(run.get("slot")), []).append(str(run.get("run_id")))
    results = {step_id: step.result_envelope for step_id, step in history.steps.items()
               if step.result_envelope is not None}
    summary = {"grants": [grant_id] if grant_id else [],
               "started": [started_id] if started_id else [],
               "runs": [run for runs in run_links.values() for run in runs],
               "results": {step_id: {"result_sha256": envelope.result_sha256}
                           for step_id, envelope in results.items()}}
    return {"history": history, "task": history.task, "run_links": run_links,
            "results": results, "repair": history.repair, "summary": summary,
            "result_event_ids": {step_id: step.result_event_id
                                 for step_id, step in history.steps.items()},
            "reasons": reasons}


def _completed_run(loaded: dict[str, Any], F: Any, *, step_id: str, slot: str,
                   run_id: Any, reasons: list[str]) -> None:
    """run link、run step result 與 receipt／manifest／DQE 引用必須指向同一 run。"""
    task_id = getattr(loaded.get("task"), "task_id", None)
    if not isinstance(run_id, str) or run_id not in loaded["run_links"].get(slot, []):
        reasons.append(f"{slot} run {run_id!r} is not linked by task {task_id}")
        return
    envelope = loaded["results"].get(step_id)
    full = envelope.full_result if envelope is not None else None
    evidence = full.get("completed_run_evidence") if isinstance(full, dict) else None
    if not isinstance(evidence, dict) or evidence.get("run_id") != run_id \
            or evidence.get("slot") != slot:
        reasons.append(f"{slot} run result does not reference run {run_id!r}")
        return
    if (evidence.get("state") != "ready" or not evidence.get("receipt_id")
            or not evidence.get("receipt_content_hash")
            or not evidence.get("artifact_manifest_id")
            or int(evidence.get("dqe_row_count") or 0) <= 0):
        reasons.append(f"{slot} run {run_id!r} lacks receipt/manifest/DQE lineage")


def _closed_host_run_facts(loaded: dict[str, Any], owners: dict[str, str],
                           runs: dict[str, Any], F: Any) -> dict[str, dict[str, Any]]:
    """由已 hash 驗證的 run step result 重建各 slot 的執行身分。"""
    from terminux_engine.bounded_execution.analysis_tasks import repair_evidence as RE

    out: dict[str, dict[str, Any]] = {}
    for slot, step_id in (("baseline", F.STEP_RUN_BASELINE),
                          ("candidate", F.STEP_RUN_CANDIDATE)):
        envelope = loaded["results"].get(step_id)
        full = envelope.full_result if envelope is not None else None
        evidence = full.get("completed_run_evidence") if isinstance(full, dict) else None
        if not isinstance(evidence, dict) or evidence.get("run_id") != runs.get(slot):
            continue
        out[slot] = RE.host_run_facts(
            evidence, task_id=owners[slot], slot=slot,
            result_event_id=loaded["result_event_ids"].get(step_id))
    return out


def _native_setup_margin_replay_context(task: Any, repair: dict[str, Any],
                                        ) -> tuple[dict[str, Any] | None, str | None]:
    """host-native setup-margin 的 replay 身分：授權綁定取自 task snapshot。

    proof binary pin 與 helper 雜湊由 task snapshot（進 task digest）決定；保存的
    verdict 必須宣告同一組綁定。判定當時的主機觀測（binary 核對、輸入重新雜湊）
    原樣沿用，replay 不重讀主機現況。
    """
    plan = getattr(task, "execution_plan", None) or {}
    binding = plan.get("native_tool_binding") if isinstance(plan, dict) else None
    pin = ((binding or {}).get("tools") or {}).get("FORMAL_YOSYS") \
        if isinstance(binding, dict) else None
    recipe = getattr(task, "repair_recipe", None) or {}
    helpers = sorted({str(helper.get("sha256")) for helper in recipe.get("helpers") or []
                      if isinstance(helper, dict) and helper.get("sha256")})
    persisted = repair.get("native_proof") if isinstance(repair, dict) else None
    recorded = (persisted or {}).get("binding") if isinstance(persisted, dict) else None
    if (not isinstance(pin, dict) or not isinstance(recorded, dict)
            or recorded.get("tool") != pin or recorded.get("helper_sha256") != helpers
            or not isinstance(recorded.get("qualified_digest"), str)):
        return None, "persisted native setup-margin proof binding differs from the task snapshot"
    return ({"tool": pin, "qualified_digest": recorded["qualified_digest"],
             "helper_sha256": helpers,
             "host_observations": persisted.get("host_observations")}, None)


def _replay_attempt(attempt: dict[str, Any], load_task: Any, F: Any) -> dict[str, Any]:
    """單一 attempt：自己的 task、自己的 extension、自己的 evidence。"""
    from terminux_engine.bounded_execution.analysis_tasks.flow_controller import (
        resolve_task_method_extension,
    )
    from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME

    attempt_id = attempt.get("attempt_id")
    view: dict[str, Any] = {"attempt_id": attempt_id, "task_id": attempt.get("task_id"),
                            "status": UNVERIFIABLE, "reasons": [],
                            "engineering": {"status": None}}
    reasons: list[str] = view["reasons"]
    loaded = load_task(attempt.get("task_id"))
    reasons.extend(loaded["reasons"])
    task = loaded.get("task")
    if task is None:
        return view
    method = ((task.repair_recipe or {}).get("method") or {})
    try:
        dispatch = resolve_task_method_extension(
            task, method_variable=method.get("variable"))
    except ME.MethodExtensionError as exc:
        view["status"] = (WORK_SCHEMA_UNSUPPORTED if exc.code == ME.WORK_SCHEMA_UNSUPPORTED
                          else UNVERIFIABLE)
        view["method_extension"] = {"resolution": "REJECTED", "reason_code": exc.code}
        reasons.append(f"method extension not resolvable: {exc.reason}")
        return view
    if dispatch is None:
        reasons.append("the attempt task is not governed by a method extension")
        return view
    extension = dispatch.extension
    view["method_extension"] = {
        "method_identity": extension.method_identity,
        "extension_version": extension.extension_version,
        "evidence_schema": list(extension.fresh_reader_schemas),
        "resolution": "LEGACY_SETUP_MARGIN" if dispatch.legacy else "PINNED"}
    repair = loaded.get("repair")
    if not isinstance(repair, dict):
        reasons.append("no persisted repair verdict")
        return view
    view["engineering"] = _replay_repair(repair, loaded, load_task, dispatch, F, reasons)
    if not reasons:
        view["status"] = VERIFIED
    return view


def _replay_repair(repair: dict[str, Any], loaded: dict[str, Any], load_task: Any,
                   dispatch: Any, F: Any, reasons: list[str]) -> dict[str, Any]:
    from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME

    task = loaded["task"]
    extension = dispatch.extension
    engineering: dict[str, Any] = {"status": repair.get("repair_status"),
                                   "reason_codes": repair.get("reason_codes"),
                                   "recomputed_status": None}
    # 保存的 verdict 必須宣告同一 extension 與 acceptance policy（evidence schema）。
    persisted = repair.get("method_extension")
    if dispatch.legacy:
        declared_ok = persisted is None or (
            isinstance(persisted, dict)
            and persisted.get("resolution") == "LEGACY_SETUP_MARGIN"
            and persisted.get("method_identity") == extension.method_identity)
    else:
        declared_ok = (isinstance(persisted, dict)
                       and persisted.get("resolution") == "PINNED"
                       and persisted.get("method_identity") == extension.method_identity
                       and persisted.get("extension_version") == extension.extension_version)
    if not declared_ok or repair.get("policy_id") != extension.acceptance_policy_id:
        reasons.append("persisted verdict does not declare the task method extension")
        return engineering

    # task／run／receipt／manifest／DQE／result-event lineage。
    runs = repair.get("runs") or {}
    _completed_run(loaded, F, step_id=F.STEP_RUN_CANDIDATE, slot="candidate",
                   run_id=runs.get("candidate"), reasons=reasons)
    baseline_plan = (task.execution_plan or {}).get("baseline_plan") or {}
    baseline_task_id = task.task_id
    if baseline_plan.get("mode") == "REUSE_VALIDATED_EAP3_BASELINE":
        reference = baseline_plan.get("reference") or {}
        source = load_task(reference.get("source_task_id"))
        reasons.extend(source["reasons"])
        baseline_task_id = reference.get("source_task_id")
        if source.get("task") is None or \
                source["task"].task_digest != reference.get("source_task_digest"):
            reasons.append("imported baseline task identity does not match the snapshot")
        else:
            if (reference.get("run") or {}).get("run_id") != runs.get("baseline"):
                reasons.append("baseline run differs from the imported baseline")
            _completed_run(source, F, step_id=F.STEP_RUN_BASELINE, slot="baseline",
                           run_id=runs.get("baseline"), reasons=reasons)
    else:
        _completed_run(loaded, F, step_id=F.STEP_RUN_BASELINE, slot="baseline",
                       run_id=runs.get("baseline"), reasons=reasons)

    evidence = repair.get("evidence") or {}
    payloads: dict[str, dict[str, Any]] = {"baseline": {}, "candidate": {}}
    roots: dict[str, str] = {}
    variants: dict[str, str] = {}
    owners = {"baseline": baseline_task_id, "candidate": task.task_id}
    for slot in ("baseline", "candidate"):
        slot_evidence = evidence.get(slot) or {}
        if not slot_evidence:
            continue
        if (slot_evidence.get("task_id") != owners[slot]
                or slot_evidence.get("slot") != slot
                or slot_evidence.get("run_id") != runs.get(slot)
                or not slot_evidence.get("output_root")):
            reasons.append(f"repair evidence lineage mismatch: {slot}")
            continue
        roots[slot] = str(slot_evidence["output_root"])
        variants[slot] = str(slot_evidence.get("variant"))
        reports = (Path(roots[slot]) / "reports" / task.platform_id / task.design_id
                   / variants[slot]).resolve()
        for name, ref in (slot_evidence.get("refs") or {}).items():
            path = Path(str(ref.get("path"))).resolve()
            if reports not in path.parents:
                reasons.append(f"repair evidence outside its slot coordinate: {slot}.{name}")
                continue
            if not path.is_file() or _sha256(path) != ref.get("sha256"):
                reasons.append(f"repair evidence hash mismatch: {slot}.{name}")
                continue
            payloads[slot][name] = json.loads(path.read_text(encoding="utf-8"))
    candidate_value = task.value_for_slot("candidate")
    stored_method = repair.get("method") or {}
    if stored_method.get("candidate_value") not in (None, candidate_value):
        reasons.append("persisted verdict candidate differs from the task snapshot")
    compare_envelope = loaded["results"].get(F.STEP_COMPARE)
    facts = compare_facts_from_result(
        compare_envelope.full_result if compare_envelope is not None
        and isinstance(compare_envelope.full_result, dict) else {})
    if not facts:
        reasons.append("compare step result carries no metric comparisons")
    host = None
    if "candidate" in roots:
        host = ME.read_consumption_evidence(
            extension, roots["candidate"], platform=task.platform_id,
            design=task.design_id, variant=variants["candidate"],
            expected_value=candidate_value)
    method = dict(((task.repair_recipe or {}).get("method") or {})
                  or {"variable": task.parameter_name})
    native_proof = None
    if extension is ME.SETUP_MARGIN_NATIVE_PROOF:
        native_proof, problem = _native_setup_margin_replay_context(task, repair)
        if problem is not None:
            reasons.append(problem)
            return engineering
    try:
        result = ME.evaluate_acceptance(
            extension, baseline=payloads["baseline"], candidate=payloads["candidate"],
            host_consumption=host, method=method, candidate_value=candidate_value,
            metric_comparisons=facts.get("metric_comparisons") or (),
            execution_ok=True, inputs_approved=True, inputs_reason=None,
            legacy=dispatch.legacy,
            host_run_facts=_closed_host_run_facts(loaded, owners, runs, F),
            native_proof=native_proof)
    except ME.MethodExtensionError as exc:
        reasons.append(f"method evidence rejected ({exc.code}): {exc.reason}")
        engineering["rejection_code"] = exc.code
        return engineering
    recomputed = result.payload
    engineering.update({
        "recomputed_status": recomputed.get("repair_status"),
        "recomputed_reason_codes": list(recomputed.get("reason_codes") or []),
        "policy_id": recomputed.get("policy_id"),
        "candidate_value": candidate_value,
        "proof_status": {key: (value or {}).get("status")
                         for key, value in (repair.get("proofs") or {}).items()}})
    if recomputed.get("repair_status") != repair.get("repair_status") \
            or list(recomputed.get("reason_codes") or []) != list(repair.get("reason_codes") or []):
        reasons.append("recomputed repair verdict does not match the persisted verdict")
    if recomputed.get("evidence_origin") == ME.ORFS_NOT_RUN:
        # synthetic／缺 per-slot evidence 只證明 routing，不能成為可驗證的工程結果。
        engineering["evidence_origin"] = ME.ORFS_NOT_RUN
        reasons.append("CORE_UTILIZATION evidence is ORFS_NOT_RUN")
    return engineering


__all__ = ["UNVERIFIABLE", "VERIFIED", "WORK_SCHEMA_UNSUPPORTED",
           "compare_facts_from_result", "replay_closed_work"]
