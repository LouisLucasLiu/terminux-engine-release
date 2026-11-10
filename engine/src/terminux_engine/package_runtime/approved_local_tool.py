# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package 宣告的本機工具 adapter；只接受已固定的 task-owned 輸入。"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.execution_contract import (
    ArtifactCandidate, ArtifactDiscoveryResult, ExecutionContractError,
    ExecutionRequest, PreparedExecution, validate_within_boundary,
)
from terminux_engine.orchestration.domain_runner import DomainRunState
from terminux_engine.orchestration.single_tool_runner import SingleToolRunner


class ApprovedLocalToolAdapter:
    """通用 typed-change 工具接線；不解讀任何 CAD 方法或參數名稱。"""

    adapter_id = "approved_local_tool"
    adapter_version = "approved_local_tool/1"

    def __init__(self, *, executable: str, executable_sha256: str,
                 argument_template: list[str], materialized_sha256: str,
                 artifact_paths: list[str], candidate_value: str) -> None:
        path = Path(executable)
        if not path.is_absolute() or not path.is_file():
            raise ExecutionContractError("approved executable must be an existing absolute file")
        if not isinstance(executable_sha256, str) or len(executable_sha256) != 64:
            raise ExecutionContractError("approved executable digest is invalid")
        if not isinstance(materialized_sha256, str) or len(materialized_sha256) != 64:
            raise ExecutionContractError("approved input digest is invalid")
        if not isinstance(argument_template, list) or any(
                not isinstance(item, str) for item in argument_template):
            raise ExecutionContractError("argument template must be a string list")
        if (not isinstance(candidate_value, str) or not candidate_value
                or "{candidate_value}" not in argument_template):
            raise ExecutionContractError("candidate placeholder is required")
        if not isinstance(artifact_paths, list) or any(
                not isinstance(item, str) or not item or Path(item).is_absolute()
                or ".." in Path(item).parts for item in artifact_paths):
            raise ExecutionContractError("artifact paths must be task-owned relative paths")
        self._executable = path.resolve()
        self._executable_sha256 = executable_sha256
        self._argument_template = tuple(argument_template)
        self._materialized_sha256 = materialized_sha256
        self._artifact_paths = tuple(artifact_paths)
        self._candidate_value = candidate_value

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        """每次 launch 前重驗 executable 與 materialized bytes，不信任舊路徑。"""
        if set(request.parameters) != {"candidate_file", "output_root"}:
            raise ExecutionContractError("approved tool parameters differ from the fixed binding")
        candidate = validate_within_boundary(
            request.parameters["candidate_file"], request.output_boundary)
        output = validate_within_boundary(
            request.parameters["output_root"], request.output_boundary)
        if output != request.cwd:
            raise ExecutionContractError("tool cwd differs from approved output root")
        if (not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest()
                != self._materialized_sha256):
            raise ExecutionContractError("materialized candidate does not match task digest")
        if (not self._executable.is_file()
                or hashlib.sha256(self._executable.read_bytes()).hexdigest()
                != self._executable_sha256):
            raise ExecutionContractError("approved executable changed before launch")
        substitutions = {"{candidate_file}": str(candidate),
                         "{output_root}": str(output),
                         "{candidate_value}": self._candidate_value}
        argv = [str(self._executable)]
        for value in self._argument_template:
            if value in substitutions:
                argv.append(substitutions[value])
            elif "{" in value or "}" in value:
                raise ExecutionContractError("unknown argument placeholder")
            else:
                argv.append(value)
        return PreparedExecution(
            argv=argv, cwd=output, env=None,
            timeout_seconds=request.timeout_seconds,
            adapter_metadata={"executable_sha256": self._executable_sha256,
                              "materialized_sha256": self._materialized_sha256,
                              "adapter_version": self.adapter_version},
            artifact_discovery_context={"paths": list(self._artifact_paths)},
        )

    def discover_artifacts(
        self, request: ExecutionRequest, prepared: PreparedExecution, *,
        process_exit_code: int,
    ) -> ArtifactDiscoveryResult:
        """只回報已宣告、落在本 task output root 內的結果檔。"""
        output = Path(request.parameters["output_root"]).resolve()
        candidates = []
        for relative in self._artifact_paths:
            path = validate_within_boundary(output / relative, output)
            candidates.append(ArtifactCandidate(
                adapter_role="package.output", host_path=path, exists=path.is_file()))
        return ArtifactDiscoveryResult(output_roots=[output],
                                       artifact_candidates=candidates)


def _validate_descriptor_config(config: dict[str, Any]) -> None:
    """驗證 Package 所釘住的 descriptor adapter；不解讀其 domain 語意。"""
    required = {"schema_version", "tool_id", "adapter_spec", "adapter_kwargs",
                "parameters", "host_executable", "output_root", "artifact_root",
                "timeout_seconds", "step_id"}
    optional = {"dqe_hook", "event_metadata"}
    if (not required.issubset(config) or set(config) - required - optional
            or config.get("schema_version") != "package.adapter_binding.v1"):
        raise ExecutionContractError("package adapter config has unexpected fields")
    if (not isinstance(config["tool_id"], str) or not config["tool_id"]
            or not isinstance(config["adapter_spec"], str)
            or not config["adapter_spec"].startswith("terminux_engine.packages.")
            or not isinstance(config["adapter_kwargs"], dict)
            or not isinstance(config["parameters"], dict)
            or not isinstance(config["step_id"], str) or not config["step_id"]
            or not isinstance(config["timeout_seconds"], (int, float))
            or config["timeout_seconds"] <= 0):
        raise ExecutionContractError("package adapter identity or timeout is invalid")
    for key in ("host_executable", "output_root", "artifact_root"):
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ExecutionContractError("package adapter paths must be absolute")
    if not Path(config["host_executable"]).is_file():
        raise ExecutionContractError("package adapter host executable is unavailable")
    hook = config.get("dqe_hook")
    if hook is not None:
        if (not isinstance(hook, dict)
                or set(hook) != {"schema_version", "registry_loader_spec", "package_id", "input_binding"}
                or hook.get("schema_version") != "package.single_tool_dqe_hook.v1"
                or not isinstance(hook.get("registry_loader_spec"), str)
                or not hook["registry_loader_spec"].startswith("terminux_engine.packages.")
                or not isinstance(hook.get("package_id"), str) or not hook["package_id"]
                or not isinstance(hook.get("input_binding"), str) or not hook["input_binding"]):
            raise ExecutionContractError("package DQE hook is invalid")
    metadata = config.get("event_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ExecutionContractError("package event metadata is invalid")


def build_package_adapter(config: dict[str, Any]) -> Any:
    """由 Package descriptor 載入 adapter；Common 不依 module 名稱分支。"""
    _validate_descriptor_config(config)
    module_name, separator, class_name = config["adapter_spec"].partition(":")
    if not separator or not module_name or not class_name:
        raise ExecutionContractError("package adapter spec is invalid")
    adapter_type = getattr(importlib.import_module(module_name), class_name, None)
    if not callable(adapter_type):
        raise ExecutionContractError("package adapter is unavailable")
    adapter = adapter_type(**config["adapter_kwargs"])
    if not isinstance(getattr(adapter, "adapter_id", None), str):
        raise ExecutionContractError("package adapter identity is invalid")
    return adapter


def bind_package_run_identity(config: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    """讓 descriptor adapter 在 child 前綁定既有 Kernel run identity。

    這是可選的 Package adapter hook，不改變沒有 hook 的單工具語意；Common
    不解讀 proof／method，只要求回傳同一份已驗證 config shape。
    """
    _validate_descriptor_config(config)
    if (not isinstance(identity, dict) or any(
            not isinstance(identity.get(key), str) or not identity[key]
            for key in ("run_id", "task_id", "task_digest", "grant_id"))):
        raise ExecutionContractError("package run identity is invalid")
    adapter = build_package_adapter(config)
    binder = getattr(adapter, "bind_kernel_identity", None)
    if not callable(binder):
        return dict(config)
    bound = binder(dict(config), dict(identity))
    if not isinstance(bound, dict):
        raise ExecutionContractError("package run binding returned invalid config")
    _validate_descriptor_config(bound)
    return bound


def release_package_run_resources(config: dict[str, Any]) -> dict[str, Any] | None:
    """讓 descriptor adapter 在 runner 被強制終止後釋放它自己的外部資源。

    與 `bind_package_run_identity()` 同一種可選 hook：adapter 沒有
    `release_external_resources` 就回 `None`（沒有需要管理端代為清理的外部
    資源）；Common 不解讀 adapter 的資源種類，只要求回傳 JSON object。
    呼叫端（daemon immediate cancel）負責把結果獨立記錄，不改寫 run 的
    terminal 結果。
    """
    _validate_descriptor_config(config)
    adapter = build_package_adapter(config)
    releaser = getattr(adapter, "release_external_resources", None)
    if not callable(releaser):
        return None
    result = releaser(dict(config))
    if not isinstance(result, dict):
        raise ExecutionContractError("package resource release returned invalid result")
    return result


def validate_package_single_tool_config(config: dict[str, Any]) -> None:
    """固定 task-only child 的 JSON config；未知欄位不可悄悄生效。"""
    if isinstance(config, dict) and config.get("schema_version") == "package.adapter_binding.v1":
        _validate_descriptor_config(config)
        return
    required = {"tool_id", "executable", "executable_sha256", "argument_template",
                "materialized_sha256", "artifact_paths", "candidate_file",
                "output_root", "artifact_root", "timeout_seconds", "step_id",
                "candidate_value"}
    if not isinstance(config, dict) or set(config) != required:
        raise ExecutionContractError("package single-tool config has unexpected fields")
    if (not isinstance(config["tool_id"], str) or not config["tool_id"]
            or not isinstance(config["step_id"], str) or not config["step_id"]
            or not isinstance(config["timeout_seconds"], (int, float))
            or config["timeout_seconds"] <= 0):
        raise ExecutionContractError("package tool identity or timeout is invalid")
    root = Path(config["output_root"])
    candidate = Path(config["candidate_file"])
    artifact = Path(config["artifact_root"])
    if (not root.is_absolute() or not candidate.is_absolute()
            or not artifact.is_absolute() or not candidate.is_relative_to(root)):
        raise ExecutionContractError("package tool paths are outside task-owned roots")
    ApprovedLocalToolAdapter(
        executable=config["executable"],
        executable_sha256=config["executable_sha256"],
        argument_template=config["argument_template"],
        materialized_sha256=config["materialized_sha256"],
        artifact_paths=config["artifact_paths"],
        candidate_value=config["candidate_value"],
    )


class PackageSingleToolRunner(SingleToolRunner):
    """把固定 child config 映射為既有 Kernel 的單工具 workflow。"""

    workflow_type = "package_single_tool"

    def start_run(self, config: dict[str, Any]) -> DomainRunState:
        validate_package_single_tool_config(config)
        if config.get("schema_version") == "package.adapter_binding.v1":
            return super().start_run({
                "tool_id": config["tool_id"], "parameters": config["parameters"],
                "cwd": config["output_root"], "step_id": config["step_id"],
                "event_metadata": dict(config.get("event_metadata") or {}),
            })
        return super().start_run({
            "tool_id": config["tool_id"],
            "parameters": {"candidate_file": config["candidate_file"],
                           "output_root": config["output_root"]},
            "cwd": config["output_root"], "step_id": config["step_id"],
        })


__all__ = ["ApprovedLocalToolAdapter", "PackageSingleToolRunner", "bind_package_run_identity", "build_package_adapter",
           "release_package_run_resources", "validate_package_single_tool_config"]
