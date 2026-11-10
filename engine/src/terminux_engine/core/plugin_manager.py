# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import importlib.util
import logging
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import yaml

logger = logging.getLogger(__name__)

_VALID_OPS           = {"eq", "ne", "gt", "lt", "gte", "lte", "contains", "regex"}
_VALID_BLOCK_SOURCES = {"work_directory", "env_var"}


# ── Plugin 權限等級 ────────────────────────────────────────────────────────────

class PluginPermission(str, Enum):
    READ_ONLY = "READ_ONLY"
    NOTIFY    = "NOTIFY"
    SUGGEST   = "SUGGEST"
    AUTO_EXEC = "AUTO_EXEC"


# 授權等級序（低→高）：READ_ONLY < NOTIFY < SUGGEST < AUTO_EXEC
PERMISSION_ORDER: dict[PluginPermission, int] = {
    PluginPermission.READ_ONLY: 0,
    PluginPermission.NOTIFY:    1,
    PluginPermission.SUGGEST:   2,
    PluginPermission.AUTO_EXEC: 3,
}


# ── Block Resolver 相關型別（遷移自 layer1.classifier）───────────────

@dataclass
class BlockPattern:
    source:        str   # "work_directory" 或 "env_var"
    pattern:       str   # source=="work_directory"：regex 字串；source=="env_var"：env var 名稱
    capture_group: int   # 1-based；source=="env_var" 時忽略


class BlockResolverProtocol(Protocol):
    def resolve_block(self, work_directory: str, env_snapshot: dict) -> str | None: ...


@dataclass
class BlockResolverPlugin:
    plugin_id:   str
    patterns:    list[BlockPattern]
    py_resolver: BlockResolverProtocol | None   # block_resolver.py 的 module wrapper；無則 None


# ── Trigger 相關型別（遷移自 layer1.trigger）────────────────────────

@dataclass
class Condition:
    field: str   # ShellEvent 屬性名 或 extracted_data key
    op:    str   # eq / ne / gt / lt / gte / lte / contains / regex
    value: Any


@dataclass
class PluginTriggerDef:
    plugin_id:           str
    event_type:          str               # 目前唯一合法值："on_command_complete"
    conditions:          list[Condition]
    context_query:       dict | None
    ai_condition:        str | None
    advanced_perception: bool
    has_action_py:       bool


# ── 內部 Plugin 資料結構 ───────────────────────────────────────────────────────

@dataclass
class LoadedPlugin:
    """plugin_manager 內部使用，不對外暴露。"""
    plugin_id:               str
    name:                    str
    version:                 str
    permission:              PluginPermission
    # ── perception ────────────────────────────────────────────────────────────
    event_type:              str
    conditions:              list[Condition]
    context_query:           dict | None
    ai_condition:            str | None
    advanced_perception:     bool
    # ── action ────────────────────────────────────────────────────────────────
    ai_action:               str | None
    has_action_py:           bool
    # ── block_resolver ────────────────────────────────────────────────────────
    block_resolver_patterns: list[BlockPattern]
    has_block_resolver_py:   bool
    # ── loaded Python modules（None = 對應 .py 檔案不存在或 import 失敗）──────
    perception_module:       ModuleType | None
    action_module:           ModuleType | None
    block_resolver_module:   ModuleType | None
    # ── filesystem ────────────────────────────────────────────────────────────
    plugin_dir:              Path


# ── BlockResolverModuleWrapper：將 block_resolver.py module 包裝成 Protocol ──

class BlockResolverModuleWrapper:
    """
    讓 block_resolver.py module 符合 BlockResolverProtocol。
    plugin_manager 內部使用，作為 BlockResolverPlugin.py_resolver 的值。
    """
    def __init__(self, module: ModuleType) -> None:
        self._module = module

    def resolve_block(self, work_directory: str, env_snapshot: dict) -> str | None:
        """
        呼叫 block_resolver.py 的 resolve_block(work_directory, env_snapshot) -> str | None。
        拋出任何 exception → 向上傳遞（由 layer1.classifier 捕獲）。
        """
        return self._module.resolve_block(work_directory, env_snapshot)


# ── 錯誤型別 ──────────────────────────────────────────────────────────────────

class PluginCallError(Exception):
    """
    run_perception_py / run_action_py 呼叫失敗時拋出。
    注意：此型別同時定義於 layer2.parser（歷史原因）。
    兩者語意相同，layer2.parser 優先使用自身定義。
    """
    ...


class PluginLoadError(Exception):
    """單一 plugin 載入失敗時 plugin_manager 內部使用（不對外拋出）。"""
    ...


# ── PluginManager ─────────────────────────────────────────────────────────────

class PluginManager:

    def __init__(self, plugins_dir: str = "plugins") -> None:
        self._plugins_dir = Path(plugins_dir)
        self._plugins, self._failed_dirs = self._load_all()
        logger.info(
            "PluginManager initialized: %d loaded, %d failed",
            len(self._plugins), len(self._failed_dirs),
        )

    def reload(self) -> None:
        """
        重新掃描 plugins_dir，重建所有 LoadedPlugin。
        copy-on-write：先建構新 dict，最後一次性替換。
        """
        new_plugins, new_failed_dirs = self._load_all()
        self._plugins      = new_plugins       # Python GIL 保護，原子替換
        self._failed_dirs  = new_failed_dirs
        logger.info(
            "PluginManager reloaded: %d loaded, %d failed",
            len(self._plugins), len(self._failed_dirs),
        )

    # ── layer1.classifier ─────────────────────────────────────────────────────

    def get_block_resolvers(self) -> list[BlockResolverPlugin]:
        """回傳所有已載入 plugin 的 block resolver 資訊（snapshot）。"""
        result = []
        for lp in list(self._plugins.values()):
            py_resolver = (
                BlockResolverModuleWrapper(lp.block_resolver_module)
                if lp.block_resolver_module is not None
                else None
            )
            result.append(BlockResolverPlugin(
                plugin_id=lp.plugin_id,
                patterns=lp.block_resolver_patterns,
                py_resolver=py_resolver,
            ))
        return result

    # ── layer1.trigger ────────────────────────────────────────────────────────

    def get_trigger_defs(self) -> list[PluginTriggerDef]:
        """回傳所有已載入 plugin 的 trigger 定義（snapshot）。"""
        return [
            PluginTriggerDef(
                plugin_id=lp.plugin_id,
                event_type=lp.event_type,
                conditions=lp.conditions,
                context_query=lp.context_query,
                ai_condition=lp.ai_condition,
                advanced_perception=lp.advanced_perception,
                has_action_py=lp.has_action_py,
            )
            for lp in list(self._plugins.values())
        ]

    # ── layer3.executor ───────────────────────────────────────────────────────

    def get_plugin_permission(self, plugin_id: str) -> PluginPermission:
        """
        plugin_id 存在：回傳對應 permission。
        plugin_id 不存在：回傳 READ_ONLY，記錄 error log。
        """
        lp = self._plugins.get(plugin_id)
        if lp is None:
            logger.error("get_plugin_permission: plugin not found: %s", plugin_id)
            return PluginPermission.READ_ONLY
        return lp.permission

    # ── layer2.parser ─────────────────────────────────────────────────────────

    def get_plugin_ai_condition(self, plugin_id: str) -> str | None:
        """
        plugin_id 存在：回傳 perception.ai_condition（無宣告時回傳 None）。
        plugin_id 不存在：回傳 None，記錄 warning log。
        """
        lp = self._plugins.get(plugin_id)
        if lp is None:
            logger.warning("get_plugin_ai_condition: plugin not found: %s", plugin_id)
            return None
        return lp.ai_condition

    def get_plugin_ai_action(self, plugin_id: str) -> str | None:
        """
        has_action_py is True：回傳 None（action.py 優先）。
        has_action_py is False：回傳 plugin.yaml 的 action.ai_action。
        plugin_id 不存在：回傳 None，記錄 warning log。
        """
        lp = self._plugins.get(plugin_id)
        if lp is None:
            logger.warning("get_plugin_ai_action: plugin not found: %s", plugin_id)
            return None
        if lp.has_action_py:
            return None
        return lp.ai_action

    def get_plugin_advanced_perception(self, plugin_id: str) -> bool:
        """
        plugin_id 存在：回傳 perception.advanced_perception（無宣告預設 False）。
        plugin_id 不存在：回傳 False，記錄 warning log。
        """
        lp = self._plugins.get(plugin_id)
        if lp is None:
            logger.warning("get_plugin_advanced_perception: plugin not found: %s", plugin_id)
            return False
        return lp.advanced_perception

    def run_perception_py(
        self, plugin_id: str, event_dict: dict, context: Any
    ) -> bool:
        """
        呼叫 perception.py 的 evaluate(event_dict, context) 並回傳 bool 結果。
        plugin_id 不存在：PluginCallError
        perception_module 為 None：PluginCallError
        evaluate() 拋出 exception：包裝成 PluginCallError（__cause__ 保留原始）
        """
        lp = self._plugins.get(plugin_id)
        if lp is None:
            raise PluginCallError(f"Plugin not found: {plugin_id}")
        if lp.perception_module is None:
            raise PluginCallError(f"No perception.py for plugin: {plugin_id}")
        try:
            result = lp.perception_module.evaluate(event_dict, context)
            return bool(result)
        except Exception as e:
            raise PluginCallError(
                f"perception.py evaluate() failed for plugin '{plugin_id}': {e}"
            ) from e

    def run_action_py(
        self, plugin_id: str, event_dict: dict, context: Any
    ) -> None:
        """
        呼叫 action.py 的 execute(event_dict, context)。
        plugin_id 不存在：PluginCallError
        action_module 為 None：PluginCallError
        execute() 拋出 exception：包裝成 PluginCallError（__cause__ 保留原始）
        """
        lp = self._plugins.get(plugin_id)
        if lp is None:
            raise PluginCallError(f"Plugin not found: {plugin_id}")
        if lp.action_module is None:
            raise PluginCallError(f"No action.py for plugin: {plugin_id}")
        try:
            lp.action_module.execute(event_dict, context)
        except Exception as e:
            raise PluginCallError(
                f"action.py execute() failed for plugin '{plugin_id}': {e}"
            ) from e

    # ── layer2.context ────────────────────────────────────────────────────────

    def get_perception_module(self, plugin_id: str) -> ModuleType | None:
        """
        plugin_id 存在：回傳 perception_module（可能為 None，表示無 perception.py）。
        plugin_id 不存在：回傳 None，記錄 warning log。
        """
        lp = self._plugins.get(plugin_id)
        if lp is None:
            logger.warning("get_perception_module: plugin not found: %s", plugin_id)
            return None
        return lp.perception_module

    # ── diagnostic ────────────────────────────────────────────────────────────

    def loaded_plugin_ids(self) -> list[str]:
        return list(self._plugins.keys())

    def failed_plugin_dirs(self) -> list[str]:
        return list(self._failed_dirs)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_all(self) -> tuple[dict[str, LoadedPlugin], list[str]]:
        """掃描 plugins_dir，載入所有 plugin，回傳 (new_plugins, failed_dirs)。"""
        if not self._plugins_dir.is_dir():
            # 0.1.0 起不隨附任何 plugin，目錄不存在是正常狀態；
            # 只留 __init__／reload 的一行 INFO，不發 WARNING。
            logger.debug("plugins_dir not found: %s", self._plugins_dir)
            return {}, []

        new_plugins:  dict[str, LoadedPlugin] = {}
        failed_dirs:  list[str]               = []

        for entry in sorted(self._plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            try:
                lp = self._load_plugin(entry)
                if lp.plugin_id in new_plugins:
                    logger.warning(
                        "Duplicate plugin_id '%s': dir '%s' overrides previous",
                        lp.plugin_id, entry.name,
                    )
                new_plugins[lp.plugin_id] = lp
            except PluginLoadError as e:
                logger.error("Failed to load plugin dir '%s': %s", entry.name, e)
                failed_dirs.append(entry.name)

        # 目錄為空（沒有任何 plugin 子目錄）時不重複記錄，
        # 與目錄不存在相同，只留 __init__／reload 的一行 INFO。
        if new_plugins or failed_dirs:
            logger.info("Loaded %d plugins, %d failed", len(new_plugins), len(failed_dirs))
        return new_plugins, failed_dirs

    def _load_plugin(self, plugin_dir: Path) -> LoadedPlugin:
        """載入單一 plugin 目錄，回傳 LoadedPlugin。失敗拋出 PluginLoadError。"""

        # ── Step 1: Parse plugin.yaml ─────────────────────────────────────────
        yaml_path = plugin_dir / "plugin.yaml"
        try:
            with open(yaml_path) as f:
                raw = yaml.safe_load(f)
        except FileNotFoundError:
            raise PluginLoadError(f"Missing plugin.yaml in {plugin_dir.name}")
        except yaml.YAMLError as e:
            raise PluginLoadError(
                f"YAML parse error in {plugin_dir.name}/plugin.yaml: {e}"
            )

        if raw is None:
            raw = {}

        # Validate top-level required fields
        for req_field in ("id", "permission", "perception"):
            if req_field not in raw:
                raise PluginLoadError(
                    f"Missing required field in {plugin_dir.name}/plugin.yaml"
                )

        perception_raw = raw.get("perception")
        if not isinstance(perception_raw, dict) or "event" not in perception_raw:
            raise PluginLoadError(
                f"Missing required field in {plugin_dir.name}/plugin.yaml"
            )

        plugin_id = str(raw["id"])
        name      = str(raw.get("name", plugin_id))
        version   = str(raw.get("version", "0.0.0"))

        # Parse permission
        perm_value = raw["permission"]
        try:
            permission = PluginPermission(perm_value)
        except (ValueError, KeyError):
            raise PluginLoadError(f"Invalid permission: {perm_value}")

        event_type = str(perception_raw["event"])

        # Parse conditions
        conditions: list[Condition] = []
        for cond_raw in perception_raw.get("conditions") or []:
            op = str(cond_raw.get("op", ""))
            if op not in _VALID_OPS:
                raise PluginLoadError(f"Invalid op: {op}")
            conditions.append(Condition(
                field=str(cond_raw["field"]),
                op=op,
                value=cond_raw.get("value"),
            ))

        context_query = perception_raw.get("context_query") or None
        if isinstance(context_query, dict) and not context_query:
            context_query = None

        ai_condition_raw = perception_raw.get("ai_condition")
        ai_condition = str(ai_condition_raw).strip() if ai_condition_raw else None

        advanced_perception = bool(perception_raw.get("advanced_perception", False))

        # Parse action
        action_raw = raw.get("action") or {}
        ai_action_raw = action_raw.get("ai_action") if isinstance(action_raw, dict) else None
        ai_action = str(ai_action_raw).strip() if ai_action_raw else None

        # Parse block_resolver.patterns
        block_resolver_raw = raw.get("block_resolver") or {}
        block_resolver_patterns: list[BlockPattern] = []
        if isinstance(block_resolver_raw, dict):
            for pat_raw in block_resolver_raw.get("patterns") or []:
                source = str(pat_raw.get("source", ""))
                if source not in _VALID_BLOCK_SOURCES:
                    raise PluginLoadError(f"Invalid block_resolver source: {source}")
                block_resolver_patterns.append(BlockPattern(
                    source=source,
                    pattern=str(pat_raw.get("pattern", "")),
                    capture_group=int(pat_raw.get("capture_group", 1)),
                ))

        # ── Step 2: Import Python modules ─────────────────────────────────────

        def _try_import(
            module_name: str, required_callable: str
        ) -> tuple[ModuleType | None, bool]:
            py_path = plugin_dir / f"{module_name}.py"
            if not py_path.exists():
                return None, False
            try:
                spec = importlib.util.spec_from_file_location(
                    name=f"terminux_plugin.{plugin_id}.{module_name}",
                    location=py_path,
                )
                module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                if not callable(getattr(module, required_callable, None)):
                    logger.warning(
                        "Plugin '%s': %s.py missing callable '%s'; module disabled",
                        plugin_id, module_name, required_callable,
                    )
                    return None, False
                return module, True
            except Exception:
                logger.warning(
                    "Plugin '%s': failed to import %s.py:\n%s",
                    plugin_id, module_name, traceback.format_exc(),
                )
                return None, False

        perception_module,      _                = _try_import("perception",     "evaluate")
        action_module,          has_action_py    = _try_import("action",         "execute")
        block_resolver_module,  has_block_resolver_py = _try_import("block_resolver", "resolve_block")

        # If perception.py failed to load, advanced_perception is effectively False
        if perception_module is None:
            advanced_perception = False

        # ── Step 3: Assemble LoadedPlugin ─────────────────────────────────────
        return LoadedPlugin(
            plugin_id=plugin_id,
            name=name,
            version=version,
            permission=permission,
            event_type=event_type,
            conditions=conditions,
            context_query=context_query,
            ai_condition=ai_condition,
            advanced_perception=advanced_perception,
            ai_action=ai_action,
            has_action_py=has_action_py,
            block_resolver_patterns=block_resolver_patterns,
            has_block_resolver_py=has_block_resolver_py,
            perception_module=perception_module,
            action_module=action_module,
            block_resolver_module=block_resolver_module,
            plugin_dir=plugin_dir,
        )
