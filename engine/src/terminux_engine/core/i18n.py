# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""daemon 靜態 chat / notification 訊息的 i18n catalog。

分工：
- LLM 生成的回應 → 由 prompt 的語言指示控制（agent_prompt）
- daemon 自己組的固定字串（時間描述、血緣圖通知）→ 本模組

讓最後這一類也 follow preferred language。用法：
    tr("lineage_graph_generated", language, path=p, count=c)
    tr("time_minutes_ago", language, n=5)

未知 key → 回 key 本身（fail-safe，不拋）；未知語言 → 降級 DEFAULT_LANGUAGE；
format 參數不齊 → 回未代入的 template（不拋）。
"""

from __future__ import annotations

from terminux_engine.core.preferences import DEFAULT_LANGUAGE, normalize_language

# 每個 key → {language: template}。template 以 str.format 佔位。
_MESSAGES: dict[str, dict[str, str]] = {
    "lineage_graph_generated": {
        "zh-Hant": "Lineage graph 已生成：{path}\n共 {count} 個 job。"
                   "已為你在瀏覽器開啟。",
        "en": "Lineage graph generated: {path}\n{count} job(s) total. "
              "Opened in your browser.",
        "zh-Hans": "Lineage graph 已生成：{path}\n共 {count} 个 job。"
                   "已为你在浏览器打开。",
    },
    "lineage_graph_empty": {
        "zh-Hant": "Lineage ledger 為空，尚無 job 記錄可視覺化。",
        "en": "Lineage ledger is empty — no jobs to visualize yet.",
        "zh-Hans": "Lineage ledger 为空，尚无 job 记录可视觉化。",
    },
    "lineage_graph_failed": {
        "zh-Hant": "Lineage graph 生成失敗：{error}",
        "en": "Lineage graph generation failed: {error}",
        "zh-Hans": "Lineage graph 生成失败：{error}",
    },
    "time_just_now": {
        "zh-Hant": "剛剛", "en": "just now", "zh-Hans": "刚刚",
    },
    "time_minutes_ago": {
        "zh-Hant": "{n} 分鐘前", "en": "{n} min ago", "zh-Hans": "{n} 分钟前",
    },
    "time_hours_ago": {
        "zh-Hant": "{n} 小時前", "en": "{n} hr ago", "zh-Hans": "{n} 小时前",
    },
    "time_days_ago": {
        "zh-Hant": "{n} 天前", "en": "{n} day(s) ago", "zh-Hans": "{n} 天前",
    },
}


def tr(key: str, language: str | None = None, **kwargs: object) -> str:
    """訊息 key + 語言 → 在地化字串（帶 format 參數）。"""
    entry = _MESSAGES.get(key)
    if entry is None:
        return key
    lang = normalize_language(language)
    template = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return template
    return template
