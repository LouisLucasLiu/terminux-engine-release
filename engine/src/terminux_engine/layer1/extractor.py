# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from terminux_engine.ecc.models import ShellEvent

logger = logging.getLogger(__name__)


# ── Exception ──────────────────────────────────────────────────────────────────

class ExtractorConfigError(Exception):
    """YAML 載入或解析失敗時拋出。"""
    pass


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class MatchCondition:
    command_type:     str | None = None   # CommandType 字串值；None 表示不限
    command_contains: str | None = None   # None 表示不限


@dataclass
class ExtractAction:
    target:        str                                          # dot-notation key
    source:        Literal["command", "stdout_summary", "stderr_summary"]
    method:        Literal["static", "regex"]
    value:         str | None = None                           # method==static 時使用
    pattern:       re.Pattern | None = None                    # method==regex 時使用（已編譯）
    capture_group: int = 1                                     # method==regex 時使用
    value_type:    Literal["str", "int", "float"] = "str"     # 提取後型別轉換


@dataclass
class ExtractRule:
    id:          str
    description: str
    match:       MatchCondition
    actions:     list[ExtractAction]


# ── DataExtractor ──────────────────────────────────────────────────────────────

class DataExtractor:

    def __init__(self, rules_path: str) -> None:
        self._rules_path: str = rules_path
        self._rules: list[ExtractRule] = self._load_rules(rules_path)

    def extract(self, event: ShellEvent) -> ShellEvent:
        """
        對 event 套用所有命中的 rule，將提取結果寫入 event.extracted_data。
        不拋出 exception（所有 exception 在內部捕獲）。
        回傳同一個 event 物件（in-place 修改）。
        """
        try:
            for rule in self._rules:
                if self._matches(rule.match, event):
                    for action in rule.actions:
                        result = self._apply_action(action, event)
                        if result is not None:
                            self._set_nested(event.extracted_data, action.target, result)
        except Exception as e:
            logger.error("Unexpected error in extract(): %s", e, exc_info=True)
        return event

    def reload(self, rules_path: str | None = None) -> None:
        """
        重新解析 YAML，更新 self._rules。
        解析失敗：拋出 ExtractorConfigError，self._rules 維持舊版本不變。
        """
        path = rules_path if rules_path is not None else self._rules_path
        new_rules = self._load_rules(path)   # raises ExtractorConfigError on failure
        self._rules = new_rules
        if rules_path is not None:
            self._rules_path = rules_path

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_rules(self, rules_path: str) -> list[ExtractRule]:
        """載入並解析 YAML 設定檔，回傳 list[ExtractRule]。失敗時拋出 ExtractorConfigError。"""
        try:
            with open(rules_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except FileNotFoundError:
            raise ExtractorConfigError(f"Rules file not found: {rules_path}")
        except OSError as e:
            raise ExtractorConfigError(f"Failed to read rules file '{rules_path}': {e}")
        except yaml.YAMLError as e:
            raise ExtractorConfigError(f"YAML parse error in '{rules_path}': {e}")

        if raw is None:
            raw = {}

        raw_rules = raw.get("rules") or []
        if not isinstance(raw_rules, list):
            raise ExtractorConfigError(f"'rules' must be a list in '{rules_path}'")

        rules: list[ExtractRule] = []
        for raw_rule in raw_rules:
            rule = self._parse_rule(raw_rule)
            if rule is not None:
                rules.append(rule)

        return rules

    def _parse_rule(self, raw: Any) -> ExtractRule | None:
        """解析單條 rule dict。格式錯誤時記錄 warning 並回傳 None。"""
        if not isinstance(raw, dict):
            logger.warning("Skipping invalid rule (not a dict): %r", raw)
            return None

        rule_id   = raw.get("id")
        desc      = raw.get("description", "")
        match_raw = raw.get("match")
        extract_raw = raw.get("extract")

        # 驗證必填欄位
        if not rule_id:
            logger.warning("Skipping rule: missing required field 'id'")
            return None
        if match_raw is None:
            logger.warning("Skipping rule %r: missing required field 'match'", rule_id)
            return None
        if extract_raw is None:
            logger.warning("Skipping rule %r: missing required field 'extract'", rule_id)
            return None
        if not isinstance(extract_raw, list):
            logger.warning("Skipping rule %r: 'extract' must be a list", rule_id)
            return None

        # 解析 match 條件（match 存在但可為空 dict → match all）
        if not isinstance(match_raw, dict):
            match_raw = {}
        match = MatchCondition(
            command_type=match_raw.get("command_type") or None,
            command_contains=match_raw.get("command_contains") or None,
        )

        # 解析 actions
        actions: list[ExtractAction] = []
        for action_raw in extract_raw:
            action = self._parse_action(str(rule_id), action_raw)
            if action is not None:
                actions.append(action)

        return ExtractRule(
            id=str(rule_id),
            description=str(desc),
            match=match,
            actions=actions,
        )

    def _parse_action(self, rule_id: str, raw: Any) -> ExtractAction | None:
        """解析單條 action dict。格式錯誤時記錄 warning 並回傳 None。"""
        if not isinstance(raw, dict):
            logger.warning("Skipping invalid action in rule %r: %r", rule_id, raw)
            return None

        target = raw.get("target")
        source = raw.get("source")
        method = raw.get("method")

        if not target:
            logger.warning("Skipping action in rule %r: missing 'target'", rule_id)
            return None
        if source not in ("command", "stdout_summary", "stderr_summary"):
            logger.warning(
                "Skipping action in rule %r: invalid source %r", rule_id, source
            )
            return None
        if method not in ("static", "regex"):
            logger.warning(
                "Skipping action in rule %r: invalid method %r", rule_id, method
            )
            return None

        value_type_raw = raw.get("value_type", "str")
        if value_type_raw not in ("str", "int", "float"):
            logger.warning(
                "Skipping action in rule %r: invalid value_type %r", rule_id, value_type_raw
            )
            return None

        # 處理 regex pattern 預編譯
        compiled_pattern: re.Pattern | None = None
        if method == "regex":
            pattern_str = raw.get("pattern")
            if not pattern_str:
                logger.warning(
                    "Skipping action in rule %r: regex method requires 'pattern'", rule_id
                )
                return None
            try:
                compiled_pattern = re.compile(str(pattern_str))
            except re.error as e:
                logger.warning(
                    "Skipping action in rule %r: invalid regex %r: %s",
                    rule_id, pattern_str, e,
                )
                return None

        # value 欄位
        raw_value = raw.get("value")
        value_str: str | None = str(raw_value) if raw_value is not None else None

        return ExtractAction(
            target=str(target),
            source=source,
            method=method,
            value=value_str,
            pattern=compiled_pattern,
            capture_group=int(raw.get("capture_group", 1)),
            value_type=value_type_raw,
        )

    def _matches(self, match: MatchCondition, event: ShellEvent) -> bool:
        """檢查 event 是否符合 MatchCondition（所有條件為 AND）。"""
        if match.command_type is not None:
            if event.command_type.value != match.command_type:
                return False
        if match.command_contains is not None:
            if match.command_contains not in event.command:
                return False
        return True

    def _apply_action(
        self, action: ExtractAction, event: ShellEvent
    ) -> str | int | float | None:
        """套用提取動作，回傳提取值（已轉型），或 None（無結果）。"""
        # 取得 source_text
        if action.source == "command":
            source_text = event.command
        elif action.source == "stdout_summary":
            source_text = event.stdout_summary or ""
        else:  # stderr_summary
            source_text = event.stderr_summary or ""

        # 依 method 取得 raw_value
        if action.method == "static":
            raw_value = action.value
            if raw_value is None:
                logger.warning(
                    "static action has no 'value' for target %r", action.target
                )
                return None
        else:  # regex
            assert action.pattern is not None
            m = action.pattern.search(source_text)
            if m is None:
                return None
            try:
                raw_value = m.group(action.capture_group)
            except IndexError:
                logger.warning(
                    "capture_group %d out of range for target %r",
                    action.capture_group, action.target,
                )
                return None
            if raw_value is None:
                return None

        # 型別轉換
        if action.value_type == "str":
            return str(raw_value)
        elif action.value_type == "int":
            try:
                return int(raw_value)
            except (ValueError, TypeError) as e:
                logger.warning(
                    "int conversion failed for %r (target %r): %s",
                    raw_value, action.target, e,
                )
                return None
        else:  # float
            try:
                return float(raw_value)
            except (ValueError, TypeError) as e:
                logger.warning(
                    "float conversion failed for %r (target %r): %s",
                    raw_value, action.target, e,
                )
                return None

    def _set_nested(self, data: dict, key: str, value: Any) -> None:
        """以 dot-notation key 將 value 寫入巢狀 dict。中間層不存在時自動建立。"""
        parts = key.split(".")
        current = data
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            if not isinstance(current[part], dict):
                logger.warning(
                    "_set_nested: intermediate key %r is not a dict, "
                    "skipping write for %r",
                    part, key,
                )
                return
            current = current[part]
        current[parts[-1]] = value
