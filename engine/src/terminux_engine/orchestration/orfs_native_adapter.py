# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# 出處：本模組呼叫第三方開源工具 OpenROAD-flow-scripts（ORFS）與 OpenROAD
# （皆為 BSD 3-Clause，Terminux 未隨附），詳見 THIRD_PARTY_NOTICES.md 的 External EDA Tools 一節。
"""ORFS host-native adapter。

以目前使用者的帳號、在主機上直接執行由管理員安裝的 ORFS 與 EDA 工具；不使用
container，也不需要 root。ORFS-specific 的概念（`env.sh`、`WORK_HOME`、
`FLOW_VARIANT`、`DESIGN_CONFIG`、stage 檔名）只存在於本檔案，共同層只看到
`PreparedExecution.argv` 與中性的 `ArtifactCandidate`。

執行契約（依 ORFS `flow/Makefile` 與 `flow/scripts/variables.mk`）：

- make 必須在 `flow/` 執行：`include $(DESIGN_CONFIG)` 以 make 的 cwd 解析相對路徑；
- 輸出全部落在 `$(WORK_HOME)/{results,reports,logs,objects}/<platform>/<DESIGN_NICKNAME>/<variant>`，
  因此 ORFS 安裝目錄可以是唯讀的，使用者只需要能寫入自己的 `work_home`；
- 未明確設定 `OPENROAD_EXE`／`YOSYS_EXE` 時，ORFS 使用 `$(FLOW_HOME)/../tools/install/...`
  而不是 PATH，所以工具身分以 ORFS 自己的 `print-%` 解析結果記錄，不以 `command -v` 推論。

環境不繼承呼叫端的 shell：argv 以 `env -i` 開頭，只帶入 allowlist 內的變數；工具所需
的其他環境由使用者指定的 env script 提供。argv 與 script 內容全部由本檔案以
`shlex.quote` 組裝，`request.parameters` 非空即拒絕。

身分證據分兩層：執行前宣告與解析的檔案（路徑＋SHA-256）記在 receipt 的
`tool_version_evidence`；run 內由 ORFS 解析出的工具寫進一份 artifact。兩者都只代表
「解析到哪一個 binary」，不代表已證明 flow 實際執行的就是它。
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.execution_contract import (
    ArtifactCandidate,
    ArtifactDiscoveryResult,
    ExecutionContractError,
    ExecutionRequest,
    PreparedExecution,
    validate_within_boundary,
)
from terminux_engine.orchestration.orfs_adapter import (
    _LOG_ARTIFACTS,
    _REPORT_ARTIFACTS,
    _RESULT_ARTIFACTS,
    ORFS_TOOL_ID,
    host_git_head,
)

ORFS_NATIVE_ADAPTER_ID = "orfs.native"
#: adapter 實作本身的版本，不是 ORFS 或工具的版本。
ORFS_NATIVE_ADAPTER_VERSION = "orfs.native/1"
ORFS_NATIVE_RUNNER = "host_native"

#: run 內寫出的工具解析證據（相對於 logs root）。
TOOL_IDENTITY_FILENAME = "terminux_native_tool_identity.txt"
TOOL_IDENTITY_ROLE = "orfs.native_tool_identity"

#: 允許覆寫的 ORFS 工具變數（`variables.mk` 中明確設定即優先）。
TOOL_OVERRIDE_NAMES: frozenset[str] = frozenset({"OPENROAD_EXE", "YOSYS_EXE"})
#: 從呼叫端環境帶入子行程的變數名稱；其餘一律不帶。
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM",
)
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

#: source env script 之前與之後都不得存在的名稱（包含已定義的空值）。這些名稱會
#: 改變 make 的語意或輸出位置，必須只來自本 adapter 組裝的 make 參數。
FORBIDDEN_ENV_NAMES: tuple[str, ...] = (
    "MAKEFLAGS", "GNUMAKEFLAGS", "MAKEOVERRIDES", "MAKEFILES",
    "DESIGN_CONFIG", "FLOW_VARIANT", "WORK_HOME", "IN_NIX_SHELL",
)
#: 只能由 `tool_overrides` 設定的名稱；env script 不得注入。
_TOOL_ENV_NAMES: tuple[str, ...] = ("OPENROAD_EXE", "YOSYS_EXE")
#: ORFS 解析結果中要記錄的變數。
RESOLVED_VARIABLES: tuple[str, ...] = (
    "OPENROAD_EXE", "YOSYS_EXE", "PYTHON_EXE", "DESIGN_NICKNAME",
)

#: 固定的失敗碼，與工具本身的失敗區分。
EXIT_FORBIDDEN_ENV = 90
EXIT_DESIGN_NICKNAME_MISMATCH = 91

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_RELPATH = re.compile(r"^[A-Za-z0-9_./-]+$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    real = path.resolve()
    try:
        return {"path": str(path), "realpath": str(real), "sha256": _sha256_file(real),
                "size": real.stat().st_size}
    except OSError as exc:
        return {"path": str(path), "realpath": str(real), "sha256": None,
                "error": f"unreadable: {exc!r}"}


def _require_absolute_file(value: Path, label: str, *, executable: bool) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ExecutionContractError(f"{label} must be an absolute path, got {value!r}")
    if not value.is_file():
        raise ExecutionContractError(f"{label} is not an existing file: {value}")
    if executable and not os.access(value, os.X_OK):
        raise ExecutionContractError(f"{label} is not executable: {value}")
    if not executable and not os.access(value, os.R_OK):
        raise ExecutionContractError(f"{label} is not readable: {value}")
    return value


@dataclass(frozen=True)
class OrfsNativeAdapterConfig:
    """host-native ORFS 執行的部署設定。只存在於 adapter 建構時，不進共同 contract。"""

    orfs_repo_root: Path
    flow_dir: Path
    #: 本次輸出的根目錄（ORFS 的 `WORK_HOME`），必須由目前使用者擁有。
    work_home: Path
    env_script: Path
    make_executable: Path
    design: str
    platform: str
    variant: str
    design_config_relpath: str
    bash_executable: Path = Path("/bin/bash")
    env_executable: Path = Path("/usr/bin/env")
    tool_overrides: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label in ("orfs_repo_root", "flow_dir", "work_home"):
            value = getattr(self, label)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ExecutionContractError(f"{label} must be an absolute path, got {value!r}")
        if not self.flow_dir.is_dir():
            raise ExecutionContractError(f"flow_dir is not a directory: {self.flow_dir}")
        if not self.work_home.is_dir():
            raise ExecutionContractError(f"work_home is not a directory: {self.work_home}")
        object.__setattr__(self, "work_home", self.work_home.resolve())
        owner = self.work_home.stat().st_uid
        if owner != os.geteuid():
            raise ExecutionContractError(
                f"work_home must be owned by the current user (uid {os.geteuid()}), "
                f"owner is uid {owner}: {self.work_home}")
        if not os.access(self.work_home, os.W_OK | os.X_OK):
            raise ExecutionContractError(f"work_home is not writable: {self.work_home}")
        _require_absolute_file(self.env_script, "env_script", executable=False)
        _require_absolute_file(self.make_executable, "make_executable", executable=True)
        _require_absolute_file(self.bash_executable, "bash_executable", executable=True)
        _require_absolute_file(self.env_executable, "env_executable", executable=True)
        for label in ("design", "platform", "variant"):
            value = getattr(self, label)
            if not isinstance(value, str) or not _SAFE_TOKEN.match(value):
                raise ExecutionContractError(
                    f"{label} must match {_SAFE_TOKEN.pattern}, got {value!r}")
        self._validate_design_config()
        overrides = dict(self.tool_overrides)
        unknown = sorted(set(overrides) - TOOL_OVERRIDE_NAMES)
        if unknown:
            raise ExecutionContractError(
                f"tool_overrides only accepts {sorted(TOOL_OVERRIDE_NAMES)}, got {unknown}")
        for name, value in overrides.items():
            _require_absolute_file(value, f"tool_overrides.{name}", executable=True)
        object.__setattr__(self, "tool_overrides", overrides)

    def _validate_design_config(self) -> None:
        rel = self.design_config_relpath
        if (not isinstance(rel, str) or not rel or not _SAFE_RELPATH.match(rel)
                or rel.startswith("/") or ".." in Path(rel).parts):
            raise ExecutionContractError(
                "design_config_relpath must be a relative path under flow/ without '..', "
                f"got {rel!r}")
        target = (self.flow_dir / rel).resolve()
        try:
            target.relative_to(self.flow_dir.resolve())
        except ValueError as exc:
            raise ExecutionContractError(
                f"design_config_relpath resolves outside flow/: {target}") from exc
        if not target.is_file():
            raise ExecutionContractError(f"design config not found: {target}")

    def output_root(self, kind: str) -> Path:
        return self.work_home / kind / self.platform / self.design / self.variant


def _guard_lines(names: tuple[str, ...]) -> str:
    return "\n".join(
        f'if [ -n "${{{name}+x}}" ]; then '
        f'echo "TERMINUX_FORBIDDEN_ENV {name} is set" >&2; exit {EXIT_FORBIDDEN_ENV}; fi'
        for name in names)


def build_native_script(cfg: OrfsNativeAdapterConfig) -> str:
    """組出子行程內 bash 執行的固定 script；所有外部值都經 `shlex.quote`。

    順序：環境檢查 → `cd flow/` → source env script → 再檢查 → 工具覆寫 →
    以 ORFS `print-%` 記錄解析結果 → `exec` 設定的 make 跑 flow。
    """
    make = shlex.quote(str(cfg.make_executable))
    make_args = " ".join([
        f"DESIGN_CONFIG={shlex.quote(cfg.design_config_relpath)}",
        f"FLOW_VARIANT={shlex.quote(cfg.variant)}",
        f"WORK_HOME={shlex.quote(str(cfg.work_home))}",
    ])
    logs_root = shlex.quote(str(cfg.output_root("logs")))
    identity_path = shlex.quote(str(cfg.output_root("logs") / TOOL_IDENTITY_FILENAME))
    post_source_names = FORBIDDEN_ENV_NAMES + tuple(
        name for name in _TOOL_ENV_NAMES if name not in cfg.tool_overrides)
    overrides = "\n".join(
        f"export {name}={shlex.quote(str(path))}"
        for name, path in sorted(cfg.tool_overrides.items()))
    prints = " ".join(f"print-{name}" for name in RESOLVED_VARIABLES)
    design = shlex.quote(cfg.design)
    return f"""set -eu
{_guard_lines(FORBIDDEN_ENV_NAMES + _TOOL_ENV_NAMES)}
cd {shlex.quote(str(cfg.flow_dir))}
set +u
source {shlex.quote(str(cfg.env_script))}
set -u
{_guard_lines(post_source_names)}
{overrides}
mkdir -p {logs_root}
resolved=$({make} {make_args} {prints} 2>/dev/null </dev/null || true)
resolved_value() {{ printf '%s\\n' "$resolved" | sed -n "s/^$1: //p" | head -n 1; }}
{{
  echo "# terminux native tool identity"
  echo "identity_status: resolved_before_run"
  echo "execution_proof: not_established"
  echo "make.path: "{make}
  for name in {' '.join(RESOLVED_VARIABLES)}; do
    echo "$name.resolved: $(resolved_value "$name")"
  done
  for name in OPENROAD_EXE YOSYS_EXE; do
    tool=$(resolved_value "$name")
    if [ -n "$tool" ] && [ -f "$tool" ]; then
      real=$(readlink -f -- "$tool")
      echo "$name.realpath: $real"
      echo "$name.sha256: $(sha256sum -- "$real" | cut -d' ' -f1)"
      case "$name" in
        OPENROAD_EXE) version=$("$real" -version 2>&1 </dev/null | head -n 1 || true) ;;
        *) version=$("$real" -V 2>&1 </dev/null | head -n 1 || true) ;;
      esac
      echo "$name.version_text: $version"
    else
      echo "$name.status: unavailable"
    fi
  done
}} > {identity_path}
nickname=$(resolved_value DESIGN_NICKNAME)
if [ -n "$nickname" ] && [ "$nickname" != {design} ]; then
  echo "TERMINUX_DESIGN_NICKNAME_MISMATCH resolved=$nickname expected="{design} >&2
  exit {EXIT_DESIGN_NICKNAME_MISMATCH}
fi
exec {make} {make_args}
"""


def _allowlisted_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.setdefault("PATH", _DEFAULT_PATH)
    return env


class OrfsNativeAdapter:
    """ORFS 的 host-native `ExecutionAdapter`。"""

    adapter_id = ORFS_NATIVE_ADAPTER_ID
    adapter_version = ORFS_NATIVE_ADAPTER_VERSION

    def __init__(self, config: OrfsNativeAdapterConfig) -> None:
        self._config = config

    @property
    def config(self) -> OrfsNativeAdapterConfig:
        return self._config

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        if request.parameters:
            raise ExecutionContractError(
                "orfs native adapter does not accept parameters, got: "
                f"{sorted(request.parameters)}")
        cfg = self._config
        validate_within_boundary(cfg.work_home, request.output_boundary)
        child_env = _allowlisted_env()
        # `env -i` 清空繼承的環境；共同 executor 會把 PreparedExecution.env 疊在
        # 呼叫端環境上，因此隔離必須由 argv 本身完成。
        argv = [
            str(cfg.env_executable), "-i",
            *(f"{name}={value}" for name, value in sorted(child_env.items())),
            str(cfg.bash_executable), "--noprofile", "--norc", "-c",
            build_native_script(cfg),
        ]
        design_config = (cfg.flow_dir / cfg.design_config_relpath).resolve()
        adapter_metadata: dict[str, Any] = {
            "runner": ORFS_NATIVE_RUNNER,
            "adapter_version": ORFS_NATIVE_ADAPTER_VERSION,
            "host_flow_path": str(cfg.flow_dir),
            "work_home": str(cfg.work_home),
            "design": cfg.design,
            "platform": cfg.platform,
            "variant": cfg.variant,
            "design_config_relpath": cfg.design_config_relpath,
            "container_identity": None,
            "inputs_mounted_readonly": False,
            "network_isolation": "not_enforced_host_policy",
            "env_allowlist": sorted(child_env),
            "forbidden_env_names": list(FORBIDDEN_ENV_NAMES),
            "tool_version_evidence": {
                "tool_source": "host_env_script",
                "orfs_scripts_commit": host_git_head(cfg.orfs_repo_root),
                "declared": {
                    "env_script": _file_identity(cfg.env_script),
                    "make_executable": _file_identity(cfg.make_executable),
                    "bash_executable": _file_identity(cfg.bash_executable),
                    "design_config": _file_identity(design_config),
                    "tool_overrides": {name: _file_identity(path)
                                       for name, path in sorted(cfg.tool_overrides.items())},
                },
                "resolved_identity_role": TOOL_IDENTITY_ROLE,
                "resolved_identity_status": "resolved_before_run",
                "executed_identity": "NOT_VERIFIED",
            },
        }
        return PreparedExecution(
            argv=argv,
            cwd=cfg.work_home,
            env=None,
            timeout_seconds=request.timeout_seconds,
            adapter_metadata=adapter_metadata,
            artifact_discovery_context={
                "results_root": str(cfg.output_root("results")),
                "reports_root": str(cfg.output_root("reports")),
                "logs_root": str(cfg.output_root("logs")),
            },
        )

    def discover_artifacts(
        self,
        request: ExecutionRequest,
        prepared: PreparedExecution,
        *,
        process_exit_code: int,
    ) -> ArtifactDiscoveryResult:
        context = prepared.artifact_discovery_context
        results_root = Path(context["results_root"])
        reports_root = Path(context["reports_root"])
        logs_root = Path(context["logs_root"])
        boundary = request.output_boundary
        candidates: list[ArtifactCandidate] = []
        for role, filename in _RESULT_ARTIFACTS.items():
            candidates.append(self._candidate(role, results_root / filename, boundary))
        for role, filename in _REPORT_ARTIFACTS.items():
            candidates.append(self._candidate(role, reports_root / filename, boundary))
        for role, filename in _LOG_ARTIFACTS.items():
            candidates.append(self._candidate(role, logs_root / filename, boundary))
        candidates.append(self._candidate(
            TOOL_IDENTITY_ROLE, logs_root / TOOL_IDENTITY_FILENAME, boundary))
        return ArtifactDiscoveryResult(
            output_roots=[results_root, reports_root, logs_root],
            artifact_candidates=candidates,
            artifact_manifest_reference=None,
        )

    @staticmethod
    def _candidate(role: str, path: Path, boundary: Path) -> ArtifactCandidate:
        # 候選「位置」以字面路徑檢查，必須落在 work_home 內。位置若是 symlink，
        # 其目標由 artifact collector 依 output root 重新驗證：界外記為
        # boundary_rejected 且不讀內容，run 本身照常收尾、不會被當成有效產物。
        location = Path(os.path.normpath(path))
        try:
            location.relative_to(boundary)
        except ValueError as exc:
            raise ExecutionContractError(
                f"artifact location outside output_boundary: {location} not under {boundary}"
            ) from exc
        return ArtifactCandidate(adapter_role=role, host_path=location,
                                 exists=location.is_file())

    def cleanup(self, request: ExecutionRequest,
                prepared: PreparedExecution) -> dict[str, Any]:
        """host-native run 沒有 container 或其他 adapter 持有的外部資源。

        這個結果**不**代表取消已成功：Kernel 仍必須另外取得 executor 的
        `process_group_confirmed_gone=True`，才會把中斷的 run 判為 `cancelled`。
        """
        del request, prepared
        return {"attempted": True, "ok": True, "resource_kind": "none",
                "reason": "no_adapter_owned_resources",
                "process_termination": "confirmed_by_executor"}



def build_orfs_native_tool_definition(
    config: OrfsNativeAdapterConfig,
    *,
    expected_packages: list[str],
    default_timeout: float = 900.0,
):
    """建立 host-native ORFS 的 `ToolDefinition`；`event_tool_name` 等於 `tool_id`。"""
    from terminux_engine.orchestration.tool_registry import ToolDefinition

    return ToolDefinition(
        tool_id=ORFS_TOOL_ID,
        executables=[str(config.env_executable)],
        event_tool_name=ORFS_TOOL_ID,
        expected_packages=list(expected_packages),
        default_timeout=default_timeout,
        command_builder=lambda _parameters: [],  # 未被使用：adapter 已提供 argv
        adapter=OrfsNativeAdapter(config),
    )


__all__ = [
    "ENV_ALLOWLIST",
    "EXIT_DESIGN_NICKNAME_MISMATCH",
    "EXIT_FORBIDDEN_ENV",
    "FORBIDDEN_ENV_NAMES",
    "ORFS_NATIVE_ADAPTER_ID",
    "ORFS_NATIVE_ADAPTER_VERSION",
    "ORFS_NATIVE_RUNNER",
    "ORFS_TOOL_ID",
    "OrfsNativeAdapter",
    "OrfsNativeAdapterConfig",
    "RESOLVED_VARIABLES",
    "TOOL_IDENTITY_FILENAME",
    "TOOL_IDENTITY_ROLE",
    "TOOL_OVERRIDE_NAMES",
    "build_native_script",
    "build_orfs_native_tool_definition",
]
