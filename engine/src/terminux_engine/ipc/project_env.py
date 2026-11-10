# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""client 端 project 環境變數解析的**唯一**規則。

三個正式 client（C++ GUI、`orchestration.session_bootstrap`、`scripts/terminux-chat`）
在 session.init 前都用同一組規則決定「要不要宣告 project」：

| 設定情況                         | 行為                                        |
|----------------------------------|---------------------------------------------|
| 只有 `TERMINUX_PROJECT_ID`       | 使用它                                      |
| 只有舊 `TERMINUX_PROJECT`        | 相容採用，並給一句提示（舊名稱）            |
| 兩者相同                         | 正常採用                                    |
| 兩者非空且不同                   | **明確回報衝突**（`ProjectEnvConflictError`）|
| 兩者都未提供有效值               | session **未宣告** project（`project_id=None`）|

「有效值」＝去掉前後空白後非空。明確給了值但不合法（含空白／控制字元、過長）
⇒ `ProjectEnvInvalidError`，不以 fallback 掩蓋。

C++ 端 `src/ipc/project_env.cpp` 與 `scripts/terminux-chat`（stdlib-only）各自
實作**同一組**規則；規則改動三處要一起改。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

PRIMARY_ENV = "TERMINUX_PROJECT_ID"
LEGACY_ENV = "TERMINUX_PROJECT"
MAX_PROJECT_ID_CHARS = 128


class ProjectEnvError(ValueError):
    """project 環境變數無法決定出單一合法值。"""

    code = "TERM_PROJECT_ENV_INVALID"


class ProjectEnvConflictError(ProjectEnvError):
    code = "TERM_PROJECT_ENV_CONFLICT"


class ProjectEnvInvalidError(ProjectEnvError):
    code = "TERM_PROJECT_ENV_INVALID"


@dataclass(frozen=True)
class ProjectDeclaration:
    """client 要在 session.init 宣告的 project。

    - ``project_id``：``None`` ⇒ 不宣告（session 未綁 project，之後可採用
      Profile 的 project）。
    - ``source_env``：值來自哪個變數（``TERMINUX_PROJECT_ID``／``TERMINUX_PROJECT``），
      未宣告時為 ``None``。
    - ``warning``：使用舊名稱時的一句提示；否則 ``None``。
    """

    project_id: str | None
    source_env: str | None
    warning: str | None

    @property
    def declared(self) -> bool:
        return self.project_id is not None


def _valid_or_raise(value: str, env_name: str) -> str:
    if len(value) > MAX_PROJECT_ID_CHARS or any(
            ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProjectEnvInvalidError(
            f"{env_name}={value!r} is not a valid project id: it must not contain "
            f"whitespace or control characters and must be at most "
            f"{MAX_PROJECT_ID_CHARS} characters")
    return value


def resolve_project_declaration(
    environ: Mapping[str, str] | None = None,
) -> ProjectDeclaration:
    """依模組 docstring 的表格決定 session.init 要宣告的 project。"""
    env = os.environ if environ is None else environ
    primary = (env.get(PRIMARY_ENV) or "").strip()
    legacy = (env.get(LEGACY_ENV) or "").strip()

    if primary and legacy and primary != legacy:
        raise ProjectEnvConflictError(
            f"{PRIMARY_ENV}={primary!r} and {LEGACY_ENV}={legacy!r} disagree; unset one "
            f"of them (the supported name is {PRIMARY_ENV})")
    if primary:
        return ProjectDeclaration(project_id=_valid_or_raise(primary, PRIMARY_ENV),
                                  source_env=PRIMARY_ENV, warning=None)
    if legacy:
        return ProjectDeclaration(
            project_id=_valid_or_raise(legacy, LEGACY_ENV), source_env=LEGACY_ENV,
            warning=(f"{LEGACY_ENV} is a legacy name; it still works but please "
                     f"switch to {PRIMARY_ENV}"))
    return ProjectDeclaration(project_id=None, source_env=None, warning=None)


def session_init_project_params(declaration: ProjectDeclaration) -> dict[str, str]:
    """要合併進 session.init ``params`` 的欄位：未宣告 ⇒ **不送** ``project_id``。

    client 永遠不送 ``project_source``（來源由 daemon 決定）。
    """
    if declaration.project_id is None:
        return {}
    return {"project_id": declaration.project_id}


__all__ = [
    "LEGACY_ENV",
    "MAX_PROJECT_ID_CHARS",
    "PRIMARY_ENV",
    "ProjectDeclaration",
    "ProjectEnvConflictError",
    "ProjectEnvError",
    "ProjectEnvInvalidError",
    "resolve_project_declaration",
    "session_init_project_params",
]
