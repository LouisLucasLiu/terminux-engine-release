# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""偵測 provider 回覆 content 中未被解析的 tool call 標記。

OpenAI-compatible 推論服務若沒有為模型啟用 tool call parser，模型自己的
tool call 語法會原樣落在 `content`，`tool_calls` 欄位則是空的：tool 沒有
執行，標記卻會被當成回答顯示。這裡**只做偵測**，不解析任何廠商私有格式。

偵測規則集中在 `_MARKERS`；之後支援其他廠商時只在這裡加一筆。為了不把
「回答中提到這些字樣」誤判成 tool call，規則只認**結構位置**：標記必須是
某一行開頭的 tag（前面只能有空白），而且 Markdown fenced code block 內的
內容一律不看。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNPARSED_TOOL_CALL_CODE = "PROVIDER_UNPARSED_TOOL_CALL"

#: 全形與半形的直線都接受（DeepSeek 的特殊 token 使用全形 `｜`）。
_BAR = r"[｜|]"


@dataclass(frozen=True)
class _Marker:
    family: str
    pattern: re.Pattern[str]


_MARKERS: tuple[_Marker, ...] = (
    # DeepSeek DSML：`<｜DSML｜invoke name="...">`（實測亦見 `<｜｜DSML｜｜ invoke ...>`）。
    # 只認帶 `name=` 的 invoke tag；單獨的 container tag 不足以判定。
    _Marker("deepseek_dsml", re.compile(
        rf"^[ \t]*<[ \t]*{_BAR}+[ \t]*DSML[ \t]*{_BAR}+[ \t]*invoke\b[^>\n]*\bname[ \t]*=",
        re.MULTILINE)),
    # DeepSeek V3 原生 tool call token：`<｜tool▁calls▁begin｜>`／`<｜tool▁call▁begin｜>`。
    _Marker("deepseek_tool_call_tokens", re.compile(
        rf"^[ \t]*<{_BAR}tool▁calls?▁begin{_BAR}>", re.MULTILINE)),
)

_FENCED_BLOCK = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)


def detect_unparsed_tool_call(content: str | None) -> str | None:
    """content 含未解析的 tool call 標記時回傳標記家族名稱，否則 None。"""
    if not content:
        return None
    visible = _FENCED_BLOCK.sub("", content)
    for marker in _MARKERS:
        if marker.pattern.search(visible):
            return marker.family
    return None


class UnparsedToolCallError(RuntimeError):
    """provider 回覆的 content 是未解析的 tool call，不能當成回答。

    訊息格式比照其他 daemon 錯誤：錯誤碼、英文說明、補救方式；不含回覆內容。
    """

    code = UNPARSED_TOOL_CALL_CODE

    def __init__(self, family: str) -> None:
        self.family = family
        super().__init__(
            f"{UNPARSED_TOOL_CALL_CODE}: the model returned a tool call that the "
            f"inference service did not parse ({family} markup in the response "
            "content); it was not shown as an answer and no tool was run; enable "
            "the tool call parser for this model in the inference service and "
            "restart it")


def user_message_from_error(error: str | None) -> str | None:
    """從 AgentRunner 的 error 字串取出可直接顯示的訊息（只認本模組的錯誤）。"""
    if not error:
        return None
    index = error.find(UNPARSED_TOOL_CALL_CODE + ":")
    return error[index:] if index >= 0 else None


__all__ = [
    "UNPARSED_TOOL_CALL_CODE",
    "UnparsedToolCallError",
    "detect_unparsed_tool_call",
    "user_message_from_error",
]
