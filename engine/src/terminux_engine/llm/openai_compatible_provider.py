# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""通用 OpenAI-compatible LLM provider。

支援任何實作 `/v1/chat/completions` 的服務，包括：
- DeepSeek（https://api.deepseek.com）
- OpenAI 原生（https://api.openai.com）
- Groq、Together AI、本地 vLLM…

設計原則（與 GeminiCompletionProvider 對齊）：
- lazy init：__init__ 不發出任何 HTTP request
- `complete(prompt: str) -> str` 與 `chat(messages, tools) -> dict` 雙介面
- 不引入新 pip 依賴；HTTP 用 Python 標準庫 `urllib`
- `chat()` 回傳扁平 normalized dict，供 `ResponseParser.parse_openai()` 解析
  （與 Gemini 對齊；adapter 為純透傳）
"""

from __future__ import annotations

import json
import getpass
import logging
import math
import multiprocessing
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from terminux_engine.llm.unparsed_tool_call import (
    UnparsedToolCallError,
    detect_unparsed_tool_call,
)

if TYPE_CHECKING:
    from terminux_engine.llm.generation import CompletionResult

_log = logging.getLogger(__name__)

OPENAI_TIMEOUT_ENV = "TERMINUX_OPENAI_TIMEOUT_SEC"
OUTBOUND_PAYLOAD_POLICY_ENV = "TERMINUX_OUTBOUND_PAYLOAD_POLICY_FILE"
DEFAULT_TIMEOUT_SEC = 120.0
_APPROVED_DATA_CLASSES = frozenset({
    "public_gcd_nangate45", "sanitized_n1b_evidence", "bounded_ecc",
    "agent_history", "tool_result", "system_instructions",
})
_FORBIDDEN_REQUEST_PATTERNS = (
    re.compile(r"/(?:home|data|root|etc|var|tmp)/[^\s\"']+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|password|access[_-]?token)\s*[:=]\s*[\"']?[^\s,\"']{4,}",
               re.IGNORECASE),
)


def configured_timeout_sec(*, default: float = DEFAULT_TIMEOUT_SEC) -> float:
    """讀取 OpenAI-compatible 的整體 request deadline。

    空值沿用既有 120 秒；非法值 fail-closed 成預設值並留下 warning，避免 smoke
    因 typo 得到無限制等待。此值是整體 deadline，不只是單次 socket read timeout。
    """
    raw = os.environ.get(OPENAI_TIMEOUT_ENV, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _log.warning("invalid %s; using default timeout", OPENAI_TIMEOUT_ENV)
        return default
    if not math.isfinite(value) or value <= 0 or value > 3600:
        _log.warning("out-of-range %s; using default timeout", OPENAI_TIMEOUT_ENV)
        return default
    return value


@dataclass(frozen=True)
class OpenAICompatibleProviderConfig:
    """OpenAI-compatible API 初始化設定。"""

    api_key: str
    model: str
    base_url: str = "https://api.deepseek.com"
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    #: Runtime composition 啟用時，timeout 會終止擁有 HTTP socket 的 child process，
    #: 不讓 urllib 在 daemon thread 內無限等待或於流程結束後回來復活工作。
    enforce_hard_timeout: bool = False
    max_tokens: int = 4096
    temperature: float = 0.3


class OpenAICompatibleAPIError(RuntimeError):
    """HTTP 非 2xx 或 response 結構異常時 raise。"""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(
            f"OpenAI-compatible API error: status={status_code} body={body[:500]!r}"
        )
        self.status_code = status_code
        self.body = body


class OpenAICompatibleTimeoutError(RuntimeError):
    """整體 request deadline 到期，HTTP worker 已被終止。"""

    #: ChatExpertAdapter 可不依賴特定 provider 型別，仍可安全辨認 timeout。
    is_provider_timeout = True

    def __init__(self, message: str, *, last_stage: str | None = None,
                 stages: tuple[str, ...] = (), send_released: bool = True) -> None:
        super().__init__(message)
        self.last_stage = last_stage
        self.stages = stages
        #: False 只在 parent 確定沒有放行 child 送出時（未放行即逾時，0 bytes）；預設保守視為已放行。
        self.send_released = send_released


class OutboundPayloadRejected(RuntimeError):
    """完整外送內容未列入本機核准清單。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _restricted_opener() -> Any:
    """核准目的地不得透過代理或 HTTP redirect 轉送。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


VIOLATION_PATTERN = "pattern"
VIOLATION_LOCAL_IDENTITY = "local_identity"


def outbound_text_violation(text: str) -> str | None:
    """最終 Gate 的禁止樣式與本機身分規則；回傳違反類型（`pattern`／`local_identity`）或 None。

    `_check_outbound_payload` 與可外送歷史投影共用這一份規則，兩者不會各自漂移。
    """
    for pattern in _FORBIDDEN_REQUEST_PATTERNS:
        if pattern.search(text):
            return VIOLATION_PATTERN
    for value in (getpass.getuser(), socket.gethostname(), os.environ.get("HOME", "")):
        if value and len(value) >= 4 and value.casefold() in text.casefold():
            return VIOLATION_LOCAL_IDENTITY
    return None


def _check_outbound_payload(endpoint: str, model: str, payload: bytes, *,
                            policy: Any = None) -> bool:
    """若設定核准檔，依 policy 的 `approval_mode` 放行同一份序列化 request。

    `exact_digest`（v1 與 v2）：只放行完全相同的 endpoint、model 與 request digest。
    `structural_c2`（v2）：以 host 指定的類別序列、`limits`、禁止樣式與目的地判定，
    不要求 digest；類別只來自 transcript 位置，內容不能自稱類別。

    Profile 模式下 policy 是必要條件：路徑缺失、空白或無法驗證時一律拒絕，
    不得回傳 False 讓呼叫端改走非 restricted transport。

    `policy` 給定時直接使用這份已載入的 policy（同一次請求只讀一次 policy 檔）。
    """
    from terminux_engine.llm.egress_policy import REASON_UNAVAILABLE, EgressPolicyError, load_policy
    from terminux_engine.llm.task_scope import outbound_policy_path, profile_containment_active

    if policy is None:
        path = outbound_policy_path()
        if path is None:
            if profile_containment_active():
                raise OutboundPayloadRejected("outbound payload policy required")
            return False
        if not os.path.isabs(path):
            raise OutboundPayloadRejected("outbound payload policy path must be absolute")
        try:
            policy = load_policy(path)
        except EgressPolicyError as error:
            raise OutboundPayloadRejected(error.reason) from error
        except RecursionError as error:
            raise OutboundPayloadRejected(REASON_UNAVAILABLE) from error
    if policy.endpoint != endpoint or policy.model != model:
        raise OutboundPayloadRejected("outbound destination or model not approved")
    from terminux_engine.llm.generation import sha256_text

    try:
        request_text = payload.decode("utf-8")
    except UnicodeError as error:
        raise OutboundPayloadRejected("outbound payload encoding invalid") from error
    pattern_reason = "C2_PATTERN_DENIED" if policy.structural else "outbound payload contains prohibited content"
    identity_reason = "C2_PATTERN_DENIED:local_identity" if policy.structural else "outbound payload contains local identity"
    violation = outbound_text_violation(request_text)
    if violation == VIOLATION_PATTERN:
        raise OutboundPayloadRejected(pattern_reason)
    if violation == VIOLATION_LOCAL_IDENTITY:
        raise OutboundPayloadRejected(identity_reason)
    if policy.structural:
        _check_structural_request(policy, payload)
        return True
    if sha256_text(request_text) not in policy.allowed_payload_sha256:
        raise OutboundPayloadRejected("complete outbound payload not approved")
    return True


def _check_structural_request(policy: Any, payload: bytes) -> None:
    """`structural_c2` 的類別與上限檢查；在 `check_final_request` 之後執行，body 結構已知合法。

    類別序列由 `task_scope.structural_request_classes` 依 transcript 位置給出；每個類別
    必須在 policy 的 `approved_data_classes` 內。上限：本回合學員文字、歷史字元數與
    回合數（host 投影時計算）、payload 內任何助理文字。
    """
    from terminux_engine.llm import task_scope

    doc = json.loads(payload)
    limits = policy.limits
    for name, _ in task_scope.structural_request_classes(doc):
        if name not in policy.approved_data_classes:
            raise OutboundPayloadRejected(f"C2_CLASS_DENIED:{name}")
    # 歷史投影在 turn 開始時依當時的 policy 計算；policy 在 turn 內被換成另一種模式或
    # 另一個目的地時，這份投影不再適用。
    bound = task_scope.bound_policy_identity()
    if bound is None or not bound.structural or bound.destination != policy.destination:
        raise OutboundPayloadRejected("C2_POLICY_CHANGED")
    messages = doc["messages"]
    learner_text = messages[1].get("content") if len(messages) > 1 else None
    if not isinstance(learner_text, str) or len(learner_text) > limits.learner_text_max_chars:
        raise OutboundPayloadRejected("C2_LIMIT_EXCEEDED:learner_text_max_chars")
    history = json.loads(messages[0]["content"]).get("conversation_history")
    if history is not None:
        if len(history) > limits.history_max_chars:
            raise OutboundPayloadRejected("C2_LIMIT_EXCEEDED:history_max_chars")
        if task_scope.exportable_history_turns() > limits.history_max_turns:
            raise OutboundPayloadRejected("C2_LIMIT_EXCEEDED:history_max_turns")
    for message in messages:
        content = message.get("content") if message.get("role") == "assistant" else None
        if isinstance(content, str) and len(content) > limits.model_output_max_chars:
            raise OutboundPayloadRejected("C2_LIMIT_EXCEEDED:model_output_max_chars")


def _audit_egress(provider: "OpenAICompatibleProvider", policy: Any, *, stage: str, decision: str,
                  reason: str | None, request_index: int, payload: bytes | None,
                  required: bool, event: str = "egress_decision") -> None:
    """`structural_c2` 的本機決策紀錄；`required` 時寫入失敗即拒絕外送（AUDIT_UNAVAILABLE）。"""
    from terminux_engine.llm import egress_audit, task_scope

    classes: tuple[tuple[str, int], ...] = ()
    if payload is not None:
        try:
            classes = task_scope.structural_request_classes(json.loads(payload))
        except Exception:  # noqa: BLE001 - 類別序列取不到時只少記 classes，不影響決策
            classes = ()
    try:
        if required:
            egress_audit.require_hashing_mode()
        egress_audit.record(
            stage=stage, decision=decision, reason_code=reason, request_index=request_index,
            endpoint_host=urllib.parse.urlsplit(provider._endpoint).hostname,
            model=provider._config.model, policy=policy, payload=payload,
            classes=classes, runs=task_scope.tracked_runs(), event=event)
    except Exception as error:  # noqa: BLE001 - 必要紀錄的任何失敗都必須在送出前拒絕
        if required:
            raise OutboundPayloadRejected("AUDIT_UNAVAILABLE") from error
        _log.warning("egress audit record failed stage=%s decision=%s error=%s",
                     stage, decision, type(error).__name__)


def _reject_body_secret(value: Any, secret: str, depth: int = 0) -> None:
    """包含巢狀JSON文字的body只做拒絕檢查；不回傳或記錄秘密。"""
    if depth > 32:
        raise OutboundPayloadRejected("outbound payload nesting not approved")
    if isinstance(value, str):
        if secret in value:
            raise OutboundPayloadRejected("outbound payload contains a credential")
        if value.lstrip().startswith(("{", "[", '"')):
            try:
                nested = json.loads(value)
            except ValueError:
                return
            if nested != value:
                _reject_body_secret(nested, secret, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_body_secret(key, secret, depth + 1)
            _reject_body_secret(item, secret, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_body_secret(item, secret, depth + 1)


class OpenAICompatibleProvider:
    """
    通用 OpenAI-compatible API provider。

    與 `GeminiCompletionProvider` / `LlamaCppCompletionProvider` 直接互換：
    - complete(prompt, *, max_output_tokens=None) -> str
    - complete_with_metadata(prompt, *, max_output_tokens=None) -> CompletionResult
    - chat(messages, tools=None, *, max_output_tokens=None) -> dict（扁平 normalized）
    """

    #: 這個 adapter 會把 per-request output budget 轉成 OpenAI-compatible
    #: 的 `max_tokens`，並從 response 的 `usage` 回收實際用量，因此 router
    #: 可以走 metadata channel。不支援的 provider 保持 False，router 會誠實
    #: 標 `budget_supported=False`，不假裝套用過。
    supports_output_budget: bool = True

    def __init__(self, config: OpenAICompatibleProviderConfig) -> None:
        if not config.api_key:
            raise ValueError("OpenAICompatibleProvider requires non-empty api_key")
        if not config.model:
            raise ValueError("OpenAICompatibleProvider requires non-empty model")
        if not math.isfinite(config.timeout_sec) or config.timeout_sec <= 0:
            raise ValueError("OpenAICompatibleProvider timeout_sec must be positive and finite")
        self._config = config
        # 預先拼好 endpoint；base_url 結尾有無 "/" 都接受
        self._endpoint = config.base_url.rstrip("/") + "/v1/chat/completions"
        endpoint = urllib.parse.urlsplit(self._endpoint)
        _log.info(
            "openai-compatible provider configured model=%s endpoint_scheme=%s "
            "endpoint_host=%s endpoint_port=%s hard_timeout=%s timeout_sec=%s "
            "stream=false max_tokens=%s proxy_configured=%s",
            config.model, endpoint.scheme, endpoint.hostname or "<invalid>",
            endpoint.port or "default", config.enforce_hard_timeout,
            config.timeout_sec, config.max_tokens, bool(urllib.request.getproxies()),
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def complete(self, prompt: str, *, max_output_tokens: int | None = None) -> str:
        """
        Single-shot completion：包成單一 user message 呼叫 chat API。

        `max_output_tokens` 為 per-request output budget；`None` 時沿用
        `OpenAICompatibleProviderConfig.max_tokens`。

        回傳 assistant message content；若 normalize 後無 text → RuntimeError。
        """
        result = self.chat(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            max_output_tokens=max_output_tokens,
        )
        text = result.get("text")
        if text is None or text == "":
            raise RuntimeError(
                f"OpenAI-compatible API returned empty text. result={str(result)[:200]!r}"
            )
        return text

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        max_output_tokens: int | None = None,
    ) -> dict:
        """
        Multi-turn chat with optional OpenAI function calling.

        messages: OpenAI-format message list（直接 forward）
        tools: OpenAI function calling schema list；None / 空 list → 不送 tools 欄位

        回傳扁平 normalized dict（供 `ResponseParser.parse_openai` 解析）::

            {
                "tool_calls": [
                    {"id": "call_xxx", "name": "...", "arguments": dict | str},
                    ...
                ],
                "text": "..."  # 或 None
            }
        """
        raw = self._post(self._build_request(messages, tools, max_output_tokens))
        return _normalize_response(raw)

    def complete_with_metadata(
        self, prompt: str, *, max_output_tokens: int | None = None
    ) -> "CompletionResult":
        """與 `complete()` 相同的請求，但一併回傳 vendor-neutral completion metadata。

        文字內容與 `complete()` 完全一致——這裡**不做任何本地裁切**，
        回傳的就是 provider 給的 content。

        `usage` 缺席或欄位不完整時，對應欄位誠實回 `None`；**不推估**。
        """
        from terminux_engine.llm.generation import (
            FINISH_MAX_OUTPUT_TOKENS, FINISH_OTHER, FINISH_STOP, FINISH_UNAVAILABLE,
            CompletionMetadata, CompletionResult, sha256_text,
        )

        effective = (max_output_tokens if max_output_tokens is not None
                     else self._config.max_tokens)
        raw = self._post(self._build_request(
            [{"role": "user", "content": prompt}], None, max_output_tokens))
        normalized = _normalize_response(raw)
        text = normalized.get("text")
        if text is None or text == "":
            raise RuntimeError(
                f"OpenAI-compatible API returned empty text. result={str(normalized)[:200]!r}"
            )

        raw_finish = _first_choice_finish_reason(raw)
        # OpenAI-compatible 的 finish_reason 詞彙；未知值一律歸 OTHER，
        # 缺席才是 UNAVAILABLE。不對任何特定廠商特判。
        finish = {"stop": FINISH_STOP, "length": FINISH_MAX_OUTPUT_TOKENS,
                  }.get((raw_finish or "").lower(),
                        FINISH_OTHER if raw_finish else FINISH_UNAVAILABLE)
        usage = _usage_tokens(raw)

        return CompletionResult(
            text=text,
            metadata=CompletionMetadata(
                provider="openai_compatible", model=self._config.model,
                requested_output_tokens=max_output_tokens,
                effective_output_tokens=effective, budget_supported=True,
                finish_reason=finish, provider_finish_reason=raw_finish,
                response_chars=len(text), response_sha256=sha256_text(text),
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                total_tokens=usage["total_tokens"],
            ),
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    def _build_request(
        self, messages: list[dict], tools: list[dict] | None,
        max_output_tokens: int | None,
    ) -> dict[str, Any]:
        """每次呼叫都建一份**全新**的 request body。

        刻意不共用任何 mutable 狀態：並行 request 之間不會互相污染
        （`max_tokens` 只是這一份 dict 的區域值）。
        """
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": _normalize_messages_for_openai(messages),
            "max_tokens": (max_output_tokens if max_output_tokens is not None
                           else self._config.max_tokens),
            "temperature": self._config.temperature,
        }
        if tools:
            body["tools"] = [_wrap_tool(t) for t in tools]
        from terminux_engine.llm.task_scope import (
            DEEPSEEK_NON_THINKING_ENDPOINT, DEEPSEEK_NON_THINKING_MODEL, NON_THINKING_CONTROL,
        )

        if (self._endpoint == DEEPSEEK_NON_THINKING_ENDPOINT
                and self._config.model == DEEPSEEK_NON_THINKING_MODEL):
            body["thinking"] = dict(NON_THINKING_CONTROL)
        return body

    # ── Internal ──────────────────────────────────────────────────────────────

    def _post(self, body: dict) -> dict:
        """POST 到 `/v1/chat/completions`，回傳 JSON-decoded dict。"""
        payload = json.dumps(body).encode("utf-8")
        from terminux_engine.llm.task_scope import (
            TaskScopeRejected, bound_policy_identity, check_final_request,
            current_request_index, ensure_context_epoch_current, ensure_turn_policy_current, outbound_policy_path,
            policy_identity, profile_containment_active, register_request, retire_turn_epoch,
            revoke_context_on_authority_failure, turn_started_without_policy,
        )
        contained = profile_containment_active()
        # turn 開始時（`bind_conversation`）讀到的 policy 是本 turn 的外送契約：turn 內與
        # tool continuation 之間都不得換成另一種核准模式或目的地；turn 開始時讀不到則本 turn
        # 沒有外送授權。目前或 turn 開始時任一方是 `structural_c2`，每次進入最終 Gate 的請求
        # 都要留下本機決策紀錄；policy 身分只用於 audit 與請求預算，放行與否仍由下面的 Gate 決定。
        policy = policy_identity() if contained else None
        bound = bound_policy_identity() if contained else None
        unauthorized_turn = contained and turn_started_without_policy()
        audit_policy = next((p for p in (policy, bound) if p is not None and p.structural), None)
        audit_required = audit_policy is not None
        request_index = 0
        try:
            if contained or os.environ.get(OUTBOUND_PAYLOAD_POLICY_ENV):
                _reject_body_secret(body, self._config.api_key)
            if policy is not None and (unauthorized_turn or (bound is not None and (
                    bound.structural != policy.structural or bound.destination != policy.destination
                    or bound.sha256 != policy.sha256))):
                # 這次讀到的 policy 不是 turn 開始時的那一份（或 turn 開始時讀不到；讀取點已結束本 turn 的
                # epoch）：以 C2 碼拒絕，不再計入請求預算、不再進 Gate（Gate 會以同樣的理由拒絕同一份請求）。
                raise OutboundPayloadRejected("C2_POLICY_CHANGED")
            request_index = register_request(policy)
            if contained and policy is None and outbound_policy_path() is not None:
                # 上方讀不到 policy（外送上下文 epoch 已在讀取點結束）：以精確原因拒絕；若此時
                # 又讀得到，代表檔案在同一次請求內被替換，不得改用另一份 policy 送出。
                # 路徑未設定則由下方 Gate 以 EGRESS_POLICY_REQUIRED 拒絕。
                _check_outbound_payload(self._endpoint, self._config.model, payload)
                raise OutboundPayloadRejected("C2_POLICY_CHANGED")
            check_final_request(payload)
            restricted = _check_outbound_payload(
                self._endpoint, self._config.model, payload, policy=policy
            )
            # 目前的 policy 自身放行之後，仍須與 turn 開始時讀到的是同一份（同 revision）：例如 structural
            # turn 內被換成恰好核准此 payload 的 digest policy，或只改了上限。歷史是依 turn 開始時的 policy 選出的。
            if unauthorized_turn or (bound is not None and (bound.structural != policy.structural
                                                            or bound.destination != policy.destination
                                                            or bound.sha256 != policy.sha256)):
                raise OutboundPayloadRejected("C2_POLICY_CHANGED")
            if contained and not restricted:
                # transport 選擇與 policy 判定同源：Profile 模式只允許 restricted transport。
                raise OutboundPayloadRejected("outbound payload policy required")
        except (TaskScopeRejected, OutboundPayloadRejected) as error:
            if contained and policy is None:
                # policy 權威讀不到：先結束外送上下文 epoch，再完成拒絕（讀取點已結束一次，這裡確保順序）。
                # 不在 turn 內的請求（例如 REQUEST_SCOPE_MISSING）不是權威失敗，不結束 epoch。
                revoke_context_on_authority_failure()
            elif contained and (unauthorized_turn or (bound is not None and policy.sha256 != bound.sha256)):
                # turn 內讀到不同 revision（或 turn 開始時讀不到）：只結束本 turn 的 epoch
                # （同 session 較新的 epoch 不受影響）。
                retire_turn_epoch()
            if audit_required:
                _audit_egress(self, audit_policy, stage="final_gate", decision="DENY", reason=str(error),
                              request_index=max(request_index, current_request_index()),
                              payload=None, required=False)
            raise
        if audit_required:
            _audit_egress(self, audit_policy, stage="transport", decision="ALLOW", reason=None,
                          request_index=request_index, payload=payload, required=True)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(
            url=self._endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )

        msg_count = len(body.get("messages") or [])
        tool_count = len(body.get("tools") or [])
        endpoint = urllib.parse.urlsplit(self._endpoint)
        _log.info(
            "openai-compatible POST endpoint_host=%s model=%s messages=%d tools=%d "
            "payload_bytes=%d max_tokens=%s stream=false proxy_configured=%s",
            endpoint.hostname or "<invalid>", self._config.model, msg_count, tool_count,
            len(payload), body.get("max_tokens"), bool(urllib.request.getproxies()),
        )
        started = time.monotonic()
        stages: tuple[str, ...] = ()
        outcome = "ok"

        def _context_revoked() -> bool:
            try:
                ensure_context_epoch_current()
            except TaskScopeRejected:
                return True
            except Exception:  # noqa: BLE001 - 只用於紀錄標籤；不得遮蔽 transport 的原結果
                return False
            return False

        def _note_outcome() -> None:
            if audit_required:
                reason = outcome
                if outcome not in _NOT_SENT_OUTCOMES and _context_revoked():
                    # 送出前核對之後才登記的撤銷：這份請求已放行（可能已送出 bytes，無法召回），只能記錄；
                    # 同一 turn 之後的請求會在最終 Gate 被拒（不重試）。
                    reason = f"{outcome};revoked_after_send_release"
                # 送出前取消（0 bytes）記為 DENY，避免與已送出的 transport 結果混淆。
                decision = "DENY" if outcome in _NOT_SENT_OUTCOMES else "ALLOW"
                _audit_egress(self, audit_policy, stage="transport", decision=decision, reason=reason,
                              request_index=request_index, payload=None, required=False,
                              event="transport_outcome")

        def _before_send() -> None:
            # 第一個 byte 之前的最後核對：重讀 policy 必須仍是 turn 開始時那一份；放行之後才登記的撤銷
            # 在這裡取消這份請求，不重試、不 fallback。
            nonlocal outcome
            try:
                ensure_turn_policy_current()
                ensure_context_epoch_current()
            except TaskScopeRejected:
                outcome = "revoked_before_send"
                _note_outcome()
                raise
            except Exception:
                outcome = "presend_check_failed"
                _note_outcome()
                raise

        try:
            if self._config.enforce_hard_timeout:
                status, raw_bytes, stages = _post_with_hard_deadline(
                    endpoint=self._endpoint, headers=headers, payload=payload,
                    timeout_sec=self._config.timeout_sec, restricted=restricted,
                    before_send=_before_send if contained else None)
            else:
                opener = _restricted_opener() if restricted else None
                if contained:
                    _before_send()
                with (opener.open(req, timeout=self._config.timeout_sec)
                      if opener else urllib.request.urlopen(req, timeout=self._config.timeout_sec)) as resp:
                    status = resp.status
                    raw_bytes = resp.read()
        except urllib.error.HTTPError as e:
            _elapsed = round(time.monotonic() - started, 2)
            outcome = f"http_error:{e.code}"
            _note_outcome()
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            if restricted:
                _log.warning("openai-compatible POST failed status=%d elapsed=%ss",
                             e.code, _elapsed)
                raise OpenAICompatibleAPIError(e.code, "[redacted]") from e
            _log.warning(
                "openai-compatible POST failed status=%d elapsed=%ss body=%.300s",
                e.code, _elapsed, err_body,
            )
            raise OpenAICompatibleAPIError(e.code, err_body) from e
        except urllib.error.URLError as e:
            _elapsed = round(time.monotonic() - started, 2)
            outcome = "url_error"
            _note_outcome()
            _log.warning(
                "openai-compatible POST connection failed elapsed=%ss reason=%s",
                _elapsed, e.reason,
            )
            raise RuntimeError(
                f"OpenAI-compatible API connection failed: {e.reason}"
            ) from e
        except TaskScopeRejected:
            # `_before_send` 已記錄；沒有任何 byte 送出。
            raise
        except OpenAICompatibleTimeoutError as error:
            _elapsed = round(time.monotonic() - started, 2)
            # child 停在放行點時逾時：沒有放行、沒有連線（0 bytes）
            outcome = "timeout_before_send" if not error.send_released else "timeout"
            _note_outcome()
            _log.warning(
                "openai-compatible POST timed out elapsed=%ss deadline=%ss; "
                "HTTP worker terminated last_stage=%s stages=%s",
                _elapsed, self._config.timeout_sec,
                error.last_stage or "unavailable", error.stages,
            )
            raise
        except RuntimeError as exc:
            if outcome == "presend_check_failed":
                raise  # `_before_send` 已記錄；沒有任何 byte 送出
            _elapsed = round(time.monotonic() - started, 2)
            outcome = f"error:{type(exc).__name__}"
            _note_outcome()
            _log.warning("openai-compatible POST failed elapsed=%ss error=%s", _elapsed,
                         type(exc).__name__)
            raise

        _elapsed = round(time.monotonic() - started, 2)
        outcome = f"status:{status}"
        _note_outcome()
        if status < 200 or status >= 300:
            _log.warning(
                "openai-compatible POST non-2xx status=%d elapsed=%ss", status, _elapsed
            )
            raise OpenAICompatibleAPIError(
                status, "[redacted]" if restricted else raw_bytes.decode("utf-8", errors="replace")
            )

        _log.info(
            "openai-compatible POST ok status=%d elapsed=%ss response_bytes=%d stages=%s",
            status, _elapsed, len(raw_bytes), stages,
        )

        try:
            return json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"OpenAI-compatible API returned invalid JSON: {e}"
            ) from e


#: transport_outcome 中確定沒有放行送出（0 bytes）的結果；以 DENY 記錄。
_NOT_SENT_OUTCOMES = frozenset({"revoked_before_send", "presend_check_failed", "timeout_before_send"})


def _safe_worker_send(result_pipe: Any, message: tuple[Any, ...]) -> None:
    """parent deadline 已到而關 pipe 時，child 不因回報失敗留下 traceback。"""
    try:
        result_pipe.send(message)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _instrument_worker_network_stages(result_pipe: Any) -> None:
    """在 worker 內記錄不含 endpoint／IP／header 的傳輸階段。"""
    def stage(name: str) -> None:
        _safe_worker_send(result_pipe, ("stage", name, time.monotonic()))

    original_getaddrinfo = socket.getaddrinfo
    original_create_connection = socket.create_connection
    original_wrap_socket = ssl.SSLContext.wrap_socket
    original_send = http.client.HTTPConnection.send
    original_begin = http.client.HTTPResponse.begin

    def observed_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        stage("dns_lookup_start")
        try:
            result = original_getaddrinfo(*args, **kwargs)
        except BaseException:
            stage("dns_lookup_failed")
            raise
        stage("dns_lookup_complete")
        return result

    def observed_create_connection(*args: Any, **kwargs: Any) -> Any:
        stage("tcp_connect_start")
        try:
            result = original_create_connection(*args, **kwargs)
        except BaseException:
            stage("tcp_connect_failed")
            raise
        stage("tcp_connect_complete")
        return result

    def observed_wrap_socket(self: ssl.SSLContext, *args: Any, **kwargs: Any) -> Any:
        stage("tls_handshake_start")
        try:
            result = original_wrap_socket(self, *args, **kwargs)
        except BaseException:
            stage("tls_handshake_failed")
            raise
        stage("tls_handshake_complete")
        return result

    def observed_send(self: http.client.HTTPConnection, *args: Any, **kwargs: Any) -> Any:
        stage("request_write_start")
        try:
            result = original_send(self, *args, **kwargs)
        except BaseException:
            stage("request_write_failed")
            raise
        stage("request_write_complete")
        return result

    def observed_begin(self: http.client.HTTPResponse, *args: Any, **kwargs: Any) -> Any:
        stage("response_headers_wait")
        try:
            result = original_begin(self, *args, **kwargs)
        except BaseException:
            stage("response_headers_failed")
            raise
        stage("response_headers_received")
        return result

    socket.getaddrinfo = observed_getaddrinfo
    socket.create_connection = observed_create_connection
    ssl.SSLContext.wrap_socket = observed_wrap_socket
    http.client.HTTPConnection.send = observed_send
    http.client.HTTPResponse.begin = observed_begin


def _hard_deadline_worker(
    endpoint: str, headers: dict[str, str], payload: bytes, timeout_sec: float,
    result_pipe: Any, restricted: bool, go_pipe: Any = None,
) -> None:
    """獨立 process 內做單一 HTTP POST；parent 可在 deadline 強制關閉 socket。

    `go_pipe` 給定時，建立 opener 之後回報 `awaiting_send_release` 並等待 parent 的 `"send"`；收到其他值、
    pipe 關閉或逾時都不建立連線（parent 在這裡做送出前的最後核對，視窗不含 child 啟動時間）。
    """
    _instrument_worker_network_stages(result_pipe)
    _safe_worker_send(result_pipe, ("stage", "worker_started", time.monotonic()))
    try:
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        # opener（含 TLS context）在等待放行之前建立：放行到第一個 byte 之間只剩連線與寫入。
        opener = _restricted_opener() if restricted else None
        if go_pipe is not None:
            _safe_worker_send(result_pipe, ("stage", "awaiting_send_release", time.monotonic()))
            try:
                go = go_pipe.recv() if go_pipe.poll(timeout_sec) else None
            except (EOFError, OSError):
                go = None
            finally:
                go_pipe.close()
            if go != "send":
                _safe_worker_send(result_pipe, ("aborted",))
                return
            _safe_worker_send(result_pipe, ("stage", "send_released", time.monotonic()))
        with (opener.open(request, timeout=timeout_sec)
              if opener else urllib.request.urlopen(request, timeout=timeout_sec)) as response:
            _safe_worker_send(result_pipe, ("stage", "response_body_read_start", time.monotonic()))
            raw = response.read()
            _safe_worker_send(result_pipe, ("stage", "response_body_read_complete", time.monotonic()))
            _safe_worker_send(result_pipe, ("ok", int(response.status), raw))
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        _safe_worker_send(result_pipe, ("http_error", int(error.code), body))
    except urllib.error.URLError as error:
        _safe_worker_send(result_pipe, ("url_error", str(error.reason)))
    except BaseException as error:  # worker 必須把故障交回 parent；不能靜默卡住。
        _safe_worker_send(result_pipe, ("error", type(error).__name__, str(error)[:500]))
    finally:
        result_pipe.close()


def _post_with_hard_deadline(
    *, endpoint: str, headers: dict[str, str], payload: bytes, timeout_sec: float,
    restricted: bool = False, before_send: Callable[[], None] | None = None,
) -> tuple[int, bytes, tuple[str, ...]]:
    """以可終止 child process 實作 end-to-end deadline，確保不留晚到 request。

    `before_send` 給定時，child 準備好之後（`awaiting_send_release`）先停在連線之前；parent 在此呼叫
    `before_send()`，它拋出時 child 不建立連線、例外原樣拋給呼叫端，否則才放行 child 送出。
    """
    context = multiprocessing.get_context("spawn")
    receive_pipe, send_pipe = context.Pipe(duplex=False)
    go_receive, go_send = context.Pipe(duplex=False) if before_send is not None else (None, None)
    worker = context.Process(
        target=_hard_deadline_worker,
        args=(endpoint, headers, payload, timeout_sec, send_pipe, restricted, go_receive), daemon=True,
    )
    worker.start()
    send_pipe.close()
    if go_receive is not None:
        go_receive.close()
    stages: list[str] = []
    message: tuple[Any, ...] | None = None
    # parent 端的放行事實：一旦送出 "send"（或沒有放行握手），child 就可能已連線並送出 bytes。
    released = before_send is None
    deadline = time.monotonic() + timeout_sec
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if not receive_pipe.poll(remaining):
                break
            try:
                candidate = receive_pipe.recv()
            except EOFError:
                raise RuntimeError("OpenAI-compatible worker exited without a result") from None
            if candidate[0] == "stage":
                stages.append(str(candidate[1]))
                if candidate[1] == "awaiting_send_release" and go_send is not None and before_send is not None:
                    try:
                        before_send()
                    except BaseException:
                        try:
                            go_send.send("abort")
                        except OSError:
                            pass  # child 已結束：同樣不會送出
                        go_send.close()
                        go_send = None
                        worker.join(timeout=5)
                        if worker.is_alive():
                            worker.kill()
                            worker.join(timeout=5)
                        raise
                    if time.monotonic() >= deadline:
                        # 核對期間已超過總期限：不放行，交給下方的逾時流程終止 child
                        try:
                            go_send.send("abort")
                        except OSError:
                            pass
                        go_send.close()
                        go_send = None
                        break
                    released = True
                    try:
                        go_send.send("send")
                    except OSError:
                        pass  # child 已結束：下一次 recv 以「沒有結果」回報
                    go_send.close()
                    go_send = None
                continue
            message = candidate
            break
        if message is None:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
            last_stage = stages[-1] if stages else None
            raise OpenAICompatibleTimeoutError(
                f"OpenAI-compatible API request timed out after {timeout_sec}s",
                last_stage=last_stage, stages=tuple(stages), send_released=released)
        worker.join(timeout=5)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
            raise RuntimeError("OpenAI-compatible worker did not exit after response")
    finally:
        receive_pipe.close()
        if go_send is not None:
            go_send.close()
        if worker.is_alive() and message is None:
            # 例外（含 pipe 故障）離開時不留下仍可能連線的 child
            worker.terminate()
            worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
    if message is None:  # pragma: no cover - defensive；上面已處理 timeout。
        raise RuntimeError("OpenAI-compatible worker produced no result")
    kind = message[0]
    if kind == "aborted":  # child 等不到放行（parent 未放行即結束），沒有建立連線
        raise RuntimeError("OpenAI-compatible worker did not receive the send release")
    if kind == "ok":
        return int(message[1]), bytes(message[2]), tuple(stages)
    if kind == "http_error":
        raise OpenAICompatibleAPIError(int(message[1]), str(message[2]))
    if kind == "url_error":
        raise RuntimeError(f"OpenAI-compatible API connection failed: {message[1]}")
    raise RuntimeError(f"OpenAI-compatible worker failed: {message[1]}")


# ─── Helpers（module-level，方便單元測試）─────────────────────────────────────


def _wrap_tool(tool: dict) -> dict:
    """允許 caller 傳「已包好 {"type": "function", "function": {...}}」
    或「裸 function schema」兩種格式，統一輸出 OpenAI tool entry。"""
    if isinstance(tool, dict) and tool.get("type") == "function" and "function" in tool:
        return tool
    return {"type": "function", "function": tool}


def _normalize_messages_for_openai(messages: list[dict]) -> list[dict]:
    """把 AgentRunner 內部使用的扁平 message 格式轉成 OpenAI 嚴格規格。

    AgentRunner 寫回 assistant.tool_calls 時用扁平 dict
    （`{"id","name","arguments":dict}`）——這格式 Gemini provider 在
    `_convert_messages_to_contents` 內部能消化，但 OpenAI / DeepSeek 規格要求：

        {
            "id": "call_xxx",
            "type": "function",
            "function": {"name": "...", "arguments": "json-string"}
        }

    另外 OpenAI 要求 `arguments` 是 JSON 字串而非 dict；`tool` role 的
    `content` 也必須是字串。本函式做這些 normalize，避免 provider 把扁平
    格式原樣送出去被 server 400 reject。

    回傳新 list（不 mutate 原 messages）。
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            new_calls = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                # 已是 OpenAI 原生格式（有 type / function）→ 略過轉換
                if tc.get("type") == "function" and "function" in tc:
                    new_calls.append(tc)
                    continue
                args = tc.get("arguments")
                if isinstance(args, dict):
                    args_str = json.dumps(args, ensure_ascii=False)
                elif isinstance(args, str):
                    args_str = args
                else:
                    args_str = json.dumps(args, ensure_ascii=False) if args is not None else "{}"
                new_calls.append(
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": args_str,
                        },
                    }
                )
            # OpenAI 規格：帶 tool_calls 的 assistant message content 必須存在
            # （可為 null 或字串）。AgentRunner 沒設 content，預填 None 即可
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content"),
                    "tool_calls": new_calls,
                }
            )
        elif role == "tool":
            content = m.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            entry = {
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": content,
            }
            # OpenAI 規格不需要 name 欄位（但容許）；保留以方便除錯
            if "name" in m:
                entry["name"] = m["name"]
            out.append(entry)
        else:
            out.append(m)
    return out


def _first_choice_finish_reason(raw: dict) -> str | None:
    """OpenAI-compatible `choices[0].finish_reason`；缺席回 None（不猜）。"""
    choices = raw.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    return reason if isinstance(reason, str) and reason else None


#: OpenAI-compatible `usage` 的欄位名 → vendor-neutral 欄位名。
#: reasoning token 不是 base spec 的一部分，放在 `completion_tokens_details`
#: 底下；沒有就是 None，不推估。
_USAGE_FIELD_MAP = {
    "input_tokens": "prompt_tokens",
    "output_tokens": "completion_tokens",
    "total_tokens": "total_tokens",
}


def _usage_tokens(raw: dict) -> dict[str, int | None]:
    """把 provider 的 `usage` 映射成 vendor-neutral token 欄位。

    `usage` 缺席、不是 dict、或某個欄位不是整數時，該欄位回 `None`——
    **誠實回報不可得，不推估、不補 0**（補 0 會讓 budget 以為沒用量）。
    """
    out: dict[str, int | None] = {k: None for k in _USAGE_FIELD_MAP}
    out["reasoning_tokens"] = None
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return out
    for neutral, vendor in _USAGE_FIELD_MAP.items():
        value = usage.get(vendor)
        # bool 是 int 的子類別，明確排除
        if isinstance(value, int) and not isinstance(value, bool):
            out[neutral] = value
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
        if isinstance(reasoning, int) and not isinstance(reasoning, bool):
            out["reasoning_tokens"] = reasoning
    return out


def _normalize_response(raw: dict) -> dict:
    """OpenAI-compatible `chat.completion` response → 扁平 normalized dict。

    - 沒有 choices                 → 視為空回應（tool_calls=[], text=None）
    - tool_calls[].arguments 是 JSON 字串 → 嘗試 parse 成 dict；失敗保留原 str
    - content 夾帶未解析的 tool call 標記 → raise UnparsedToolCallError
    """
    choices = raw.get("choices") or []
    if not choices:
        return {"tool_calls": [], "text": None}

    msg = choices[0].get("message", {}) or {}
    content = msg.get("content")
    tool_calls_raw = msg.get("tool_calls") or []

    # 推論服務沒解析的 tool call 會落在 content；不能當成回答顯示。
    family = detect_unparsed_tool_call(content if isinstance(content, str) else None)
    if family is not None:
        _log.warning("openai_compatible response carries unparsed tool call markup: %s", family)
        raise UnparsedToolCallError(family)

    tool_calls: list[dict] = []
    for tc in tool_calls_raw:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "")
        # arguments 在 OpenAI spec 是 JSON 字串；嘗試 parse，失敗保留原字串
        # 讓下游 ResponseParser._coerce_arguments 處理
        if isinstance(args_raw, str):
            try:
                args: Any = json.loads(args_raw) if args_raw else {}
                if not isinstance(args, dict):
                    args = args_raw  # 非 dict（list/scalar）保留原字串
            except json.JSONDecodeError:
                _log.warning(
                    "OpenAI tool_call arguments not valid JSON: %s", args_raw[:200]
                )
                args = args_raw
        else:
            args = args_raw if isinstance(args_raw, dict) else {}

        tool_calls.append(
            {
                "id": tc.get("id", "") if isinstance(tc, dict) else "",
                "name": name,
                "arguments": args,
            }
        )

    result = {
        "tool_calls": tool_calls,
        "text": content if content else None,
    }
    finish_reason = _first_choice_finish_reason(raw)
    if finish_reason is not None:
        result["finish_reason"] = finish_reason
    return result
