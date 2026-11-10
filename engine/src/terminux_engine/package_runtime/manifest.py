# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""package.yaml loader 與 validation。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from terminux_engine.package_runtime.entrypoint import (
    EntrypointError,
    MalformedEntrypointError,
    parse_entrypoint,
    resolve_entrypoint,
)
from terminux_engine.package_runtime.errors import DuplicateManifestKeyError, ManifestError
from terminux_engine.package_runtime.version import API_VERSION, SCHEMA_VERSION

_ALLOWED_PHASES = frozenset({"before", "main", "after"})


# ─── Duplicate YAML mapping key 偵測 ─────────────────────────────────────────
# PyYAML 的 yaml.safe_load() 預設對重複 mapping key 靜默保留最後一個，不報
# 錯。這是唯一一套 duplicate-key-safe loader，production `load_manifest()`
# 與 offline validator 共用同一個 class（validator 從本模組 import，不自製
# 第二套 YAML parser）。


class _DuplicateManifestKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping_no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    """建構 mapping 時偵測重複 key，並盡力附上巢狀 key path 供診斷使用。"""
    path = getattr(loader, "_manifest_key_path", [])
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            full_path = ".".join([*path, str(key)])
            raise DuplicateManifestKeyError(f"duplicate key: {key!r} (path: {full_path})")
        loader._manifest_key_path = [*path, str(key)]
        mapping[key] = loader.construct_object(value_node, deep=True)
        loader._manifest_key_path = path
    return mapping


_DuplicateManifestKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


def _load_yaml_text(text: str) -> Any:
    """統一的 manifest YAML 解析入口：duplicate-key-safe，不靜默覆寫。"""
    return yaml.load(text, Loader=_DuplicateManifestKeyLoader)
_ALLOWED_PROMPT_PATTERNS = frozenset({"judgment", "digest", "comparison"})


@dataclass(frozen=True)
class PackageIdentity:
    """package identity。"""

    id: str
    version: str
    api: str


@dataclass(frozen=True)
class ActionSpec:
    """Action 的 manifest metadata。

    ``handler`` 是既有的 ``module.function`` entrypoint；description 與
    parameters 僅供 Agent tool schema 使用，皆為選填。
    """

    handler: str
    description: str | None = None
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 這個 action 的 outer execution timeout（秒）。`None` 代表未宣告，
    #: 由呼叫端沿用既有預設，行為與加入此欄位之前完全相同。
    #:
    #: LLM-backed 的 action 必須明確宣告：它們的耗時由「最大 LLM 呼叫次數 ×
    #: per-call timeout ＋ bounded overhead」決定，遠大於 registry 的 5 秒預設
    #:。Common **不會**依 action 名稱、vendor 或 provider 推導這個
    #: 值——只讀 manifest 宣告。
    timeout_sec: float | None = None


@dataclass(frozen=True)
class PromptPhaseSpec:
    """Prompt 定義的 phase metadata。"""

    prompt_path: Path
    pattern: Literal["judgment", "digest", "comparison"]


@dataclass(frozen=True)
class PackageManifest:
    """Validated package manifest。"""

    package: PackageIdentity
    schema: int
    when_tools: tuple[str, ...] = ()
    phases: dict[str, str] = field(default_factory=dict)
    prompt_phases: dict[str, PromptPhaseSpec] = field(default_factory=dict)
    actions: dict[str, str] = field(default_factory=dict)
    action_specs: dict[str, ActionSpec] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    @property
    def package_id(self) -> str:
        """回傳 package id。"""
        return self.package.id

    @property
    def version(self) -> str:
        """回傳 package version。"""
        return self.package.version

    def has_phase(self, phase: str) -> bool:
        """檢查 manifest 是否宣告 phase。"""
        return phase in self.phases or phase in self.prompt_phases

    def is_prompt_phase(self, phase: str) -> bool:
        """檢查 phase 是否由 prompt 定義。"""
        return phase in self.prompt_phases

    def prompt_phase_spec(self, phase: str) -> PromptPhaseSpec:
        """回傳 prompt phase metadata。"""
        return self.prompt_phases[phase]

    def phase_entrypoint(self, phase: str) -> str:
        """回傳 phase entrypoint。"""
        return self.phases[phase]


def load_manifest(path: str | Path) -> PackageManifest:
    """讀取並驗證 package.yaml。"""
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"failed to read manifest: {manifest_path}: {exc}") from exc
    try:
        raw = _load_yaml_text(text)
    except DuplicateManifestKeyError as exc:
        raise ManifestError(f"duplicate key in manifest: {manifest_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"failed to parse manifest: {manifest_path}: {exc}") from exc
    return parse_manifest(raw, source_path=manifest_path)


def parse_manifest(raw: Any, *, source_path: Path | None = None) -> PackageManifest:
    """驗證 raw manifest dict 並回傳 PackageManifest。"""
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a mapping")

    package_raw = raw.get("package")
    if not isinstance(package_raw, dict):
        raise ManifestError("package is required")

    package_id = _required_str(package_raw, "id", "package.id")
    package_version = _required_str(package_raw, "version", "package.version")
    package_api = _required_str(package_raw, "api", "package.api")
    if package_api != API_VERSION:
        raise ManifestError(f"unsupported package.api: {package_api}")

    schema = raw.get("schema")
    if schema != SCHEMA_VERSION:
        raise ManifestError(f"unsupported schema: {schema}")

    package_dir = source_path.parent.resolve() if source_path is not None else None

    phases, prompt_phases = _validate_phase_map(
        raw.get("phases", {}), source_path=source_path, package_dir=package_dir
    )
    for phase_name in set(phases) | set(prompt_phases):
        if phase_name not in _ALLOWED_PHASES:
            raise ManifestError(f"unknown phase: {phase_name}")

    actions, action_specs = _validate_action_map(raw.get("actions", {}), package_dir=package_dir)
    for action_name in actions:
        if "." not in action_name:
            raise ManifestError(f"action name must be namespaced: {action_name}")
        if not action_name.startswith(f"{package_id}."):
            raise ManifestError(
                f"action name must start with package id '{package_id}.': {action_name}"
            )

    when_raw = raw.get("when", {})
    if when_raw is None:
        when_raw = {}
    if not isinstance(when_raw, dict):
        raise ManifestError("when must be a mapping")
    tools_raw = when_raw.get("tools", [])
    if tools_raw is None:
        tools_raw = []
    if not isinstance(tools_raw, list) or not all(isinstance(item, str) for item in tools_raw):
        raise ManifestError("when.tools must be a list of strings")

    permissions = raw.get("permissions", {})
    if permissions is None:
        permissions = {}
    if not isinstance(permissions, dict):
        raise ManifestError("permissions must be a mapping")

    return PackageManifest(
        package=PackageIdentity(id=package_id, version=package_version, api=package_api),
        schema=schema,
        when_tools=tuple(tools_raw),
        phases=phases,
        prompt_phases=prompt_phases,
        actions=actions,
        action_specs=action_specs,
        permissions=permissions,
        source_path=source_path,
    )


def _required_str(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} is required")
    return value


def _validate_python_entrypoint(entrypoint: str, *, label: str, package_dir: Path | None) -> None:
    """驗證 Python entrypoint。`package_dir` 已知時做完整 grammar + containment
    檢查（重用 `entrypoint.py` 的 canonical resolver）；`package_dir` 為 `None`
    時（合成 dict、無真實檔案的呼叫）只做 grammar-only 檢查，不碰檔案系統。
    """
    try:
        if package_dir is not None:
            resolve_entrypoint(package_dir, entrypoint)
        else:
            parse_entrypoint(entrypoint)
    except MalformedEntrypointError as exc:
        raise ManifestError(
            f"{label} entrypoint must use module.function format: {exc}"
        ) from exc
    except EntrypointError as exc:
        raise ManifestError(f"{label} entrypoint invalid: {exc}") from exc


def _validate_entrypoint_map(
    value: Any, label: str, *, package_dir: Path | None = None
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a mapping")
    result: dict[str, str] = {}
    for name, entrypoint in value.items():
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{label} key must be a non-empty string")
        if not isinstance(entrypoint, str):
            raise ManifestError(
                f"{label}.{name} entrypoint must use module.function format"
            )
        _validate_python_entrypoint(entrypoint, label=f"{label}.{name}", package_dir=package_dir)
        result[name] = entrypoint
    return result


def _validate_phase_map(
    value: Any,
    *,
    source_path: Path | None,
    package_dir: Path | None = None,
) -> tuple[dict[str, str], dict[str, PromptPhaseSpec]]:
    """驗證 Python entrypoint 與 prompt phase 兩種宣告形式。"""
    if value is None:
        return {}, {}
    if not isinstance(value, dict):
        raise ManifestError("phases must be a mapping")

    phases: dict[str, str] = {}
    prompt_phases: dict[str, PromptPhaseSpec] = {}
    for phase_name, config in value.items():
        if not isinstance(phase_name, str) or not phase_name:
            raise ManifestError("phases key must be a non-empty string")

        if isinstance(config, str):
            _validate_python_entrypoint(
                config, label=f"phases.{phase_name}", package_dir=package_dir
            )
            phases[phase_name] = config
            continue

        if not isinstance(config, dict):
            raise ManifestError(
                f"phases.{phase_name} must be an entrypoint string or a mapping"
            )
        prompt_phases[phase_name] = _validate_prompt_phase_spec(
            config,
            phase_name=phase_name,
            source_path=source_path,
        )
    return phases, prompt_phases


def _validate_prompt_phase_spec(
    raw: dict[str, Any],
    *,
    phase_name: str,
    source_path: Path | None,
) -> PromptPhaseSpec:
    """驗證 prompt phase 的路徑、pattern 與 package 範圍。"""
    prompt = raw.get("prompt")
    pattern = raw.get("pattern")
    if not isinstance(prompt, str) or not prompt:
        raise ManifestError(f"phases.{phase_name}.prompt is required")
    if not isinstance(pattern, str) or pattern not in _ALLOWED_PROMPT_PATTERNS:
        allowed = ", ".join(sorted(_ALLOWED_PROMPT_PATTERNS))
        raise ManifestError(
            f"phases.{phase_name}.pattern must be one of: {allowed}"
        )
    if source_path is None:
        raise ManifestError(
            f"phases.{phase_name} prompt validation requires manifest source_path"
        )

    relative_path = Path(prompt)
    if relative_path.is_absolute():
        raise ManifestError(f"phases.{phase_name}.prompt must be a relative path")

    package_dir = source_path.parent.resolve()
    prompt_path = (package_dir / relative_path).resolve()
    try:
        prompt_path.relative_to(package_dir)
    except ValueError as exc:
        raise ManifestError(
            f"phases.{phase_name}.prompt must stay within package directory"
        ) from exc
    if not prompt_path.is_file():
        raise ManifestError(f"phases.{phase_name}.prompt file not found: {prompt}")

    return PromptPhaseSpec(prompt_path=prompt_path, pattern=pattern)


def _validate_action_map(
    value: Any, *, package_dir: Path | None = None
) -> tuple[dict[str, str], dict[str, ActionSpec]]:
    """驗證 action 的舊字串格式與含 Agent metadata 的物件格式。"""
    if value is None:
        return {}, {}
    if not isinstance(value, dict):
        raise ManifestError("actions must be a mapping")

    actions: dict[str, str] = {}
    specs: dict[str, ActionSpec] = {}
    for name, raw_action in value.items():
        if not isinstance(name, str) or not name:
            raise ManifestError("actions key must be a non-empty string")

        if isinstance(raw_action, str):
            handler = raw_action
            description = None
            parameters: dict[str, dict[str, Any]] = {}
            timeout_sec: float | None = None
        elif isinstance(raw_action, dict):
            handler = raw_action.get("handler")
            description = raw_action.get("description")
            parameters = _validate_action_parameters(
                raw_action.get("parameters", {}), action_name=name
            )
            timeout_sec = _validate_action_timeout(
                raw_action.get("timeout_sec"), action_name=name
            )
            if description is not None and (
                not isinstance(description, str) or not description.strip()
            ):
                raise ManifestError(
                    f"actions.{name}.description must be a non-empty string"
                )
        else:
            raise ManifestError(
                f"actions.{name} must be an entrypoint string or a mapping"
            )

        if not isinstance(handler, str):
            raise ManifestError(
                f"actions.{name} handler must use module.function format"
            )
        _validate_python_entrypoint(handler, label=f"actions.{name}", package_dir=package_dir)
        actions[name] = handler
        specs[name] = ActionSpec(
            handler=handler,
            description=description,
            parameters=parameters,
            timeout_sec=timeout_sec,
        )
    return actions, specs


#: 單一 action 的 outer timeout 上限。刻意有界——manifest 不得宣告 unbounded。
MAX_ACTION_TIMEOUT_SEC = 900.0


def _validate_action_timeout(value: Any, *, action_name: str) -> float | None:
    """驗證可選的 `timeout_sec`。未宣告回 `None`（沿用既有預設）。

    拒絕非數值、bool、非正數、NaN、無限大與超過上限的值——fail closed，
    不猜、也不允許 unbounded。
    """
    import math

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(
            f"actions.{action_name}.timeout_sec must be a positive number of seconds"
        )
    seconds = float(value)
    if math.isnan(seconds) or math.isinf(seconds):
        raise ManifestError(
            f"actions.{action_name}.timeout_sec must be finite; unbounded is not allowed"
        )
    if seconds <= 0:
        raise ManifestError(
            f"actions.{action_name}.timeout_sec must be greater than 0"
        )
    if seconds > MAX_ACTION_TIMEOUT_SEC:
        raise ManifestError(
            f"actions.{action_name}.timeout_sec exceeds the {MAX_ACTION_TIMEOUT_SEC}s "
            f"upper bound"
        )
    return seconds


def _validate_action_parameters(
    value: Any,
    *,
    action_name: str,
) -> dict[str, dict[str, Any]]:
    """驗證可選 parameters mapping，保留 JSON Schema 的欄位內容。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"actions.{action_name}.parameters must be a mapping")

    parameters: dict[str, dict[str, Any]] = {}
    for parameter_name, raw_schema in value.items():
        if not isinstance(parameter_name, str) or not parameter_name:
            raise ManifestError(
                f"actions.{action_name}.parameters key must be a non-empty string"
            )
        if not isinstance(raw_schema, dict):
            raise ManifestError(
                f"actions.{action_name}.parameters.{parameter_name} must be a mapping"
            )
        required = raw_schema.get("required")
        if required is not None and not isinstance(required, bool):
            raise ManifestError(
                f"actions.{action_name}.parameters.{parameter_name}.required must be a boolean"
            )
        parameters[parameter_name] = dict(raw_schema)
    return parameters
