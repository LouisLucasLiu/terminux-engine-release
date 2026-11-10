# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# 出處：本模組呼叫第三方開源工具 OpenROAD-flow-scripts（ORFS）與 OpenROAD
# （皆為 BSD 3-Clause，Terminux 未隨附），詳見 THIRD_PARTY_NOTICES.md 的 External EDA Tools 一節。
"""ORFS Docker reference adapter（舊 container 執行模式；不再啟動新的 run）。

`terminux-orfs-run` 已改用 host-native 的 `orfs.native`
（`orfs_native_adapter.py`）。本模組保留給歷史 import、`orfs.docker` receipt／
artifact manifest 的讀取，以及 role 表與 `host_git_head` 等共用的純讀取工具：
`OrfsDockerAdapter.prepare()` 與 `build_orfs_tool_definition()` 一律以
`LEGACY_DOCKER_EXECUTION_DISABLED` 拒絕，`docker_image_identity()` 不呼叫
container client。沒有自動轉換，也沒有 Docker fallback。

實作 `ExecutionAdapter`——所有 ORFS-specific 概念（Docker image、bind
mount、`FLOW_VARIANT`、`DESIGN_CONFIG`、stage 檔名）**只存在於本檔案**，
不得滲入 `execution_contract.py`／`kernel.py`／`tool_registry.py`。共同層只看得到這個 adapter 回傳的 `PreparedExecution.argv`
（一個 `list[str]`）與 `ArtifactCandidate`（中性 role + path + exists）。

`OrfsDockerAdapter` 不使用 `shell=True`；Docker container 內需要的
`bash -c "..."` 語意，其字串內容是這個 adapter 自己組裝的 argv 的一個
元素，不是共同層解析或組裝的 shell command string（Implementation
Constraint #5）。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.docker_container_cleanup import (
    CleanupBudget,
    ContainerIdentity,
    ContainerOps,
    FixedCleanupBudget,
    SubprocessContainerOps,
    release_container,
)
from terminux_engine.orchestration.execution_contract import (
    ArtifactCandidate,
    ArtifactDiscoveryResult,
    ExecutionContractError,
    ExecutionRequest,
    PreparedExecution,
    validate_within_boundary,
)

ORFS_ADAPTER_ID = "orfs.docker"
# adapter 實作本身的版本（非 ORFS 版本，也非 image tag）。
ORFS_ADAPTER_VERSION = "orfs.docker/1"

# 每個候選 artifact 相對於 `results|reports|logs/<platform>/<design>/<variant>/`
# 的檔名與 adapter_role——完全是 ORFS 慣例，只存在於本檔案。
#: `1_synth.v` 與 `1_2_yosys.v` 是同一個合成輸出在不同 ORFS revision 的命名。
#: 兩個都宣告成候選（不存在的那一個會如實記成 `missing`），manifest 才會含
#: 實際產生的那一份；adapter 不判斷哪一個「應該」出現，也不改名合併。
_RESULT_ARTIFACTS = {
    "orfs.final_layout": "6_final.gds",
    "orfs.final_def": "6_final.def",
    "orfs.final_database": "6_final.odb",
    "orfs.final_netlist": "6_final.v",
    "orfs.synth_netlist_1_synth": "1_synth.v",
    "orfs.synth_netlist_1_2_yosys": "1_2_yosys.v",
}
_REPORT_ARTIFACTS = {
    "orfs.finish_report": "6_finish.rpt",
    "orfs.route_drc_report": "5_route_drc.rpt",
}
_LOG_ARTIFACTS = {
    "orfs.finish_log": "6_report.log",
    # CORE_UTILIZATION 的消費證據在 floorplan：OpenROAD 把它實際收到的
    # `-utilization` 寫在這份 log 裡。宣告成 artifact 之後，manifest 才會以
    # 相同 hash 保存它——否則「工具真的消費了這個值」只能靠讀檔，無法 attest。
    "orfs.floorplan_log": "2_1_floorplan.log",
}


@dataclass(frozen=True)
class OrfsDockerAdapterConfig:
    """ORFS adapter 的部署設定——只存在於 adapter 建構時，不進共同 contract。"""

    orfs_repo_root: Path
    flow_dir: Path
    docker_image: str
    design: str
    platform: str
    variant: str
    design_config_relpath: str
    #: 前景 CLI 指定的 container 身分（名稱加 label）。`None` 時 argv 與清理行為
    #: 都和之前相同。
    container_identity: ContainerIdentity | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "orfs_repo_root", Path(self.orfs_repo_root).resolve())
        object.__setattr__(self, "flow_dir", Path(self.flow_dir).resolve())
        for label, value in (
            ("docker_image", self.docker_image),
            ("design", self.design),
            ("platform", self.platform),
            ("variant", self.variant),
            ("design_config_relpath", self.design_config_relpath),
        ):
            if not value:
                raise ExecutionContractError(f"OrfsDockerAdapterConfig.{label} must not be empty")


def host_git_head(repo_root: Path) -> str:
    """從 host 端讀取 ORFS repo 的 HEAD commit（read-only，不進容器）。

    對應 vendor-neutral execution contract：adapter 從 host repo 取得 commit evidence，不依賴
    container 內 `.git`（容器映像建置時排除 `.git*`，見 vendor-neutral execution contract）或
    `metadata.json`（不執行 `genMetrics.py`）。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc!r}"
    if result.returncode != 0:
        return f"unavailable: {result.stderr.strip() or 'git rev-parse failed'}"
    return result.stdout.strip()


def docker_image_identity(image_ref: str, *,
                          docker_executable: str = "docker") -> dict[str, Any]:
    """從 host 端讀取 Docker image 的 **immutable** identity（read-only）。

    舊 container 模式已停用：只有套件內的 fixture corpus replay CLI（模擬
    docker CLI 邊界、從不啟動 container）會被查詢；其餘執行檔一律不呼叫，
    如實回報 `unavailable: LEGACY_DOCKER_EXECUTION_DISABLED`，不 fabricate。

    `docker_executable` 由 user 的 Profile `execution` 宣告決定（預設
    `docker`），與實際 `docker run` 用的是同一支 client。

    `docker_image` 是 mutable tag，單獨不足以作為 immutable
    identity；這裡額外取 image ID（content-addressed digest，永遠可得）
    與 RepoDigests（只有 pull 下來的 image 才有，本機 build 的 image 會是
    空清單——此時仍以 image ID 作為 immutable identity，不 fabricate）。

    不 pull、不 build、不修改任何 image。
    """
    from terminux_engine.orchestration.legacy_docker_execution import (
        LEGACY_DOCKER_EXECUTION_DISABLED,
        is_corpus_replay_executable,
    )

    identity: dict[str, Any] = {"docker_image_id": None, "docker_repo_digests": []}
    if not is_corpus_replay_executable(docker_executable):
        identity["docker_image_id"] = (
            f"unavailable: {LEGACY_DOCKER_EXECUTION_DISABLED}: the container client "
            "is not queried")
        return identity
    try:
        result = subprocess.run(
            [
                docker_executable, "image", "inspect", image_ref,
                "--format", "{{.Id}}\t{{json .RepoDigests}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        identity["docker_image_id"] = f"unavailable: {exc!r}"
        return identity
    if result.returncode != 0:
        identity["docker_image_id"] = (
            f"unavailable: {result.stderr.strip() or 'docker image inspect failed'}"
        )
        return identity
    raw = result.stdout.strip()
    image_id, _, digests_json = raw.partition("\t")
    identity["docker_image_id"] = image_id
    try:
        parsed = json.loads(digests_json) if digests_json else []
    except json.JSONDecodeError:
        parsed = []
    identity["docker_repo_digests"] = parsed if isinstance(parsed, list) else []
    return identity


class OrfsDockerAdapter:
    """ORFS 的 vendor-specific `ExecutionAdapter` 實作。"""

    adapter_id = ORFS_ADAPTER_ID
    adapter_version = ORFS_ADAPTER_VERSION

    def __init__(self, config: OrfsDockerAdapterConfig) -> None:
        self._config = config

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        # 舊 container 模式不再啟動新的 run：在 argv、image 查詢與任何
        # docker 呼叫之前拒絕。
        from terminux_engine.orchestration.legacy_docker_execution import (
            PATH_ORFS_RUN,
            LegacyDockerExecutionDisabled,
        )

        del request
        raise LegacyDockerExecutionDisabled(PATH_ORFS_RUN)

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

        candidates: list[ArtifactCandidate] = []
        for role, filename in {**_RESULT_ARTIFACTS}.items():
            candidates.append(self._candidate(role, results_root / filename, request.output_boundary))
        for role, filename in _REPORT_ARTIFACTS.items():
            candidates.append(self._candidate(role, reports_root / filename, request.output_boundary))
        for role, filename in _LOG_ARTIFACTS.items():
            candidates.append(self._candidate(role, logs_root / filename, request.output_boundary))

        return ArtifactDiscoveryResult(
            output_roots=[results_root, reports_root, logs_root],
            artifact_candidates=candidates,
            artifact_manifest_reference=None,
        )

    @staticmethod
    def _candidate(role: str, path: Path, boundary: Path) -> ArtifactCandidate:
        # 超出 output_boundary 的路徑不得納入
        # receipt。這裡的候選路徑全部由 adapter 自己組裝、落在
        # `flow_dir` 底下，理論上必然在界內；驗證是 defense-in-depth，
        # 若真的失敗則明確拋出而非靜默排除（diagnostic 由呼叫端的例外
        # 訊息保留）。
        validated = validate_within_boundary(path, boundary)
        return ArtifactCandidate(adapter_role=role, host_path=validated, exists=validated.is_file())


class ManagedContainerOrfsDockerAdapter(OrfsDockerAdapter):
    """有 container 身分的 ORFS adapter：`cleanup()` 確認這個 run 的 container 已不存在。

    由 Kernel 既有的 `finally` 呼叫，涵蓋正常結束、逾時、中斷與例外。只清理名稱
    與 label 都相符的 container；結果交給 Kernel 記錄，不改寫工具結果。
    """

    def __init__(self, config: OrfsDockerAdapterConfig, *,
                 container_ops: ContainerOps | None = None,
                 cleanup_budget: CleanupBudget | None = None) -> None:
        if config.container_identity is None:
            raise ExecutionContractError("a managed container adapter needs an identity")
        super().__init__(config)
        self._container_ops = container_ops or SubprocessContainerOps("docker")
        self._cleanup_budget = cleanup_budget

    def cleanup(self, request: ExecutionRequest,
                prepared: PreparedExecution) -> dict[str, Any]:
        del request, prepared
        identity = self._config.container_identity
        assert identity is not None
        return release_container(self._container_ops, identity,
                                 budget=self._cleanup_budget or FixedCleanupBudget())


ORFS_TOOL_ID = "orfs"


def build_orfs_tool_definition(
    config: OrfsDockerAdapterConfig,
    *,
    expected_packages: list[str],
    default_timeout: float = 900.0,
    container_ops: ContainerOps | None = None,
    cleanup_budget: CleanupBudget | None = None,
):
    """舊 container 模式的 ORFS `ToolDefinition`；已停用。

    一律以 `LEGACY_DOCKER_EXECUTION_DISABLED` 拒絕，不建立任何可派送的
    tool definition。新的 ORFS run 用 `build_orfs_native_tool_definition()`。
    """
    from terminux_engine.orchestration.legacy_docker_execution import (
        PATH_ORFS_RUN,
        LegacyDockerExecutionDisabled,
    )

    del config, expected_packages, default_timeout, container_ops, cleanup_budget
    raise LegacyDockerExecutionDisabled(PATH_ORFS_RUN)
