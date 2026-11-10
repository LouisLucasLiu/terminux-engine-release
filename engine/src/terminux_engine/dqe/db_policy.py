# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE DB manager 解析的 canonical policy 層。

所有會寫入 DQE DB 的 package（`dc_shell_dqe`、`icc2_shell_dqe`／`mmmc_sum_dqe`／
`ptpx_dqe` 透過 `_be_dqe_common.db_writer`）共用同一個五層優先序與同一個
`TERMINUX_DQE_DEFAULT_DB_POLICY` 環境變數判斷。這是 domain-specific 邏輯
（DQE DB 是 dqe package 的內部概念），不屬於 Package Runtime Core，故放在
`terminux_engine.dqe` 底下而非 `package_runtime`。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from terminux_engine.dqe.db import DqeDbManager
from terminux_engine.package_runtime.context import PackageContext

_POLICY_ENV = "TERMINUX_DQE_DEFAULT_DB_POLICY"
_PATH_ENV = "TERMINUX_DQE_DB_PATH"

DQE_DB_DEFAULT_DENIED = "DQE_DB_DEFAULT_DENIED"
DQE_DB_POLICY_INVALID = "DQE_DB_POLICY_INVALID"


class DqeDbIsolationError(RuntimeError):
    """`TERMINUX_DQE_DEFAULT_DB_POLICY=deny` 且缺少明確 DB 來源時拋出。"""

    def __init__(self, message: str, *, reason: str = DQE_DB_DEFAULT_DENIED) -> None:
        super().__init__(message)
        self.reason = reason


class DqeDbPolicyError(ValueError):
    """`TERMINUX_DQE_DEFAULT_DB_POLICY` 值不合法（非 `allow`/`deny`/未設定）時拋出。"""

    def __init__(self, message: str, *, reason: str = DQE_DB_POLICY_INVALID) -> None:
        super().__init__(message)
        self.reason = reason


def get_default_db_policy() -> Literal["allow", "deny"]:
    """讀取 `TERMINUX_DQE_DEFAULT_DB_POLICY`；未設定時為 `allow`。"""
    raw = os.environ.get(_POLICY_ENV)
    if raw is None:
        return "allow"
    if raw == "allow" or raw == "deny":
        return raw
    raise DqeDbPolicyError(
        f"{_POLICY_ENV} must be 'allow' or 'deny', got {raw!r}"
    )


def resolve_dqe_db_manager(ctx: PackageContext) -> DqeDbManager:
    """依既有五層優先序解析 DQE DB manager。

    1. ``ctx.services.dqe_db_manager``
    2. ``ctx.facts["dqe_db_path"]``
    3. ``ctx.event.metadata["dqe_db_path"]``
    4. ``TERMINUX_DQE_DB_PATH``
    5. ``DqeDbManager()``（production 預設路徑）——僅在
       ``get_default_db_policy() == "allow"``（未設定時的既有行為）才可觸達；
       `deny` 時在建立/開啟此 default manager 之前拋出 `DqeDbIsolationError`。

    前四層順序與既有行為完全不變；只有第五層新增 policy gate。
    """
    services = ctx.services
    injected = getattr(services, "dqe_db_manager", None) if services is not None else None
    if injected is not None:
        return injected

    path = (
        ctx.facts.get("dqe_db_path")
        or ctx.event.metadata.get("dqe_db_path")
        or os.environ.get(_PATH_ENV)
    )
    if isinstance(path, str) and path:
        return DqeDbManager(Path(path).expanduser())

    policy = get_default_db_policy()
    if policy == "deny":
        raise DqeDbIsolationError(
            "no explicit DQE DB manager or path was provided "
            f"(services.dqe_db_manager / facts['dqe_db_path'] / "
            f"event.metadata['dqe_db_path'] / {_PATH_ENV} all absent) and "
            f"{_POLICY_ENV}=deny forbids falling back to the default "
            "production DQE DB"
        )
    return DqeDbManager()


def resolve_dqe_db_manager_for_run(
    ctx: PackageContext, *, run_id: str | None,
) -> DqeDbManager:
    """依 run_id 解析 DQE DB manager；imported baseline run 導向來源 DB。

    當 ``FlowBindingContext.imported_run_dqe_overrides`` 有這個 run_id 的
    覆寫時，回傳指向來源 DQE DB 的 manager（唯讀；來源位置由 Profile 釘選，
    不接受 caller 指定）。其他 run 仍走標準五層優先序。

    若呼叫端沒有提供 ``run_id``（latest run 模式），不走覆寫——因為 latest
    模式總是查目標 DB，不存在跨 DB 解析的語意。
    """
    if run_id is not None:
        from terminux_engine.bounded_execution.analysis_tasks.flow_binding_context import (
            get_current_flow_context,
        )

        flow_context = get_current_flow_context()
        if flow_context is not None:
            override_path = flow_context.imported_run_dqe_overrides.get(run_id)
            if isinstance(override_path, str) and override_path:
                return DqeDbManager(Path(override_path))
    return resolve_dqe_db_manager(ctx)
