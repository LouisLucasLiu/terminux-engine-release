# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal

import httpx
import yaml

from terminux_engine.optional_dependencies import (
    MissingOptionalDependencyError, import_optional,
)

from terminux_engine.ecc.models import ShellEvent

logger = logging.getLogger(__name__)


# ── Action Types ───────────────────────────────────────────────────────────────

@dataclass
class NotifyInlineAction:
    type:    Literal["notify_inline"] = "notify_inline"
    content: str = ""


@dataclass
class NotifyStatusbarAction:
    type:    Literal["notify_statusbar"] = "notify_statusbar"
    content: str = ""


@dataclass
class NotifyPanelAction:
    """Layer 3 收到此 type 時記錄 warning 並跳過（預留 Option B UI）。"""
    type:    Literal["notify_panel"] = "notify_panel"
    title:   str = ""
    content: str = ""


@dataclass
class SuggestCommandAction:
    """Layer 3 通過 SUGGEST 授權且命令與理由非空時，以 IPC 送出建議。"""
    type:    Literal["suggest_command"] = "suggest_command"
    command: str = ""
    reason:  str = ""


@dataclass
class ExecCommandAction:
    """NOT IMPLEMENTED。layer3.executor 收到此 type 會記錄 warning 並跳過。"""
    type:    Literal["exec_command"] = "exec_command"
    command: str = ""
    reason:  str = ""


AnyAction = (
    NotifyInlineAction | NotifyStatusbarAction |
    NotifyPanelAction  | SuggestCommandAction  | ExecCommandAction
)


@dataclass
class LlmActionResult:
    actions: list[AnyAction] = field(default_factory=list)
    # 空 list 表示 LLM 未產生任何 action（JSON 解析失敗 retry 後仍失敗時）


# ── Config ─────────────────────────────────────────────────────────────────────

@dataclass
class LlmConfig:
    provider:               str
    model:                  str
    ollama_base_url:        str
    anthropic_api_key:      str
    model_path:             str  = ""   # llama_cpp provider 用
    max_tokens:             int  = 1024
    timeout_seconds:        int  = 30
    retry_on_parse_failure: bool = True
    max_concurrent:         int  = 1    # 同時最多幾個 LLM 推理（llama_cpp 須保持 1）


class LlmConfigError(Exception):
    """設定檔載入或解析失敗時拋出。"""
    pass


class LlmCompletionError(Exception):
    """LlmClient.complete() 呼叫失敗時拋出。"""
    pass


# ── Helper functions ───────────────────────────────────────────────────────────

def _resolve_env_var(value: str) -> str:
    """解析 ${ENV_VAR} 格式，取代為對應環境變數值。環境變數不存在時使用空字串。"""
    def _replace(match: re.Match) -> str:
        return os.environ.get(match.group(1), "")
    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _build_condition_prompt(
    condition: str,
    event: ShellEvent,
    query_result: list[dict],
) -> tuple[str, str]:
    """回傳 (system, user) 兩段 prompt 文字。"""
    system = (
        "你是一個 EDA 工程管理助理。\n"
        "你的任務是判斷以下條件是否成立。\n"
        "只能回答 \"YES\" 或 \"NO\"，不得包含任何其他文字。"
    )

    event_summary = {
        "member_id":     event.member_id,
        "block":         event.block,
        "command":       event.command,
        "exit_code":     event.exit_code,
        "timestamp":     event.timestamp.isoformat(),
        "extracted_data": event.extracted_data,
    }

    user = (
        f"## 當前事件\n"
        f"{json.dumps(event_summary, ensure_ascii=False, indent=2)}\n\n"
        f"## 歷史事件（最近 {len(query_result)} 筆）\n"
        f"{json.dumps(query_result, ensure_ascii=False, indent=2)}\n\n"
        f"## 判斷條件\n"
        f"{condition}\n\n"
        f"請回答 YES 或 NO。"
    )

    return system, user


def _build_action_prompt(
    ai_action: str,
    event: ShellEvent,
    query_result: list[dict],
) -> tuple[str, str]:
    """回傳 (system, user) 兩段 prompt 文字。"""
    system = (
        "你是一個 EDA 工程管理助理。\n"
        "你的任務是依照指示產生通知 action。\n"
        "你必須回傳合法的 JSON，格式如下：\n"
        '{\n'
        '  "actions": [\n'
        '    {"type": "notify_inline", "content": "..."},\n'
        '    {"type": "notify_statusbar", "content": "..."}\n'
        '  ]\n'
        '}\n'
        "type 合法值：notify_inline, notify_statusbar, notify_panel（未來）,\n"
        "             suggest_command（未來）, exec_command（未來）\n"
        "不得包含 JSON 以外的任何文字（不要有 markdown code block）。"
    )

    event_summary = {
        "member_id":     event.member_id,
        "block":         event.block,
        "command":       event.command,
        "exit_code":     event.exit_code,
        "timestamp":     event.timestamp.isoformat(),
        "extracted_data": event.extracted_data,
    }

    user = (
        f"## 當前事件\n"
        f"{json.dumps(event_summary, ensure_ascii=False, indent=2)}\n\n"
        f"## 歷史事件\n"
        f"{json.dumps(query_result, ensure_ascii=False, indent=2)}\n\n"
        f"## 指示\n"
        f"{ai_action}"
    )

    return system, user


def _parse_bool_response(raw_text: str) -> bool:
    """解析 LLM 的 YES/NO 回應。"""
    text = raw_text.strip().upper()
    if text.startswith("YES"):
        return True
    if text.startswith("NO"):
        return False
    logger.warning("unexpected bool response: %s", raw_text[:100])
    return False


def _parse_action_json(raw_text: str) -> LlmActionResult | None:
    """
    解析 LLM 的 JSON action 回應。
    失敗（JSON parse error 或結構不符）回傳 None。
    """
    # Step 1：去除 markdown code block
    cleaned = raw_text.strip()
    if "```" in cleaned:
        first_idx = cleaned.find("```")
        last_idx  = cleaned.rfind("```")
        if first_idx != last_idx:
            inner = cleaned[first_idx + 3: last_idx]
            if inner.startswith("json"):
                inner = inner[4:]
            cleaned = inner.strip()

    # Step 2：JSON parse
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    # Step 3：驗證頂層結構
    if not isinstance(data.get("actions"), list):
        return None

    # Step 4：解析每個 action
    actions: list[AnyAction] = []
    for action_dict in data["actions"]:
        action_type = action_dict.get("type")
        if action_type == "notify_inline":
            actions.append(NotifyInlineAction(
                content=action_dict.get("content", ""),
            ))
        elif action_type == "notify_statusbar":
            actions.append(NotifyStatusbarAction(
                content=action_dict.get("content", ""),
            ))
        elif action_type == "notify_panel":
            actions.append(NotifyPanelAction(
                title=action_dict.get("title", ""),
                content=action_dict.get("content", ""),
            ))
        elif action_type == "suggest_command":
            actions.append(SuggestCommandAction(
                command=action_dict.get("command", ""),
                reason=action_dict.get("reason", ""),
            ))
        elif action_type == "exec_command":
            actions.append(ExecCommandAction(
                command=action_dict.get("command", ""),
                reason=action_dict.get("reason", ""),
            ))
        else:
            logger.warning("unknown action type: %r", action_type)

    return LlmActionResult(actions=actions)


# ── LlmClient ──────────────────────────────────────────────────────────────────

class LlmClient:

    def __init__(self, config_path: str) -> None:
        self._config = self._load_config(config_path)
        self._anthropic_client = None
        self._llama_instance = None  # llama_cpp 懶惰初始化
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)

        if self._config.provider == "anthropic":
            try:
                anthropic = import_optional("anthropic", "anthropic")
            except MissingOptionalDependencyError as exc:
                raise LlmConfigError(str(exc)) from exc
            self._anthropic_client = anthropic.AsyncAnthropic(
                api_key=self._config.anthropic_api_key,
            )
        elif self._config.provider == "ollama":
            pass  # httpx.AsyncClient 每次呼叫建立
        elif self._config.provider == "llama_cpp":
            pass  # Llama 在首次呼叫時懶惰初始化（模型載入耗時）
        else:
            raise LlmConfigError(
                f"unsupported provider: {self._config.provider}"
            )
        from terminux_engine.llm.task_scope import profile_containment_active

        if profile_containment_active() and not self._local_provider():
            # 啟動時即公告遠端 provider 已停用，部署檢查不必等到第一次呼叫才看到拒絕。
            logger.info("TERM_PROFILE_CONTAINMENT disabled=layer2_remote_provider")

    def _local_provider(self) -> bool:
        """provider 是否不離開本機：in-process llama_cpp，或 host 為 loopback 的 Ollama。"""
        from terminux_engine.llm.task_scope import loopback_url

        return (self._config.provider == "llama_cpp"
                or (self._config.provider == "ollama" and loopback_url(self._config.ollama_base_url)))

    async def evaluate_condition(
        self,
        condition:    str,
        event:        ShellEvent,
        query_result: list[dict],
    ) -> bool:
        """判斷 ai_condition 是否成立。不拋出 exception。"""
        system, user = _build_condition_prompt(condition, event, query_result)
        try:
            async with self._semaphore:
                raw_text = await self._call_llm(system, user)
        except Exception as e:
            logger.error(
                "LLM call failed [provider=%s, model=%s]: %s",
                self._config.provider, self._config.model, e, exc_info=True,
            )
            return False
        return _parse_bool_response(raw_text)

    async def execute_ai_action(
        self,
        ai_action:    str,
        event:        ShellEvent,
        query_result: list[dict],
    ) -> LlmActionResult:
        """依 ai_action 描述產生結構化 action 指令。不拋出 exception。"""
        system, user = _build_action_prompt(ai_action, event, query_result)

        # 第一次呼叫
        try:
            async with self._semaphore:
                raw_text = await self._call_llm(system, user)
        except Exception as e:
            logger.error(
                "LLM call failed [provider=%s, model=%s]: %s",
                self._config.provider, self._config.model, e, exc_info=True,
            )
            return LlmActionResult()

        result = _parse_action_json(raw_text)
        if result is not None:
            return result

        # 解析失敗
        if not self._config.retry_on_parse_failure:
            logger.error(
                "Action JSON parse failed, retry disabled. Raw: %s",
                raw_text[:500],
            )
            return LlmActionResult()

        # Retry
        logger.warning("JSON parse failed, retrying...")
        try:
            async with self._semaphore:
                raw_text2 = await self._call_llm(system, user)
        except Exception as e:
            logger.error(
                "LLM retry call failed [provider=%s, model=%s]: %s",
                self._config.provider, self._config.model, e, exc_info=True,
            )
            return LlmActionResult()

        result2 = _parse_action_json(raw_text2)
        if result2 is not None:
            return result2

        logger.error(
            "Action JSON parse failed after retry. Raw: %s",
            raw_text[:500],
        )
        return LlmActionResult()

    async def complete(self, system: str, user: str) -> str:
        """呼叫 LLM 並回傳完整文字回覆。失敗時拋出 LlmCompletionError。"""
        try:
            async with self._semaphore:
                return await self._call_llm(system, user)
        except Exception as e:
            raise LlmCompletionError(f"LLM completion failed: {e}") from e

    async def _call_llm(self, system: str, user: str) -> str:
        """分派至對應 provider 的 LLM 呼叫。"""
        from terminux_engine.llm.task_scope import profile_containment_active

        if profile_containment_active() and not self._local_provider():
            # Profile 模式只允許不離開本機的 Layer 2 provider。
            raise LlmCompletionError("PROFILE_CONTAINMENT: remote Layer 2 provider disabled")
        if self._config.provider == "anthropic":
            return await self._call_anthropic(system, user)
        elif self._config.provider == "llama_cpp":
            return await self._call_llama_cpp(system, user)
        else:  # ollama
            return await self._call_ollama(system, user)

    async def _call_anthropic(self, system: str, user: str) -> str:
        """呼叫 Anthropic API，以 asyncio.wait_for 限制 timeout。"""
        response = await asyncio.wait_for(
            self._anthropic_client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            ),
            timeout=self._config.timeout_seconds,
        )
        return response.content[0].text

    async def _call_llama_cpp(self, system: str, user: str) -> str:
        """使用 llama_cpp.Llama 載入 .gguf 模型推理（同步操作包裝於 asyncio.to_thread）。"""
        Llama = import_optional("llama_cpp", "local-llm").Llama

        # 懶惰初始化：首次呼叫時載入模型
        if self._llama_instance is None:
            model_path = self._config.model_path
            max_tokens = self._config.max_tokens
            self._llama_instance = await asyncio.to_thread(
                Llama,
                model_path=model_path,
                n_ctx=max(4096, max_tokens * 2),
                verbose=False,
            )

        instance = self._llama_instance
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        max_tokens = self._config.max_tokens

        def _sync_call() -> str:
            output = instance.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
            )
            return output["choices"][0]["message"]["content"]

        return await asyncio.wait_for(
            asyncio.to_thread(_sync_call),
            timeout=self._config.timeout_seconds,
        )

    async def _call_ollama(self, system: str, user: str) -> str:
        """呼叫 Ollama /api/chat endpoint。"""
        from terminux_engine.llm.task_scope import profile_containment_active

        client_options: dict = {"timeout": self._config.timeout_seconds}
        if profile_containment_active():
            # loopback 檢查只看 URL；環境 proxy 會把請求轉送到其他主機，Profile 模式下不採用。
            client_options["trust_env"] = False
        async with httpx.AsyncClient(**client_options) as client:
            response = await client.post(
                f"{self._config.ollama_base_url}/api/chat",
                json={
                    "model": self._config.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
        return response.json()["message"]["content"]

    def _load_config(self, config_path: str) -> LlmConfig:
        """載入並解析 llm_config.yaml。失敗時拋出 LlmConfigError。"""
        try:
            with open(config_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except FileNotFoundError:
            raise LlmConfigError(f"Config file not found: {config_path}")
        except OSError as e:
            raise LlmConfigError(
                f"Failed to read config file '{config_path}': {e}"
            )
        except yaml.YAMLError as e:
            raise LlmConfigError(f"YAML parse error in '{config_path}': {e}")

        if raw is None:
            raw = {}

        # 解析 ${ENV_VAR} 格式
        api_key_raw = str(raw.get("anthropic_api_key", ""))
        api_key = _resolve_env_var(api_key_raw)

        return LlmConfig(
            provider=str(raw.get("provider", "")),
            model=str(raw.get("model", "")),
            ollama_base_url=str(
                raw.get("ollama_base_url", "http://localhost:11434")
            ),
            anthropic_api_key=api_key,
            model_path=str(raw.get("model_path", "")),
            max_tokens=int(raw.get("max_tokens", 1024)),
            timeout_seconds=int(raw.get("timeout_seconds", 30)),
            retry_on_parse_failure=bool(
                raw.get("retry_on_parse_failure", True)
            ),
            max_concurrent=int(raw.get("max_concurrent", 1)),
        )
