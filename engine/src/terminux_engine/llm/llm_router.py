# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""LLM Router：根據 TaskType 分流到不同 LLM provider / embedder。

開發階段四個 task type 全走同一 provider（向下相容）。
上線後透過 YAML config 精細配置不同模型。
"""

import concurrent.futures
from typing import TYPE_CHECKING
import logging
import threading
from enum import Enum
from typing import Any

from terminux_engine.llm.router_config import (
    DEFAULT_GEMINI_MODEL,
    EmbeddingProviderConfig,
    ProviderConfig,
    RouterConfig,
)
from terminux_engine.memory.embedder import (
    Embedder,
    GeminiEmbedder,
    LocalEmbedder,
    NoOpEmbedder,
)

_log = logging.getLogger(__name__)


# ── TaskType ──────────────────────────────────────────────────────────────────

class TaskType(str, Enum):
    REALTIME = "realtime"       # @@ chat query, gate explanation（user 在等）
    BACKGROUND = "background"   # compaction, session digest（背景任務）
    ANALYSIS = "analysis"       # weekly digest, observed preference（離線深度分析）
    EMBEDDING = "embedding"     # vector embedding 計算


# ── Exception ─────────────────────────────────────────────────────────────────

class LLMNotConfiguredError(Exception):
    """Provider 未配置（backend = "none"）時 raise。"""
    pass


# ── Provider 工廠 ─────────────────────────────────────────────────────────────

def create_provider(config: ProviderConfig) -> Any:
    """根據 config.backend 建立對應的 completion provider instance。

    回傳值符合 complete(prompt: str) -> str 協議。
    backend = "none" → 回傳 None。
    """
    if config.backend == "llama_cpp":
        from terminux_engine.llm.llama_cpp_provider import (
            LlamaCppCompletionProvider,
            LlamaCppProviderConfig,
        )
        return LlamaCppCompletionProvider(
            LlamaCppProviderConfig(model_path=config.model_path or ""),
        )

    if config.backend == "gemini":
        from terminux_engine.llm.gemini_provider import (
            GeminiCompletionProvider,
            GeminiProviderConfig,
        )
        return GeminiCompletionProvider(
            GeminiProviderConfig(
                model=config.model_name or DEFAULT_GEMINI_MODEL,
                api_key=config.api_key or "",
            ),
        )

    if config.backend == "openai_compatible":
        from terminux_engine.llm.openai_compatible_provider import (
            OpenAICompatibleProvider,
            OpenAICompatibleProviderConfig,
            configured_timeout_sec,
        )
        api_key = config.api_key or ""
        if not api_key:
            _log.warning(
                "openai_compatible backend has no api_key; provider unavailable"
            )
            return None
        return OpenAICompatibleProvider(
            OpenAICompatibleProviderConfig(
                api_key=api_key,
                model=config.model_name or "deepseek-chat",
                base_url=config.base_url or "https://api.deepseek.com",
                timeout_sec=configured_timeout_sec(),
                enforce_hard_timeout=True,
            ),
        )

    # "none" 或未知 backend
    return None


def _create_embedder(config: EmbeddingProviderConfig) -> Embedder:
    """根據 config.backend 建立對應的 Embedder instance。"""
    if config.backend == "gemini":
        return GeminiEmbedder(
            api_key=config.api_key or "",
            model_name=config.model_name or "gemini-embedding-001",
        )
    if config.backend == "local":
        return LocalEmbedder(model_path=config.model_path or "")
    # "noop" / 其他 → NoOpEmbedder
    return NoOpEmbedder()


# ── LLMRouter ─────────────────────────────────────────────────────────────────

# 用於 is_configured() 判斷的 "未配置" backend 集合
_UNCONFIGURED_BACKENDS = frozenset({"none", "noop"})

# TaskType → RouterConfig 欄位的 mapping
_TASK_CONFIG_MAP: dict[TaskType, str] = {
    TaskType.REALTIME: "realtime",
    TaskType.BACKGROUND: "background",
    TaskType.ANALYSIS: "analysis",
}


if TYPE_CHECKING:  # pragma: no cover - 僅供型別註記
    from terminux_engine.llm.generation import CompletionResult

class LLMRouter:
    """根據 TaskType 分流到不同 LLM provider 的統一入口。"""

    def __init__(self, config: RouterConfig) -> None:
        self._config = config
        self._providers: dict[TaskType, Any] = {}
        self._embedder: Embedder | None = None
        # per-slot session lock。Agent 多輪呼叫期間持有，
        # 防止其它 caller 中途搶走同一 slot 的 provider。用 RLock 讓同一 thread
        # 可以重入（例如 Agent 內部巢狀呼叫）。
        self._session_locks: dict[str, threading.RLock] = {}
        self._session_locks_guard = threading.Lock()
        self._init_providers()

    def _init_providers(self) -> None:
        """建立 provider mapping。Provider 本身有 lazy loading，此處不做 heavy init。"""
        # 用一個 cache 避免相同 config 建立多個 provider instance
        seen: dict[int, Any] = {}  # id(ProviderConfig) → provider

        for task_type, field in _TASK_CONFIG_MAP.items():
            pc: ProviderConfig = getattr(self._config, field)
            config_id = id(pc)
            if config_id in seen:
                self._providers[task_type] = seen[config_id]
            else:
                provider = create_provider(pc)
                seen[config_id] = provider
                self._providers[task_type] = provider

        # Embedder
        self._embedder = _create_embedder(self._config.embedding)

    def generate(
        self,
        prompt: str,
        task_type: TaskType,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout: float | None = None,
        *,
        max_output_tokens: int | None = None,
    ) -> str:
        """根據 task_type dispatch 到對應 provider 的 complete()。

        若 provider 為 None（backend = "none"）→ raise LLMNotConfiguredError。
        若 timeout 不為 None 且超時 → raise TimeoutError。

        `max_output_tokens` 是 per-request 的 output budget；`max_tokens` 是它的
        deprecated 別名。**兩者都不指定時沿用 provider config 的預設值**——
        刻意不再帶 512 這個從未生效過的舊預設，否則所有既有呼叫端會被一起
        砍到 512。
        """
        return self.generate_with_metadata(
            prompt, task_type, max_tokens=max_tokens, temperature=temperature,
            timeout=timeout, max_output_tokens=max_output_tokens).text

    def generate_with_metadata(
        self,
        prompt: str,
        task_type: TaskType,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        timeout: float | None = None,
        *,
        max_output_tokens: int | None = None,
    ) -> "CompletionResult":
        """與 `generate()` 相同，但一併回傳 completion metadata。

        provider 若不支援 per-request budget，metadata 會**明確標示
        `budget_supported=False`**，而不是假裝已經套用。
        """
        from terminux_engine.llm.generation import (
            GenerationOptions, unsupported_budget_metadata,
        )

        if task_type == TaskType.EMBEDDING:
            raise LLMNotConfiguredError(
                "TaskType.EMBEDDING 不支援 generate()，請使用 embed()"
            )

        provider = self._providers.get(task_type)
        if provider is None:
            raise LLMNotConfiguredError(
                f"LLM provider not configured for task type: {task_type.value}"
            )

        requested = max_output_tokens if max_output_tokens is not None else max_tokens
        # 每次呼叫各自建立 options，沒有任何 module/instance 級可變狀態，
        # 因此 concurrent request 不會互相污染。
        options = GenerationOptions(max_output_tokens=requested)
        supported = bool(getattr(provider, "supports_output_budget", False)) and hasattr(
            provider, "complete_with_metadata")

        if supported:
            def call():
                return provider.complete_with_metadata(
                    prompt, max_output_tokens=options.max_output_tokens)
        else:
            model = getattr(getattr(provider, "_config", None), "model", None) or ""
            name = type(provider).__name__

            def call():
                text = provider.complete(prompt)
                if options.max_output_tokens is not None:
                    _log.warning(
                        "provider %s does not accept a per-request output budget; "
                        "requested %s was not applied", name, options.max_output_tokens)
                from terminux_engine.llm.generation import CompletionResult
                return CompletionResult(
                    text=text,
                    metadata=unsupported_budget_metadata(
                        provider=name, model=model, text=text,
                        requested=options.max_output_tokens))

        if timeout is not None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(call)
                try:
                    return future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"LLM generate timed out after {timeout}s "
                        f"for task type: {task_type.value}"
                    )

        return call()

    def embed(self, text: str) -> list[float] | None:
        """呼叫 embedding provider。backend 為 "noop" → return None。"""
        if self._embedder is None:
            return None
        return self._embedder.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """批次 embedding。backend 為 "noop" → return [None] * len(texts)。"""
        if self._embedder is None:
            return [None] * len(texts)
        return self._embedder.embed_batch(texts)

    def is_configured(self, task_type: TaskType) -> bool:
        """檢查指定 task type 的 provider 是否已配置（非 none/noop）。"""
        if task_type == TaskType.EMBEDDING:
            return self._config.embedding.backend not in _UNCONFIGURED_BACKENDS
        field = _TASK_CONFIG_MAP.get(task_type)
        if field is None:
            return False
        pc: ProviderConfig = getattr(self._config, field)
        return pc.backend not in _UNCONFIGURED_BACKENDS

    @property
    def config(self) -> RouterConfig:
        return self._config

    # ── public provider getter ─────────────────────────

    def get_provider(self, task_type: TaskType | str) -> Any | None:
        """取得指定 task_type 的 provider instance。

        - 未配置 / backend = none → None（caller 自行 fallback）
        - 接受 TaskType enum 或字串 ("realtime" / "background" / "analysis" / "embedding")
        - 取代之前 jsonrpc_server 直接讀 `_providers` 私有欄位的做法
        """
        if isinstance(task_type, str):
            try:
                task_type = TaskType(task_type)
            except ValueError:
                return None
        return self._providers.get(task_type)

    # ── Session lock（Agent 多輪保護）─────────────────

    def _get_session_lock(self, slot: str) -> threading.RLock:
        """取得 slot 對應的 RLock（首次呼叫時建立）。"""
        with self._session_locks_guard:
            lock = self._session_locks.get(slot)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[slot] = lock
            return lock

    def acquire_session(self, slot: str, timeout: float = 60.0) -> bool:
        """取得指定 slot 的 session lock。

        - blocking with timeout；超時回 False，呼叫端應自行處理
        - 同 thread 已持有 → RLock 重入成功（回 True）
        - slot 字串自由（'realtime' / 'analysis' / ...），未預先檢查 TaskType
          相容；caller 自行對齊
        """
        lock = self._get_session_lock(slot)
        return lock.acquire(timeout=timeout)

    def release_session(self, slot: str) -> None:
        """釋放 session lock。未持有時的 release 會 raise RuntimeError，
        caller 應確保 acquire/release 配對。"""
        lock = self._get_session_lock(slot)
        lock.release()
