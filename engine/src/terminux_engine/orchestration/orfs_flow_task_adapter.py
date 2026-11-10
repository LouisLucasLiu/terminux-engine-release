# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# 出處：本模組呼叫第三方開源工具 OpenROAD-flow-scripts（ORFS）與 OpenROAD
# （皆為 BSD 3-Clause，Terminux 未隨附），詳見 THIRD_PARTY_NOTICES.md 的 External EDA Tools 一節。
"""flow task 專用的隔離 ORFS adapter。

與既有 `OrfsDockerAdapter` 的差別只有**隔離**，執行機制完全沿用同一套
（同一個 image、同一條 `make` 命令形式、同一組 artifact 慣例）：

| 項目 | 既有 adapter | 本 adapter |
| --- | --- | --- |
| inputs | `flow_dir` 可寫 bind | 固定 snapshot 的 `flow/` **唯讀**掛載 |
| outputs | 寫回同一個 `flow_dir` | 另一個**可寫**目錄，經 `WORK_HOME` 導向 |
| network | docker 預設 bridge | **`--network=none`** |
| 有效設定 | 無 | 執行前以 ORFS 自己的 `print-%`／`vars` 取得 |

`WORK_HOME` 是 ORFS 自己的變數（`flow/Makefile`：`export WORK_HOME?=.`），
`LOG_DIR`／`OBJECTS_DIR`／`REPORTS_DIR`／`RESULTS_DIR` 全部由它推導
（`flow/scripts/variables.mk`）。因此**不需要**逐一覆蓋四個輸出子目錄，
也不需要對唯讀 input 樹做任何寫入。

兩個 slot 的 container 內 input 路徑與命令形式**完全相同**；只有 host 端的
輸出 bind target 不同——那是操作差異，不是工程參數差異。

**client（RPC caller）不能塞任意 Docker flags**：argv 由本檔案組裝，
`request.parameters` 非空即拒絕。**user 在 Profile 內宣告**的
執行方式（docker args／docker executable／host-native 工具 binding）會經由 grant
綁定的 ledger metadata 傳到這裡，原樣接進 argv 並完整記錄；Terminux 不因內容
拒絕——OS 權限才是邊界。固定的隔離保證（inputs 唯讀、輸出獨立、預設無網路）
仍由本檔案先放進 argv；user args 接在其後，docker 以後者為準，這是 user 的選擇，
且已在 DRAFT 揭露。

每個 container 帶 `terminux.task_id`／`slot`／`run_id` label 與
`--cidfile`，cancel 依 cidfile 精確停止，不靠全機器 `docker ps`。
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
from dataclasses import dataclass
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
    docker_image_identity,
)

#: 新增的證據也要走正式 Collector → manifest → receipt reference。
#:
#: 只登記**兩個 slot 都會產生**的角色。ECO 專屬的檔案（拓樸差異、兩組
#: proof）刻意**不**進這張表：`artifact_role_availability` 是逐 role 比對
#: 兩個 run 的，只有 candidate 有的角色會造成不對稱 mismatch，讓整個比較被
#: 降級。那三份的身分改由 host 的 `VerifiedRepairEvidence` 以 path＋sha256
#: 驗證，並寫進 `flow_task.repair.v1`——證據一樣可追溯，只是不經 role 表。
_R5_REPORT_ARTIFACTS: dict[str, str] = {
    "orfs.terminux_r5_setup_endpoints": "terminux_r5_final_{slot}_setup_endpoints.json",
    "orfs.terminux_r5_core": "terminux_r5_final_{slot}_core.json",
}
_R5_RESULT_ARTIFACTS: dict[str, str] = {
    "orfs.terminux_r5_final_lec_netlist": "6_final_lec.v",
}

ADAPTER_ID = "orfs.docker.flow_task"
ADAPTER_VERSION = "orfs.docker.flow_task/1"

#: Container 內固定路徑。兩個 slot 相同。
CONTAINER_FLOW_PATH = "/OpenROAD-flow-scripts/flow"
CONTAINER_WORK_HOME = "/work"
#: baseline final netlist 的唯讀 reference 掛載點。固定路徑，
#: 因此容器內的 checker 不需要任何 `-e`（adapter 仍然一個都不傳）。
CONTAINER_REFERENCE_PATH = "/terminux_reference"

#: 執行前寫出的有效設定證據檔（相對於 WORK_HOME）。
EFFECTIVE_SETTINGS_RELPATH = "terminux_effective_settings.txt"
#: RC-4：執行當下實際 EDA binary 的身分證據（相對於 WORK_HOME）。
TOOL_BINARIES_RELPATH = "terminux_tool_binaries.txt"
#: 要記錄身分的工具。只查 `source env.sh` 之後 PATH 上實際解析到的那一個。
TOOL_BINARY_NAMES: tuple[str, ...] = ("openroad", "yosys")
#: ORFS 自己的 `vars` target 產物（相對於 WORK_HOME）。
ORFS_VARS_RELPATH_TEMPLATE = "objects/{platform}/{design}/{variant}/vars"

#: 初次頂層 Make 讀設計設定前，這些名稱一律不得存在於環境中
#: （**包含已定義的空值**）。`${VAR+x}` 連空值都抓得到。
FORBIDDEN_ENV_NAMES: tuple[str, ...] = (
    "CORE_UTILIZATION",
    # 本版切換的合成策略與明確 floorplan 邊界同樣不得由頂層
    # 環境注入——它們必須來自已核准的 config bytes。
    "ABC_AREA", "ABC_CLOCK_PERIOD_IN_PS", "ABC_SCRIPT",
    "DIE_AREA", "CORE_AREA",
    "MAKEFLAGS", "GNUMAKEFLAGS", "MAKEOVERRIDES", "MAKEFILES",
)

#: 要記錄有效值的變數。由 ORFS 的 `print-%` 取得，**不解讀 log**。
CAPTURED_VARIABLES: tuple[str, ...] = (
    "CORE_UTILIZATION", "ABC_AREA", "DIE_AREA", "CORE_AREA",
    # 固定方法的參數。沒有它，effective settings 讀不到值，
    # run evidence 會直接判 `effective_value_mismatch`。
    "SETUP_SLACK_MARGIN",
    "DESIGN_CONFIG", "DESIGN_NAME", "PLATFORM",
    "FLOW_VARIANT", "VERILOG_FILES", "SDC_FILE", "PLATFORM_DIR",
    "WORK_HOME", "RESULTS_DIR", "OBJECTS_DIR", "LOG_DIR", "REPORTS_DIR",
)

#: assertion 失敗時的固定 exit code，與工具本身的失敗區分開。
EXIT_FORBIDDEN_ENV = 90
#: 本 slot 的 container id 檔（相對於 output_root）。
CIDFILE_NAME = "terminux_container.cid"


@dataclass(frozen=True)
class OrfsFlowTaskAdapterConfig:
    """本 adapter 的部署設定。全部由 host 依 profile／binding 決定。"""

    #: 固定 snapshot 內的 `flow/` 目錄（唯讀掛載來源）。
    snapshot_flow_dir: Path
    #: 本 slot 專屬的可寫輸出根目錄（掛到 `WORK_HOME`）。
    output_root: Path
    #: immutable image 身分（`sha256:<64 hex>`），不是可漂移的 tag。
    docker_image_id: str
    design: str
    platform: str
    variant: str
    #: 相對於 `flow/` 的 design config 路徑。
    design_config_relpath: str
    #: 唯讀 reference 目錄（setup repair 的 candidate 才有）。
    reference_dir: Path | None = None
    #: Profile 內的 `execution` 宣告（已由 flow_profile 驗過形狀）。
    execution: dict[str, Any] | None = None
    #: container 身分 label。由 host 依 grant 綁定的 metadata 提供。
    task_id: str | None = None
    slot: str | None = None
    run_id: str | None = None

    @property
    def runner(self) -> str:
        return str((self.execution or {}).get("runner") or "docker")

    @property
    def docker_executable(self) -> str:
        return str((self.execution or {}).get("docker_executable") or "docker")

    @property
    def user_docker_args(self) -> tuple[str, ...]:
        return tuple(str(a) for a in (self.execution or {}).get("docker_args") or ())

    @property
    def cidfile(self) -> Path:
        return self.output_root / CIDFILE_NAME

    @property
    def container_labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        for key, value in (("task_id", self.task_id), ("slot", self.slot),
                           ("run_id", self.run_id)):
            if value:
                labels += ["--label", f"terminux.{key}={value}"]
        for key, value in self.declared_identity_labels:
            labels += ["--label", f"terminux.{key}={value}"]
        return tuple(labels)

    @property
    def declared_identity_labels(self) -> tuple[tuple[str, str], ...]:
        """host 已驗證、原樣傳遞的執行身分標籤。

        adapter **不解讀**內容：它只把 binding 裡 host 放進去的 method 與
        fixture corpus 身分轉成 label，讓 Profile 指定的 executable 依身分選
        corpus，而不是依 design 名稱、參數名稱或候選值。
        """
        execution = self.execution or {}
        rows: list[tuple[str, str]] = []
        extension = execution.get("method_extension")
        if isinstance(extension, dict):
            for key in ("method_identity", "extension_version", "descriptor_sha256"):
                if extension.get(key):
                    rows.append((f"method_extension.{key}", str(extension[key])))
        corpus = execution.get("fixture_corpus")
        if isinstance(corpus, dict):
            for key in ("adapter_schema", "manifest_path", "manifest_sha256"):
                if corpus.get(key):
                    rows.append((f"fixture_corpus.{key}", str(corpus[key])))
        return tuple(rows)

    def __post_init__(self) -> None:
        if self.reference_dir is not None and (
                not isinstance(self.reference_dir, Path)
                or not self.reference_dir.is_absolute()):
            raise ExecutionContractError(
                "reference_dir must be an absolute Path when present")
        for field in ("snapshot_flow_dir", "output_root"):
            value = getattr(self, field)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ExecutionContractError(
                    f"{field} must be an absolute Path, got {value!r}")
        for field in ("docker_image_id", "design", "platform", "variant",
                      "design_config_relpath"):
            if not str(getattr(self, field)).strip():
                raise ExecutionContractError(f"{field} must not be empty")
        if not self.docker_image_id.startswith("sha256:"):
            raise ExecutionContractError(
                "docker_image_id must be an immutable sha256 identity, not a tag")

    @property
    def orfs_vars_relpath(self) -> str:
        return ORFS_VARS_RELPATH_TEMPLATE.format(
            platform=self.platform, design=self.design, variant=self.variant)


def _build_container_script(cfg: OrfsFlowTaskAdapterConfig, *,
                            flow_path: str = CONTAINER_FLOW_PATH,
                            work_home: str = CONTAINER_WORK_HOME,
                            env_source: str = "../env.sh",
                            make_executable: str = "make",
                            tool_env: dict[str, str] | None = None,
                            tool_binaries_from_env: bool = False) -> str:
    """組出 container（或 host-native）內要跑的固定 script。

    順序是刻意的：

    1. `source ../env.sh` **之前**與**之後**各驗一次禁止的環境名稱——
       env.sh 或 image 若注入了同名變數，這裡就會 fail closed。
    2. 用 ORFS 自己的 `print-%` 把有效設定寫進輸出區（EDA stage 使用設定
       **之前**）。
    3. 用 ORFS 自己的 `vars` target 產生完整變數快照。
    4. 才跑真正的 flow。
    """
    make_args = (
        f"DESIGN_CONFIG={shlex.quote(cfg.design_config_relpath)} "
        f"FLOW_VARIANT={shlex.quote(cfg.variant)} "
        f"WORK_HOME={shlex.quote(work_home)}"
    )
    make = shlex.quote(make_executable)
    tool_exports = "\n".join(
        f"export {name}={shlex.quote(value)}"
        for name, value in sorted((tool_env or {}).items()))
    prints = " ".join(f"print-{name}" for name in CAPTURED_VARIABLES)
    guard_lines = "\n".join(
        f'if [ -n "${{{name}+x}}" ]; then '
        f'echo "TERMINUX_FORBIDDEN_ENV {name} is set (value=[${{{name}}}])" >&2; '
        f"exit {EXIT_FORBIDDEN_ENV}; fi"
        for name in FORBIDDEN_ENV_NAMES
    )
    settings_path = f"{work_home}/{EFFECTIVE_SETTINGS_RELPATH}"
    binaries_path = f"{work_home}/{TOOL_BINARIES_RELPATH}"
    tool_names = " ".join(TOOL_BINARY_NAMES)
    # host-native：ORFS 用明確設定的 OPENROAD_EXE／YOSYS_EXE，不搜尋 PATH，
    # 因此身分也從同一組變數記錄；預設（舊 runner）維持 PATH 解析，位元不變。
    resolve_line = (
        'case "$tool" in openroad) tool_path="${OPENROAD_EXE:-}" ;; '
        '*) tool_path="${YOSYS_EXE:-}" ;; esac'
        if tool_binaries_from_env
        else 'tool_path=$(command -v "$tool" 2>/dev/null || true)')
    return f"""set -eu
cd {shlex.quote(flow_path)}
# ── 1a. source 之前：host／image 環境不得帶入這些名稱 ──
{guard_lines}
source {shlex.quote(env_source)}
# ── 1b. source 之後：env.sh 也不得帶入 ──
{guard_lines}
{tool_exports}
mkdir -p {shlex.quote(work_home)}
# ── 1c. 實際 EDA binary 身分：realpath、bytes SHA-256 與版本輸出 ──
{{
  echo "# terminux tool binaries (resolved after env source)"
  for tool in {tool_names}; do
    {resolve_line}
    if [ -z "$tool_path" ]; then echo "$tool.status: unavailable"; continue; fi
    tool_real=$(readlink -f "$tool_path" 2>/dev/null || echo "$tool_path")
    echo "$tool.path: $tool_path"
    echo "$tool.realpath: $tool_real"
    echo "$tool.sha256: $(sha256sum "$tool_real" 2>/dev/null | cut -d' ' -f1)"
    case "$tool" in
      openroad) tool_version=$("$tool_real" -version 2>&1 </dev/null | head -1 || true) ;;
      *) tool_version=$("$tool_real" -V 2>&1 </dev/null | head -1 || true) ;;
    esac
    echo "$tool.version: $tool_version"
  done
}} > {shlex.quote(binaries_path)}
# ── 2. EDA stage 使用設定之前，取 ORFS 自己解析出的有效值 ──
{{
  echo "# terminux effective settings (make print-%)"
  {make} {make_args} {prints} 2>&1 | grep -E '^[A-Z_]+:' || true
  echo "# forbidden env names checked: {' '.join(FORBIDDEN_ENV_NAMES)}"
  echo "# argv: {make} {make_args}"
}} > {shlex.quote(settings_path)}
# ── 3. ORFS 自己的完整變數快照 ──
{make} {make_args} vars
# ── 4. 真正的 flow ──
{make} {make_args}
"""


_LOG = logging.getLogger(__name__)

#: 受管展示批次的帳目位置。只有測試／展示會設它。
MANAGED_BATCH_ENV = "TERMINUX_R5_BATCH_ROOT"
MANAGED_ATTEMPT_ENV = "TERMINUX_R5_BATCH_ATTEMPT"

#: 受管批次帳目模組（只存在於開發 repo 的展示 harness）。
_MANAGED_BATCH_HARNESS = "tests.support.r5_checks"


class ManagedBatchUnavailableError(RuntimeError):
    """設定了受管批次，但這次安裝沒有批次帳目 harness；派送不得繼續。"""

    code = "managed_batch_harness_unavailable"


def _reserve_managed_launch(cfg: Any) -> str | None:
    """在正式 EDA 派送前向受管批次預占 1 次 launch。

    沒有設定 `TERMINUX_R5_BATCH_ROOT` 時完全不做事——production 不經過它。
    預占失敗（額度用完或帳目不可確認）就讓派送**直接失敗**，不靜默放行。
    """
    root = os.environ.get(MANAGED_BATCH_ENV)
    if not root:
        return None
    from pathlib import Path as _Path

    # daemon 派生的 child 以 daemon 的 cwd 啟動，而那個 cwd 不一定是 repo
    # 根目錄（隔離展示 daemon 就是跑在自己的臨時目錄裡）。批次帳目模組屬於
    # 展示 harness，只能用**這個檔案自己的位置**推導 repo 根目錄來 import，
    # 不能依賴 cwd；否則預占會以 `ModuleNotFoundError` 讓 child 在派送前直接
    # 死掉，看起來像沒有原因的 `child_died`。
    _repo_root = _Path(__file__).resolve().parents[4]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    try:
        from tests.support.r5_checks import demo_batch as batch
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        # 只有 harness 本身（或它的上層 package）缺席才視為未提供；其他 import 錯誤照常拋出。
        if not (_MANAGED_BATCH_HARNESS.startswith(missing + ".")
                or missing.startswith(_MANAGED_BATCH_HARNESS)):
            raise
        raise ManagedBatchUnavailableError(
            f"{MANAGED_BATCH_ENV} is set but the managed batch harness is not "
            "available in this installation; refusing to dispatch") from exc

    batch.BATCH_ROOT = _Path(root)
    batch.LOCK_PATH = batch.BATCH_ROOT / "batch.lock"
    batch.LEDGER_PATH = batch.BATCH_ROOT / "reservations.jsonl"
    batch.COUNTER_LOCK_PATH = batch.BATCH_ROOT / "launch_counter.lock"
    # batch **身分與上限**也必須跟著 root 走。只改路徑而沿用模組預設，會用
    # 上一個 batch 的 id 去讀這一個 batch 的帳本，`_read_ledger` 會判成
    # 「這一行屬於別的 batch」並拋 BatchLedgerDamaged，於是 child 在組出
    # docker argv 前就死掉。身分由 batch root 內的 `batch_identity.json`
    # 提供；沒有那個檔案時沿用模組預設（既有 batch 行為不變）。
    identity_path = batch.BATCH_ROOT / "batch_identity.json"
    if identity_path.is_file():
        import json as _json

        try:
            identity = _json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"the managed batch identity is unreadable: {exc}") from None
        if not isinstance(identity, dict) or not identity.get("batch_id"):
            raise RuntimeError("the managed batch identity is invalid")
        batch.BATCH_ID = str(identity["batch_id"])
        limit = identity.get("max_launches")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise RuntimeError(
                "the managed batch identity carries no positive max_launches")
        batch.MAX_LAUNCHES = limit
        if identity.get("approval_reference"):
            batch.APPROVAL_REFERENCE = str(identity["approval_reference"])
    attempt_id = os.environ.get(MANAGED_ATTEMPT_ENV, "unknown-attempt")
    return batch.reserve_launch(
        attempt_id=attempt_id,
        purpose=f"{cfg.design}/{cfg.platform} variant={cfg.variant}")


class OrfsFlowTaskAdapter:
    """flow task 專用的隔離 ORFS `ExecutionAdapter`。"""

    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    def __init__(self, config: OrfsFlowTaskAdapterConfig) -> None:
        self._config = config

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        if request.parameters:
            raise ExecutionContractError(
                "orfs flow task adapter does not accept parameters, got: "
                f"{sorted(request.parameters)}")
        cfg = self._config
        # 舊 container 模式與舊 host runner 不再啟動新的 run：在 launch 預占、
        # cidfile 與 argv 之前拒絕。只剩 fixture corpus replay（不呼叫 Docker）。
        from terminux_engine.orchestration.legacy_docker_execution import (
            reject_flow_task_execution,
        )

        reject_flow_task_execution(cfg.execution)
        # 受管展示批次的 launch 計數。**在組出 docker argv 之前**
        # 就要預占成功，否則不派送。只有測試／展示環境會設這個 env；
        # production 路徑完全不受影響。
        launch_id = _reserve_managed_launch(cfg)
        if launch_id:
            _LOG.info("managed batch launch reserved: %s", launch_id)
        # docker 拒絕已存在的 cidfile；上一個 attempt 的殘留在這裡清掉。
        try:
            cfg.cidfile.unlink()
        except FileNotFoundError:
            pass
        argv = [
            cfg.docker_executable, "run", "--rm",
            # EDA container 預設無網路；輸入只來自固定 snapshot/image。
            "--network=none",
            # 精確的 container 身分。
            *cfg.container_labels,
            "--cidfile", str(cfg.cidfile),
            # inputs 唯讀——after phase 也不可能寫進 input snapshot。
            "-v", f"{cfg.snapshot_flow_dir}:{CONTAINER_FLOW_PATH}:ro",
            # 本 slot 專屬的可寫輸出區，經 ORFS 自己的 WORK_HOME 導向。
            "-v", f"{cfg.output_root}:{CONTAINER_WORK_HOME}",
            # 功能驗證參考。**唯讀**，且與 inputs／outputs 都分開——
            # 它不是 candidate 的設計輸入，只給 checker 當 gold。
            *(["-v", f"{cfg.reference_dir}:{CONTAINER_REFERENCE_PATH}:ro"]
              if cfg.reference_dir is not None else []),
            # user 在 Profile 宣告、已在 DRAFT 揭露並核准的 args。
            *cfg.user_docker_args,
            cfg.docker_image_id,
            "bash", "-c", _build_container_script(cfg),
        ]
        results_root = cfg.output_root / "results" / cfg.platform / cfg.design / cfg.variant
        reports_root = cfg.output_root / "reports" / cfg.platform / cfg.design / cfg.variant
        logs_root = cfg.output_root / "logs" / cfg.platform / cfg.design / cfg.variant
        adapter_metadata: dict[str, Any] = {
            "container_flow_path": CONTAINER_FLOW_PATH,
            "container_work_home": CONTAINER_WORK_HOME,
            "host_input_path": str(cfg.snapshot_flow_dir),
            "host_output_path": str(cfg.output_root),
            "host_reference_path": (str(cfg.reference_dir)
                                    if cfg.reference_dir is not None else None),
            "reference_mounted_readonly": cfg.reference_dir is not None,
            "inputs_mounted_readonly": True,
            "network_mode": "none",
            "design": cfg.design,
            "platform": cfg.platform,
            "variant": cfg.variant,
            "design_config_relpath": cfg.design_config_relpath,
            "effective_settings_relpath": EFFECTIVE_SETTINGS_RELPATH,
            "orfs_vars_relpath": cfg.orfs_vars_relpath,
            "forbidden_env_names": list(FORBIDDEN_ENV_NAMES),
            "adapter_version": ADAPTER_VERSION,
            "runner": "docker",
            "docker_executable": cfg.docker_executable,
            "user_docker_args": list(cfg.user_docker_args),
            "container_identity": {
                "labels": {"terminux.task_id": cfg.task_id, "terminux.slot": cfg.slot,
                           "terminux.run_id": cfg.run_id,
                           **{f"terminux.{key}": value
                              for key, value in cfg.declared_identity_labels}},
                "cidfile": str(cfg.cidfile),
            },
            "tool_version_evidence": {
                # image 身分即工具身分——snapshot 內雖然有 tools/，本次執行
                # 的 executable 由 pinned image 供應。
                "docker_image": cfg.docker_image_id,
                "tool_source": "pinned_image",
                **docker_image_identity(cfg.docker_image_id,
                                        docker_executable=cfg.docker_executable),
            },
        }
        return PreparedExecution(
            argv=argv,
            cwd=cfg.output_root,
            # 不繼承任意 host Make 變數：明確給一份最小環境。
            env=self._container_client_env(),
            timeout_seconds=request.timeout_seconds,
            adapter_metadata=adapter_metadata,
            artifact_discovery_context={
                "slot": cfg.variant.rsplit("_", 1)[-1],
                "results_root": str(results_root),
                "reports_root": str(reports_root),
                "logs_root": str(logs_root),
                "work_home": str(cfg.output_root),
            },
        )

    @staticmethod
    def _container_client_env() -> dict[str, str]:
        """docker **client** 的環境。

        container 內部環境由 image ＋ `env.sh` 決定（docker 預設不傳遞 host
        環境，本 adapter 也不加任何 `-e`）；這裡只是不讓 host 的 Make 變數
        影響 docker client 本身。provider secrets 不出現在這裡。
        """
        import os

        keep = ("PATH", "HOME", "LANG", "LC_ALL", "DOCKER_HOST", "XDG_RUNTIME_DIR")
        env = {name: os.environ[name] for name in keep if name in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        return env

    def discover_artifacts(
        self, request: ExecutionRequest, prepared: PreparedExecution, *,
        process_exit_code: int,
    ) -> ArtifactDiscoveryResult:
        context = prepared.artifact_discovery_context
        results_root = Path(context["results_root"])
        reports_root = Path(context["reports_root"])
        logs_root = Path(context["logs_root"])

        candidates: list[ArtifactCandidate] = []
        for role, filename in _RESULT_ARTIFACTS.items():
            candidates.append(self._candidate(role, results_root / filename,
                                              request.output_boundary))
        for role, filename in _REPORT_ARTIFACTS.items():
            candidates.append(self._candidate(role, reports_root / filename,
                                              request.output_boundary))
        for role, filename in _LOG_ARTIFACTS.items():
            candidates.append(self._candidate(role, logs_root / filename,
                                              request.output_boundary))
        # setup repair 的共同證據。檔案不存在時 `exists=False`，對 flow task／bounded utilization search 的
        # 既有 run 沒有任何影響（兩側同樣缺席，因此仍然對稱）。
        slot = context.get("slot") or ""
        for role, template in _R5_REPORT_ARTIFACTS.items():
            candidates.append(self._candidate(
                role, reports_root / template.format(slot=slot),
                request.output_boundary))
        for role, filename in _R5_RESULT_ARTIFACTS.items():
            candidates.append(self._candidate(role, results_root / filename,
                                              request.output_boundary))
        return ArtifactDiscoveryResult(
            output_roots=[results_root, reports_root, logs_root],
            artifact_candidates=candidates,
            artifact_manifest_reference=None,
        )

    @staticmethod
    def _candidate(role: str, path: Path, boundary: Path) -> ArtifactCandidate:
        validated = validate_within_boundary(path, boundary)
        return ArtifactCandidate(adapter_role=role, host_path=validated,
                                 exists=validated.is_file())


NATIVE_ADAPTER_ID = "orfs.native.flow_task"
NATIVE_ADAPTER_VERSION = "orfs.native.flow_task/1"
#: 執行前（script 內）工具 pin 不符時的固定 exit code。
EXIT_NATIVE_TOOL_MISMATCH = 92
#: flow 實際使用、因此派送前必須核對的工具（`FORMAL_YOSYS` 屬於 formal proof）。
NATIVE_FLOW_TOOLS: tuple[str, ...] = ("MAKE", "OPENROAD_EXE", "YOSYS_EXE")
#: in-flow proof（setup-margin 的 transition miter）使用的 binary。Profile 有釘就與
#: flow 工具一起核對，並以 `NATIVE_PROOF_TOOL_ENV` 交給 Profile 釘住的 helper。
NATIVE_PROOF_TOOLS: tuple[str, ...] = ("FORMAL_YOSYS",)
NATIVE_PROOF_TOOL_ENV = "TERMINUX_FORMAL_YOSYS_EXE"


@dataclass(frozen=True)
class OrfsNativeFlowTaskAdapterConfig:
    """host-native flow task 的部署設定；工具全部來自 Profile 授權的 binding。"""

    snapshot_flow_dir: Path
    output_root: Path
    native_tool_binding: dict[str, Any]
    native_tool_binding_sha256: str
    design: str
    platform: str
    variant: str
    design_config_relpath: str
    reference_dir: Path | None = None
    execution: dict[str, Any] | None = None
    task_id: str | None = None
    slot: str | None = None
    run_id: str | None = None
    env_executable: Path = Path("/usr/bin/env")
    bash_executable: Path = Path("/bin/bash")

    @classmethod
    def from_execution(cls, *, execution: dict[str, Any] | None,
                       native_tool_binding_sha256: str,
                       **fields: Any) -> "OrfsNativeFlowTaskAdapterConfig":
        """binding 只能來自 grant 綁定的執行宣告，且 digest 必須與 config 相同。"""
        from terminux_engine.bounded_execution.analysis_tasks.canonical import (
            sha256_payload,
        )

        binding = (execution or {}).get("native_tool_binding")
        if not isinstance(binding, dict):
            raise ExecutionContractError(
                "a host-native run requires the native tool binding from its grant")
        if sha256_payload(binding) != native_tool_binding_sha256:
            raise ExecutionContractError(
                "the native tool binding differs from the one bound to this run")
        return cls(native_tool_binding=binding,
                   native_tool_binding_sha256=native_tool_binding_sha256,
                   execution=execution, **fields)

    def __post_init__(self) -> None:
        for field_name in ("snapshot_flow_dir", "output_root"):
            value = getattr(self, field_name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ExecutionContractError(f"{field_name} must be an absolute Path")
        if self.reference_dir is not None and not self.reference_dir.is_absolute():
            raise ExecutionContractError("reference_dir must be an absolute Path")
        for field_name in ("design", "platform", "variant", "design_config_relpath"):
            if not str(getattr(self, field_name)).strip():
                raise ExecutionContractError(f"{field_name} must not be empty")
        self.pins()  # 形狀不符即在建構時拒絕

    def pins(self) -> dict[str, Any]:
        from terminux_engine.orchestration.native_tool_identity import (
            NativeToolIdentityError,
            parse_pin,
        )

        tools = self.native_tool_binding.get("tools")
        if not isinstance(tools, dict) or any(name not in tools for name in NATIVE_FLOW_TOOLS):
            raise ExecutionContractError(
                f"the native tool binding must pin {list(NATIVE_FLOW_TOOLS)}")
        try:
            pins = {name: parse_pin(tools[name], where=f"tools.{name}")
                    for name in (*NATIVE_FLOW_TOOLS, *NATIVE_PROOF_TOOLS) if name in tools}
            pins["ENV_SCRIPT"] = parse_pin(self.native_tool_binding.get("env_script"),
                                           where="env_script")
        except NativeToolIdentityError as exc:
            raise ExecutionContractError(exc.message) from exc
        return pins

    @property
    def orfs_vars_relpath(self) -> str:
        return ORFS_VARS_RELPATH_TEMPLATE.format(
            platform=self.platform, design=self.design, variant=self.variant)


class OrfsNativeFlowTaskAdapter(OrfsFlowTaskAdapter):
    """以 Profile 授權的站點工具在主機上執行 flow task；沒有 container、沒有 image。"""

    adapter_id = NATIVE_ADAPTER_ID
    adapter_version = NATIVE_ADAPTER_VERSION

    def __init__(self, config: OrfsNativeFlowTaskAdapterConfig) -> None:
        self._native = config

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        from terminux_engine.orchestration.orfs_native_adapter import ENV_ALLOWLIST
        from terminux_engine.orchestration.native_tool_identity import (
            RUN_MARKER_ENV,
            NativeToolIdentityError,
            identity_layers,
            pgid_record_line,
            pin_check_script,
            strict_env_argv,
            verify_pin,
        )

        if request.parameters:
            raise ExecutionContractError(
                "orfs native flow task adapter does not accept parameters, got: "
                f"{sorted(request.parameters)}")
        cfg = self._native
        pins = cfg.pins()
        launch_id = _reserve_managed_launch(cfg)
        if launch_id:
            _LOG.info("managed batch launch reserved: %s", launch_id)
        # 派送前重新解析 realpath 並重算雜湊；不符即零派發。
        try:
            resolved = {name: verify_pin(pin, where=name,
                                         executable=name != "ENV_SCRIPT")
                        for name, pin in sorted(pins.items())}
        except NativeToolIdentityError as exc:
            raise ExecutionContractError(f"native tool identity rejected: {exc}") from exc
        tool_env = {"OPENROAD_EXE": pins["OPENROAD_EXE"].realpath,
                    "YOSYS_EXE": pins["YOSYS_EXE"].realpath}
        if "FORMAL_YOSYS" in pins:
            tool_env[NATIVE_PROOF_TOOL_ENV] = pins["FORMAL_YOSYS"].realpath
        script = "\n".join((
            "set -eu",
            pgid_record_line(cfg.output_root),
            pin_check_script(pins, exit_code=EXIT_NATIVE_TOOL_MISMATCH),
            _build_container_script(
                cfg, flow_path=str(cfg.snapshot_flow_dir), work_home=str(cfg.output_root),
                env_source=pins["ENV_SCRIPT"].realpath,
                make_executable=pins["MAKE"].realpath, tool_env=tool_env,
                tool_binaries_from_env=True),
        ))
        marker = {RUN_MARKER_ENV: cfg.run_id} if cfg.run_id else {}
        argv = strict_env_argv(
            [str(cfg.bash_executable), "--noprofile", "--norc", "-c", script],
            extra_env=marker)
        results_root = cfg.output_root / "results" / cfg.platform / cfg.design / cfg.variant
        reports_root = cfg.output_root / "reports" / cfg.platform / cfg.design / cfg.variant
        logs_root = cfg.output_root / "logs" / cfg.platform / cfg.design / cfg.variant
        adapter_metadata: dict[str, Any] = {
            "host_input_path": str(cfg.snapshot_flow_dir),
            "host_output_path": str(cfg.output_root),
            "host_reference_path": (str(cfg.reference_dir)
                                    if cfg.reference_dir is not None else None),
            "inputs_mounted_readonly": False,
            "network_isolation": "not_enforced_host_policy",
            "design": cfg.design, "platform": cfg.platform, "variant": cfg.variant,
            "design_config_relpath": cfg.design_config_relpath,
            "effective_settings_relpath": EFFECTIVE_SETTINGS_RELPATH,
            "orfs_vars_relpath": cfg.orfs_vars_relpath,
            "forbidden_env_names": list(FORBIDDEN_ENV_NAMES),
            "adapter_version": NATIVE_ADAPTER_VERSION,
            "runner": "host_native",
            "environment_boundary": "env -i allowlist",
            # 環境來源：只記名稱；值由 allowlist 與 pin 過的 env script 決定。
            "environment_provenance": {
                "inherited_names": sorted(ENV_ALLOWLIST),
                "tool_env_names": sorted(tool_env),
                "run_marker_env": RUN_MARKER_ENV,
                "env_script_sha256": pins["ENV_SCRIPT"].sha256,
            },
            "container_identity": None,
            "tool_version_evidence": {
                "tool_source": "native_tool_binding",
                "native_tool_binding_sha256": cfg.native_tool_binding_sha256,
                "native_tool_binding": cfg.native_tool_binding,
                "identity": identity_layers(
                    declared={name: pin.to_json_dict() for name, pin in sorted(pins.items())},
                    resolved=resolved),
                "docker_image": None,
            },
        }
        return PreparedExecution(
            argv=argv, cwd=cfg.output_root, env=None,
            timeout_seconds=request.timeout_seconds,
            adapter_metadata=adapter_metadata,
            artifact_discovery_context={
                "slot": cfg.variant.rsplit("_", 1)[-1],
                "results_root": str(results_root),
                "reports_root": str(reports_root),
                "logs_root": str(logs_root),
                "work_home": str(cfg.output_root),
            },
        )

    def cleanup(self, request: ExecutionRequest,
                prepared: PreparedExecution) -> dict[str, Any]:
        """沒有 container；只確認本 run 的 native process group 已不存在。"""
        from terminux_engine.orchestration.native_tool_identity import (
            release_native_process_group,
        )

        del request, prepared
        if not self._native.run_id:
            return {"attempted": False, "ok": True, "resource_kind": "native_process_group",
                    "reason": "no_run_identity"}
        return release_native_process_group(self._native.output_root, self._native.run_id)


def cancel_container_by_cidfile(output_root: str | Path, *,
                                docker_executable: str = "docker",
                                timeout_sec: float = 20.0) -> dict[str, Any]:
    """依本 slot 的 cidfile 精確停止**既有**的舊 container（stop-only）。

    身分：cidfile 由該 slot 的 `docker run --cidfile` 寫在 slot 專屬輸出區內，
    內容就是那個 container 的 id；這裡只對那個 id 送 `kill`。沒有 cidfile
    （尚未啟動、host-native run、或已 `--rm` 清掉）就如實回報；**絕不**用
    `docker ps` 猜、也不停別的 container。指令經 stop-only 形狀核對，不可能
    建立或啟動 container；id 形狀不合法時不呼叫 docker。
    """
    import subprocess

    from terminux_engine.orchestration.legacy_docker_execution import (
        LegacyExecutionDisabled,
        run_legacy_stop_only,
    )

    cid_path = Path(output_root) / CIDFILE_NAME
    try:
        cid = cid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return {"cidfile": str(cid_path), "container_id": None,
                "requested": False, "reason": "no cidfile for this slot"}
    if not cid:
        return {"cidfile": str(cid_path), "container_id": None,
                "requested": False, "reason": "empty cidfile"}
    try:
        result = run_legacy_stop_only(docker_executable, ["kill", cid],
                                      timeout=timeout_sec)
    except LegacyExecutionDisabled as exc:
        return {"cidfile": str(cid_path), "container_id": None,
                "requested": False, "reason": exc.message}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"cidfile": str(cid_path), "container_id": cid,
                "requested": True, "confirmed": False, "reason": repr(exc)}
    return {"cidfile": str(cid_path), "container_id": cid, "requested": True,
            "confirmed": result.returncode == 0,
            "exit_code": result.returncode,
            "stderr_tail": (result.stderr or "").strip()[-300:]}


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "EXIT_NATIVE_TOOL_MISMATCH",
    "NATIVE_ADAPTER_ID",
    "NATIVE_ADAPTER_VERSION",
    "NATIVE_FLOW_TOOLS",
    "NATIVE_PROOF_TOOLS",
    "NATIVE_PROOF_TOOL_ENV",
    "OrfsNativeFlowTaskAdapter",
    "OrfsNativeFlowTaskAdapterConfig",
    "CAPTURED_VARIABLES",
    "CIDFILE_NAME",
    "cancel_container_by_cidfile",
    "CONTAINER_FLOW_PATH",
    "CONTAINER_WORK_HOME",
    "EFFECTIVE_SETTINGS_RELPATH",
    "EXIT_FORBIDDEN_ENV",
    "FORBIDDEN_ENV_NAMES",
    "OrfsFlowTaskAdapter",
    "OrfsFlowTaskAdapterConfig",
]
