# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""明確啟用的任務能力與 request 來源邊界；未設定時保留既有行為。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator

from terminux_engine import learner_strict
from terminux_engine.ipc.request_context import RequestContextMissingError, get_current_request_context
from terminux_engine.llm.egress_policy import (
    TIMING_PATH_EVIDENCE_CLASS,
    EgressPolicy,
    EgressPolicyError,
    load_policy,
)

PROFILE_ENV = "TERMINUX_AGENT_TASK_PROFILE_FILE"
OUTBOUND_POLICY_ENV = "TERMINUX_OUTBOUND_PAYLOAD_POLICY_FILE"
#: DeepSeek API 預設開啟 thinking；只有官方 endpoint 與此 model 的精確組合才送出關閉控制。
DEEPSEEK_NON_THINKING_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_NON_THINKING_MODEL = "deepseek-flash"
NON_THINKING_CONTROL: dict[str, str] = {"type": "disabled"}
_FINAL_BODY_KEYS = frozenset({"model", "messages", "max_tokens", "temperature", "tools"})
_INSTRUCTIONS = ("Answer only the current user's question using the explicitly supplied public text "
                 "and this conversation. For an explicit orchestration run, call "
                 "query_orchestration_evidence before interpreting its results. "
                 "Report unavailable or partial evidence honestly. Never execute commands, "
                 "invent evidence, or use other sessions. Respond in English.")
#: Profile 含 `run_binding` 時 system prompt 的第四個欄位說明（host 常數，不含任何使用者文字）。
_BOUND_RUNS_DESCRIPTION = ("Runs the learner bound in this session with /bind-run, valid when this turn "
                           "started. When the learner refers to the run they bound, use the listed "
                           "orchestration_run_id for evidence queries.")


class TaskScopeRejected(RuntimeError):
    """任務來源或能力未獲允許；錯誤僅包含穩定的來源代碼。"""


@dataclass(frozen=True)
class RunBindingPolicy:
    """管理端在 Profile 檔內明示的動態 Run 綁定規則；缺少此鍵即不提供綁定。

    四個欄位全部必填，程式不提供任何預設值：`max_runs_per_session`（1..8）、
    `require_runner`（receipt `adapter_metadata.runner` 必須相等）、
    `allowed_designs`（`platform/design` 字串清單）、`max_age_sec`（receipt
    `finished_at` 距今的秒數上限）。
    """

    max_runs_per_session: int
    require_runner: str
    allowed_designs: tuple[str, ...]
    max_age_sec: int


@dataclass(frozen=True)
class TaskProfile:
    profile_id: str
    tools: tuple[str, ...]
    context_sections: tuple[str, ...]
    allowed_runs: tuple[tuple[str, int], ...]
    run_binding: RunBindingPolicy | None = None
    #: Profile 檔 bytes 的 SHA256；Run 綁定以它判定「綁定時的 Profile 是否仍是現在這一份」。
    profile_sha256: str = ""

    @property
    def allowed_run_ids(self) -> tuple[str, ...]:
        return tuple(run for run, _ in self.allowed_runs)


@dataclass(frozen=True)
class _Scope:
    profile: TaskProfile
    session_id: str
    system_prompt: str
    schemas: tuple[str, ...]
    denied_names: tuple[str, ...]
    #: `activate()` 時快照一次的有效 Run（靜態 `allowed_runs` ∪ 本 session 綁定）；
    #: turn 內所有強制點只讀這份快照，綁定或撤銷在下一個 turn 才生效。
    effective_runs: tuple[tuple[str, int], ...] = ()
    #: 由綁定加入的 Run 在綁定當下權威投影列的 content hash；turn 內每次取用
    #: evidence 都必須與它一致（靜態 `allowed_runs` 沒有釘選）。
    run_pins: tuple[tuple[str, str], ...] = ()
    #: 本 turn 內由 daemon 行程內可信層（evidence tool 的 executor）宣告 integrity veto 的 run：
    #: 該 run 的任何 evidence 結果在進入 transcript 前一律 `EVIDENCE_INTEGRITY_DENIED`。
    #: 放在 scope 物件而不是 ContextVar：tool executor 在 `copy_context()` 的 worker thread
    #: 內執行，ContextVar 的寫入不會回到呼叫端；同一個 scope 物件則被兩邊共享。
    #: 這是 caller 不可見的私有狀態——不進回應、transcript、request 或 audit payload。
    vetoed_runs: set[str] = field(default_factory=set, compare=False, repr=False)

    @property
    def run_pin_map(self) -> dict[str, str]:
        return dict(self.run_pins)

    @property
    def effective_run_ids(self) -> tuple[str, ...]:
        return tuple(run for run, _ in self.effective_runs)

    @property
    def effective_run_map(self) -> dict[str, int]:
        return dict(self.effective_runs)


_active: ContextVar[_Scope | None] = ContextVar("terminux_task_scope", default=None)

#: Profile 模式下允許的 completion backend：受 TaskScope 最終 Gate 約束的
#: openai_compatible，以及不連網的本機 llama_cpp。
CONTAINED_COMPLETION_BACKENDS = frozenset({"openai_compatible", "llama_cpp", "none"})
#: Profile 模式下允許的 embedding backend：不連網者。
CONTAINED_EMBEDDING_BACKENDS = frozenset({"local", "noop"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def profile_containment_active() -> bool:
    """daemon 以 Profile 模式啟動時為真；Learner 發行版（`learner_strict.LEARNER_STRICT`）一律為真。

    只看 env 是否存在，不看檔案能否載入：檔案缺失或無效時 Agent 已
    fail-closed，其餘遠端出口也必須同樣關閉，不能因載入失敗而恢復外送。
    """
    return learner_strict.LEARNER_STRICT or os.environ.get(PROFILE_ENV) is not None


def outbound_policy_path() -> str | None:
    """外送 policy 檔路徑；未設定、空字串或純空白一律視為未設定。

    policy 是否存在的判定與最終 transport 的選擇都必須經過這個函式：兩處若各自
    解讀同一個 env 值（truthiness 與 strip 後為空），純空白值會讓前者放行、後者
    視為沒有 policy，進而走非 restricted transport。
    """
    raw = os.environ.get(OUTBOUND_POLICY_ENV)
    if raw is None:
        return None
    path = raw.strip()
    return path or None


def contain_router_config(config: Any) -> Any:
    """Profile 模式下把 router 中的遠端 completion／embedding 換成不可用。"""
    from terminux_engine.llm.router_config import EmbeddingProviderConfig, ProviderConfig

    if not profile_containment_active():
        return config
    slots = {}
    for name in ("realtime", "background", "analysis"):
        provider = getattr(config, name)
        if provider.backend in CONTAINED_COMPLETION_BACKENDS:
            slots[name] = provider
        else:
            slots[name] = ProviderConfig(backend="none")
    embedding = config.embedding
    if embedding.backend not in CONTAINED_EMBEDDING_BACKENDS:
        embedding = EmbeddingProviderConfig(backend="noop")
    return config.model_copy(update={**slots, "embedding": embedding})


def rag_embedding_allowed() -> bool:
    """RAG embedding 在 Profile 模式下只允許明確指定的本機 backend。"""
    if not profile_containment_active():
        return True
    return os.environ.get("TERMINUX_EMBEDDING_BACKEND", "").strip().lower() == "local"


def loopback_url(url: str) -> bool:
    """URL 的 host 是否為本機 loopback。"""
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def load_profile() -> TaskProfile | None:
    """只接受本人0600普通檔；非法或未知政策不降級。

    檔案存在但讀不到或無效時，拋出之前結束目前 request session 的外送上下文 epoch
    （見 `revoke_context_on_authority_failure`）；在 turn 內讀到與 turn 開始時不同的 Profile 也一樣。
    Learner 發行版沒有設定 Profile 時同樣視為權威讀不到，以 `TASK_PROFILE_REQUIRED` 拒絕。
    """
    raw = os.environ.get(PROFILE_ENV)
    if raw is None:
        if not learner_strict.LEARNER_STRICT:
            return None
        revoke_context_on_authority_failure()
        raise TaskScopeRejected("TASK_PROFILE_REQUIRED:llm.task_scope.load_profile")
    try:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                raise ValueError()
            raw_bytes = stream.read(65537)
        if len(raw_bytes) > 65536:
            raise ValueError()
        doc = json.loads(raw_bytes.decode("utf-8"))
        # `run_binding` 是唯一的選填鍵；其餘鍵集合必須完全相等（舊 daemon 讀到新鍵
        # 仍是 TASK_POLICY_INVALID，由 schema 本身保證升級順序）。
        if not isinstance(doc, dict) or set(doc) - {"run_binding"} != {
            "profile_id", "tools", "context_sections", "allowed_runs"
        }:
            raise ValueError()
        if not isinstance(doc["profile_id"], str) or not doc["profile_id"].strip():
            raise ValueError()
        for name in ("tools", "context_sections"):
            values = doc[name]
            if (not isinstance(values, list) or not all(isinstance(v, str) and v for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError()
        if not set(doc["context_sections"]) <= {"conversation_history"}:
            raise ValueError()
        # 目前已定義的任務能力僅為唯讀run evidence；新增能力須定義其資料契約。
        if not set(doc["tools"]) <= {"query_orchestration_evidence"}:
            raise ValueError()
        runs = doc["allowed_runs"]
        if (not isinstance(runs, dict) or not all(isinstance(k, str) and k.startswith("run_")
                and k.replace("_", "").isalnum() and type(v) is int and v > 0 for k, v in runs.items())):
            raise ValueError()
        binding = _parse_run_binding(doc["run_binding"]) if "run_binding" in doc else None
        profile = TaskProfile(doc["profile_id"], tuple(doc["tools"]),
                              tuple(doc["context_sections"]), tuple(sorted(runs.items())),
                              run_binding=binding,
                              profile_sha256=hashlib.sha256(raw_bytes).hexdigest())
    except Exception:  # noqa: BLE001 - 任何讀取／解析失敗都是「權威讀不到」，不只已知的例外型別
        # 權威讀不到：先結束本 session 的外送上下文 epoch，再完成拒絕（之後改回相同 bytes 也不復活）。
        revoke_context_on_authority_failure()
        raise TaskScopeRejected("TASK_POLICY_INVALID:llm.task_scope.load_profile") from None
    _retire_if_changed_in_turn("profile", profile.profile_sha256)
    return profile


def _parse_run_binding(value: Any) -> RunBindingPolicy:
    """`run_binding` 四鍵全部必填且範圍固定；任何缺漏或型別錯誤都讓整份 Profile 無效。"""
    if not isinstance(value, dict) or set(value) != {
        "max_runs_per_session", "require_runner", "allowed_designs", "max_age_sec"
    }:
        raise ValueError()
    limit = value["max_runs_per_session"]
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError()
    runner = value["require_runner"]
    if not isinstance(runner, str) or not runner.strip() or runner != runner.strip():
        raise ValueError()
    designs = value["allowed_designs"]
    if (not isinstance(designs, list) or not designs or len(set(designs)) != len(designs)
            or not all(isinstance(item, str) and item.count("/") == 1
                       and all(part and part.replace("_", "").replace("-", "").isalnum()
                               for part in item.split("/")) for item in designs)):
        raise ValueError()
    age = value["max_age_sec"]
    if type(age) is not int or not 1 <= age <= 31536000:
        raise ValueError()
    return RunBindingPolicy(limit, runner, tuple(designs), age)


def revoke_context_on_authority_failure() -> None:
    """Profile 模式下 Profile 或外送 policy 權威讀不到、無法驗證或在 turn 內改變時呼叫。

    無條件結束目前 request session 的外送上下文 epoch：先前回合的內容是在一份已無法證明的授權下
    選出的，之後即使檔案改回相同 bytes 也不再外送（epoch 只前進）。沒有 Profile env 或沒有
    request context（daemon 啟動檢查）時沒有可結束的 session。
    """
    if not profile_containment_active():
        return
    try:
        context = get_current_request_context()
    except RequestContextMissingError:
        return
    from terminux_engine.llm import context_epoch, run_binding

    run_binding.retire_session_epoch(context.session_id, context_epoch.REASON_AUTHORITY_UNAVAILABLE)


def _retire_if_changed_in_turn(kind: str, sha256: str) -> None:
    """turn 內（`bind_conversation` 已記錄 turn 開始的身分列）任何讀取點讀到不同的 Profile／policy：
    結束 turn 開始時的 epoch（若它仍是目前 epoch）。之後恢復成 turn 開始時的 bytes 也不會讓它復活；
    同 session 已在新授權下開始的較新 epoch 不受影響。讀不到則由 `revoke_context_on_authority_failure`
    無條件結束目前 epoch。

    turn 開始時讀不到（身分列為空字串）也是 turn 開始時的身分：turn 內之後讀到的任何有效檔案都是改變。
    """
    bound = _bound_epoch.get()
    if bound is None or bound[0] is None:
        return
    observed = next((row[2] for row in bound[1] if row[0] == f"~{kind}"), None)
    if observed is not None and observed != sha256:
        _retire_bound_epoch()


def policy_identity() -> EgressPolicy | None:
    """以最終 Gate 相同的身分條件讀取目前的外送 policy 檔；讀不到或形狀無效回 None。

    這裡不拒絕請求：policy 缺失或無效時由最終 Gate 以精確碼拒絕。回傳值只用於
    歷史投影的 revision／destination、請求預算與 audit 身分。Profile 模式下讀不到時（任何例外）
    先結束目前 request session 的外送上下文 epoch；turn 內任何讀取點讀到與 turn 開始時不同的
    有效 revision，結束本 turn 的 epoch（之後恢復相同 bytes 也不復活）。
    """
    path = outbound_policy_path()
    if path is None:
        revoke_context_on_authority_failure()
        return None
    try:
        policy = load_policy(path)
    except Exception:  # noqa: BLE001 - 任何讀取／解析失敗都是「權威讀不到」
        revoke_context_on_authority_failure()
        return None
    _retire_if_changed_in_turn("policy", policy.sha256)
    return policy


def loaded_authority_identity() -> dict[str, str | None]:
    """本程序此刻以最終 Gate 相同的 loader 驗證成功的 Profile／policy 身分；只含雜湊。

    `profile_sha256`／`policy_sha256` 是驗證成功的檔案 bytes 雜湊（與 `TaskProfile.profile_sha256`、
    `EgressPolicy.sha256` 同源）；`*_source_sha256` 是 env 中路徑字串的雜湊。對應檔案沒有設定、
    讀不到或無效時兩者皆為 None。不回傳路徑或內容。

    daemon 在 listener 就緒時以自己的程序計算並公告（`main._runtime_identity`），launcher 以自己的
    env 計算預期值後比對。daemon 之後每個 turn 仍依同一路徑重讀，公告值只是就緒當下的快照。
    只在 turn 之外呼叫（daemon 啟動、launcher）。
    """
    try:
        profile = load_profile()
    except TaskScopeRejected:
        profile = None
    policy = None
    policy_path = outbound_policy_path()
    if policy_path is not None and os.path.isabs(policy_path):
        try:
            policy = load_policy(policy_path)
        except Exception:  # noqa: BLE001 - 任何讀取／解析失敗都是「沒有可公告的身分」
            policy = None
    return {
        "profile_sha256": profile.profile_sha256 if profile is not None else None,
        "profile_source_sha256": (_source_fingerprint(os.environ.get(PROFILE_ENV))
                                  if profile is not None else None),
        "policy_sha256": policy.sha256 if policy is not None else None,
        "policy_source_sha256": _source_fingerprint(policy_path) if policy is not None else None,
    }


def _source_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def exportable_history_snapshot() -> str | None:
    """`bind_conversation()` 為本 session 計算的可外送歷史快照。

    不依賴 `current()`：prompt 組裝發生在 `activate()` 之前，當時尚無 scope。
    沒有綁定或綁定不屬於目前 session → `CONVERSATION_SOURCE_MISSING`。
    """
    origin = _origin.get()
    context = get_current_request_context()
    if origin is None or origin[0] != context.session_id:
        raise TaskScopeRejected("CONVERSATION_SOURCE_MISSING:ipc.jsonrpc_server")
    return origin[2]


def exportable_history_turns() -> int:
    """快照內的回合數（host 計算）；無綁定時為 0。"""
    origin = _origin.get()
    if origin is None:
        return 0
    return origin[3]


def bound_policy_identity() -> EgressPolicy | None:
    """`bind_conversation()` 當下讀到的 policy 身分；寫入歷史的回合以它標記。"""
    return _bound_identity.get()


def bound_context_epoch() -> str | None:
    """`bind_conversation()` 當下本 session 的外送上下文 epoch；寫入歷史的回合以它標記。"""
    bound = _bound_epoch.get()
    return bound[0] if bound is not None else None


def turn_started_without_policy() -> bool:
    """Profile turn 開始時（`bind_conversation`）讀不到外送 policy：本 turn 沒有外送授權。

    之後在同一 turn 內讀到的任何有效 policy（包括逐位元組相同的檔案、恰好核准這份 body 的 digest
    policy）都不放行；下一個 turn 重新判定。不在 Profile turn 內時為 False。
    """
    return _bound_epoch.get() is not None and _bound_identity.get() is None


def current_context_epoch() -> str:
    """目前 request session 的外送上下文 epoch（綁定中取 turn 開始時的值）。"""
    bound = bound_context_epoch()
    if bound is not None:
        return bound
    from terminux_engine.llm import context_epoch

    return context_epoch.current(get_current_request_context().session_id)


def history_status() -> dict[str, Any] | None:
    """`bind_conversation()` 為本 turn 計算的歷史投影摘要（只有計數，不含任何內容）。

    只有 `structural_c2` 投影會產生摘要；其餘情況回傳 None。
    """
    status = _history_status.get()
    return {**status, "withheld": dict(status["withheld"])} if status is not None else None


def history_text_exportable(text: str, denied_names: tuple[str, ...] = ()) -> bool:
    """一段歷史文字放進最終請求後，是否仍會通過最終 Gate 的內容規則。

    同時檢查原文與它在最終 body 內的樣子（system prompt 以 `ensure_ascii=False` 序列化、
    再整份以 `json.dumps` 序列化，跳脫字元會改變樣式的比對結果）：未授權能力名稱
    （`casefold` 子字串，同 `check_final_request`）、禁止樣式與本機身分
    （`openai_compatible_provider.outbound_text_violation`）。任一形式違反即不可外送。
    """
    from terminux_engine.llm.openai_compatible_provider import outbound_text_violation

    serialized = json.dumps(json.dumps(text, ensure_ascii=False)[1:-1])[1:-1]
    for form in (text, serialized):
        folded = form.casefold()
        if any(name.casefold() in folded for name in denied_names):
            return False
        if outbound_text_violation(form) is not None:
            return False
    return True


def _bound_runs_field(runs: tuple[tuple[str, int], ...], pins: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """綁定 Run 的身分欄位：只列由綁定加入、turn 開始時仍有效的 Run 與 attempt（不含靜態清單）。"""
    attempts = dict(runs)
    return {"description": _BOUND_RUNS_DESCRIPTION,
            "runs": [{"orchestration_run_id": run, "attempt": attempts[run]} for run, _ in pins]}


def build_system_prompt(profile: TaskProfile, history: str | None) -> str:
    """只渲染明確允許的同stream對話；不附加隱含背景或能力目錄。

    Profile 含 `run_binding` 規則時另加 `bound_runs`：本 turn 開始時有效的綁定 Run 身分
    （與 `activate()` 的有效 Run 快照同一來源），讓模型能以既有唯讀 Evidence Tool 查詢學員綁定的 Run。
    """
    doc: dict[str, Any] = {
        "profile_id": profile.profile_id, "instructions": _INSTRUCTIONS,
        "conversation_history": history if "conversation_history" in profile.context_sections else None}
    if profile.run_binding is not None:
        doc["bound_runs"] = _bound_runs_field(*_effective_runs_and_pins(profile))
    return json.dumps(doc, ensure_ascii=False)


def current() -> _Scope | None:
    return _active.get()


@contextmanager
def activate(profile: TaskProfile | None, system_prompt: str,
             schemas: list[dict[str, Any]], denied_names: tuple[str, ...] = (), query: str = "") -> Iterator[None]:
    """scope只存於request ContextVar，結束或例外時還原。"""
    if profile is None:
        yield
        return
    context = get_current_request_context()
    origin = _origin.get()
    if origin is None or origin[0] != context.session_id:
        raise TaskScopeRejected("CONVERSATION_SOURCE_MISSING:ipc.jsonrpc_server")
    runs, pins = _effective_runs_and_pins(profile)
    keys = {"profile_id", "instructions", "conversation_history"}
    if profile.run_binding is not None:
        keys.add("bound_runs")
    try:
        doc = json.loads(system_prompt)
        if (not isinstance(doc, dict) or set(doc) != keys
                or ("bound_runs" in keys and doc["bound_runs"] != _bound_runs_field(runs, pins))
                or doc["profile_id"] != profile.profile_id or doc["instructions"] != _INSTRUCTIONS
                or (doc["conversation_history"] is not None and not isinstance(doc["conversation_history"], str))
                or (doc["conversation_history"] and "conversation_history" not in profile.context_sections)
                or doc["conversation_history"] != (origin[2] if "conversation_history" in profile.context_sections else None)):
            raise ValueError()
    except (ValueError, TypeError):
        raise TaskScopeRejected("CONTEXT_SOURCE_DENIED:llm.agent_prompt") from None
    token = _active.set(_Scope(profile, context.session_id, system_prompt,
                               tuple(_canonical(t) for t in schemas), denied_names,
                               effective_runs=runs, run_pins=pins))
    transcript_token = _transcript.set(tuple(_canonical(m) for m in (
        {"role": "system", "content": system_prompt}, {"role": "user", "content": query})))
    classes_token = _classes.set((("system_instructions",), ("learner_text",)))
    calls_token = _calls.set(())
    requests_token = _requests.set(0)
    try:
        yield
    finally:
        _requests.reset(requests_token)
        _calls.reset(calls_token)
        _classes.reset(classes_token)
        _transcript.reset(transcript_token)
        _active.reset(token)


def effective_runs(profile: TaskProfile) -> tuple[tuple[str, int], ...]:
    """有效 Run 的唯一來源：靜態 `allowed_runs` ∪ 本 session 仍有效的綁定。

    綁定由 `llm.run_binding` 以 EventStore 權威驗證後寫入；這裡只讀取。沒有
    `run_binding` 規則的 Profile 永遠只有靜態清單。
    """
    return _effective_runs_and_pins(profile)[0]


def _effective_runs_and_pins(profile: TaskProfile) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, str], ...]]:
    """有效 Run 與綁定 Run 的 content hash 釘選；同一 Run 若已在靜態清單，以靜態設定為準、不釘選。"""
    merged: dict[str, int] = dict(profile.allowed_runs)
    pins: dict[str, str] = {}
    if profile.run_binding is not None:
        from terminux_engine.llm.run_binding import bound_pins_for_current_session

        # 只有 turn 開始時（`bind_conversation`）記錄為 valid 的綁定可在本 turn 使用：之後才新增的、
        # 已失效、被判污染或權威讀不到的綁定都不提供 evidence。
        bound = _bound_epoch.get()
        observed = ({(run, attempt, digest) for run, attempt, digest, state in bound[1]
                     if state == "valid" and not run.startswith("~")} if bound is not None else None)
        for run, attempt, content_hash in bound_pins_for_current_session(profile):
            if observed is not None and (run, attempt, content_hash) not in observed:
                continue
            if run not in merged:
                merged[run] = attempt
                pins[run] = content_hash
    return tuple(sorted(merged.items())), tuple(sorted(pins.items()))


def _final_body_keys(doc: Any) -> frozenset[str]:
    """最終 request 可出現的頂層欄位。

    `thinking` 只在 turn 開始時綁定的管理端 policy 精確核准 DeepSeek 官方 endpoint 與該 model，
    且值恰為關閉控制時才接受。provider 實際的 endpoint 與 model 由 `_check_outbound_payload`
    以同一份 policy 逐字比對，所以 body 無法自稱目的地。
    """
    identity = _bound_identity.get()
    if (isinstance(doc, dict) and identity is not None
            and identity.endpoint == DEEPSEEK_NON_THINKING_ENDPOINT
            and identity.model == DEEPSEEK_NON_THINKING_MODEL
            and doc.get("model") == DEEPSEEK_NON_THINKING_MODEL
            and doc.get("thinking") == NON_THINKING_CONTROL):
        return _FINAL_BODY_KEYS | {"thinking"}
    return _FINAL_BODY_KEYS


def check_final_request(payload: bytes) -> None:
    """檢查最終HTTP body的完整schema、source與session；不修改任何bytes。

    正常返回只表示「此刻」可放行；送出第一個 byte 之前，呼叫端仍須以 `ensure_turn_policy_current()`
    重讀 policy、以 `ensure_context_epoch_current()` 核對放行之後才登記的撤銷
    （`OpenAICompatibleProvider._post` 兩者都已這樣做）。
    """
    profile = load_profile()
    if profile is None:
        return
    if outbound_policy_path() is None:
        revoke_context_on_authority_failure()
        raise TaskScopeRejected("EGRESS_POLICY_REQUIRED:llm.task_scope")
    scope = current()
    if scope is None or scope.profile != profile or get_current_request_context().session_id != scope.session_id:
        if scope is not None and scope.profile != profile:
            # 啟用之後 Profile 才改變：同樣是授權改變，先結束本 turn 的 epoch 再拒絕。
            _retire_bound_epoch()
        raise TaskScopeRejected("REQUEST_SCOPE_MISSING:llm.task_scope")
    _check_context_epoch(profile)
    text = payload.decode("utf-8")
    if any(name.casefold() in text.casefold() for name in scope.denied_names):
        raise TaskScopeRejected("CAPABILITY_CATEGORY_DENIED:llm.tool_registry")
    doc = json.loads(payload)
    if (not isinstance(doc, dict) or not set(doc) <= _final_body_keys(doc)
            or not isinstance(doc.get("messages"), list) or not doc["messages"]):
        raise TaskScopeRejected("REQUEST_SCHEMA_DENIED:llm.openai_compatible_provider")
    if doc["messages"][0] != {"role": "system", "content": scope.system_prompt}:
        raise TaskScopeRejected("CONTEXT_SOURCE_DENIED:llm.agent_prompt")
    for tool in doc.get("tools", []):
        if _canonical(tool) not in scope.schemas:
            raise TaskScopeRejected("TOOL_SCHEMA_DENIED:llm.tool_registry")
    if tuple(_canonical(m) for m in doc["messages"]) != _transcript.get():
        raise TaskScopeRejected("MESSAGE_SOURCE_DENIED:llm.agent_runner")
    allowed = set(profile.tools)
    for index, message in enumerate(doc["messages"]):
        if (not isinstance(message, dict) or not set(message) <= {"role", "content", "tool_calls", "tool_call_id", "name"}
                or message.get("role") not in {"system", "user", "assistant", "tool"}
                or (index > 0 and message.get("role") == "system")):
            raise TaskScopeRejected("MESSAGE_CATEGORY_DENIED:llm.agent_runner")
        if message.get("role") == "tool" and message.get("name") not in allowed:
            raise TaskScopeRejected("TOOL_RESULT_DENIED:llm.agent_runner")
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            if function.get("name") not in allowed:
                raise TaskScopeRejected("TOOL_CALL_DENIED:llm.agent_runner")
            args = json.loads(function.get("arguments", "{}"))
            if set(args) != {"orchestration_run_id"} or args["orchestration_run_id"] not in scope.effective_run_ids:
                raise TaskScopeRejected("RUN_SCOPE_DENIED:llm.agent_runner")


def _check_context_epoch(profile: TaskProfile) -> None:
    """最終 Gate 的撤銷檢查（每一份請求的放行點）。

    先以 turn 開始時的 projector 重新核對綁定 Run 的權威列，再於綁定鎖內一次取得「epoch＋綁定狀態」：
    epoch 已結束（解除／替換綁定、session 撤銷、evidence 被拒）或 turn 開始時有效的綁定已不再有效，
    這份請求即拒絕。放行之後才完成的撤銷不作用於這份已放行的請求；已送出的 bytes 無法召回。
    """
    from terminux_engine.llm import context_epoch, run_binding

    bound = _bound_epoch.get()
    context = get_current_request_context()
    if bound is None or bound[0] is None:
        raise TaskScopeRejected("CONTEXT_EPOCH_REVOKED:llm.context_epoch")
    if next((row[2] for row in bound[1] if row[0] == "~policy"), None) == "":
        # turn 開始時 policy 讀不到：本 turn 沒有外送授權。之後讀到的任何有效 policy（相同 bytes、
        # 恰好核准這份 body 的 digest policy、另一種模式）都不是本 turn 的授權，不得降級放行。
        # 在讀取權威之前判定，拒絕碼不受之後讀取點（可能已結束 epoch）的影響。
        run_binding.retire_epoch_if_current(context.session_id, bound[0], context_epoch.REASON_REVOKED_AT_GATE)
        raise TaskScopeRejected("C2_POLICY_CHANGED:llm.task_scope")
    unavailable = run_binding.revalidate_authority(profile, _bound_projector.get(), context=context)
    epoch, state = run_binding.epoch_snapshot(profile, context=context, unavailable=unavailable)
    if epoch != bound[0]:
        raise TaskScopeRejected("CONTEXT_EPOCH_REVOKED:llm.context_epoch")
    # 歷史是在 turn 開始時、以當時的 Profile 選出的；本 turn 啟用的 Profile 必須是同一份。
    # turn 內的 policy 變更見函式末段（結束 epoch，拒絕碼由 `_post` 的 C2 判定）。
    observed_profile = next((row[2] for row in bound[1] if row[0] == "~profile"), None)
    if (observed_profile != profile.profile_sha256
            or context_epoch.revokes(run_binding.binding_rows(bound[1]), run_binding.binding_rows(state))):
        # 在放行點偵測到的撤銷同樣結束本 epoch，不因之後恢復而讓舊回合復活。
        run_binding.retire_epoch_if_current(context.session_id, bound[0], context_epoch.REASON_REVOKED_AT_GATE)
        current_state = {(run, attempt, digest): row_state
                         for run, attempt, digest, row_state in run_binding.binding_rows(state)}
        lost = [current_state.get((run, attempt, digest))
                for run, attempt, digest, row_state in run_binding.binding_rows(bound[1]) if row_state == "valid"
                and current_state.get((run, attempt, digest)) != "valid"]
        if observed_profile == profile.profile_sha256 and lost and all(
                row_state == run_binding.STATE_UNAVAILABLE for row_state in lost):
            # 失去效力的綁定全部只是權威暫時讀不到：給學員可辨識的碼，而不是「授權已撤銷」。
            raise TaskScopeRejected("CONTEXT_AUTHORITY_UNAVAILABLE:llm.run_binding")
        raise TaskScopeRejected("CONTEXT_EPOCH_REVOKED:llm.context_epoch")
    observed_policy = next((row[2] for row in bound[1] if row[0] == "~policy"), "")
    current_policy = next((row[2] for row in state if row[0] == "~policy"), "")
    if observed_policy and current_policy != observed_policy:
        # turn 開始時 policy 有效、這次讀到不同 revision（或讀不到）：本 turn 的歷史不再適用，結束 epoch
        # 並拒絕。呼叫端自己讀到的 policy 若也不同，`_post` 會先以 C2_POLICY_CHANGED 拒絕。
        run_binding.retire_epoch_if_current(context.session_id, bound[0], context_epoch.REASON_REVOKED_AT_GATE)
        raise TaskScopeRejected("CONTEXT_EPOCH_REVOKED:llm.context_epoch")


def ensure_turn_policy_current() -> None:
    """送出第一個 byte 之前重讀外送 policy：必須仍是 turn 開始時的同一份（模式、revision、目的地）。

    讀不到、turn 開始時就讀不到，或讀到另一份有效 policy，都結束本 turn 的 epoch 並拒絕
    （`C2_POLICY_CHANGED`）。不在 Profile turn 內時不核對。這個核對與第一個 byte 之間仍有視窗
    （沒有時間上限保證）：之後才發生的替換不作用於這份已放行的請求。
    """
    if _bound_epoch.get() is None:
        return
    snapshot = _bound_identity.get()
    current_policy = policy_identity()
    if (snapshot is None or current_policy is None or current_policy.sha256 != snapshot.sha256
            or current_policy.structural != snapshot.structural
            or current_policy.destination != snapshot.destination):
        _retire_bound_epoch()
        raise TaskScopeRejected("C2_POLICY_CHANGED:llm.task_scope")


def ensure_context_epoch_current() -> None:
    """送出第一個 byte 之前的最後核對：turn 開始時記錄的外送上下文 epoch 仍是本 session 目前的 epoch。

    只比對記憶體中已登記的撤銷（解除／替換綁定、session 撤銷、Gate 結束、Profile／policy 讀取失敗），
    不重新讀取 EventStore 或檔案。不在 Profile turn 內（沒有 turn 開始的 epoch）時不核對。
    """
    bound = _bound_epoch.get()
    if bound is None:
        return
    from terminux_engine.llm import context_epoch

    if bound[0] is None or context_epoch.peek(get_current_request_context().session_id) != bound[0]:
        raise TaskScopeRejected("CONTEXT_EPOCH_REVOKED:llm.context_epoch")


def retire_turn_epoch() -> None:
    """結束本 turn 開始時記錄的外送上下文 epoch（若它仍是目前 epoch）；不在 turn 內時不做事。"""
    _retire_bound_epoch()


def _retire_bound_epoch() -> None:
    """結束 turn 開始時記錄的 epoch（若它仍是目前 epoch）。"""
    from terminux_engine.llm import context_epoch, run_binding

    bound = _bound_epoch.get()
    if bound is not None and bound[0] is not None:
        run_binding.retire_epoch_if_current(get_current_request_context().session_id, bound[0],
                                            context_epoch.REASON_REVOKED_AT_GATE)


_origin: ContextVar[tuple[str, int, str | None, int] | None] = ContextVar("terminux_task_conversation", default=None)
_bound_identity: ContextVar[EgressPolicy | None] = ContextVar("terminux_task_policy_identity", default=None)
#: turn 開始時本 session 的 (epoch, 綁定狀態指紋)；最終 Gate 據此判斷 turn 期間是否被撤銷。
_bound_epoch: ContextVar[tuple[str | None, tuple[Any, ...]] | None] = ContextVar(
    "terminux_task_context_epoch", default=None)
_history_status: ContextVar[dict[str, Any] | None] = ContextVar("terminux_task_history_status", default=None)
#: turn 開始時使用的 EventStore projector；最終 Gate 以它重新核對綁定 Run 的權威列。
_bound_projector: ContextVar[Any] = ContextVar("terminux_task_projector", default=None)
_transcript: ContextVar[tuple[str, ...]] = ContextVar("terminux_task_messages", default=())
#: 與 `_transcript` 逐一對應的 host 指定類別；內容不能自稱類別。每則訊息一組類別：
#: 多數訊息只有一個；含 `timing_path` section 的 evidence 訊息同時帶 `evidence_bundle`
#: 與 `timing_path_evidence`，兩個類別各自計算位元組並各自需要 policy 核准。
_classes: ContextVar[tuple[tuple[str, ...], ...]] = ContextVar("terminux_task_classes", default=())
_calls: ContextVar[tuple[tuple[str, str, str, bool], ...]] = ContextVar("terminux_task_calls", default=())
#: 本 turn 進入最終 Gate 的請求數；`activate()` 歸零。
_requests: ContextVar[int] = ContextVar("terminux_task_requests", default=0)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def bind_conversation(conversation: Any, *, projector: Any = None,
                      denied_names: tuple[str, ...] = ()) -> Iterator[None]:
    """在正式chat入口綁定目前stream的buffer快照，不保存shared current caller。

    Profile 模式下這裡是可外送歷史的**唯一**計算點：`structural_c2` 只投影
    `agent_c2`、屬於本 session 目前外送上下文 epoch、且 policy revision／destination 相符的
    回合，排除內容不會通過最終 Gate 的回合（`history_text_exportable`），並套用 policy
    `limits`；`exact_digest`（v1 與 v2 digest）投影本 epoch 的回合，格式與完整顯示歷史相同
    （digest 白名單逐份核准實際送出的 bytes），未標記 epoch 的回合不外送。policy 讀不到時投影為空，
    本 turn 也不得外送（`turn_started_without_policy`）。

    epoch 先依本 session 的綁定狀態更新（`llm.context_epoch.observe`）；給定 `projector`
    時同時重新核對每個綁定 Run 的權威列，被判污染即結束目前 epoch。`denied_names` 必須
    與本 turn `activate()` 使用的未授權能力名稱相同。
    """
    context = get_current_request_context()
    contained = profile_containment_active()
    identity = policy_identity() if contained else None
    epoch: str | None = None
    fingerprint: tuple[Any, ...] = ()
    if contained:
        epoch, fingerprint = _observe_context_epoch(context, projector)
        observed_policy = next((row[2] for row in fingerprint if row[0] == "~policy"), "")
        if fingerprint and observed_policy != (identity.sha256 if identity is not None else ""):
            # 投影用的 policy 與指紋用的 policy 是兩次讀取；不一致時本 turn 的 epoch 不可信，直接結束。
            from terminux_engine.llm import context_epoch, run_binding
            run_binding.retire_epoch_if_current(context.session_id, epoch, context_epoch.REASON_REVOKED_AT_GATE)
    turns = 0
    status: dict[str, Any] | None = None
    if conversation is None:
        history = None
    elif not contained:
        history = conversation.get_history_text()
    elif identity is not None and not identity.structural:
        # exact_digest：只投影本 epoch 的回合（顯示格式與完整歷史相同）。其他 epoch 的回合（已撤銷的上下文）
        # 與未標記 epoch 的回合（無法證明屬於目前授權）都不外送；Profile 模式寫入的回合一律帶 epoch。
        history = conversation.get_history_text_for_epoch(epoch, include_untagged=False)
    else:
        limits = identity.limits if identity is not None else None
        projection = conversation.export_projection(
            policy_revision=identity.sha256 if identity is not None else None,
            destination=identity.destination if identity is not None else None,
            max_turns=limits.history_max_turns if limits is not None else None,
            max_chars=limits.history_max_chars if limits is not None else None,
            assistant_max_chars=limits.model_output_max_chars if limits is not None else None,
            context_epoch=epoch,
            content_filter=lambda text: history_text_exportable(text, denied_names),
        )
        history = projection.text
        turns = len(projection.turns)
        status = {"history_exported_turns": turns, "history_withheld_turns": projection.withheld_total,
                  "withheld": dict(projection.withheld)}
    token = _origin.set((context.session_id, id(conversation), history or None, turns))
    identity_token = _bound_identity.set(identity)
    epoch_token = _bound_epoch.set((epoch, fingerprint) if contained else None)
    status_token = _history_status.set(status)
    projector_token = _bound_projector.set(projector if contained else None)
    try:
        yield
    finally:
        _bound_projector.reset(projector_token)
        _history_status.reset(status_token)
        _bound_epoch.reset(epoch_token)
        _bound_identity.reset(identity_token)
        _origin.reset(token)


def _observe_context_epoch(context: Any, projector: Any) -> tuple[str, tuple[Any, ...]]:
    """依本 session 目前的綁定狀態更新 epoch；回傳 (epoch, 狀態指紋)。"""
    from terminux_engine.llm import context_epoch, run_binding

    try:
        profile = load_profile()
    except TaskScopeRejected:
        profile = None
    if profile is None:
        epoch, _ = context_epoch.observe(context.session_id, ())
        return epoch, ()
    unavailable = run_binding.revalidate_authority(profile, projector, context=context)
    epoch, fingerprint = run_binding.epoch_snapshot(profile, context=context, observe=True, unavailable=unavailable)
    return epoch or context_epoch.current(context.session_id), fingerprint


def register_request(policy: EgressPolicy | None = None) -> int:
    """`_post` 在序列化之後、`check_final_request` 之前呼叫；回傳本 turn 的請求序號。

    scope 外的呼叫不計數（它們隨後會以 `REQUEST_SCOPE_MISSING` 被拒）。
    `structural_c2` 下第 `requests_per_turn + 1` 次請求在這裡確定性拒絕，
    不重試、不進 fallback。`policy` 給定時沿用呼叫端已讀到的同一份 policy。
    """
    if current() is None:
        return 0
    index = _requests.get() + 1
    _requests.set(index)
    identity = policy if policy is not None else policy_identity()
    if identity is not None and identity.structural and identity.limits is not None \
            and index > identity.limits.requests_per_turn:
        raise TaskScopeRejected("C2_LIMIT_EXCEEDED:requests_per_turn")
    return index


def current_request_index() -> int:
    """本 turn 到目前為止進入最終 Gate 的請求數（含被拒的那一次）；scope 外為 0。"""
    return _requests.get() if current() is not None else 0


def structural_request_classes(doc: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    """把已通過 `check_final_request` 的 body 對應到 host 指定的類別序列與各類別位元組數。

    類別只由 transcript 位置與 `track_message` 的 source 決定：system prompt 的
    `conversation_history` 欄位、`tools` 陣列與每則訊息各自一筆；帶 `timing_path` section
    的 evidence 訊息拆成 `evidence_bundle`（去掉該 section 後的訊息）與
    `timing_path_evidence`（該 section）兩筆。訊息數與類別數不一致時視為來源不明並拒絕。
    """
    scope = current()
    if scope is None:
        raise TaskScopeRejected("REQUEST_SCOPE_MISSING:llm.task_scope")
    messages = doc.get("messages") or []
    classes = _classes.get()
    if len(messages) != len(classes):
        raise TaskScopeRejected("MESSAGE_SOURCE_DENIED:llm.agent_runner")
    rows: list[tuple[str, int]] = []
    system_doc = json.loads(scope.system_prompt)
    system_bytes = len(_canonical({k: v for k, v in system_doc.items() if k != "conversation_history"}).encode("utf-8"))
    rows.append(("system_instructions", system_bytes))
    history = system_doc.get("conversation_history")
    if history is not None:
        rows.append(("conversation_history", len(str(history).encode("utf-8"))))
    for labels, message in zip(classes[1:], messages[1:]):
        rows.extend(_message_class_rows(labels, message))
    if doc.get("tools"):
        rows.append(("tool_schema", len(_canonical(doc["tools"]).encode("utf-8"))))
    return tuple(rows)


def _message_class_rows(labels: tuple[str, ...], message: dict[str, Any]) -> list[tuple[str, int]]:
    """一則訊息的 (類別, 位元組) 列。多類別訊息只可能是帶 `timing_path` 的 evidence 訊息。"""
    if len(labels) == 1:
        return [(labels[0], len(_canonical(message).encode("utf-8")))]
    if set(labels) != {"evidence_bundle", TIMING_PATH_EVIDENCE_CLASS} or message.get("role") != "tool":
        raise TaskScopeRejected("MESSAGE_SOURCE_DENIED:llm.agent_runner")
    try:
        content = json.loads(message.get("content", ""))
        section = content["evidence"]["timing_path"]
    except (ValueError, TypeError, KeyError):
        raise TaskScopeRejected("MESSAGE_SOURCE_DENIED:llm.agent_runner") from None
    remainder = {**content, "evidence": {k: v for k, v in content["evidence"].items() if k != "timing_path"}}
    stripped = {**message, "content": json.dumps(remainder)}
    return [("evidence_bundle", len(_canonical(stripped).encode("utf-8"))),
            (TIMING_PATH_EVIDENCE_CLASS, len(_canonical(section).encode("utf-8")))]


def track_message(message: dict[str, Any], source: str) -> None:
    """只從provider解析、已驗證tool結果或budget結語追加本turn訊息。"""
    scope = current()
    if scope is None:
        return
    from terminux_engine.llm.openai_compatible_provider import _normalize_messages_for_openai
    message = _normalize_messages_for_openai([json.loads(json.dumps(message))])[0]
    calls = list(_calls.get())
    if source == "provider_tool_calls" and message.get("role") == "assistant":
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            args = json.loads(function.get("arguments", "{}"))
            if (function.get("name") not in scope.profile.tools or set(args) != {"orchestration_run_id"}
                    or args["orchestration_run_id"] not in scope.effective_run_ids
                    or not isinstance(call.get("id"), str) or any(row[0] == call["id"] for row in calls)):
                raise TaskScopeRejected("TOOL_CALL_SOURCE_DENIED:llm.agent_runner")
            calls.append((call["id"], function["name"], args["orchestration_run_id"], False))
        labels: tuple[str, ...] = ("tool_call",)
    elif source == "tool_registry" and message.get("role") == "tool":
        found = next((i for i, row in enumerate(calls) if row[0] == message.get("tool_call_id") and not row[3]), None)
        if found is None or calls[found][1] != message.get("name"):
            raise TaskScopeRejected("TOOL_RESULT_SOURCE_DENIED:llm.agent_runner")
        run = calls[found][2]
        value = json.loads(message["content"])
        validate_evidence_result(value, run, scope.effective_run_map[run],
                                 pinned_content_hash=scope.run_pin_map.get(run))
        calls[found] = (*calls[found][:3], True)
        # section 存在（不論 available 或 unavailable）就是新類別的 bytes；不以整包改標籤取代。
        labels = ("evidence_bundle",) + (
            (TIMING_PATH_EVIDENCE_CLASS,) if "timing_path" in value["evidence"] else ())
    elif source == "budget_summary" and message.get("role") == "user":
        if not message.get("content", "").startswith("budget 已用完。"):
            raise TaskScopeRejected("MESSAGE_SOURCE_DENIED:llm.agent_runner")
        labels = ("budget_summary",)
    else:
        raise TaskScopeRejected("MESSAGE_SOURCE_DENIED:llm.agent_runner")
    _calls.set(tuple(calls))
    _transcript.set((*_transcript.get(), _canonical(message)))
    _classes.set((*_classes.get(), labels))


def tracked_runs() -> tuple[tuple[str, int], ...]:
    """本 turn 已被模型要求且通過 scope 檢查的 (run_id, attempt)；供 audit 記錄。"""
    scope = current()
    if scope is None:
        return ()
    seen: dict[str, int] = {}
    for _, _, run, _ in _calls.get():
        seen.setdefault(run, scope.effective_run_map.get(run, 0))
    return tuple(sorted(seen.items()))


def veto_run_evidence(run_id: str) -> None:
    """由 daemon 行程內的可信層宣告：本 turn 內該 run 的 evidence 一律拒送。

    呼叫端是 evidence tool 的 executor，它拿到 `OrchestrationService` 的
    `ReceiptIntegrity`（其他 member 寫入同 run id 的列、或 content hash 不自洽的本人列）；
    那個判定不會出現在 tool 結果裡，caller 可見的 bundle 與沒有那些列時相同。沒有 active
    scope（非 Profile 模式）時沒有 Gate 可承接，此呼叫無效果。
    """
    scope = current()
    if scope is None:
        return
    scope.vetoed_runs.add(run_id)


def validate_evidence_result(value: Any, run: str, attempt: int,
                             pinned_content_hash: str | None = None) -> None:
    """只接受正式Evidence Bundle的已核准欄位樹；未知內容不刪除、不外送。

    行程內可信層以 `veto_run_evidence` 宣告過的 run 整份拒絕（`EVIDENCE_INTEGRITY_DENIED`）。
    Evidence Resolver 的 caller 可見投影只帶 `untrusted_receipt_count`（本人列的 content hash
    自洽與否），它只接受整數 0；舊 daemon 若仍回傳 `foreign_user_receipt_count`，同樣只接受
    整數 0（`bool`、浮點數一律拒絕）。
    `pinned_content_hash`（綁定加入的 Run）另外要求 evidence 來自 receipt 投影、附有
    resolver 判定（`untrusted_receipt_count` 為整數）與 resolver 自己選出的 receipt 摘要，且投影
    與摘要的 content hash 都等於綁定當下的權威列；resolver 沒有佐證（摘要缺席，例如 Run 已作廢
    或讀不到任何列）時，投影端的釘選列不能單獨成立。綁定之後才出現的任何列都不會被當作同一份
    evidence。
    """
    scope = current()
    if scope is not None and run in scope.vetoed_runs:
        raise TaskScopeRejected("EVIDENCE_INTEGRITY_DENIED:evidence_integrity.evidence_resolver")

    def mapping(obj: Any, keys: set[str], required: set[str] = set()) -> dict[str, Any]:
        if not isinstance(obj, dict) or not required <= set(obj) or not set(obj) <= keys:
            raise TaskScopeRejected("EVIDENCE_CATEGORY_DENIED:orchestration.evidence_bundle")
        return obj

    def scalars(obj: dict[str, Any]) -> None:
        if any(v is not None and type(v) not in (str, int, float, bool) for v in obj.values()):
            raise TaskScopeRejected("EVIDENCE_TYPE_DENIED:orchestration.evidence_bundle")
        if obj.get("attempt") is not None and (type(obj["attempt"]) is not int or obj["attempt"] != attempt):
            raise TaskScopeRejected("EVIDENCE_ATTEMPT_DENIED:orchestration.evidence_bundle")

    mapping(value, {"evidence"}, {"evidence"})
    bundle = mapping(value["evidence"], {"schema_version", "orchestration_run_id", "availability", "orchestration", "dqe", "reports", "lineage", "execution_evidence", "timing_path"},
                     {"schema_version", "orchestration_run_id", "availability", "orchestration", "dqe", "reports", "lineage"})
    if bundle["schema_version"] != "terminux.evidence_bundle.v1" or bundle["orchestration_run_id"] != run:
        raise TaskScopeRejected("EVIDENCE_RUN_DENIED:orchestration.evidence_bundle")
    if bundle["availability"] not in {"available", "partial", "unavailable", "available_from_receipt_projection"}:
        raise TaskScopeRejected("EVIDENCE_AVAILABILITY_DENIED:orchestration.evidence_bundle")
    orchestration = mapping(bundle["orchestration"], set((
        "status terminal_reason domain_outcome eco_run_id evidence_reason evidence_source lifecycle_authority "
        "dispatch_status evidence_status process_exit_code timed_out step_id attempt tool_id adapter_id "
        "artifact_manifest_id artifact_manifest_sha256 receipt_id receipt_content_hash receipt_count "
        "workflow_type task_id slot grant_id profile_digest input_manifest_hash recovery_policy").split()))
    scalars(orchestration)
    if bundle["availability"] in {"available", "available_from_receipt_projection"} and orchestration.get("attempt") != attempt:
        raise TaskScopeRejected("EVIDENCE_ATTEMPT_REQUIRED:orchestration.evidence_bundle")
    item_fields = {
        "dqe": set("dqe_id value unit status iteration step_id attempt package_id dispatch_key source_event_id".split()),
        "reports": set("report_id kind iteration sha256 bytes".split()),
        "lineage": set("kind sha256 iteration base_sha256 new_sha256 recipe_sha256".split()),
    }
    for section_name, fields in item_fields.items():
        section = mapping(bundle[section_name], {"status", "items", "truncated"}, {"status", "items", "truncated"})
        if not isinstance(section["status"], str) or type(section["truncated"]) is not bool or not isinstance(section["items"], list) or len(section["items"]) > 128:
            raise TaskScopeRejected("EVIDENCE_SECTION_DENIED:orchestration.evidence_bundle")
        for item in section["items"]:
            scalars(mapping(item, fields))
    execution = bundle.get("execution_evidence")
    if execution is not None:
        execution = mapping(execution, set("run_id terminal_outcome outcome_reasons lifecycle_source conflicts ledger_status ledger_terminal_reason receipt receipt_count untrusted_receipt_count foreign_user_receipt_count axes".split()), {"run_id"})
        if execution["run_id"] != run:
            raise TaskScopeRejected("EVIDENCE_RUN_DENIED:evidence_integrity.evidence_resolver")
        for key in ("outcome_reasons", "conflicts"):
            if key in execution and (not isinstance(execution[key], list) or not all(isinstance(v, str) for v in execution[key])):
                raise TaskScopeRejected("EVIDENCE_CATEGORY_DENIED:evidence_integrity.evidence_resolver")
        if execution.get("receipt") is not None:
            scalars(mapping(execution["receipt"], set("receipt_id content_hash step_id attempt execution_lifecycle_status process_exit_code timed_out termination_reason dispatch_status evidence_status artifact_manifest_id artifact_manifest_sha256 artifact_capture_status".split())))
        if "axes" in execution:
            axes = mapping(execution["axes"], {"artifact_manifest", "dqe", "visibility"})
            for axis in axes.values():
                axis = mapping(axis, {"status", "reasons", "row_count", "source"})
                if "reasons" in axis and (not isinstance(axis["reasons"], list) or not all(isinstance(v, str) for v in axis["reasons"])):
                    raise TaskScopeRejected("EVIDENCE_CATEGORY_DENIED:evidence_integrity.evidence_resolver")
                scalars({k: v for k, v in axis.items() if k != "reasons"})
        scalars({k: v for k, v in execution.items() if k not in {"receipt", "axes", "outcome_reasons", "conflicts"}})
        for key in ("untrusted_receipt_count", "foreign_user_receipt_count"):
            if key in execution and (type(execution[key]) is not int or execution[key] != 0):
                raise TaskScopeRejected("EVIDENCE_INTEGRITY_DENIED:evidence_integrity.evidence_resolver")
    if pinned_content_hash is not None:
        receipt = execution.get("receipt") if execution is not None else None
        if (bundle["availability"] != "available_from_receipt_projection" or execution is None
                or type(execution.get("untrusted_receipt_count")) is not int
                or orchestration.get("receipt_content_hash") != pinned_content_hash
                or not isinstance(receipt, dict)
                or receipt.get("content_hash") != pinned_content_hash):
            raise TaskScopeRejected("EVIDENCE_PIN_DENIED:llm.run_binding")
    if "timing_path" in bundle:
        _validate_timing_path_section(bundle["timing_path"], run, attempt, orchestration)


def _validate_timing_path_section(section: Any, run: str, attempt: int,
                                  orchestration: dict[str, Any]) -> None:
    """`timing_path` section 的封閉 schema 與身分核對。

    schema 由 `timing_evidence.projection.validate_section` 定義（鍵、封閉詞彙、型別、stage
    上限、不含任何物件名稱、自由文字或路徑）；這裡另外要求 section 的來源 run／attempt 等於
    本次工具呼叫的 run／attempt，且 manifest id／sha 與 bundle 的 orchestration 欄位一致。
    """
    from terminux_engine.timing_evidence.projection import SectionInvalid, validate_section

    try:
        validate_section(section)
    except SectionInvalid as exc:
        raise TaskScopeRejected(f"TIMING_PATH_SCHEMA_DENIED:{exc.code}") from None
    if section["status"] != "available":
        return
    source = section["source"]
    if source["run_id"] != run:
        raise TaskScopeRejected("TIMING_PATH_RUN_DENIED:orchestration.evidence_bundle")
    if source["attempt"] != attempt or orchestration.get("attempt") != attempt:
        raise TaskScopeRejected("TIMING_PATH_ATTEMPT_DENIED:orchestration.evidence_bundle")
    for key in ("artifact_manifest_id", "artifact_manifest_sha256"):
        if orchestration.get(key) is None or orchestration.get(key) != source[key]:
            raise TaskScopeRejected("TIMING_PATH_SOURCE_DENIED:orchestration.evidence_bundle")


def timing_path_egress_approved() -> bool:
    """本 turn 的外送 policy 是否明確核准 `timing_path_evidence` 類別。

    只看 `bind_conversation()` 當下讀到的 policy（本 turn 的外送契約）；不在 Profile turn 內、
    policy 讀不到、v1 詞彙或未列出該類別，一律 False。
    """
    if _bound_epoch.get() is None:
        return False
    identity = _bound_identity.get()
    return identity is not None and TIMING_PATH_EVIDENCE_CLASS in identity.approved_data_classes


def redact_unapproved_evidence(value: Any) -> Any:
    """Evidence tool 結果進入 transcript 之前，移除未核准類別的 section。

    目前唯一受管制的選填 section 是 `timing_path`：policy 沒有核准 `timing_path_evidence` 時
    整個鍵移除，送出的 bytes 與該 section 不存在時完全相同。其餘內容原樣回傳，不改寫。
    """
    if not isinstance(value, dict) or not isinstance(value.get("evidence"), dict):
        return value
    if "timing_path" not in value["evidence"] or timing_path_egress_approved():
        return value
    evidence = {k: v for k, v in value["evidence"].items() if k != "timing_path"}
    return {**value, "evidence": evidence}


def redact_evidence_set_timing_path(value: Any) -> Any:
    """Evidence Set tool 結果進入 transcript 之前，移除**每個** bundle 的 `timing_path` section。

    多 run 結果沒有對應的外送類別拆分：`_message_class_rows` 只把單 run 的 evidence 訊息拆成
    `evidence_bundle`＋`timing_path_evidence` 兩類，Profile 的 Gate 也只接受單 run 的 tool call
    引數，因此 `timing_path_evidence` 的核准無法對應到 evidence set 內的任何 section。這裡不看
    policy：經此工具外送的 bundle 一律不帶 `timing_path`（非 Profile 模式沒有最終 Gate，
    預設拒送只能在 tool 層成立）。其餘內容原樣回傳。

    結構不是 `{"evidence_set": {"bundles": {run_id: dict}}}` 時 fail closed：拋出
    `TaskScopeRejected`，tool 層把它收斂成錯誤結果，不把形狀不明的內容放進 transcript。
    """
    if not isinstance(value, dict) or not isinstance(value.get("evidence_set"), dict):
        raise TaskScopeRejected("EVIDENCE_SET_SHAPE_DENIED:orchestration.evidence_set")
    bundles = value["evidence_set"].get("bundles")
    if not isinstance(bundles, dict) or not all(isinstance(b, dict) for b in bundles.values()):
        raise TaskScopeRejected("EVIDENCE_SET_SHAPE_DENIED:orchestration.evidence_set")
    redacted = {run_id: {k: v for k, v in bundle.items() if k != "timing_path"}
                for run_id, bundle in bundles.items()}
    return {**value, "evidence_set": {**value["evidence_set"], "bundles": redacted}}
