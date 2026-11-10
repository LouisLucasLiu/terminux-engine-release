# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Host-controlled action proposal & approval boundary。

此模組建立一個 daemon-memory-only 的 proposal state machine，讓 Agent 能依
evidence 產生結構化 package action proposal，但**永遠不呼叫 action
handler**——approval 只改變 proposal 的狀態，不觸發任何副作用。真正的
一次性核准執行留到 evidence-first planner。

設計上刻意重用既有 notification package-action allowlist
（`_pending_package_actions`／`_PACKAGE_ACTION_TTL_SEC`）的 identity／TTL／
registry-validation 概念，而不是另建一套會與既有 permission gate 互相矛盾
的授權系統：

- proposal 的 action 必須存在於同一個 canonical `ActionRegistry`。
- proposal 只在 daemon process 記憶體內存活；daemon 重啟即全部失效
  （沒有任何持久化層）。
- 核准／拒絕完全由 host 端 request context（`CanonicalRequestContext`）
  綁定，provider（LLM）永遠看不到、也不能呼叫這個 state machine。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from terminux_engine.ipc.request_context import CanonicalRequestContext
from terminux_engine.package_runtime.action_registry import ActionRegistry
from terminux_engine.package_runtime.loader import RuntimePackage

#: 擴充 evidence-first planner 的 PENDING/APPROVED/REJECTED/EXPIRED/CANCELLED，加入
#: 一次性執行的 EXECUTING/SUCCEEDED/FAILED。EXECUTING 只由
#: ProposalRegistry.claim_for_execution() 的 atomic transition 設定；一旦離開
#: APPROVED，永遠不會回到 APPROVED（no retry、no replay）。
ProposalStatus = Literal[
    "PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED",
    "EXECUTING", "SUCCEEDED", "FAILED",
]

#: C1 固定值；proposal 尚未進入一次性執行流程時的 execution_status。
#: proposal 的授權來源。
AUTHORIZATION_SOURCE_OPERATOR = "operator_control"
AUTHORIZATION_SOURCE_TASK = "task_authorization"

#: task-owned proposal 不接受任何 operator mutation 入口。
TASK_PROPOSAL_CONTROL_DENIED = "TASK_PROPOSAL_CONTROL_DENIED"

EXECUTION_STATUS_NOT_EXECUTED = "NOT_EXECUTED"

#: claim 成功後、handler 尚未回覆前的 execution_status。
EXECUTION_STATUS_EXECUTING = "EXECUTING"

_TTL_SEC = 600.0  # 10 分鐘

#: action 自報狀態的 bounded 長度。
_MAX_ACTION_STATUS_CHARS = 64
_MAX_PENDING_PER_SESSION = 16
_MAX_CATALOG_ACTIONS = 32
_MAX_CATALOG_BYTES = 32 * 1024
_MAX_REASON_CHARS = 500
_MAX_LIST_ITEMS = 20
_MAX_LIST_ITEM_CHARS = 200
#: execution result 序列化後的 bounded 上限（沿用 agent_audit 對大型
#: tool result 只保留 bounded 摘要的既有慣例，但這裡是要回給 caller 的欄位，
#: 所以保留可讀 JSON text 而非只存 digest）。
_MAX_EXECUTION_RESULT_CHARS = 4000

#: TrustedActionResult 的 host-only 常數。
_TRUSTED_RESULT_TTL_SEC = 30 * 60.0  # 30 分鐘
_MAX_TRUSTED_RESULTS_PER_SESSION = 4
_MAX_TRUSTED_RESULT_AGGREGATE_BYTES = 64 * 1024
#: 單筆 result payload 的 bounded 上限；超過時整段標記 truncated（不做部分
#: JSON 截斷——避免產生語法上不完整、無法安全做 JSON Pointer 解析的資料）。
_MAX_TRUSTED_RESULT_PAYLOAD_CHARS = 4000
#: v1 固定值：TrustedActionResult 只可能來自 C2 execution chain。
TRUSTED_RESULT_SOURCE = "HOST_EXECUTED_PACKAGE_ACTION"

#: ResultHandleCatalog 的 host-only 常數。
_MAX_HANDLE_DEPTH = 6
_MAX_HANDLES_PER_RESULT = 64
_MAX_HANDLE_CATALOG_BYTES = 32 * 1024
_MAX_HANDLE_PREVIEW_CHARS = 200
_HANDLE_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})
#: 保守的 sensitive-key denylist（case-insensitive 子字串比對）；命中的欄位
#: （及其所有子節點）一律 `bindable=False` 且 preview 遮蔽，不看內容值，只
#: 看 key 名稱——這是 fail-closed 的啟發式，寧可誤判過度保守也不誤放行。
_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|credential|api[_-]?key|private[_-]?key|"
    r"auth|ssn|session[_-]?id)",
    re.IGNORECASE,
)

_JSON_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


@dataclass(frozen=True)
class ProposableCapability:
    """v1 metadata-only catalog 項目；不是 provider tool/function schema，
    provider 無法直接呼叫其中任何 action。"""

    action_name: str
    description: str
    parameters: dict[str, dict[str, Any]]


def build_proposable_capability_catalog(
    packages: Iterable[RuntimePackage],
    action_registry: ActionRegistry,
) -> list[ProposableCapability]:
    """每次呼叫都從當前 registry 重新建立；不信任 provider 自報能力。

    只包含已註冊、具 description 與 parameter schema 的 namespaced package
    action——`run_command`、core `orchestration.*` mutation tool 從不是
    package manifest action，天生不會出現在這裡。"""
    catalog: list[ProposableCapability] = []
    for package in packages:
        for action_name, action_spec in package.manifest.action_specs.items():
            if not action_spec.description:
                continue
            try:
                action_registry.resolve(action_name)
            except Exception:
                continue
            catalog.append(ProposableCapability(
                action_name=action_name,
                description=action_spec.description,
                parameters=dict(action_spec.parameters),
            ))
            if len(catalog) >= _MAX_CATALOG_ACTIONS:
                return catalog
    return catalog


def catalog_prompt_text(catalog: list[ProposableCapability]) -> str:
    """把 catalog 序列化成 bounded、純文字描述給 provider 讀（非 tool schema）。

    這份 catalog 不再只在「使用者明說要
    提案」時才有意義——它是 planner 做 capability selection 時看得到的第二
    類能力。因此除了 name/description，也列出 parameter schema 與 required
    欄位，讓 planner 能判斷「已知的輸入是否足以提出一筆可執行的提案」。

    仍然只暴露 action name、description、parameter 名稱/型別/required；
    絕不暴露 handler source、filesystem path、prompt 內容或任何 secret。
    這裡列出的 action 永遠不會變成 provider 可直接呼叫的 tool schema。
    """
    if not catalog:
        return ""
    lines = [
        "可提案的 package action（只能用於 proposed_action 欄位，"
        "不可直接呼叫、不會被自動執行）：",
    ]
    size = 0
    for item in catalog:
        line = f"- {item.action_name}: {item.description}"
        params = _parameter_summary(item.parameters)
        if params:
            line += f"\n    parameters: {params}"
        encoded = line.encode("utf-8")
        if size + len(encoded) > _MAX_CATALOG_BYTES:
            break
        lines.append(line)
        size += len(encoded)
    return "\n".join(lines)


def _parameter_summary(parameters: dict[str, dict[str, Any]]) -> str:
    """把 parameter schema 壓成一行 provider-safe 摘要。

    只取 name、type、required——planner 判斷「輸入是否足以提出一筆可執行
    提案」需要的最小資訊；schema 內其餘欄位（default、內部 hint 等）一律
    不外流。"""
    if not isinstance(parameters, dict) or not parameters:
        return ""
    parts: list[str] = []
    for name, spec in parameters.items():
        spec = spec if isinstance(spec, dict) else {}
        type_name = spec.get("type") or "any"
        required = "required" if spec.get("required") else "optional"
        parts.append(f"{name}({type_name}, {required})")
    return ", ".join(parts)


def binding_candidate_catalog_prompt_text(catalogs: dict[str, dict[str, Any]]) -> str:
    """把每個 proposable action 專屬的 provider-safe
    BindingCandidateCatalog view 序列化成 bounded 文字教給 provider。只有
    這裡列出的 `candidate_id` 可以出現在 `proposed_action.candidate_
    bindings` 裡；`handle_id`／`json_pointer` 從不出現在這段文字（provider
    永遠看不到）。"""
    lines: list[str] = []
    for action_name, view in catalogs.items():
        candidates = (view or {}).get("candidates") or []
        if not candidates:
            continue
        lines.append(f"- {action_name} 的可綁定候選：")
        for c in candidates:
            lines.append(
                f"    candidate_id={c.get('candidate_id')} argument_name={c.get('argument_name')} "
                f"type={c.get('value_type')} preview={c.get('preview')}"
            )
    if not lines:
        return ""
    return (
        "\n\n以下是各 action 目前可用的 binding candidate（只能用於 "
        "proposed_action.candidate_bindings，只能用列出的 candidate_id；"
        "不可自己指定 handle_id、不可猜測其他 argument 的候選、不可挑選"
        "沒有在這裡列出的候選）：\n" + "\n".join(lines)
    )


def _canonical_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str))


def validate_action_arguments(parameters_schema: dict[str, dict[str, Any]], arguments: dict[str, Any]) -> bool:
    """對 manifest parameter schema 做最小、host-only 的結構驗證。"""
    if type(arguments) is not dict:
        return False
    allowed = set(parameters_schema)
    if set(arguments) - allowed:
        return False
    for name, schema in parameters_schema.items():
        required = bool(schema.get("required", False))
        if required and name not in arguments:
            return False
        if name in arguments:
            expected_type = schema.get("type")
            checks = _JSON_TYPE_CHECKS.get(expected_type) if isinstance(expected_type, str) else None
            if checks is not None:
                value = arguments[name]
                if isinstance(value, bool) and bool not in checks:
                    return False
                if not isinstance(value, checks):
                    return False
    return True


def compute_proposal_digest(
    *,
    action_name: str,
    arguments: dict[str, Any],
    workspace: str,
    member_id: str,
    project_id: str,
    session_id: str,
    risk_class: str,
    expires_at: float,
) -> str:
    """canonical JSON 的 SHA-256；涵蓋 action/arguments/workspace/scope/risk/expiry。
    provider 永遠不能指定這個值，只有 host 端計算出來的 digest 才有效。"""
    payload = {
        "action_name": action_name,
        "arguments": _canonical_arguments(arguments),
        "workspace": workspace,
        "member_id": member_id,
        "project_id": project_id,
        "session_id": session_id,
        "risk_class": risk_class,
        "expires_at": expires_at,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class ActionProposal:
    proposal_id: str
    proposal_digest: str
    action_name: str
    arguments: dict[str, Any]
    workspace: str
    member_id: str
    project_id: str
    session_id: str
    reason: str
    expected_effects: list[str]
    possible_side_effects: list[str]
    evidence_references: list[str]
    preconditions: list[str]
    risk_class: str
    created_at: float
    expires_at: float
    status: ProposalStatus = "PENDING"
    execution_status: str = EXECUTION_STATUS_NOT_EXECUTED
    #: action **自報**的結果狀態。`execution_status` 的意思是
    #: 「tool 沒有拋例外」，不是「分析找到東西」；action 本身若在回傳內容裡
    #: 帶了 `status`／`support_status`，這裡把它原樣帶出來，讓只看 proposal
    #: 狀態的人不會誤以為分析成功。取不到時維持 `None`——**不推斷**。
    action_status: str | None = None
    #: claim 成功那次 claim 的時間戳；FAILED/SUCCEEDED 前保持 None。
    executed_at: float | None = None
    #: bounded、JSON-serializable 的 handler 回傳摘要（成功或失敗皆有）；
    #: 執行前恆為 None。
    execution_result: str | None = None
    #: 這筆 proposal 建立時宣告依據的 TrustedActionResult id 清單
    #: （純 bookkeeping／audit trail，不影響驗證邏輯）。
    prior_result_refs: list[str] = field(default_factory=list)
    #: 每個 materialized argument 的來源
    #: （"handle"／"legacy_result_binding"／未出現代表純 literal），供
    #: operator control path 顯示 provenance，不影響任何驗證邏輯。
    argument_provenance: dict[str, str] = field(default_factory=dict)
    #: 因與 handle 衝突而被忽略的 literal，只存
    #: {"digest", "type"}，絕不保存原始值。
    ignored_literals: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 授權來源。既有 operator proposal 一律維持
    #: `"operator_control"`（預設值），欄位與行為完全向下相容；只有由
    #: `AnalysisTaskService` 建立的 child proposal 會是 `"task_authorization"`。
    #: **不進入 proposal digest**——digest 演算法不改。
    authorization_source: str = AUTHORIZATION_SOURCE_OPERATOR
    #: task-owned proposal 的 parent 身分；operator proposal 恆為 None。
    task_id: str | None = None
    step_id: str | None = None
    grant_id: str | None = None

    @property
    def is_task_owned(self) -> bool:
        return self.authorization_source == AUTHORIZATION_SOURCE_TASK

    def is_expired(self, now: float) -> bool:
        return self.status == "PENDING" and now >= self.expires_at

    def public_view(self) -> dict[str, Any]:
        """回傳給 caller 的 bounded view；不外洩超過本結構的內部欄位。"""
        return {
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "action_name": self.action_name,
            "arguments": self.arguments,
            "workspace": self.workspace,
            "reason": self.reason[:_MAX_REASON_CHARS],
            "expected_effects": self.expected_effects[:_MAX_LIST_ITEMS],
            "possible_side_effects": self.possible_side_effects[:_MAX_LIST_ITEMS],
            "evidence_references": self.evidence_references[:_MAX_LIST_ITEMS],
            "preconditions": self.preconditions[:_MAX_LIST_ITEMS],
            "risk_class": self.risk_class,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "execution_status": self.execution_status,
            "action_status": self.action_status,
            "executed_at": self.executed_at,
            "execution_result": self.execution_result,
            "prior_result_refs": self.prior_result_refs[:_MAX_LIST_ITEMS],
            "argument_provenance": dict(list(self.argument_provenance.items())[:_MAX_LIST_ITEMS]),
            "ignored_literals": dict(list(self.ignored_literals.items())[:_MAX_LIST_ITEMS]),
            "authorization_source": self.authorization_source,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "grant_id": self.grant_id,
        }


@dataclass(frozen=True)
class TrustedActionResult:
    """C2 execution chain 完成後、host 建立的唯一可信 action-output
    紀錄。永遠只由 `ProposalRegistry.finalize_execution()` 內部建立——沒有
    任何 public API 讓 provider 或 user 自行建立一筆。"""

    result_id: str
    proposal_id: str
    proposal_digest: str
    action_name: str
    #: proposal.arguments 的 bounded canonical JSON 摘要（不是完整值，只供
    #: 人類辨識用；真正要拿值必須透過 result_payload + JSON Pointer）。
    arguments_summary: str
    member_id: str
    project_id: str
    session_id: str
    execution_status: Literal["SUCCEEDED", "FAILED"]
    #: bounded canonical JSON text；complete=False 時這是被截斷、不保證可
    #: 解析成合法 JSON 的片段——任何 JSON Pointer 解析都必須 fail-closed。
    result_payload: str
    #: 對「完整」（未截斷）canonical JSON 計算的 SHA-256；即使 payload 本身
    #: 因為過大被標記 truncated，這個 digest 仍反映真實的完整結果。
    result_digest: str
    created_at: float
    expires_at: float
    complete: bool
    truncated: bool
    source: str = TRUSTED_RESULT_SOURCE

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def public_view(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "action_name": self.action_name,
            "arguments_summary": self.arguments_summary,
            "execution_status": self.execution_status,
            "result_payload": self.result_payload,
            "result_digest": self.result_digest,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "complete": self.complete,
            "truncated": self.truncated,
            "source": self.source,
        }


@dataclass(frozen=True)
class ResultHandle:
    """host deterministic 從一筆 TrustedActionResult 的 payload 走訪
    出來的單一 handle。`json_pointer` 是 host-only 內部欄位——provider 永遠
    看不到、也不需要知道；provider 只挑 `handle_id`，host 用內部保存的
    mapping 解析出實際值。"""

    handle_id: str
    result_id: str
    result_digest: str
    json_pointer: str
    value_type: str  # "string"|"integer"|"number"|"boolean"|"null"|"object"|"array"
    preview: str
    bindable: bool

    def public_view(self) -> dict[str, Any]:
        """給 provider 看的 bounded view——刻意不含 `json_pointer`，provider
        只能透過 handle_id 引用，不能自己組 JSON Pointer。"""
        return {
            "handle_id": self.handle_id,
            "value_type": self.value_type,
            "preview": self.preview,
            "bindable": self.bindable,
        }


@dataclass(frozen=True)
class ResultHandleCatalog:
    """對單一 TrustedActionResult 的 deterministic handle 目錄。同一
    份 `result_payload` 永遠產生同一份 catalog（object key 排序、array 依
    index 順序），重跑不會改變 handle_id 或內容。"""

    result_id: str
    result_digest: str
    handles: tuple[ResultHandle, ...]
    complete: bool
    truncated: bool

    def public_view(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "result_digest": self.result_digest,
            "handles": [h.public_view() for h in self.handles],
            "complete": self.complete,
            "truncated": self.truncated,
        }


def _json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"


def _handle_preview(value: Any, value_type: str, *, sensitive: bool) -> str:
    if sensitive:
        return "***redacted***"
    if value_type in ("object", "array"):
        return f"<{value_type} len={len(value)}>"
    text = json.dumps(value, ensure_ascii=False, default=str) if value is not None else "null"
    return text[:_MAX_HANDLE_PREVIEW_CHARS]


def _escape_json_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def build_result_handle_catalog(
    result_id: str, result_digest: str, result_payload: str, *, complete: bool,
) -> ResultHandleCatalog:
    """host deterministic traversal——object key 排序、array
    依 index 順序，depth 上限 `_MAX_HANDLE_DEPTH`，每筆 result 最多
    `_MAX_HANDLES_PER_RESULT` 個 handle，序列化總大小上限
    `_MAX_HANDLE_CATALOG_BYTES`；任何一項超限都把 catalog 標成
    `truncated=True`（不是靜默截斷）。result 本身已被標記 `complete=False`
    （TrustedActionResult 超過 bounded payload 上限）時，直接回傳空 catalog
    並標記 truncated——截斷過的 result 不可信任何走訪出來的內容。"""
    if not complete:
        return ResultHandleCatalog(result_id, result_digest, (), complete=False, truncated=True)
    try:
        payload = json.loads(result_payload)
    except json.JSONDecodeError:
        return ResultHandleCatalog(result_id, result_digest, (), complete=False, truncated=True)

    handles: list[ResultHandle] = []
    state = {"truncated": False, "bytes": 0, "index": 0}

    def visit(value: Any, pointer: str, depth: int, sensitive_ancestor: bool) -> None:
        if len(handles) >= _MAX_HANDLES_PER_RESULT:
            state["truncated"] = True
            return
        if depth > _MAX_HANDLE_DEPTH:
            state["truncated"] = True
            return
        value_type = _json_value_type(value)
        bindable = value_type in _HANDLE_SCALAR_TYPES and not sensitive_ancestor
        preview = _handle_preview(value, value_type, sensitive=sensitive_ancestor)
        handle_id = f"rh_{result_digest[:12]}_{state['index']}"
        state["index"] += 1
        candidate = ResultHandle(
            handle_id=handle_id, result_id=result_id, result_digest=result_digest,
            json_pointer=pointer, value_type=value_type, preview=preview, bindable=bindable,
        )
        encoded_size = len(json.dumps(candidate.public_view(), ensure_ascii=False).encode("utf-8"))
        if state["bytes"] + encoded_size > _MAX_HANDLE_CATALOG_BYTES:
            state["truncated"] = True
            return
        handles.append(candidate)
        state["bytes"] += encoded_size

        if depth >= _MAX_HANDLE_DEPTH:
            if value_type in ("object", "array") and len(value) > 0:
                state["truncated"] = True
            return
        if value_type == "object":
            for key in sorted(value.keys()):
                child_sensitive = sensitive_ancestor or bool(_SENSITIVE_KEY_RE.search(key))
                visit(value[key], pointer + "/" + _escape_json_pointer_token(key), depth + 1, child_sensitive)
        elif value_type == "array":
            for i, item in enumerate(value):
                visit(item, pointer + f"/{i}", depth + 1, sensitive_ancestor)

    visit(payload, "", 0, False)
    return ResultHandleCatalog(result_id, result_digest, tuple(handles), complete=not state["truncated"], truncated=state["truncated"])


#: schema type -> 相容的 handle JSON value type 集合。只允許
#: 精確語意相容（"number" 額外接受 "integer"，因為 JSON number 涵蓋整數），
#: 不做任何跨型別的寬鬆轉換。object/array 從不出現在這裡——bindable handle
#: 天生排除 container，見 `_HANDLE_SCALAR_TYPES`。
_SCHEMA_TYPE_COMPAT: dict[str, frozenset[str]] = {
    "string": frozenset({"string"}),
    "integer": frozenset({"integer"}),
    "number": frozenset({"integer", "number"}),
    "boolean": frozenset({"boolean"}),
}


def _normalize_key(key: str) -> str:
    return re.sub(r"[-\s]+", "_", key.strip()).lower()


def _leaf_key_from_pointer(pointer: str) -> str | None:
    """從 host-only 的 json_pointer 取出最後一段 key 名稱；array index（純
    數字 token）或 root（空字串）都回傳 None——這兩者從來不是「argument
    name」語意，不該被拿去跟 schema 參數名比對。"""
    if not pointer:
        return None
    last = pointer.rsplit("/", 1)[-1]
    if last.isdigit():
        return None
    return last.replace("~1", "/").replace("~0", "~")


@dataclass(frozen=True)
class BindingCandidate:
    """單一 (action, argument, result, handle) 的綁定候選。
    只有 exact key-name（snake_case normalize 後）+ type-compatible 的
    bindable scalar leaf 才會出現在這裡；root object、container、
    non-bindable（含 sensitive）handle 永遠不會產生 candidate——這是與
    `ResultHandleCatalog`（FactReferenceCatalog，供 observed_facts
    引用、可含 root/container）唯一的交集點，且只單向讀取其 bindable
    handle，FactReferenceCatalog 本身從不被當成 binding 選單教給
    provider。"""

    candidate_id: str
    action_name: str
    argument_name: str
    result_id: str
    result_digest: str
    handle_id: str
    value_type: str
    preview: str
    match_class: str = "EXACT_KEY_TYPE"

    def public_view(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "argument_name": self.argument_name,
            "value_type": self.value_type,
            "preview": self.preview,
            "match_class": self.match_class,
        }


@dataclass(frozen=True)
class BindingCandidateCatalog:
    """對「單一 action + 單一 TrustedActionResult」的
    argument-specific binding candidate 目錄；deterministic，重跑同一份
    handle catalog + schema 永遠得到同一份 candidate 清單（含 candidate_id
    ——由 action_name/argument_name/result_digest/handle_id 的 SHA-256 決定，
    不含隨機成分）。"""

    action_name: str
    result_id: str
    result_digest: str
    candidates: tuple[BindingCandidate, ...]

    def for_argument(self, argument_name: str) -> tuple[BindingCandidate, ...]:
        return tuple(c for c in self.candidates if c.argument_name == argument_name)

    def by_id(self, candidate_id: str) -> BindingCandidate | None:
        return next((c for c in self.candidates if c.candidate_id == candidate_id), None)

    def public_view(self) -> dict[str, Any]:
        return {
            "action_name": self.action_name,
            "result_id": self.result_id,
            "result_digest": self.result_digest,
            "candidates": [c.public_view() for c in self.candidates],
        }


def build_binding_candidate_catalog(
    action_name: str,
    parameters_schema: dict[str, dict[str, Any]],
    handle_catalog: ResultHandleCatalog,
) -> BindingCandidateCatalog:
    """只有已知 `action_name` 之後才建立，僅根據該 action 的
    manifest parameter schema，從 handle catalog 篩出可綁定候選。v1 match
    規則（不做 fuzzy matching、不用 LLM embeddings、不猜 domain 語意）：

    1. handle 必須 `bindable=True`（已排除 root/container/sensitive）。
    2. handle 的 json_pointer 最後一段 key 名稱，snake_case normalize 後
       與某個 argument name 完全相等。
    3. handle 的 JSON value type 與該 argument 的 schema type 相容
       （見 `_SCHEMA_TYPE_COMPAT`）。

    同一個 handle 可能同時符合多個 argument（不同 action 的 schema 命名
    重複時），因此會產生多筆 candidate；`create()` 端的 ambiguity 判斷是
    針對「同一個 argument_name 底下有幾筆 candidate」，不是這裡先驗地去重。
    """
    candidates: list[BindingCandidate] = []
    for handle in handle_catalog.handles:
        if not handle.bindable:
            continue
        leaf_key = _leaf_key_from_pointer(handle.json_pointer)
        if leaf_key is None:
            continue
        normalized_leaf = _normalize_key(leaf_key)
        for argument_name, schema in parameters_schema.items():
            if _normalize_key(argument_name) != normalized_leaf:
                continue
            expected_type = schema.get("type")
            compat = _SCHEMA_TYPE_COMPAT.get(expected_type) if isinstance(expected_type, str) else None
            if compat is None or handle.value_type not in compat:
                continue
            candidate_id = "bc_" + hashlib.sha256(
                f"{action_name}:{argument_name}:{handle.result_digest}:{handle.handle_id}".encode("utf-8")
            ).hexdigest()[:20]
            candidates.append(BindingCandidate(
                candidate_id=candidate_id, action_name=action_name, argument_name=argument_name,
                result_id=handle.result_id, result_digest=handle.result_digest, handle_id=handle.handle_id,
                value_type=handle.value_type, preview=handle.preview,
            ))
    return BindingCandidateCatalog(
        action_name=action_name, result_id=handle_catalog.result_id,
        result_digest=handle_catalog.result_digest, candidates=tuple(candidates),
    )


def resolve_json_pointer(document: Any, pointer: str) -> tuple[bool, Any]:
    """RFC 6901 最小實作。空字串指到整份文件；解析失敗一律回傳
    ``(False, None)``——呼叫端必須 fail-closed，不得猜值。"""
    if pointer == "":
        return True, document
    if not pointer.startswith("/"):
        return False, None
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                return False, None
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


_RESULT_REFERENCE_RE = re.compile(
    r"^action_result:(res_[0-9a-f]{32}):([0-9a-f]{64})#(.*)$"
)


def parse_result_reference(reference: str) -> tuple[str, str, str] | None:
    """evidence-first planner legacy 格式：``action_result:<result_id>:<result_digest>#
    <json-pointer>``。**只保留向下相容讀取／測試用**——evidence-first planner 起新的
    provider prompt 不再教這個格式，新 live path 一律使用
    `parse_handle_reference()`。格式不符一律回傳 None（不猜測、不寬鬆
    修復）。"""
    match = _RESULT_REFERENCE_RE.match(reference)
    if match is None:
        return None
    result_id, digest, pointer = match.groups()
    return result_id, digest, pointer


_HANDLE_REFERENCE_RE = re.compile(
    r"^action_result:(res_[0-9a-f]{32}):([0-9a-f]{64}):handle:(rh_[0-9a-f]{12}_\d+)$"
)


def parse_handle_reference(reference: str) -> tuple[str, str, str] | None:
    """解析 ``action_result:<result_id>:<result_digest>:handle:
    <handle_id>``——provider 只需要提供 handle_id，不再需要自己組 JSON
    Pointer。格式不符一律回傳 None。"""
    match = _HANDLE_REFERENCE_RE.match(reference)
    if match is None:
        return None
    result_id, digest, handle_id = match.groups()
    return result_id, digest, handle_id


def _bounded_str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values[:_MAX_LIST_ITEMS]:
        out.append(str(item)[:_MAX_LIST_ITEM_CHARS])
    return out


class ProposalCreationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResultBinding:
    """follow-up proposal 的單一 argument 值，宣告要用某個
    TrustedActionResult 的某個 JSON Pointer 位置取代。host 端 deterministic
    resolve；不支援字串樣板／`eval`／任何形式的插值執行。"""

    argument_name: str
    result_id: str
    result_digest: str
    json_pointer: str


@dataclass(frozen=True)
class HandleBinding:
    """follow-up proposal 的單一 argument 值，宣告要用某個
    TrustedActionResult 的某個 handle（而不是 JSON Pointer）取代。這是
    provider-facing 的新格式；host deterministic resolve，不
    支援字串樣板／`eval`／任何形式的插值執行。"""

    argument_name: str
    result_id: str
    result_digest: str
    handle_id: str


@dataclass(frozen=True)
class CandidateBinding:
    """follow-up proposal 的單一 argument 值，宣告要用某個
    action-specific `BindingCandidate` 的 `candidate_id`（而不是直接指名
    `handle_id`）。這是 provider-facing 的**主要**格式——provider
    只從 host 教的、已經是 argument-specific 的候選清單裡選一個
    candidate_id，不再需要（也不被允許）自由瀏覽整份 handle catalog 去猜
    哪個 handle 對應哪個 argument。host deterministic 重新驗證這個
    candidate_id 是否存在於當前 turn 重新計算出的 BindingCandidateCatalog
    （見 `ProposalRegistry.create()`），不信任 provider 附帶的任何其他
    欄位。"""

    argument_name: str
    candidate_id: str



def _extract_action_status(result: Any) -> str | None:
    """從 action 的回傳內容取出它**自報**的狀態。

    `execution_status` 只表示「tool 沒有拋例外」。action 自己是否找到可用證據，
    寫在它回傳的 `status`／`support_status` 裡——例如 `explain_run` 找不到可分析
    的 run 時會回 `_failure(SUPPORT_INSUFFICIENT, ...)`，此時 `execution_status`
    仍是 `SUCCEEDED`，只看那一個欄位會被誤導。

    取不到就回 `None`：**不猜、不從 execution_status 反推**。
    """
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(result, dict):
        return None
    for key in ("status", "support_status", "global_support_status"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value[:_MAX_ACTION_STATUS_CHARS]
    return None


@dataclass(frozen=True)
class TaskProposalOwner:
    """建立 task-owned proposal 時的 parent 身分。

    只有 host 端的 `AnalysisTaskService` 會建出這個物件；IPC／LLM 送來的
    序列化 dict 不是這個型別，因此無法用來宣稱 task 所有權。
    """

    task_id: str
    step_id: str
    grant_id: str


class ProposalRegistry:
    """daemon-memory-only proposal state machine。所有 transition 皆在
    單一 lock 內完成，保證 concurrent approve 只有一次成功。"""

    def __init__(self, clock: Any = time.time) -> None:
        self._lock = threading.Lock()
        self._proposals: dict[str, ActionProposal] = {}
        self._trusted_results: dict[str, TrustedActionResult] = {}
        #: session_id -> (result_id, mode)。mode="auto" 由 `/execute`
        #: 成功後自動設定，只注入下一個 Agent turn 就自動清除；
        #: mode="explicit" 由 `/use-result` 設定，持續到被取代或
        #: `/clear-result`。
        self._active_result: dict[str, tuple[str, str]] = {}
        #: result_id -> ResultHandleCatalog，建立於
        #: `_create_trusted_result_locked()`（與 TrustedActionResult 同一
        #: 個 turn 內 deterministic 建立一次，之後只讀不重建）。
        self._handle_catalogs: dict[str, ResultHandleCatalog] = {}
        self._clock = clock

    # ── 內部：TTL 惰性過期（呼叫時機同既有 _prune_pending_package_actions）──

    def _expire_stale_locked(self) -> None:
        now = self._clock()
        for proposal in self._proposals.values():
            if proposal.is_expired(now):
                proposal.status = "EXPIRED"

    def _prune_trusted_results_locked(self, now: float) -> None:
        expired_ids = [rid for rid, r in self._trusted_results.items() if r.is_expired(now)]
        for rid in expired_ids:
            del self._trusted_results[rid]
            self._handle_catalogs.pop(rid, None)
            self._active_result = {
                sid: (result_id, mode) for sid, (result_id, mode) in self._active_result.items()
                if result_id != rid
            }

    @staticmethod
    def _result_scope_matches(result: "TrustedActionResult", context: CanonicalRequestContext) -> bool:
        return (
            result.session_id == context.session_id
            and result.member_id == context.member_id
            and result.project_id == context.project_id
        )

    # ── Creation（Agent turn 內部呼叫，非 operator control path）──────────

    def create(
        self,
        *,
        action_name: str,
        arguments: dict[str, Any],
        reason: str,
        expected_effects: Any = None,
        possible_side_effects: Any = None,
        preconditions: Any = None,
        evidence_references: list[str],
        catalog: list[ProposableCapability],
        context: CanonicalRequestContext,
        risk_class: str = "DANGEROUS",
        workspace: str | None = None,
        result_bindings: list[ResultBinding] | None = None,
        handle_bindings: list[HandleBinding] | None = None,
        candidate_bindings: list[CandidateBinding] | None = None,
        active_result_ref: tuple[str, str] | None = None,
        task_owner: "TaskProposalOwner | None" = None,
    ) -> ActionProposal:
        """建立一筆新 proposal；action 必須存在於當前 turn 的 catalog 內
        （catalog 由呼叫端每次重新從 registry 建立，不信任 provider 自報）。

        evidence-first planner `result_bindings`（legacy，只做向下相容讀取／測試，evidence-first planner 起
        新 provider prompt 不再教這個格式）與 evidence-first planner `handle_bindings`
        （新格式）都宣告某些 argument 值要用某筆 TrustedActionResult 覆寫
        ——host deterministic resolve，不支援樣板／插值；任一 binding 無法
        安全解析都直接拒絕整筆 proposal，不猜值、不部分套用。

        `handle_bindings` 額外規則（evidence-first planner，binding-authoritative
        materialization）：若 `arguments` 裡同一個 argument_name 也有
        provider 自己填的 literal 值，handle 解析出的值是**唯一權威**，
        衝突的 literal 直接被忽略（記錄在 `ignored_literals`，只存
        digest/type，不存原始值）——不因兩者不同而拒絕整筆 proposal，也
        不會用 literal 覆蓋 handle 值。"""
        match = next((c for c in catalog if c.action_name == action_name), None)
        if match is None:
            raise ProposalCreationError("PROPOSAL_ACTION_UNKNOWN")

        # 新 provider schema 把 argument 值分成
        # `literal_arguments`（就是這裡的 `arguments`，legacy 欄位向下相容）
        # 與 `handle_bindings`；host 一律先 normalize 成同一份
        # `resolved_arguments`，再進入 materialization。
        resolved_arguments = dict(arguments)
        argument_provenance: dict[str, str] = {}
        ignored_literals: dict[str, dict[str, Any]] = {}
        prior_result_refs: list[str] = []

        # 只有已知 action_name（=`match`）且呼叫端提供了
        # 目前 active 的 TrustedActionResult scope 時，才 deterministic 建立
        # 這個 action 專屬的 BindingCandidateCatalog——沒有提供
        # `active_result_ref`（或 scope 驗證失敗）時 `binding_catalog` 保持
        # None，candidate_bindings 一律拒絕、必要 argument 的 auto-bind 一律
        # 不觸發（fail-closed，不猜測要用哪個 result）。
        binding_catalog: BindingCandidateCatalog | None = None
        if active_result_ref is not None:
            ref_result_id, ref_result_digest = active_result_ref
            with self._lock:
                self._prune_trusted_results_locked(self._clock())
                record = self._trusted_results.get(ref_result_id)
                if (
                    record is not None
                    and self._result_scope_matches(record, context)
                    and record.result_digest == ref_result_digest
                ):
                    handle_cat = self._handle_catalogs.get(ref_result_id)
                    if handle_cat is not None:
                        binding_catalog = build_binding_candidate_catalog(
                            action_name, match.parameters, handle_cat,
                        )

        # legacy evidence-first planner JSON Pointer binding：維持既有行為（直接覆寫，不做
        # literal-conflict 判斷——這個機制早於 handle-authoritative 規則，
        # 只做向下相容讀取／測試，不重新定義它的語意）。
        for binding in (result_bindings or []):
            value, code = self._resolve_result_binding(binding, context)
            if code is not None:
                raise ProposalCreationError(code)
            resolved_arguments[binding.argument_name] = value
            argument_provenance[binding.argument_name] = "legacy_result_binding"
            if binding.result_id not in prior_result_refs:
                prior_result_refs.append(binding.result_id)

        # 同一個 argument 最多只能有一筆 handle
        # binding——多筆視為 ambiguous，直接拒絕，不猜要用哪一筆。
        seen_handle_arguments: set[str] = set()
        for hb in (handle_bindings or []):
            if hb.argument_name in seen_handle_arguments:
                raise ProposalCreationError("PROPOSAL_DUPLICATE_HANDLE_BINDING")
            seen_handle_arguments.add(hb.argument_name)

        # binding-authoritative materialization——handle 必須
        # 先完整通過驗證（scope/digest/bindable/exact value resolve，任一步
        # 失敗都直接拋出、不 fallback 回 literal）才會覆寫；覆寫時若原本有
        # 衝突的 literal，literal 被忽略（不因值不同整筆拒絕），只記錄
        # provenance／被忽略 literal 的 digest+type（絕不保存原始值）。
        legacy_normalized_arguments: list[str] = []
        for handle_binding in (handle_bindings or []):
            value, code = self.resolve_handle_value(
                handle_binding.result_id, handle_binding.result_digest,
                handle_binding.handle_id, context,
            )
            provenance = "handle"
            # legacy raw handle_id 指到一個真的存在、但
            # non-bindable 的 handle（root object／container／sensitive）時，
            # 若這個 argument 在 host 重新計算出的 BindingCandidateCatalog
            # 裡剛好只有唯一一個真正可綁定的 candidate，host **可以**放棄這個
            # 非法選擇、改用那個唯一 candidate 的 handle 自動修復——但必須
            # explicit audit（見 argument_provenance 標記
            # "legacy_normalized_auto_repair"），且**只**在失敗原因是
            # NOT_BINDABLE 時才嘗試；handle 根本不存在／偽造
            # （RESULT_HANDLE_UNKNOWN）永遠不觸發這個修復路徑，直接拒絕整筆
            # proposal，不得被「唯一 candidate」悄悄蓋過去。
            if code == "RESULT_HANDLE_NOT_BINDABLE" and binding_catalog is not None:
                repair_candidates = binding_catalog.for_argument(handle_binding.argument_name)
                if len(repair_candidates) == 1:
                    repair_candidate = repair_candidates[0]
                    repaired_value, repaired_code = self.resolve_handle_value(
                        repair_candidate.result_id, repair_candidate.result_digest,
                        repair_candidate.handle_id, context,
                    )
                    if repaired_code is None:
                        value, code = repaired_value, None
                        provenance = "legacy_normalized_auto_repair"
                        legacy_normalized_arguments.append(handle_binding.argument_name)
                        handle_binding = HandleBinding(
                            argument_name=handle_binding.argument_name,
                            result_id=repair_candidate.result_id,
                            result_digest=repair_candidate.result_digest,
                            handle_id=repair_candidate.handle_id,
                        )
            if code is not None:
                raise ProposalCreationError(code)
            if handle_binding.argument_name in resolved_arguments:
                literal = resolved_arguments[handle_binding.argument_name]
                if literal != value:
                    ignored_literals[handle_binding.argument_name] = {
                        "digest": hashlib.sha256(
                            json.dumps(literal, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest(),
                        "type": _json_value_type(literal),
                    }
            resolved_arguments[handle_binding.argument_name] = value
            argument_provenance[handle_binding.argument_name] = provenance
            if handle_binding.result_id not in prior_result_refs:
                prior_result_refs.append(handle_binding.result_id)

        # provider-facing 主要格式——只從 host 已算好的
        # action-specific BindingCandidateCatalog 挑 candidate_id，不再自由
        # 瀏覽整份 handle catalog。同一個 argument 不得同時被
        # result_binding／handle_binding／candidate_binding 綁定超過一次。
        for cb in (candidate_bindings or []):
            if cb.argument_name in argument_provenance:
                raise ProposalCreationError("PROPOSAL_DUPLICATE_HANDLE_BINDING")
            if binding_catalog is None:
                raise ProposalCreationError("PROPOSAL_BINDING_CANDIDATE_UNKNOWN")
            candidate = binding_catalog.by_id(cb.candidate_id)
            if candidate is None or candidate.argument_name != cb.argument_name:
                raise ProposalCreationError("PROPOSAL_BINDING_CANDIDATE_UNKNOWN")
            value, code = self.resolve_handle_value(
                candidate.result_id, candidate.result_digest, candidate.handle_id, context,
            )
            if code is not None:
                raise ProposalCreationError(code)
            if cb.argument_name in resolved_arguments:
                literal = resolved_arguments[cb.argument_name]
                if literal != value:
                    ignored_literals[cb.argument_name] = {
                        "digest": hashlib.sha256(
                            json.dumps(literal, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest(),
                        "type": _json_value_type(literal),
                    }
            resolved_arguments[cb.argument_name] = value
            argument_provenance[cb.argument_name] = "candidate"
            if candidate.result_id not in prior_result_refs:
                prior_result_refs.append(candidate.result_id)

        # 仍缺值的**必要** argument，若 host 重新計算出的
        # BindingCandidateCatalog 對這個 argument 恰好只有唯一一筆 candidate
        # → host 自動綁定（provenance="host_auto_bound"）；恰好 0 筆 →
        # 保持缺值，交由下面的 schema 驗證以 PROPOSAL_ARGUMENTS_INVALID 拒絕
        # （等同「要求提供 literal 或直接拒絕」）；2 筆以上 → 明確拒絕整筆
        # proposal（PROPOSAL_BINDING_CANDIDATE_AMBIGUOUS），不得由 host 猜測
        # 要用哪一筆。選填（非 required）argument 永遠不會被這段自動補值。
        if binding_catalog is not None:
            for argument_name, schema in match.parameters.items():
                if not bool(schema.get("required", False)) or argument_name in resolved_arguments:
                    continue
                arg_candidates = binding_catalog.for_argument(argument_name)
                if len(arg_candidates) == 1:
                    auto_candidate = arg_candidates[0]
                    value, code = self.resolve_handle_value(
                        auto_candidate.result_id, auto_candidate.result_digest,
                        auto_candidate.handle_id, context,
                    )
                    if code is None:
                        resolved_arguments[argument_name] = value
                        argument_provenance[argument_name] = "host_auto_bound"
                        if auto_candidate.result_id not in prior_result_refs:
                            prior_result_refs.append(auto_candidate.result_id)
                elif len(arg_candidates) > 1:
                    raise ProposalCreationError("PROPOSAL_BINDING_CANDIDATE_AMBIGUOUS")

        # 對 materialized final arguments（handle 已覆寫衝突 literal 之後）
        # 做 action schema 驗證；resolved type 不符合 schema 一律拒絕。
        if not validate_action_arguments(match.parameters, resolved_arguments):
            raise ProposalCreationError("PROPOSAL_ARGUMENTS_INVALID")
        arguments = resolved_arguments

        resolved_workspace = str(Path(workspace).resolve()) if workspace else str(Path.cwd().resolve())

        with self._lock:
            self._expire_stale_locked()
            pending_count = sum(
                1 for p in self._proposals.values()
                if p.status == "PENDING" and p.session_id == context.session_id
            )
            if pending_count >= _MAX_PENDING_PER_SESSION:
                raise ProposalCreationError("PROPOSAL_PENDING_LIMIT_EXCEEDED")

            now = self._clock()
            expires_at = now + _TTL_SEC
            canonical_args = _canonical_arguments(arguments)
            digest = compute_proposal_digest(
                action_name=action_name, arguments=canonical_args, workspace=resolved_workspace,
                member_id=context.member_id, project_id=context.project_id, session_id=context.session_id,
                risk_class=risk_class, expires_at=expires_at,
            )
            proposal_id = f"prop_{uuid.uuid4().hex}"
            proposal = ActionProposal(
                proposal_id=proposal_id, proposal_digest=digest, action_name=action_name,
                arguments=canonical_args, workspace=resolved_workspace,
                member_id=context.member_id, project_id=context.project_id, session_id=context.session_id,
                reason=str(reason)[:_MAX_REASON_CHARS],
                expected_effects=_bounded_str_list(expected_effects),
                possible_side_effects=_bounded_str_list(possible_side_effects),
                evidence_references=list(evidence_references)[:_MAX_LIST_ITEMS],
                preconditions=_bounded_str_list(preconditions),
                prior_result_refs=prior_result_refs[:_MAX_LIST_ITEMS],
                argument_provenance=dict(list(argument_provenance.items())[:_MAX_LIST_ITEMS]),
                ignored_literals=dict(list(ignored_literals.items())[:_MAX_LIST_ITEMS]),
                risk_class=risk_class, created_at=now, expires_at=expires_at,
                # 唯一能標記 task 所有權的地方。`task_owner` 是
                # host-only 的 dataclass，序列化的 dict 建不出來。
                authorization_source=(
                    AUTHORIZATION_SOURCE_TASK if task_owner is not None
                    else AUTHORIZATION_SOURCE_OPERATOR
                ),
                task_id=task_owner.task_id if task_owner is not None else None,
                step_id=task_owner.step_id if task_owner is not None else None,
                grant_id=task_owner.grant_id if task_owner is not None else None,
            )
            self._proposals[proposal_id] = proposal
            return proposal

    def _resolve_result_binding(
        self, binding: ResultBinding, context: CanonicalRequestContext,
    ) -> tuple[Any, str | None]:
        """deterministic JSON Pointer resolve；任何一步失敗都回傳
        ``(None, <code>)``，呼叫端必須整筆拒絕，不得猜值或部分套用。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            record = self._trusted_results.get(binding.result_id)
            if record is None or not self._result_scope_matches(record, context):
                return None, "PROPOSAL_RESULT_BINDING_UNAVAILABLE"
            if record.result_digest != binding.result_digest:
                return None, "PROPOSAL_RESULT_BINDING_UNAVAILABLE"
            if not record.complete:
                return None, "PROPOSAL_RESULT_BINDING_TRUNCATED"
            payload_text = record.result_payload
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None, "PROPOSAL_RESULT_BINDING_TRUNCATED"
        found, value = resolve_json_pointer(payload, binding.json_pointer)
        if not found:
            return None, "PROPOSAL_RESULT_BINDING_POINTER_NOT_FOUND"
        return value, None

    # ── Operator control path（不得由 provider 呼叫；host 在送往 LLM 前截取）──

    def list_for_scope(self, context: CanonicalRequestContext) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_stale_locked()
            return [
                p.public_view() for p in sorted(self._proposals.values(), key=lambda p: p.created_at)
                if p.session_id == context.session_id and p.member_id == context.member_id
                and p.project_id == context.project_id
            ]

    def get_for_scope(self, proposal_id: str, context: CanonicalRequestContext) -> dict[str, Any] | None:
        with self._lock:
            self._expire_stale_locked()
            proposal = self._proposals.get(proposal_id)
            if proposal is None or not self._scope_matches(proposal, context):
                return None
            return proposal.public_view()

    @staticmethod
    def _scope_matches(proposal: ActionProposal, context: CanonicalRequestContext) -> bool:
        return (
            proposal.session_id == context.session_id
            and proposal.member_id == context.member_id
            and proposal.project_id == context.project_id
        )

    def approve(
        self, proposal_id: str, proposal_digest: str, context: CanonicalRequestContext,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """回傳 (public_view, denial_code)。missing/foreign-scope/wrong-session
        皆回傳同一個 generic denial code，不洩漏 proposal 是否存在。"""
        return self._transition(proposal_id, proposal_digest, context, target="APPROVED")

    def reject(
        self, proposal_id: str, proposal_digest: str, context: CanonicalRequestContext,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return self._transition(proposal_id, proposal_digest, context, target="REJECTED")

    def _transition(
        self, proposal_id: str, proposal_digest: str, context: CanonicalRequestContext,
        *, target: ProposalStatus,
    ) -> tuple[dict[str, Any] | None, str | None]:
        with self._lock:
            self._expire_stale_locked()
            proposal = self._proposals.get(proposal_id)
            if proposal is None or not self._scope_matches(proposal, context):
                return None, "PROPOSAL_NOT_FOUND"
            # task-owned child 不接受任何 operator mutation
            # （approve／reject／execute）。否則使用者可以從 `/proposals`
            # 抓出 child id，在 root cancel／expiry 之後繞過 task service
            # 執行。唯讀顯示不受影響。
            if proposal.is_task_owned:
                return None, TASK_PROPOSAL_CONTROL_DENIED
            if proposal.proposal_digest != proposal_digest:
                return None, "PROPOSAL_DIGEST_MISMATCH"
            if proposal.status != "PENDING":
                return None, f"PROPOSAL_NOT_PENDING_{proposal.status}"
            proposal.status = target
            return proposal.public_view(), None

    # ── One-shot execution state machine ─────────────────────────

    def claim_for_execution(
        self, proposal_id: str, proposal_digest: str, context: CanonicalRequestContext,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """APPROVED → EXECUTING 的 atomic one-shot claim。

        整個檢查與 status mutation 都在單一 lock 內完成——concurrent caller
        只有第一個能看到 status=="APPROVED" 並贏得 claim；其餘所有 caller
        （包含同一批 replay）都會在同一個 lock 內看到 status 已經不是
        APPROVED，回傳 denial，不會有第二次 claim 成功。回傳值只給呼叫方
        （host-side control path）用來建立 proposal-bound 執行 policy，不外洩
        給 provider。"""
        with self._lock:
            self._expire_stale_locked()
            proposal = self._proposals.get(proposal_id)
            if proposal is None or not self._scope_matches(proposal, context):
                return None, "PROPOSAL_NOT_FOUND"
            # task-owned child 只能由 task service 的
            # `claim_for_task_execution()` claim。
            if proposal.is_task_owned:
                return None, TASK_PROPOSAL_CONTROL_DENIED
            return self._claim_locked(proposal, proposal_digest)

    def _claim_locked(
        self, proposal: ActionProposal, proposal_digest: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """APPROVED → EXECUTING 的實際 transition。必須已持有 `self._lock`。"""
        if proposal.proposal_digest != proposal_digest:
            return None, "PROPOSAL_DIGEST_MISMATCH"
        if proposal.status in ("EXECUTING", "SUCCEEDED", "FAILED"):
            return None, "PROPOSAL_ALREADY_EXECUTED"
        if proposal.status != "APPROVED":
            return None, f"PROPOSAL_NOT_APPROVED_{proposal.status}"
        proposal.status = "EXECUTING"
        proposal.execution_status = EXECUTION_STATUS_EXECUTING
        proposal.executed_at = self._clock()
        return proposal.public_view(), None

    # ── task 根授權派生的 host-only 入口 ──────────────────────

    @staticmethod
    def _task_grant_matches(
        proposal: ActionProposal, grant: Any, step_id: str,
        context: CanonicalRequestContext,
    ) -> str | None:
        """檢查 live grant object 是否確實授權這一筆 child proposal。

        `grant` 必須是 task service 記憶體中的 `TaskAuthorizationGrant`
        **物件**；任何 dict／namespace 都不接受。
        """
        from terminux_engine.bounded_execution.analysis_tasks.models import (
            TaskAuthorizationGrant,
        )

        if not isinstance(grant, TaskAuthorizationGrant):
            return TASK_PROPOSAL_CONTROL_DENIED
        if not proposal.is_task_owned:
            return TASK_PROPOSAL_CONTROL_DENIED
        if (proposal.task_id != grant.task_id or proposal.grant_id != grant.grant_id
                or proposal.step_id != step_id):
            return TASK_PROPOSAL_CONTROL_DENIED
        if (grant.scope.member_id != context.member_id
                or grant.scope.project_id != context.project_id):
            return TASK_PROPOSAL_CONTROL_DENIED
        return None

    def approve_from_task_grant(
        self, proposal_id: str, proposal_digest: str, context: CanonicalRequestContext,
        *, grant: Any, step_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """由 live root grant 核准一筆 task-owned child proposal。

        host-only：呼叫端必須交出 task service 目前記憶體中的 grant 物件。
        這裡**不**接受序列化 dict、不接受過期 grant，也不核准任何非
        task-owned 的 proposal。
        """
        with self._lock:
            self._expire_stale_locked()
            proposal = self._proposals.get(proposal_id)
            if proposal is None or not self._scope_matches(proposal, context):
                return None, "PROPOSAL_NOT_FOUND"
            denial = self._task_grant_matches(proposal, grant, step_id, context)
            if denial is not None:
                return None, denial
            if proposal.proposal_digest != proposal_digest:
                return None, "PROPOSAL_DIGEST_MISMATCH"
            if proposal.status != "PENDING":
                return None, f"PROPOSAL_NOT_PENDING_{proposal.status}"
            proposal.status = "APPROVED"
            return proposal.public_view(), None

    def claim_for_task_execution(
        self, proposal_id: str, proposal_digest: str, context: CanonicalRequestContext,
        *, grant: Any, step_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """task service 專用的 one-shot claim；語意與
        `claim_for_execution()` 完全相同，只是額外要求相符的 live grant。"""
        with self._lock:
            self._expire_stale_locked()
            proposal = self._proposals.get(proposal_id)
            if proposal is None or not self._scope_matches(proposal, context):
                return None, "PROPOSAL_NOT_FOUND"
            denial = self._task_grant_matches(proposal, grant, step_id, context)
            if denial is not None:
                return None, denial
            return self._claim_locked(proposal, proposal_digest)

    def finalize_execution(
        self, proposal_id: str, *, status: Literal["SUCCEEDED", "FAILED"], result: Any,
    ) -> dict[str, Any] | None:
        """EXECUTING → SUCCEEDED|FAILED，一次性、不可逆、不可重試。只有
        claim_for_execution 的贏家才會走到這裡；若 proposal 不在 EXECUTING
        （理論上不會發生，因為只有唯一 claim 贏家持有 proposal_id 呼叫這裡），
        安全地 no-op 回傳 None，不覆寫任何其他狀態。

        這是**唯一**建立 TrustedActionResult 的地方——它只在 C2 的
        one-shot execution chain（`ToolRegistry.execute()` 完成後）被呼叫，
        provider／user 沒有任何路徑可以自行觸發。回傳的 dict 額外帶
        ``trusted_result`` 欄位，供呼叫端（`jsonrpc_server._execute_
        approved_proposal()`）在同一次 control path 呼叫內就地取得新建立
        的 result_id，設成本 session 的 auto active result。"""
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        bounded_result = encoded[:_MAX_EXECUTION_RESULT_CHARS]
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.status != "EXECUTING":
                return None
            proposal.status = status
            proposal.execution_status = status
            proposal.execution_result = bounded_result
            proposal.action_status = _extract_action_status(result)
            trusted_result = self._create_trusted_result_locked(proposal, status, result)
            view = proposal.public_view()
            view["trusted_result"] = trusted_result.public_view()
            return view

    # ── TrustedActionResult store ─────────────────────────────────

    def _create_trusted_result_locked(
        self, proposal: ActionProposal, status: Literal["SUCCEEDED", "FAILED"], raw_result: Any,
    ) -> TrustedActionResult:
        now = self._clock()
        full_encoded = json.dumps(raw_result, ensure_ascii=False, sort_keys=True, default=str)
        result_digest = hashlib.sha256(full_encoded.encode("utf-8")).hexdigest()
        truncated = len(full_encoded) > _MAX_TRUSTED_RESULT_PAYLOAD_CHARS
        complete = not truncated
        stored_payload = full_encoded if complete else full_encoded[:_MAX_TRUSTED_RESULT_PAYLOAD_CHARS]
        args_summary = json.dumps(proposal.arguments, ensure_ascii=False, sort_keys=True, default=str)[:500]
        result_id = f"res_{uuid.uuid4().hex}"
        record = TrustedActionResult(
            result_id=result_id, proposal_id=proposal.proposal_id, proposal_digest=proposal.proposal_digest,
            action_name=proposal.action_name, arguments_summary=args_summary,
            member_id=proposal.member_id, project_id=proposal.project_id, session_id=proposal.session_id,
            execution_status=status, result_payload=stored_payload, result_digest=result_digest,
            created_at=now, expires_at=now + _TRUSTED_RESULT_TTL_SEC, complete=complete, truncated=truncated,
        )

        self._prune_trusted_results_locked(now)
        session_results = sorted(
            (r for r in self._trusted_results.values() if r.session_id == proposal.session_id),
            key=lambda r: r.created_at,
        )
        # rule 5：每個 session 最多保留 4 筆（含這一筆），超過先丟最舊的。
        while len(session_results) >= _MAX_TRUSTED_RESULTS_PER_SESSION:
            oldest = session_results.pop(0)
            self._trusted_results.pop(oldest.result_id, None)
            self._handle_catalogs.pop(oldest.result_id, None)
        # rule 6：session 內 aggregate 上限 64 KiB（含這一筆）。
        new_bytes = len(stored_payload.encode("utf-8"))
        while session_results and (
            sum(len(r.result_payload.encode("utf-8")) for r in session_results) + new_bytes
            > _MAX_TRUSTED_RESULT_AGGREGATE_BYTES
        ):
            oldest = session_results.pop(0)
            self._trusted_results.pop(oldest.result_id, None)
            self._handle_catalogs.pop(oldest.result_id, None)

        self._trusted_results[result_id] = record
        # TrustedActionResult 建立的同一刻，host deterministic
        # 建出這筆的 handle catalog 並快取——同一份 payload 永遠產生同一份
        # catalog，之後只讀不重建。
        self._handle_catalogs[result_id] = build_result_handle_catalog(
            result_id, result_digest, stored_payload, complete=complete,
        )
        # rule：/execute 成功後自動把這筆設成本 session 的 active result
        # （mode="auto"，只注入下一個 Agent turn，見 consume_active_result）。
        self._active_result[proposal.session_id] = (result_id, "auto")
        return record

    def list_trusted_results_for_scope(self, context: CanonicalRequestContext) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            return [
                r.public_view() for r in sorted(self._trusted_results.values(), key=lambda r: r.created_at)
                if self._result_scope_matches(r, context)
            ]

    def get_trusted_result_for_scope(
        self, result_id: str, result_digest: str, context: CanonicalRequestContext,
    ) -> dict[str, Any] | None:
        """單一 generic denial（回傳 None）：missing／expired／foreign-scope／
        digest 不符全部視為同一種「不可用」，不對外區分理由。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            record = self._trusted_results.get(result_id)
            if record is None or not self._result_scope_matches(record, context):
                return None
            if record.result_digest != result_digest:
                return None
            return record.public_view()

    # ── ResultHandleCatalog lookup／resolution ───────────────────

    def get_handle_catalog_for_scope(
        self, result_id: str, result_digest: str, context: CanonicalRequestContext,
    ) -> dict[str, Any] | None:
        """回傳 provider-safe 的 handle catalog view（不含任何 json_pointer）。
        單一 generic denial，同 `get_trusted_result_for_scope`。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            record = self._trusted_results.get(result_id)
            if record is None or not self._result_scope_matches(record, context):
                return None
            if record.result_digest != result_digest:
                return None
            catalog = self._handle_catalogs.get(result_id)
            if catalog is None:
                return None
            return catalog.public_view()

    def get_binding_candidate_catalog_for_scope(
        self, action_name: str, parameters_schema: dict[str, dict[str, Any]],
        result_id: str, result_digest: str, context: CanonicalRequestContext,
    ) -> dict[str, Any] | None:
        """供 provider prompt 組裝使用的 provider-safe view
        （只含 candidate_id/argument_name/value_type/preview/match_class，
        不含 handle_id／json_pointer）。單一 generic denial，同
        `get_handle_catalog_for_scope`。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            record = self._trusted_results.get(result_id)
            if record is None or not self._result_scope_matches(record, context):
                return None
            if record.result_digest != result_digest:
                return None
            handle_cat = self._handle_catalogs.get(result_id)
            if handle_cat is None:
                return None
            return build_binding_candidate_catalog(action_name, parameters_schema, handle_cat).public_view()

    def _find_handle_locked(
        self, result_id: str, result_digest: str, handle_id: str, context: CanonicalRequestContext,
    ) -> tuple[ResultHandle | None, str | None]:
        """呼叫端必須已持有 `self._lock`。回傳 (handle_or_None, denial_code)。
        任何一步（scope／digest／handle 是否存在於這筆 result 自己的
        catalog）失敗都回傳明確 denial code，不猜測、不跨 result 混用
        ——即使 caller 提供的 `handle_id` 恰好等於別筆 result 的某個
        handle_id 字面值，也必須先通過同一個 `result_id`/`result_digest`
        的 catalog 才算數。"""
        record = self._trusted_results.get(result_id)
        if record is None or not self._result_scope_matches(record, context):
            return None, "RESULT_REFERENCE_UNAVAILABLE"
        if record.result_digest != result_digest:
            return None, "RESULT_REFERENCE_UNAVAILABLE"
        catalog = self._handle_catalogs.get(result_id)
        if catalog is None:
            return None, "RESULT_REFERENCE_UNAVAILABLE"
        handle = next((h for h in catalog.handles if h.handle_id == handle_id), None)
        if handle is None:
            return None, "RESULT_HANDLE_UNKNOWN"
        # 防禦性重覆確認：handle 必須真的屬於這個 result_id/result_digest
        # （build_result_handle_catalog 保證這點，這裡再驗一次避免未來重構
        # 引入跨 result 混用的風險）。
        if handle.result_id != result_id or handle.result_digest != result_digest:
            return None, "RESULT_HANDLE_UNKNOWN"
        return handle, None

    def validate_handle_reference(
        self, result_id: str, result_digest: str, handle_id: str, context: CanonicalRequestContext,
    ) -> tuple[bool, str | None]:
        """驗證一個 observed_facts reference 指向的 handle
        真的存在於這筆 result 的 catalog——不要求 `bindable`（object/list
        handle 仍可用於 interpretation 引用，只是不能用於 argument
        binding，見 `resolve_handle_value`）。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            _handle, code = self._find_handle_locked(result_id, result_digest, handle_id, context)
            return code is None, code

    def resolve_handle_value(
        self, result_id: str, result_digest: str, handle_id: str, context: CanonicalRequestContext,
    ) -> tuple[Any, str | None]:
        """把一個 handle 解析成實際值，供 argument binding
        使用。只有 `bindable=True`（scalar leaf、非 sensitive）的 handle
        才能拿到值；object/list 或 sensitive 欄位一律拒絕
        （`RESULT_HANDLE_NOT_BINDABLE`），不猜值、不部分套用。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            handle, code = self._find_handle_locked(result_id, result_digest, handle_id, context)
            if handle is None:
                return None, code
            if not handle.bindable:
                return None, "RESULT_HANDLE_NOT_BINDABLE"
            record = self._trusted_results.get(result_id)
            payload_text = record.result_payload if record is not None else None
        if payload_text is None:
            return None, "RESULT_REFERENCE_UNAVAILABLE"
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None, "RESULT_REFERENCE_TRUNCATED"
        found, value = resolve_json_pointer(payload, handle.json_pointer)
        if not found:
            return None, "RESULT_HANDLE_UNKNOWN"
        return value, None

    def set_active_result(
        self, result_id: str, result_digest: str, context: CanonicalRequestContext,
    ) -> bool:
        """`/use-result` 明確選擇：mode="explicit"，持續到被取代或
        `/clear-result`，不會像 auto-active 一樣一次就自動清除。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            record = self._trusted_results.get(result_id)
            if record is None or not self._result_scope_matches(record, context):
                return False
            if record.result_digest != result_digest:
                return False
            self._active_result[context.session_id] = (result_id, "explicit")
            return True

    def clear_active_result(self, context: CanonicalRequestContext) -> None:
        with self._lock:
            self._active_result.pop(context.session_id, None)

    def peek_active_result(self, context: CanonicalRequestContext) -> dict[str, Any] | None:
        """唯讀查看目前 active result（不消耗 auto mode）；供 `/results`／
        除錯使用。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            entry = self._active_result.get(context.session_id)
            if entry is None:
                return None
            result_id, _mode = entry
            record = self._trusted_results.get(result_id)
            if record is None or not self._result_scope_matches(record, context):
                return None
            return record.public_view()

    def consume_active_result_for_turn(self, context: CanonicalRequestContext) -> dict[str, Any] | None:
        """只在**下一個相關 Agent turn**注入 active result——
        mode="auto"（`/execute` 成功後自動設定）讀取一次就清除，避免陳舊
        result 被無限期帶進之後所有不相關的 turn；mode="explicit"
        （`/use-result` 明確選擇）維持不變，直到使用者換一筆或
        `/clear-result`。"""
        with self._lock:
            self._prune_trusted_results_locked(self._clock())
            entry = self._active_result.get(context.session_id)
            if entry is None:
                return None
            result_id, mode = entry
            record = self._trusted_results.get(result_id)
            if record is None or not self._result_scope_matches(record, context):
                self._active_result.pop(context.session_id, None)
                return None
            if mode == "auto":
                del self._active_result[context.session_id]
            return record.public_view()
