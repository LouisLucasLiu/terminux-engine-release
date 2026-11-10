# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""LLM 回覆的 markdown fence 正規化。"""


def strip_single_fence(text: str) -> str:
    """只處理「整份回覆就是一個 markdown fence」這一種情況。

    provider 有時會把整份 JSON 包成 ```json ... ```。這裡只剝掉整份 fence，其餘一律
    原樣回傳：夾雜散文與 fence 的回覆仍交給後續的 JSON 解析拒絕，不做寬鬆猜測。
    """
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return text
    body = stripped[3:-3]
    newline = body.find("\n")
    if newline == -1:
        return text
    language = body[:newline].strip()
    if language and not language.isalnum():
        return text
    return body[newline + 1:]
