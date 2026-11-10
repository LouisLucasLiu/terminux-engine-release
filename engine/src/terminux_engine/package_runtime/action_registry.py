# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package action registry。"""

from __future__ import annotations

from typing import Any, Callable

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.errors import (
    ActionRegistryError,
    DuplicatePackageActionIdError,
)


class ActionRegistry:
    """註冊與解析 namespaced action。"""

    def __init__(self) -> None:
        self._actions: dict[str, Callable[..., Any]] = {}
        self._owners: dict[str, str] = {}

    def register(
        self,
        action_name: str,
        func: Callable[..., Any],
        *,
        package_id: str | None = None,
    ) -> None:
        """註冊 action callable。第二次註冊相同 id 會 raise，不覆寫既有 entry。"""
        self._validate_action_name(action_name, package_id=package_id)
        if not callable(func):
            raise ActionRegistryError(f"action is not callable: {action_name}")
        if action_name in self._actions:
            raise DuplicatePackageActionIdError(
                f"duplicate action id {action_name!r}: already registered from "
                f"{self._owners[action_name]!r}, cannot also register from {package_id!r}"
            )
        self._actions[action_name] = func
        self._owners[action_name] = package_id or action_name

    def resolve(self, action_name: str) -> Callable[..., Any]:
        """解析 action callable。"""
        try:
            return self._actions[action_name]
        except KeyError as exc:
            raise ActionRegistryError(f"unknown action: {action_name}") from exc

    def owner_of(self, action_name: str) -> str:
        """回傳註冊這個 action 的 package id（registry 驗證過的身分）。

        唯讀查詢，不改變任何註冊狀態。`register()` 已保證 action name 必須以
        `<package_id>.` 開頭，因此這裡拿到的 owner 是**註冊時驗證過**的身分，
        而不是把 action name 拆字串猜出來的。
        """
        try:
            return self._owners[action_name]
        except KeyError as exc:
            raise ActionRegistryError(f"unknown action: {action_name}") from exc

    def call(
        self,
        action_name: str,
        ctx: PackageContext,
        query: str | None = None,
    ) -> Any:
        """呼叫 action(ctx, query=None)。"""
        func = self.resolve(action_name)
        return func(ctx, query=query)

    @staticmethod
    def _validate_action_name(action_name: str, *, package_id: str | None = None) -> None:
        if "." not in action_name:
            raise ActionRegistryError(f"action name must be namespaced: {action_name}")
        if package_id is not None and not action_name.startswith(f"{package_id}."):
            raise ActionRegistryError(
                f"action name must start with package id '{package_id}.': {action_name}"
            )
