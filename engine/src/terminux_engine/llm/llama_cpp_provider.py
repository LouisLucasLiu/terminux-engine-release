# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""llama-cpp-python real provider adapter。

LlamaCppCompletionProvider 直接符合 IntentFallbackProvider／
LineageExplainerProvider 的 Protocol（complete(prompt: str) -> str）。

設計原則：
- import 模組時不載入模型（不讓 import 連帶載入大型 GGUF）
- 第一次呼叫 complete() 時才 lazy loading
- 不做 thread-safe 處理（沒有 lock／mutex）；呼叫端負責序列化
- 只支援 text-completion 路徑（取 result["choices"][0]["text"]）
"""

from dataclasses import dataclass
from pathlib import Path

from terminux_engine.optional_dependencies import import_optional


@dataclass(frozen=True)
class LlamaCppProviderConfig:
    """llama-cpp-python 初始化使用的設定值。"""
    model_path: str
    n_ctx: int = 16384
    n_gpu_layers: int = -1       # -1 = 所有 layer 都 offload 到 GPU
    max_tokens: int = 512
    temperature: float = 0.0
    repeat_penalty: float = 1.1  # 防止 token 迴圈
    disable_thinking: bool = False  # Qwen3 等 thinking-mode 模型：prepend /no_think


class LlamaCppCompletionProvider:
    """
    以 llama-cpp-python 實作的 text completion provider。

    與 IntentFallbackProvider／LineageExplainerProvider 都直接相容
    （complete(prompt: str) -> str）。

    lazy loading：__init__ 不載入任何東西，第一次呼叫 complete() 時
    才呼叫 _load_model()。
    """

    def __init__(self, config: LlamaCppProviderConfig) -> None:
        self._config = config
        self._llm = None   # lazy 初期化。型：llama_cpp.Llama | None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_model(self) -> None:
        """
        由第一次 complete() 呼叫的 lazy initializer。

        失敗的種類：
        - 模型檔不存在                  → FileNotFoundError（不在內部吞掉）
        - llama_cpp import 失敗         → 包成 MissingOptionalDependencyError 拋出
        - Llama(...) 初始化例外         → 往上拋（fail-open 由 server 層負責）
        """
        model_path = Path(self._config.model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"LLM model file not found: {self._config.model_path}"
            )

        Llama = import_optional("llama_cpp", "local-llm").Llama

        self._llm = Llama(
            model_path=str(model_path),
            n_ctx=self._config.n_ctx,
            n_gpu_layers=self._config.n_gpu_layers,
            verbose=False,
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def complete(self, prompt: str) -> str:
        """
        執行 text completion，回傳純文字字串。

        取值路徑固定為 result["choices"][0]["text"]：
        - choices 缺少或為空            → RuntimeError
        - choices[0]["text"] 缺少        → RuntimeError
        - 其他 runtime exception         → 往上拋
        """
        if self._llm is None:
            self._load_model()

        # Qwen3 thinking-mode 模型：用 ChatML 包裝 user turn + 預填空 <think></think>
        # block，讓模型跳過 thinking 階段直接輸出答案；同時加 stop token 確保
        # 輸出在 <|im_end|> 處停止，不再 echo prompt。
        if self._config.disable_thinking:
            effective_prompt = (
                "<|im_start|>user\n"
                f"{prompt}<|im_end|>\n"
                "<|im_start|>assistant\n"
                "<think>\n\n</think>\n\n"
            )
            stop_tokens = ["<|im_end|>", "<|endoftext|>"]
        else:
            effective_prompt = prompt
            stop_tokens = []

        raw = self._llm(
            effective_prompt,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            repeat_penalty=self._config.repeat_penalty,
            stop=stop_tokens,
        )

        # ── text 抽取（仕様固定：choices[0]["text"]）──────────────────────────
        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not choices:
            raise RuntimeError(
                f"LlamaCpp returned no choices. raw={str(raw)[:200]!r}"
            )

        first = choices[0]
        text = first.get("text") if isinstance(first, dict) else None
        if text is None:
            raise RuntimeError(
                f"LlamaCpp choices[0] has no 'text' field. choices={str(choices)[:200]!r}"
            )

        return text
