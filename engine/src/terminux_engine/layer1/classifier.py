# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import logging
import re

from terminux_engine.core.plugin_manager import (
    BlockPattern,
    BlockResolverPlugin,
    BlockResolverProtocol,
    PluginManager,
)
from terminux_engine.ecc.models import ShellEvent

logger = logging.getLogger(__name__)


class CommandClassifier:

    def __init__(self, plugin_manager: PluginManager) -> None:
        self._plugin_manager = plugin_manager

    def classify(self, event: ShellEvent) -> ShellEvent:
        """
        依序嘗試所有已載入 Plugin 的 block resolver，
        若全部無結果則 fallback 到 member_id。
        event.block 填入後回傳同一個物件（in-place 修改）。
        不拋出 exception。
        """
        if event.block is not None:
            logger.warning(
                "classify() called with non-None block, skipping: %s", event.block
            )
            return event

        resolvers: list[BlockResolverPlugin] = (
            self._plugin_manager.get_block_resolvers()
        )

        for resolver in resolvers:
            result = self._try_resolver(
                resolver, event.work_directory, event.env_snapshot
            )
            if result is not None and result.strip() != "":
                event.block = result.strip()
                logger.debug(
                    "block resolved by plugin %s: %s",
                    resolver.plugin_id, event.block,
                )
                return event

        # Fallback
        event.block = event.member_id
        if not event.member_id:
            logger.warning("block fallback to member_id but member_id is empty")
        else:
            logger.debug("block fallback to member_id: %s", event.block)
        return event

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _try_resolver(
        self,
        resolver: BlockResolverPlugin,
        work_directory: str,
        env_snapshot: dict,
    ) -> str | None:
        """
        嘗試單一 BlockResolverPlugin：先呼叫 py_resolver，再嘗試 patterns。
        回傳第一個有效結果（strip 後非空字串），或 None。
        """
        # 1. py_resolver
        if resolver.py_resolver is not None:
            try:
                raw = resolver.py_resolver.resolve_block(work_directory, env_snapshot)
            except Exception as e:
                logger.warning(
                    "block_resolver.py of plugin %s raised exception: %s",
                    resolver.plugin_id, e,
                )
                raw = None

            if raw is not None:
                # resolve_block 回傳非 str 型別時，嘗試 str() 轉換
                if not isinstance(raw, str):
                    try:
                        raw = str(raw)
                    except Exception as e:
                        logger.warning(
                            "block_resolver.py of plugin %s returned non-str, "
                            "str() conversion failed: %s",
                            resolver.plugin_id, e,
                        )
                        raw = None

                if raw is not None and raw.strip() != "":
                    return raw.strip()

        # 2. patterns
        for pattern in resolver.patterns:
            result = self._try_pattern(pattern, work_directory, env_snapshot)
            if result is not None and result.strip() != "":
                return result.strip()

        return None

    def _try_pattern(
        self,
        pattern: BlockPattern,
        work_directory: str,
        env_snapshot: dict,
    ) -> str | None:
        """
        嘗試單一 BlockPattern。
        work_directory：對路徑做 regex search。
        env_var：從 env_snapshot dict 取 key。
        回傳字串或 None（無結果或出錯）。
        """
        if pattern.source == "work_directory":
            try:
                match = re.search(pattern.pattern, work_directory)
            except Exception as e:
                logger.warning(
                    "invalid regex pattern %r: %s", pattern.pattern, e
                )
                return None

            if match is None:
                return None

            try:
                return match.group(pattern.capture_group)
            except IndexError:
                logger.warning(
                    "capture_group %d out of range", pattern.capture_group
                )
                return None

        elif pattern.source == "env_var":
            try:
                return env_snapshot.get(pattern.pattern)
            except AttributeError as e:
                logger.warning(
                    "env_snapshot.get() failed (env_snapshot may not be a dict): %s", e
                )
                return None

        else:
            logger.warning("unknown pattern source %r", pattern.source)
            return None
