# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""從正式 Work get 結果製作預設遮蔽的支援摘要。"""

from __future__ import annotations

import re
from typing import Any

from terminux_engine.expert_ai.eap3 import digest_json


_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BUDGET_KEYS = (
    "engineering_attempts", "provider_dispatches", "tool_launches",
    "formal_proof_launches", "candidate_materializations", "work_active_time_sec",
)


def _token(value: Any) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and _DIGEST.fullmatch(value) else None


def _counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in _BUDGET_KEYS
            if isinstance(value.get(key), int) and not isinstance(value[key], bool)
            and value[key] >= 0}


#: 引擎產生的短說明（例如 Task terminal reason）只在不含路徑分隔符的受限字元
#: 內原樣輸出；否則只留 digest，避免把例外訊息中的絕對路徑帶出去。
_TEXT = re.compile(r"^[A-Za-z0-9 _.,:;()'=+-]{1,200}$")
#: 已知在 Work snapshot 中不存在的事實；如實標示缺失，不由其他資料推導。
NOT_PERSISTED = "NOT_PERSISTED"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and _TEXT.fullmatch(value) else None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _rows(value: Any, keys: tuple[str, ...]) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    return [{key: (_digest(row.get(key)) if key.endswith("digest") else _token(row.get(key)))
             for key in keys}
            for row in value if isinstance(row, dict)]


def _usage(work: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    """已持久化的用量與預占摘要；每一塊都標明它來自 snapshot 的哪個範圍。"""
    purpose_used = budget.get("purpose_used") if isinstance(budget.get("purpose_used"), dict) else {}
    by_key: dict[str, dict[str, int]] = {}
    ledger_rows = work.get("_ledger_reservations")
    rows = [row for row in ledger_rows if isinstance(row, dict)] if isinstance(ledger_rows, list) else []
    for row in rows:
        key, state, quantity = _token(row.get("budget_key")), _token(row.get("state")), row.get("quantity")
        if key is None or state is None or not isinstance(quantity, int) or isinstance(quantity, bool):
            continue
        bucket = by_key.setdefault(key, {})
        bucket[state] = bucket.get(state, 0) + quantity
    calls = work.get("role_calls") if isinstance(work.get("role_calls"), list) else []
    provider_rows = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        provider = call.get("provider") if isinstance(call.get("provider"), dict) else {}
        size = provider.get("response_bytes")
        provider_rows.append({
            "request_id": _token(call.get("request_id")),
            "binding_id": _token(provider.get("binding_id")),
            "model": _token(provider.get("model")),
            "response_bytes": size if isinstance(size, int) and not isinstance(size, bool) else None,
            "finish_reason": _token(provider.get("finish_reason")),
        })
    return {
        "work_budget": {
            "scope": "work_snapshot.budget",
            "note": "本 Work 的額度帳；limits/used/reserved 見頂層 budget。",
            "remaining": _counts(budget.get("remaining")),
            "purpose_used": {name: _counts(value) for name, value in purpose_used.items()
                             if _token(name) is not None},
        },
        "reservations": {
            "scope": "work_snapshot._ledger_reservations" if isinstance(ledger_rows, list) else NOT_PERSISTED,
            "note": "本 Work 經 batch ledger 的預占列，依 budget key 與狀態加總 quantity。",
            "rows": len(rows), "by_budget_key": by_key,
        },
        "provider_calls": {
            "scope": "work_snapshot.role_calls[].provider",
            "rows": provider_rows,
            # provider token 用量從未寫入 Work snapshot，只能標示缺失。
            "token_usage": NOT_PERSISTED,
        },
    }


def _step_evidence(results: Any) -> list[dict[str, Any]]:
    """attempt outcome 中各 step 的結構化狀態與 receipt 關聯。"""
    if not isinstance(results, list):
        return []
    rows = []
    for row in results:
        if not isinstance(row, dict):
            continue
        envelope = row.get("envelope") if isinstance(row.get("envelope"), dict) else {}
        full = envelope.get("full_result") if isinstance(envelope.get("full_result"), dict) else {}
        receipt = full.get("receipt") if isinstance(full.get("receipt"), dict) else {}
        rows.append({
            "step_id": _token(row.get("step_id")),
            "status": _token(row.get("status")),
            "evidence_status": _token(row.get("evidence_status")),
            "execution_status": _token(envelope.get("execution_status")),
            "action_status": _token(envelope.get("action_status")),
            "grant_id": _token(envelope.get("grant_id")),
            "run_id": _token(full.get("run_id") or receipt.get("run_id")),
            "evidence_verified": _flag(full.get("evidence_verified")),
            "receipt": {
                "receipt_id": _token(receipt.get("receipt_id")),
                "content_hash": _digest(receipt.get("content_hash")),
                "termination_reason": _token(receipt.get("termination_reason")),
                "dispatch_status": _token(receipt.get("dispatch_status")),
                "evidence_status": _token(receipt.get("evidence_status")),
                "artifact_capture_status": _token(receipt.get("artifact_capture_status")),
                "artifact_manifest_id": _token(receipt.get("artifact_manifest_id")),
                "artifact_manifest_sha256": _digest(receipt.get("artifact_manifest_sha256")),
            } if receipt else None,
        })
    return rows


def _attempt_evidence(attempts: Any) -> list[dict[str, Any]]:
    """每個 attempt 的結構化終態、失敗理由與 Work／Task／receipt 關聯。"""
    if not isinstance(attempts, list):
        return []
    rows = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        engineering = (attempt.get("engineering_result")
                       if isinstance(attempt.get("engineering_result"), dict) else None)
        outcome = attempt.get("outcome") if isinstance(attempt.get("outcome"), dict) else {}
        missing: list[str] = []
        task: dict[str, Any] | None = None
        if engineering is None:
            missing.append("engineering_result")
        else:
            reason = engineering.get("terminal_reason")
            steps = engineering.get("steps") if isinstance(engineering.get("steps"), dict) else {}
            task = {
                "terminal_status": _token(engineering.get("terminal_status")),
                "terminal_reason": _text(reason),
                "terminal_reason_digest": digest_json(reason) if isinstance(reason, str) else None,
                "steps": {name: _token(value) for name, value in steps.items()
                          if _token(name) is not None},
                "engineering_result_available": _flag(engineering.get("available")),
                "package_reason_codes": [code for code in (_token(item) for item in (
                    engineering.get("reason_codes") or ())) if code is not None],
            }
            if isinstance(reason, str) and task["terminal_reason"] is None:
                missing.append("terminal_reason_text_redacted")
        # FlowTask 的 terminal reason 只有文字，沒有結構化 code。
        missing.append("task_reason_code")
        rows.append({
            "attempt_id": _token(attempt.get("attempt_id")),
            "attempt_status": _token(attempt.get("status")),
            "replacement_reason": _token(attempt.get("replacement_reason")),
            "task_id": _token(attempt.get("task_id")),
            "task_digest": _digest(attempt.get("task_digest")),
            "grant_id": _token(attempt.get("grant_id")),
            "engineering_result_digest": _digest(attempt.get("engineering_result_digest")),
            "task": task,
            "task_reason_code": None,
            "steps": _step_evidence(outcome.get("results")),
            "missing": missing,
        })
    return rows


def _criteria_verification(work: dict[str, Any]) -> dict[str, Any]:
    record = work.get("criteria_verification")
    if not isinstance(record, dict):
        # 修正前的 Work 沒有這份紀錄；不從其他事實回推。
        return {"status": NOT_PERSISTED}
    return {
        "status": "PERSISTED",
        "schema_version": _token(record.get("schema_version")),
        "decision_id": _token(record.get("decision_id")),
        "claimed_criteria_met": _flag(record.get("claimed_criteria_met")),
        "outcome": _token(record.get("outcome")),
        "reason_codes": [code for code in (_token(item) for item in (
            record.get("reason_codes") or ())) if code is not None],
        "attempt_id": _token(record.get("attempt_id")),
        "task_id": _token(record.get("task_id")),
        "engineering_result_digest": _digest(record.get("engineering_result_digest")),
    }


def summarize_work(work: dict[str, Any]) -> dict[str, Any]:
    """只匯出身分、digest、額度、終態與已保存的結構化失敗事實；不匯出 prompt、原文、路徑、憑證或 endpoint。"""
    scope = work.get("scope") if isinstance(work.get("scope"), dict) else {}
    authorization = work.get("authorization") if isinstance(work.get("authorization"), dict) else {}
    budget = work.get("budget") if isinstance(work.get("budget"), dict) else {}
    pending = work.get("pending") if isinstance(work.get("pending"), dict) else {}
    package = work.get("package") if isinstance(work.get("package"), dict) else {}
    result: dict[str, Any] = {
        "schema_version": "terminux.support_export.v1",
        "work_id": _token(work.get("work_id")),
        "status": _token(work.get("status")),
        "revision": work.get("revision") if isinstance(work.get("revision"), int) else None,
        "scope_digest": digest_json(scope),
        "package": {"package_id": _token(package.get("package_id")),
                    "version": _token(package.get("version"))},
        "authorization": {
            "authorization_id": _token(authorization.get("authorization_id")),
            "authorization_digest": _digest(authorization.get("authorization_digest")),
            "mode": _token(authorization.get("mode")),
        },
        "budget": {name: _counts(budget.get(name)) for name in ("limits", "used", "reserved")},
        "stop_reason": _token(work.get("stop_reason")),
        "pending": {"kind": _token(pending.get("kind")),
                    "reason_code": _token(pending.get("reason_code"))},
        "proposals": _rows(work.get("proposals"), (
            "proposal_id", "proposal_digest", "source_proposal_digest",
            "source_response_digest", "response_digest")),
        "role_calls": _rows(work.get("role_calls"), (
            "request_id", "role", "purpose", "dispatch_class", "status",
            "request_digest", "response_digest", "error_code")),
        "decisions": _rows(work.get("decisions"), (
            "decision_id", "kind", "based_on_domain_response_digest")),
        "attempts": _rows(work.get("attempts"), (
            "attempt_id", "status", "task_id", "task_digest", "grant_id",
            "outcome_digest", "domain_analysis_digest")),
        "usage": _usage(work, budget),
        "attempt_evidence": _attempt_evidence(work.get("attempts")),
        "criteria_verification": _criteria_verification(work),
    }
    return result
