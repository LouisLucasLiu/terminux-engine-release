# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""explain_run 資料模型與 LLM 輸出驗證。

三個層次嚴格分開：

- **Deterministic facts**：由 Python 從 receipt／DQE／manifest 建立，LLM
  不得覆寫。
- **EvidenceRef**：指向 manifest 內某個 artifact 的 verified 片段，
  line range 為 1-based inclusive，只保留短 excerpt ＋ excerpt hash。
- **LLM output**：只允許 interpretations／hypotheses／unknowns／
  recommended_checks 四個 key，且每一項的 reference 都必須存在。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
CONTEXT_TYPE = "orfs_run_intelligence_context"
INSIGHT_RECORD_TYPE = "report_insight"
NAMESPACE = "orfs_run_intelligence"

SUPPORT_SUPPORTED = "supported"
SUPPORT_PARTIAL = "partially_supported"
SUPPORT_INSUFFICIENT = "insufficient_evidence"

from terminux_engine.output_safety import find_command_invocation

#: LLM 只能輸出這四個 key，多一個都視為 schema 違規。
LLM_OUTPUT_KEYS = frozenset(
    {"interpretations", "hypotheses", "unknowns", "recommended_checks"}
)

#: recommended_check 若**呈現為指令**即拒絕。通用的 shell／OS 判斷在 Common
#: 的 `output_safety`（domain-blind）；這裡只補上本 package 自己知道的
#: domain 命令名——Common 不該認識 EDA 工具。
#:
#: 舊版是裸 substring 比對（含 `"rm "`、`";"`、`"|"`），會把
#: `"…to confi[rm ]clock targets…"` 這種正常英文整份判為指令並丟棄。
DOMAIN_COMMAND_WORDS: frozenset[str] = frozenset({
    "make", "docker", "openroad", "yosys", "klayout", "run_command",
})

_MAX_ITEMS_PER_LIST = 12
_MAX_TEXT_CHARS = 1200


#: 可以 repair 的錯誤類別——只有「格式／結構沒寫對」才給第二次機會。
CODE_NOT_JSON = "not_json"
CODE_SCHEMA = "schema"
#: 不得 repair：這些是 contract／safety violation，不是格式失誤（Part D）。
CODE_UNSAFE_COMMAND = "unsafe_command"
CODE_EVIDENCE_SCOPE = "evidence_scope"
#: **只有**「模型引用了不存在／不屬於本次允許集合的 evidence ref」
#: 這一種拒絕，才可能被 host 的 narrative policy 判為可降級。fact id 越界
#: 仍然沿用 `CODE_EVIDENCE_SCOPE`，不併進來。
CODE_EVIDENCE_REF_SCOPE = "evidence_ref_scope"
CODE_UNSUPPORTED_FACT = "unsupported_fact"

REPAIRABLE_CODES: frozenset[str] = frozenset({CODE_NOT_JSON, CODE_SCHEMA})


class LlmOutputError(ValueError):
    """LLM 輸出不符 schema／引用不存在／越界時拋出。

    `code` 決定這個失敗**能不能** repair。safety 與 evidence-scope 違規一律
    不得 repair——再問一次只是給模型第二次繞過守則的機會。
    """

    def __init__(self, message: str, *, code: str = CODE_SCHEMA,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        #: 由**受信任的 validator 程式**填入的結構化事實（被拒的 ref、
        #: 本次允許集合的 digest）。**不是**從模型 JSON 複製的。
        self.details: dict[str, Any] = dict(details or {})

    @property
    def repairable(self) -> bool:
        return self.code in REPAIRABLE_CODES


def _ref_set_digest(refs: set[str]) -> str:
    """本次允許引用集合的固定摘要。用來證明「拒絕是對這一組集合做的」。"""
    return hashlib.sha256(
        "\n".join(sorted(refs)).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class EvidenceRef:
    """指向 verified artifact 內某個片段的可重算引用。"""

    evidence_ref_id: str
    artifact_manifest_id: str
    artifact_instance_id: str
    adapter_role: str
    content_sha256: str | None
    section: str | None
    line_start: int | None
    line_end: int | None
    excerpt_hash: str | None
    excerpt_text: str
    current_content_verified: bool

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref_id": self.evidence_ref_id,
            "artifact_manifest_id": self.artifact_manifest_id,
            "artifact_instance_id": self.artifact_instance_id,
            "adapter_role": self.adapter_role,
            "content_sha256": self.content_sha256,
            "section": self.section,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "excerpt_hash": self.excerpt_hash,
            "excerpt_text": self.excerpt_text,
            "current_content_verified": self.current_content_verified,
        }


@dataclass(frozen=True)
class Fact:
    """Deterministic fact。`id` 供 LLM 引用，內容由 Python 決定。"""

    id: str
    category: str
    statement: str
    value: float | int | str | None = None
    unit: str | None = None
    evidence_ref_ids: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "statement": self.statement,
            "value": self.value,
            "unit": self.unit,
            "evidence_ref_ids": list(self.evidence_ref_ids),
        }


def validate_llm_output(
    raw: str,
    *,
    known_fact_ids: set[str],
    known_evidence_ref_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """解析並嚴格驗證 LLM 回覆。任何違規都拋 `LlmOutputError`。"""
    text = _strip_single_fence(raw.strip())
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmOutputError(f"output is not valid JSON: {exc}",
                             code=CODE_NOT_JSON) from exc
    if not isinstance(payload, dict):
        raise LlmOutputError("output is not a JSON object")

    extra = set(payload) - LLM_OUTPUT_KEYS
    if extra:
        raise LlmOutputError(f"output contains keys outside the contract: {sorted(extra)}")
    missing = LLM_OUTPUT_KEYS - set(payload)
    if missing:
        raise LlmOutputError(f"output is missing required keys: {sorted(missing)}")

    result: dict[str, list[dict[str, Any]]] = {}
    for key in ("interpretations", "hypotheses", "unknowns", "recommended_checks"):
        items = payload[key]
        if not isinstance(items, list):
            raise LlmOutputError(f"{key} is not a list")
        if len(items) > _MAX_ITEMS_PER_LIST:
            raise LlmOutputError(f"{key} has {len(items)} items (max {_MAX_ITEMS_PER_LIST})")
        result[key] = [
            _validate_item(key, item, known_fact_ids, known_evidence_ref_ids)
            for item in items
        ]

    if not result["interpretations"] and not result["unknowns"]:
        raise LlmOutputError("output has neither interpretations nor unknowns")
    return result



def _strip_single_fence(text: str) -> str:
    """只剝除「整段回覆剛好是一個 code fence」的外殼。

    Prompt 已明確要求不要加 fence，但多數 provider 仍習慣包一層。剝掉這一層
    是安全的正規化：內容仍然要通過完整的 JSON 解析與引用驗證，**沒有**放寬
    任何保證。fence 前後若還有散文（代表 model 沒有遵守 output contract），
    仍然拒絕。
    """
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        raise LlmOutputError("output starts a markdown fence but does not close it")
    body = "\n".join(lines[1:-1]).strip()
    if body.startswith("```") or "```" in body:
        raise LlmOutputError("output contains prose or extra fences around the JSON")
    return body


def _validate_item(
    key: str,
    item: Any,
    known_fact_ids: set[str],
    known_evidence_ref_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise LlmOutputError(f"{key} item is not an object")
    statement = item.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise LlmOutputError(f"{key} item has no statement")
    if len(statement) > _MAX_TEXT_CHARS:
        raise LlmOutputError(f"{key} statement exceeds {_MAX_TEXT_CHARS} characters")

    fact_ids = _string_list(item.get("fact_ids"), f"{key}.fact_ids")
    ref_ids = _string_list(item.get("evidence_ref_ids"), f"{key}.evidence_ref_ids")
    unknown_facts = [f for f in fact_ids if f not in known_fact_ids]
    unknown_refs = [r for r in ref_ids if r not in known_evidence_ref_ids]
    if unknown_facts:
        raise LlmOutputError(
            f"{key} item references unknown fact ids: {unknown_facts}",
            code=CODE_EVIDENCE_SCOPE)
    if unknown_refs:
        raise LlmOutputError(
            f"{key} item references unknown evidence refs: {unknown_refs}",
            code=CODE_EVIDENCE_REF_SCOPE,
            details={
                "rejected_evidence_refs": sorted(unknown_refs),
                "allowed_evidence_ref_count": len(known_evidence_ref_ids),
                "allowed_evidence_ref_digest": _ref_set_digest(known_evidence_ref_ids),
                "rejected_in_section": key,
            })

    out: dict[str, Any] = {
        "statement": statement.strip(),
        "fact_ids": fact_ids,
        "evidence_ref_ids": ref_ids,
    }

    if key == "interpretations":
        # interpretation 必須有支撐，不能憑空出現。
        if not fact_ids and not ref_ids:
            raise LlmOutputError("interpretation has no supporting fact or evidence")
    elif key == "hypotheses":
        validation = item.get("validation_needed")
        if not isinstance(validation, str) or not validation.strip():
            raise LlmOutputError("hypothesis is missing validation_needed")
        if _looks_like_fact_claim(statement):
            raise LlmOutputError(
                "hypothesis is phrased as an established fact: " + statement[:120],
                code=CODE_UNSUPPORTED_FACT,
            )
        out["validation_needed"] = validation.strip()
    elif key == "recommended_checks":
        finding = find_command_invocation(
            statement, extra_commands=DOMAIN_COMMAND_WORDS)
        if finding is not None:
            raise LlmOutputError(
                f"recommended_check looks like an executable command "
                f"({finding.reason}: {finding.marker!r})",
                code=CODE_UNSAFE_COMMAND,
            )
    return out


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise LlmOutputError(f"{label} must be a list of strings")
    return list(value)


_FACT_CLAIM_PATTERNS = (
    r"\bis confirmed\b", r"\bwas caused by\b", r"\bthe root cause is\b",
    r"\bproves\b", r"\bdefinitely\b", r"\bcertainly\b",
    r"\b(確認|確定|必然|肯定)(是|為)\b", r"根本原因是",
)


def _looks_like_fact_claim(statement: str) -> bool:
    lowered = statement.lower()
    return any(re.search(p, lowered) for p in _FACT_CLAIM_PATTERNS)


__all__ = [
    "CONTEXT_TYPE",
    "EvidenceRef",
    "Fact",
    "INSIGHT_RECORD_TYPE",
    "LLM_OUTPUT_KEYS",
    "LlmOutputError",
    "NAMESPACE",
    "SCHEMA_VERSION",
    "SUPPORT_INSUFFICIENT",
    "SUPPORT_PARTIAL",
    "SUPPORT_SUPPORTED",
    "canonical_json",
    "sha256_text",
    "validate_llm_output",
]
