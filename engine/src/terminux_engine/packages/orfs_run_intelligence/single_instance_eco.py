# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""單一非 clock 資料路徑 buffer instance 的等功能尺寸替換 ECO。

這是最小、受限的 typed-change 方法：固定只改**一個**具名 instance，把它從
目前的 cell 換成「已核對的等功能 buffer 類別」裡的另一顆——同邏輯功能、
只是驅動能力不同。不是調參搜尋，不要求 timing 改善；候選是否真的算
「修復」由既有的 `gcd.setup_repair_acceptance.v1`
（`repair_acceptance.evaluate_repair()`）判定，這裡不重新發明驗收規則，
只負責「這個提案是不是一個合法、受限、可追溯的單一 instance 換型」。

已核對的等功能 buffer 類別 —— 已對照 pinned library 內容核對過
--------------------------------------------------------------

``BUF_X1``／``BUF_X2``／``BUF_X4``／``BUF_X8``／``BUF_X16``。**不是**只憑
名稱或 topology 曾出現過就宣稱相容：本機有一份與正式 pinned ORFS 版本同一個
commit（``be0dca0b1fd41df54792b3012350cd52bccd99bb``，見
``flow/scripts/variables.yaml`` 與 setup repair 使用的同一個 revision）的
OpenROAD-flow-scripts 原始碼檢出，
其中 ``flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib`` 是
本次真實執行實際載入的 timing library（library 名稱 ``NangateOpenCellLibrary``，
見該檔第 37 行 ``library (NangateOpenCellLibrary) {``）。逐一核對過這五個
cell 的定義：``pin(A){direction: input;}``／``pin(Z){direction: output;
function: "A";}`` 完全相同，只有 ``drive_strength``／``area``／電容值不同
——這是本包宣稱「等功能、只是驅動能力不同」的直接依據，不是猜測。

**一個容易誤解、特別核對過的地方**：同一份 library 裡的 ``CLKBUF_X1``／
``CLKBUF_X3`` 其實也是相同的 ``pin(A)``／``pin(Z)``／``function: "A"``
定義——單就 pin／功能而言，OpenROAD 的 ``equiv_cell_ports``（見下）不會把
它們判定為不相容。排除它們**不是**因為功能不同，而是範圍決定：本方法固定
的 stage（``place``，見下）在 ORFS 真實流程裡執行時，clock buffer 根本
還沒被插入——``clock_tree_synthesis`` 要到 CTS 階段才會產生它們（見
``flow/scripts/cts.tcl``），比 ``place`` 更晚。白名單只收錄 place 階段
資料路徑上真的會出現的非 clock buffer 家族，是一個階段範圍限制，不是
「這些 cell 功能不相容」的宣稱。

真實 EDA 邊界，以及工具原生已經做的檢查
----------------------------------------

本模組**不啟動任何工具**。所有 ``build_*`` 函式只組出真實 OpenROAD Tcl 與
docker 呼叫的**文字**，回傳值一律帶 ``executed: False``。

真正做替換的指令是 OpenROAD／OpenSTA 的 ``replace_cell``，定義在 pinned
``tools/OpenROAD``（子模組 commit ``6cb3f2b704604a8961573218e1c8adaae9fe9937``）
的 ``src/sta/network/NetworkEdit.tcl:212-228``::

    proc replace_cell { instance lib_cell } {
      set cell [get_lib_cell_warn "lib_cell" $lib_cell]
      if { $cell != "NULL" } {
        set inst [get_instance_error "instance" $instance]
        set inst_cell [$inst liberty_cell]
        if { $inst_cell == "NULL" || ![equiv_cell_ports $inst_cell $cell] } {
          return 0
        }
        replace_cell_cmd $inst $cell
        return 1
      } else {
        return 0
      }
    }

這個指令**本身就是一次原子的「先查再改」**：`lib_cell` 參數必須是
``<library>/<cell>``（如 ``NangateOpenCellLibrary/BUF_X2``，用法取自 pinned
``src/rsz/test/repair_setup_dont_touch_sizeup.tcl:63``）；找不到 lib cell
→ 優雅回傳 0（不拋例外）；instance 不存在 → ``get_instance_error`` 直接拋
Tcl 例外（fail closed，不會走到修改）；**pin 相容性檢查
（``equiv_cell_ports``）是工具自己在修改前做的**，不合格一樣回傳 0、完全
不修改任何東西。本包的白名單因此只需要擋「不在已知同功能家族內」與
「clock-tree／範圍外」這兩類；pin 級相容性由工具原生保證，不是本包重新
發明的檢查。

插入點 ``POST_RESIZE_TCL`` 的確切位置與前後鏈路（沿用既有 setup repair hook 的同一
個插入點，見 ``flow/scripts/resize.tcl``／``flow/Makefile``）：`resize`
stage 先 ``load_design 3_3_place_gp.odb 2_floorplan.sdc``，做完
resize／repair_timing 後執行 ``source_step_tcl POST RESIZE``（實際上就是
``source $::env(POST_RESIZE_TCL)``），**接著才** ``orfs_write_db
.../3_4_place_resized.odb``——hook 對活的、已載入的設計狀態做修改，寫檔
發生在 hook 之後，不是修改一份已經寫死的檔案。Makefile 的
``do-step,3_5_place_dp,$(RESULTS_DIR)/3_4_place_resized.odb,detail_place``
明確宣告下一個 stage（``detail_place``，legalization）的輸入就是這個
剛寫出的、含 hook 修改的 ODB；``3_5_place_dp.odb`` 再被複製成
``3_place.odb``，是 CTS stage（``do-step,4_1_cts,$(RESULTS_DIR)/3_place.odb
...``）真正讀取的檔案。整條鏈路裡沒有任何一步回頭重載修改前的快照——
detail_place 做的 legalization 是既有 flow 本來就會做的事，本方法不需要
（也不應該）另外處理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from terminux_engine._version import __version__

METHOD_ID = "gcd.single_instance_buffer_swap.v1"
METHOD_VERSION = "v1"
CHANGE_KIND = "single_instance_cell_swap"
#: 這個方法沒有單一數值候選（不像 SETUP_SLACK_MARGIN／CORE_UTILIZATION 的
#: `changes.parameter`／`changes.candidate`）；`MethodExtension.parameter`
#: 與 `flow_task_binding()` 的 `parameter_name` 都固定用這個字串，單一來源
#: 避免兩處各自硬編碼後漂移。
PARAMETER_NAME = "SINGLE_INSTANCE_BUFFER_SWAP"

#: 目前唯一支援、且有真實 hook 先例（`POST_RESIZE_TCL`，見
#: `post_resize_restructure.tcl`）的 stage。不支援 synthesis／CTS／route。
STAGE_PLACE = "place"
SUPPORTED_STAGES: frozenset[str] = frozenset({STAGE_PLACE})

#: 查過 pinned `flow/Makefile` 的 `do-step`／`do-copy` 依賴鏈，逐段確認
#: hook 修改後的 ODB 真的被下一段消費，不是被重載掉：`resize`（本方法
#: 掛 hook 的 stage）載入 `3_3_place_gp.odb`，寫出 `3_4_place_resized.odb`；
#: `detail_place`（legalization，既有 flow 自動接手，不是本方法要做的事）
#: 的宣告輸入**就是**這一份，寫出 `3_5_place_dp.odb`；複製成 `3_place.odb`，
#: 是 CTS stage 的宣告輸入。整條鏈沒有任何一步回頭讀取 hook 之前的快照。
PLACE_STAGE_DEPENDENCY_CHAIN: tuple[str, ...] = (
    "3_3_place_gp.odb", "3_4_place_resized.odb", "3_5_place_dp.odb",
    "3_place.odb")

#: 目前唯一有真實觀測證據的 platform／library。
LIBRARY_NANGATE45 = "nangate45"
SUPPORTED_LIBRARIES: frozenset[str] = frozenset({LIBRARY_NANGATE45})

#: 我們的 `library` 欄位值 → pinned .lib 檔裡真正的 `library (...)` 陳述式
#: 名稱（見模組 docstring；核對自
#: `flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib:37`）。
#: `replace_cell` 的 `lib_cell` 參數必須是這個名稱＋`/`＋cell 名稱。
LIBRARY_NAME_IN_LIB: dict[str, str] = {LIBRARY_NANGATE45: "NangateOpenCellLibrary"}

#: 已核對的等功能非 clock 資料路徑 buffer 家族——pin(A)/pin(Z)/function 逐一
#: 對照 pinned library 內容確認相同，只有 drive_strength／area 不同
#: （見模組 docstring）。
NON_CLOCK_BUFFER_FAMILY: tuple[str, ...] = (
    "BUF_X1", "BUF_X2", "BUF_X4", "BUF_X8", "BUF_X16")
NON_CLOCK_BUFFER_FAMILY_SET: frozenset[str] = frozenset(NON_CLOCK_BUFFER_FAMILY)

#: 同一份 pinned library 裡的 clock buffer；pin／function 定義其實與上面
#:的家族相同（`equiv_cell_ports` 不會拒絕），排除它們是因為 `place` 階段
#: 執行時 clock buffer 還沒被插入（CTS 才會插入），不是功能不相容——只用
#: 來在錯誤訊息裡舉例，不是排除規則本身（排除規則是「不在白名單內」）。
_OBSERVED_CLOCK_BUFFERS: tuple[str, ...] = ("CLKBUF_X1", "CLKBUF_X3")


def qualified_lib_cell(library: str, cell: str) -> str:
    """組出 `replace_cell` 需要的 `<library>/<cell>` 形式（見模組 docstring）。"""
    return f"{LIBRARY_NAME_IN_LIB[library]}/{cell}"

#: 唯一允許的宣告式材料化 step operation；與 SETUP_SLACK_MARGIN／
#: CORE_UTILIZATION 的 `orfs.eap.materialize_*` 同一種命名慣例。
MATERIALIZE_OPERATION = "orfs.eap.materialize_single_instance_swap"

#: `changes` 必須**恰好**包含這些鍵——沿用本 Package 既有 validator
#: （`proposal_validator.py`）對 `changes` 用 exact key set 把關的慣例。
REQUIRED_CHANGE_KEYS: frozenset[str] = frozenset({
    "kind", "stage", "library", "source_candidate", "target_instance",
    "expected_cell", "replacement_cell", "reason", "evidence_reference",
})
REQUIRED_SOURCE_CANDIDATE_KEYS: frozenset[str] = frozenset({
    "candidate_reference", "netlist_sha256"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: 單一具名 instance path：至少一段、每段是識別字（含常見 Verilog 分隔
#: 字元 `$`），不得含萬用字元或空白——一次只能指一個 instance。
_INSTANCE_PATH_RE = re.compile(r"^[A-Za-z_][\w$]*(/[A-Za-z_][\w$]*)*$")

REASON_OK = "ok"
REASON_CHANGE_FORMAT_INVALID = "change_format_invalid"
REASON_KIND_UNSUPPORTED = "kind_unsupported"
REASON_STAGE_UNSUPPORTED = "stage_unsupported"
REASON_LIBRARY_UNSUPPORTED = "library_unsupported"
REASON_SOURCE_CANDIDATE_INVALID = "source_candidate_invalid"
REASON_INSTANCE_INVALID = "target_instance_invalid"
REASON_CELL_NOT_IN_ALLOWED_FAMILY = "cell_not_in_allowed_family"
REASON_NO_OP_REPLACEMENT = "no_op_replacement"
REASON_REASON_MISSING = "reason_missing"
REASON_EVIDENCE_REFERENCE_MISSING = "evidence_reference_missing"


@dataclass(frozen=True)
class ProposalCheck:
    """`changes` dict 的格式與範圍判定。`passed=False` 時一定帶 reason codes。"""

    passed: bool
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {"status": "PASSED" if self.passed else "REJECTED",
                "reason_codes": list(self.reason_codes),
                "reasons": list(self.reasons)}


def _fail(codes: list[str], reasons: list[str], code: str, reason: str) -> None:
    if code not in codes:
        codes.append(code)
    reasons.append(reason)


def validate_single_instance_swap_change(changes: Any) -> ProposalCheck:
    """驗證 `changes` dict 本身的格式與固定範圍（靜態部分）。

    這裡**不**核對來源漂移（`expected_cell` 是不是真的是 instance 目前的
    cell）——那需要 host 端已驗證的 instance 快照，是 `check_source_drift()`
    的職責，且必須晚於這裡、早於任何材料化副作用。
    """
    codes: list[str] = []
    reasons: list[str] = []
    if not isinstance(changes, dict) or set(changes) != REQUIRED_CHANGE_KEYS:
        return ProposalCheck(False, (REASON_CHANGE_FORMAT_INVALID,),
                             (f"changes must be an object containing exactly "
                              f"{sorted(REQUIRED_CHANGE_KEYS)}",))
    if changes.get("kind") != CHANGE_KIND:
        _fail(codes, reasons, REASON_KIND_UNSUPPORTED,
              f"kind must be {CHANGE_KIND!r}")
    if changes.get("stage") not in SUPPORTED_STAGES:
        _fail(codes, reasons, REASON_STAGE_UNSUPPORTED,
              f"stage must be one of {sorted(SUPPORTED_STAGES)}")
    if changes.get("library") not in SUPPORTED_LIBRARIES:
        _fail(codes, reasons, REASON_LIBRARY_UNSUPPORTED,
              f"library must be one of {sorted(SUPPORTED_LIBRARIES)}")

    source = changes.get("source_candidate")
    if (not isinstance(source, dict)
            or set(source) != REQUIRED_SOURCE_CANDIDATE_KEYS
            or not isinstance(source.get("candidate_reference"), str)
            or not source["candidate_reference"].strip()
            or not isinstance(source.get("netlist_sha256"), str)
            or not _SHA256_RE.fullmatch(source["netlist_sha256"])):
        _fail(codes, reasons, REASON_SOURCE_CANDIDATE_INVALID,
              "source_candidate must be {candidate_reference: non-empty str, "
              "netlist_sha256: 64 lowercase hex chars}")

    instance = changes.get("target_instance")
    if (not isinstance(instance, str)
            or not _INSTANCE_PATH_RE.fullmatch(instance)):
        _fail(codes, reasons, REASON_INSTANCE_INVALID,
              "target_instance must be a single hierarchical instance path "
              "with no wildcard or whitespace")

    expected_cell = changes.get("expected_cell")
    replacement_cell = changes.get("replacement_cell")
    for label, cell in (("expected_cell", expected_cell),
                        ("replacement_cell", replacement_cell)):
        if cell not in NON_CLOCK_BUFFER_FAMILY_SET:
            _fail(codes, reasons, REASON_CELL_NOT_IN_ALLOWED_FAMILY,
                  f"{label}={cell!r} is not in the approved non-clock buffer "
                  f"family {NON_CLOCK_BUFFER_FAMILY}; clock-tree cells such as "
                  f"{_OBSERVED_CLOCK_BUFFERS} and sequential/cross-voltage "
                  "cells are out of scope for this method")
    if (expected_cell in NON_CLOCK_BUFFER_FAMILY_SET
            and replacement_cell in NON_CLOCK_BUFFER_FAMILY_SET
            and expected_cell == replacement_cell):
        _fail(codes, reasons, REASON_NO_OP_REPLACEMENT,
              "replacement_cell must differ from expected_cell; this method "
              "only accepts a real drive-strength change")

    if (not isinstance(changes.get("reason"), str)
            or not changes["reason"].strip()):
        _fail(codes, reasons, REASON_REASON_MISSING,
              "reason must be a non-empty string")
    if (not isinstance(changes.get("evidence_reference"), str)
            or not changes["evidence_reference"].strip()):
        _fail(codes, reasons, REASON_EVIDENCE_REFERENCE_MISSING,
              "evidence_reference must be a non-empty string identifying the "
              "persisted engineering evidence this change is based on")

    if codes:
        return ProposalCheck(False, tuple(codes), tuple(reasons))
    return ProposalCheck(True, (REASON_OK,),
                         ("the single-instance buffer swap proposal is well "
                          "formed and within the approved scope",))


REASON_SOURCE_DRIFT = "source_cell_drift"
REASON_INSTANCE_NOT_FOUND = "instance_not_found_in_snapshot"


def check_source_drift(
    *, expected_cell: str, observed_instance_snapshot: Any, target_instance: str,
) -> tuple[bool, str | None, str | None]:
    """核對提案宣稱的 `expected_cell` 是否符合 host 已驗證的 instance 快照。

    `observed_instance_snapshot` 必須是 host 端從本次真實（或受控標記為
    fixture 的）hook 輸出讀回的資料，不是提案自己宣稱的內容——與 CORE_UTILIZATION
    的 `_authorization_denial()` 同一個原則：範圍核對只信任 host 已驗證的
    lineage，不信任呼叫端自稱。

    回傳 `(ok, reason_code, reason)`；`ok=False` 時呼叫端必須在任何材料化
    副作用**之前**拒絕。
    """
    if not isinstance(observed_instance_snapshot, dict):
        return False, REASON_INSTANCE_NOT_FOUND, (
            "no verified instance snapshot is available for drift checking")
    instances = observed_instance_snapshot.get("instances")
    if not isinstance(instances, dict) or target_instance not in instances:
        return False, REASON_INSTANCE_NOT_FOUND, (
            f"instance {target_instance!r} was not found in the verified "
            "snapshot; it may not exist at this stage or the path is wrong")
    observed_cell = instances[target_instance].get("cell")
    if observed_cell != expected_cell:
        return False, REASON_SOURCE_DRIFT, (
            f"declared expected_cell={expected_cell!r} does not match the "
            f"verified snapshot's current cell {observed_cell!r} for "
            f"{target_instance!r}; the source has drifted since the proposal "
            "was written")
    return True, None, None


def acceptance_to_attempt_outcome(*, candidate_selected: bool) -> dict[str, Any]:
    """把 `method_extensions.evaluate_acceptance()` 的判定結果轉成
    `attempt.outcome.acceptance` 這個既有形狀——`eap3.py::candidate_qualification_history()`
    早就認得這個「沒有 FlowTask 工程摘要的 runtime（isolated fixture）以其
    acceptance 為證據」形狀（見 `_attempt_candidate_qualifies()`），
    這裡不是新發明一套判定機制，只是把已經算好的判定結果轉成那個既有
    reader 認得的形狀，讓 single_instance_eco 的候選也能走同一條既有的
    持久化／讀回路徑。

    只接受呼叫端已經從 `repair_acceptance.evaluate_repair()`（經
    `evaluate_acceptance()`）算好的 `candidate_selected`——不在這裡重新判定
    驗收邏輯。`complete` 固定 `True`：`evaluate_acceptance()` 是同步純函式，
    呼叫成功就代表判定已經跑到底，即使結論是 `UNVERIFIABLE`／
    `NOT_REPAIRED`（此時 `candidate_selected=False`），那也是一個明確、
    完整的判定，不是「還沒判定完」。
    """
    return {"complete": True, "passed": candidate_selected,
            "criteria_met": candidate_selected}


#: 逐一核對過 pinned 原始碼的相對執行位置與資料流依據（見模組 docstring）；
#: `EVIDENCE_FILENAME` 是本方法在 candidate slot 輸出區固定寫出的檔名
#: （對應 `bounded_execution/analysis_tasks/repair_evidence.py` 裡登記的
#: `terminux_b3_eco_diff.json`／`terminux_b3_instance_snapshot.json`）。
ECO_DIFF_FILENAME = "terminux_b3_eco_diff.json"
INSTANCE_SNAPSHOT_FILENAME = "terminux_b3_instance_snapshot.json"
#: 修改前後「target instance 接到的每一條 net、net 上除了 target
#: 以外還有哪些 pin」快照——不是只確認 target 的 cell 換了，而是保存非目標
#: instance／連接的比較依據。Operation 2 會寫出同一
#: 個格式的 `READBACK_NEIGHBOR_SNAPSHOT_FILENAME`，兩者在 host（Python）端
#: 逐欄位比對，不在 Tcl 裡手刻 JSON parser。
NEIGHBOR_SNAPSHOT_FILENAME = "terminux_b3_neighbor_snapshot.json"
READBACK_INSTANCE_SNAPSHOT_FILENAME = "terminux_b3_readback_instance_snapshot.json"
READBACK_NEIGHBOR_SNAPSHOT_FILENAME = "terminux_b3_readback_neighbor_snapshot.json"
#: Operation 1 在同一個行程內、修改前就先保存可取得的版本資訊——不為了
#: 探測版本額外啟動第三個 OpenROAD 行程。
#: **修正記錄**：原本假設 `openroad_version`／`openroad_git_describe`
#: 這兩個 pinned SWIG binding 全域指令（`tools/OpenROAD/src/OpenRoad.i:282-291`）
#: 可用，真實執行時發現本機 image 的實際 Tcl 直譯器裡是
#: `invalid command name`——原始碼裡有定義不等於這個 build 真的註冊了它，
#: 靜態核對到此為止不夠。已改成不呼叫任何版本查詢指令，固定寫
#: `"unavailable"`＋原因（見 `build_standalone_modify_and_save_tcl()`），
#: 不猜替代 API、不為了查版本額外啟動探測。
TOOL_VERSION_FILENAME = "terminux_b3_tool_version.json"


def _swap_and_verify_core_tcl(
    *, target_instance: str, expected_cell: str, replacement_cell: str,
    replacement_lib_cell: str, expected_target_pin_net: list[str] | None = None,
) -> str:
    """共用核心片段：先查再改、改後核對、寫出證據。**不能單獨執行**——呼叫端
    必須先把設計載入（`build_post_resize_hook_tcl()` 靠 `resize.tcl` 既有的
    `load_design`；`build_standalone_modify_and_save_tcl()` 自己呼叫同一個
    `load_design`），這裡只負責「查、改、再查、寫證據」。

    這是 Operation 1 唯一的核對／修改實作——`build_post_resize_hook_tcl()`
    （掛在真實 `POST_RESIZE_TCL` 的那份）與
    `build_standalone_modify_and_save_tcl()`（獨立的 Operation 1
    腳本）都呼叫這同一個函式，不是兩份平行的測試專用實作。

    補上先前遺留缺口的部分：
    - `net_delta` 改成對 target instance 每一根 pin 的 `pin=net` 字串在
      修改前後逐一比對算出來的整數，不是預填的常數 0（`get_nets -of_objects`
      ／`get_pins -of_objects` 兩個指令都對照過 pinned
      `tools/OpenROAD/src/sta/sdc/Sdc.tcl` 的 `get_pins`／`get_cells` proc
      定義，兩者都支援 `-of_objects`，用法與
      `tools/OpenROAD/src/rsz/test/gain_buffering3.tcl:69-70`
      （`get_nets -of_objects [get_pins ...]`）一致，不是猜測 API）。
    - 新增「target 接到的每一條 net，net 上除了 target 自己以外的 pin 清單」
      修改前後比對；只要有任何一條這樣的 net 的其他 pin 清單變了，視為範圍
      外連接變化，`error`、不寫出成功證據——這是「保存非目標 instance／
      連接的比較依據」的具體實作，範圍限定在真的可能被一次 pin-for-pin
      footprint 相容替換影響到的 net（即 target 自己的 pin 所連到的 net），
      不是宣稱掃過全設計的 net graph。

    single-instance ECO 正式鏈路：`expected_target_pin_net` 是 host 在 proposal 階段由已核對
    digest 的來源 netlist 解析出的 target pin→net（見
    `single_instance_eco_host.verify_source_candidate()`）。有提供時，在
    **修改之前**對實際載入的設計再核對一次連接，不一致就 `error`、不修改；
    與上面 cell 漂移核對是同一種「先查再改」。`None` 時（例如
    `build_post_resize_hook_tcl()`）不加這段，既有腳本內容不變。
    """
    connectivity_check = ""
    if expected_target_pin_net is not None:
        # 每個元素加大括號：net 名稱常含 `[`（例如 `req_msg[0]`），不加括號
        # Tcl 會把 `[0]` 當成 command substitution。呼叫端已保證元素不含
        # 大括號、反斜線或空白（見 adapter 的 `_PIN_NET_RE`）。
        if any(set(item) & set("{}\\ \t\n\"") for item in expected_target_pin_net):
            raise ValueError("expected_target_pin_net contains characters that need escaping")
        expected_list = " ".join("{" + item + "}" for item in expected_target_pin_net)
        connectivity_check = (
            "\n"
            "# ---- before: confirm the host-verified target connectivity ----\n"
            f"set expected_target_pin_net [list {expected_list}]\n"
            "if { [lsort $target_pin_net_before] ne"
            " [lsort $expected_target_pin_net] } {\n"
            "    error \"source connectivity drift for $target_instance:"
            " expected [lsort $expected_target_pin_net],"
            " found [lsort $target_pin_net_before]\"\n"
            "}\n")
    return (
        "# ---- before: confirm the approved instance/cell, before any mutation ----\n"
        "if { [catch {get_property [get_cells $target_instance] ref_name}"
        " current_ref] } {\n"
        "    error \"cannot confirm the current cell for instance"
        " $target_instance before modifying it: $current_ref\"\n"
        "}\n"
        "if { $current_ref ne $expected_cell } {\n"
        "    error \"source cell drift: expected $expected_cell,"
        " found $current_ref for $target_instance\"\n"
        "}\n"
        "if { [get_lib_cell $replacement_lib_cell] == \"NULL\" } {\n"
        "    error \"replacement cell not found in the loaded library:"
        " $replacement_lib_cell\"\n"
        "}\n"
        "set instance_count_before [sta::network_leaf_instance_count]\n"
        "set pin_count_before [sta::network_leaf_pin_count]\n"
        "set refs_before [dict create]\n"
        "foreach c [get_cells *] {\n"
        "    dict set refs_before [get_full_name $c] [get_property $c ref_name]\n"
        "}\n"
        "\n"
        "# ---- before: real pin->net connectivity for target and its neighbors ----\n"
        "set target_pin_net_before {}\n"
        "set neighbor_nets_before [dict create]\n"
        "foreach p [get_pins -of_objects [get_cells $target_instance]] {\n"
        "    set pin_full [get_full_name $p]\n"
        "    set pin_leaf [lindex [split $pin_full \"/\"] end]\n"
        "    set net [get_nets -of_objects $p]\n"
        "    set net_name \"\"\n"
        "    if { $net ne \"\" } {\n"
        "        set net_name [get_full_name $net]\n"
        "        set other_pins {}\n"
        "        foreach np [get_pins -of_objects $net] {\n"
        "            set np_full [get_full_name $np]\n"
        "            if { $np_full ne $pin_full } {\n"
        "                lappend other_pins $np_full\n"
        "            }\n"
        "        }\n"
        "        dict set neighbor_nets_before $net_name [lsort $other_pins]\n"
        "    }\n"
        "    lappend target_pin_net_before \"$pin_leaf=$net_name\"\n"
        "}\n"
        + connectivity_check +
        "\n"
        "# ---- modify: one real, already-atomic tool call ----\n"
        "set ok [replace_cell $target_instance $replacement_lib_cell]\n"
        "if { $ok == 0 } {\n"
        "    error \"replace_cell rejected the swap (incompatible ports or"
        " an unresolved reference); no candidate is published for this attempt\"\n"
        "}\n"
        "\n"
        "# ---- after: confirm exactly the target changed and nothing else did ----\n"
        "set new_ref [get_property [get_cells $target_instance] ref_name]\n"
        "if { $new_ref ne $replacement_cell } {\n"
        "    error \"replace_cell reported success but ref_name is"
        " $new_ref, not $replacement_cell\"\n"
        "}\n"
        "set instance_count_after [sta::network_leaf_instance_count]\n"
        "set pin_count_after [sta::network_leaf_pin_count]\n"
        "set out_of_scope_changes {}\n"
        "foreach c [get_cells *] {\n"
        "    set full_name [get_full_name $c]\n"
        "    set before_ref [dict get $refs_before $full_name]\n"
        "    set after_ref [get_property $c ref_name]\n"
        "    if { $full_name ne $target_instance && $before_ref ne $after_ref } {\n"
        "        lappend out_of_scope_changes \"$full_name:$before_ref->$after_ref\"\n"
        "    }\n"
        "}\n"
        "if { [llength $out_of_scope_changes] != 0 } {\n"
        "    error \"replace_cell affected instances outside the approved"
        " scope: [join $out_of_scope_changes {, }]\"\n"
        "}\n"
        "\n"
        "# ---- after: real pin->net connectivity again, plus a real net_delta ----\n"
        "set target_pin_net_after {}\n"
        "set neighbor_nets_after [dict create]\n"
        "foreach p [get_pins -of_objects [get_cells $target_instance]] {\n"
        "    set pin_full [get_full_name $p]\n"
        "    set pin_leaf [lindex [split $pin_full \"/\"] end]\n"
        "    set net [get_nets -of_objects $p]\n"
        "    set net_name \"\"\n"
        "    if { $net ne \"\" } {\n"
        "        set net_name [get_full_name $net]\n"
        "        set other_pins {}\n"
        "        foreach np [get_pins -of_objects $net] {\n"
        "            set np_full [get_full_name $np]\n"
        "            if { $np_full ne $pin_full } {\n"
        "                lappend other_pins $np_full\n"
        "            }\n"
        "        }\n"
        "        dict set neighbor_nets_after $net_name [lsort $other_pins]\n"
        "    }\n"
        "    lappend target_pin_net_after \"$pin_leaf=$net_name\"\n"
        "}\n"
        "set net_delta 0\n"
        "foreach before_entry $target_pin_net_before after_entry"
        " $target_pin_net_after {\n"
        "    if { $before_entry ne $after_entry } {\n"
        "        incr net_delta\n"
        "    }\n"
        "}\n"
        "set connectivity_violations {}\n"
        "dict for {net_name other_pins_before} $neighbor_nets_before {\n"
        "    set other_pins_after {}\n"
        "    if { [dict exists $neighbor_nets_after $net_name] } {\n"
        "        set other_pins_after [dict get $neighbor_nets_after $net_name]\n"
        "    }\n"
        "    if { $other_pins_before ne $other_pins_after } {\n"
        "        lappend connectivity_violations $net_name\n"
        "    }\n"
        "}\n"
        "if { [llength $connectivity_violations] != 0 } {\n"
        "    error \"replace_cell changed connectivity on nets outside the"
        " approved scope: [join $connectivity_violations {, }]\"\n"
        "}\n"
        "\n"
        "# ---- only now: write the before/after evidence this method owns ----\n"
        "# 手動組 JSON 文字，沿用 pinned setup repair hook 既有的\n"
        "# terminux_r5_write_json_dict／terminux_r5_write_topology 同一種寫法，\n"
        "# 不依賴任何未經核對的 Tcl JSON 套件。\n"
        f"set eco_diff_fp [open [file join $::env(REPORTS_DIR) {ECO_DIFF_FILENAME}] w]\n"
        "puts $eco_diff_fp \"\\{\"\n"
        "puts $eco_diff_fp \"  \\\"instance_delta\\\":"
        " [expr {$instance_count_after - $instance_count_before}],\"\n"
        "puts $eco_diff_fp \"  \\\"net_delta\\\": $net_delta,\"\n"
        "puts $eco_diff_fp \"  \\\"iterm_delta\\\":"
        " [expr {$pin_count_after - $pin_count_before}],\"\n"
        "puts $eco_diff_fp \"  \\\"changed_masters\\\":"
        " \\[\\\"$expected_cell:1->0\\\", \\\"$replacement_cell:0->1\\\"\\],\"\n"
        "set tpn_before_json {}\n"
        "foreach e $target_pin_net_before { lappend tpn_before_json \"\\\"$e\\\"\" }\n"
        "puts $eco_diff_fp \"  \\\"target_pin_net_before\\\":"
        " \\[[join $tpn_before_json {, }]\\],\"\n"
        "set tpn_after_json {}\n"
        "foreach e $target_pin_net_after { lappend tpn_after_json \"\\\"$e\\\"\" }\n"
        "puts $eco_diff_fp \"  \\\"target_pin_net_after\\\":"
        " \\[[join $tpn_after_json {, }]\\]\"\n"
        "puts $eco_diff_fp \"\\}\"\n"
        "close $eco_diff_fp\n"
        "set snapshot_fp [open [file join $::env(REPORTS_DIR) "
        f"{INSTANCE_SNAPSHOT_FILENAME}] w]\n"
        "puts $snapshot_fp \"\\{\"\n"
        "puts $snapshot_fp \"  \\\"instances\\\": \\{\"\n"
        "puts $snapshot_fp \"    \\\"$target_instance\\\":"
        " \\{\\\"cell\\\": \\\"$new_ref\\\"\\}\"\n"
        "puts $snapshot_fp \"  \\}\"\n"
        "puts $snapshot_fp \"\\}\"\n"
        "close $snapshot_fp\n"
        f"set neighbor_fp [open [file join $::env(REPORTS_DIR) "
        f"{NEIGHBOR_SNAPSHOT_FILENAME}] w]\n"
        "puts $neighbor_fp \"\\{\"\n"
        "puts $neighbor_fp \"  \\\"nets\\\": \\{\"\n"
        "set neighbor_net_names [lsort [dict keys $neighbor_nets_after]]\n"
        "set neighbor_net_count [llength $neighbor_net_names]\n"
        "set neighbor_net_i 0\n"
        "foreach net_name $neighbor_net_names {\n"
        "    incr neighbor_net_i\n"
        "    set other_pins [dict get $neighbor_nets_after $net_name]\n"
        "    set pins_json {}\n"
        "    foreach pin $other_pins { lappend pins_json \"\\\"$pin\\\"\" }\n"
        "    set neighbor_comma \",\"\n"
        "    if { $neighbor_net_i == $neighbor_net_count } { set neighbor_comma \"\" }\n"
        "    puts $neighbor_fp \"    \\\"$net_name\\\":"
        " \\[[join $pins_json {, }]\\]$neighbor_comma\"\n"
        "}\n"
        "puts $neighbor_fp \"  \\}\"\n"
        "puts $neighbor_fp \"\\}\"\n"
        "close $neighbor_fp\n"
    )


def build_post_resize_hook_tcl(
    *, target_instance: str, expected_cell: str, replacement_cell: str,
    library: str = LIBRARY_NANGATE45,
) -> str:
    """組出真實 `POST_RESIZE_TCL` hook 內容（**不執行**）。

    每一個指令都對照過 pinned 原始碼（見模組 docstring 引用的確切檔案與
    行號），不是憑印象生成。核對／修改／再核對／寫證據的實際邏輯在
    `_swap_and_verify_core_tcl()`——這裡只負責宣告 `target_instance`／
    `expected_cell`／`replacement_cell`／`replacement_lib_cell` 四個變數，
    再接上同一份共用核心（`build_standalone_modify_and_save_tcl()` 用的是
    同一個核心，不是兩份平行實作）。

    legalization（detail placement）不在這裡處理——它是 `resize` stage
    之後既有的下一個 flow stage（`detail_place.tcl`，見 docstring），本
    hook 修改的是同一個活的設計狀態，legalization 會自動作用在修改後的
    結果上，不需要本方法另外呼叫。
    """
    lib_cell = qualified_lib_cell(library, replacement_cell)
    return (
        "# terminux gcd.single_instance_buffer_swap.v1 — POST_RESIZE_TCL hook.\n"
        f"# Generated by Terminux {__version__}\n"
        "# GENERATED, NOT EXECUTED. Every command below is verified against\n"
        "# the pinned OpenROAD-flow-scripts / OpenROAD source;\n"
        "# see single_instance_eco.py module docstring for exact file:line refs.\n"
        f"set target_instance {{{target_instance}}}\n"
        f"set expected_cell {{{expected_cell}}}\n"
        f"set replacement_cell {{{replacement_cell}}}\n"
        f"set replacement_lib_cell {{{lib_cell}}}\n"
        "\n"
        + _swap_and_verify_core_tcl(
            target_instance=target_instance, expected_cell=expected_cell,
            replacement_cell=replacement_cell, replacement_lib_cell=lib_cell)
    )


def build_config_hook_wiring(*, hook_script_path: str) -> str:
    """組出把 hook 掛進 candidate `config.mk` 的那一行（**不寫檔**）。

    沿用 setup repair 既有的真實掛法（同一個變數、同一種寫法，見設計引用的
    ``export POST_RESIZE_TCL = $(TERMINUX_R5_HELPER_DIR)/post_resize_restructure.tcl``），
    只是這裡指向本方法產生的 hook script。這一行本身不是「已掛上」——真正
    掛上需要把它寫進 candidate 的 `config.mk`（既有材料化路徑的事，本包
    不重建 config 寫入機制），這裡只組出正確的內容。
    """
    return f"export POST_RESIZE_TCL = {hook_script_path}\n"


def build_candidate_flow_argv(
    *, docker_image: str, flow_dir_mount: str, design_config_relpath: str,
    variant: str, container_flow_path: str = "/OpenROAD-flow-scripts/flow",
) -> dict[str, Any]:
    """組出真實 docker 呼叫（**不執行**）——跑完整條 candidate flow，不只換型。

    argv 形狀沿用既有 `OrfsDockerAdapter.prepare()` 的真實 ORFS run 模式
    （bind-mount `flow_dir`、`docker run --rm`、容器內 `make
    DESIGN_CONFIG=... FLOW_VARIANT=...`，見 `orfs_adapter.py:180-187`）。
    這裡刻意不指定 Make target（沿用預設目標跑到 finish），因為本方法的
    evidence set 需要 `final` stage 的 setup／core 結果，不能只跑到 `place`
    就停；`POST_RESIZE_TCL` 掛在 candidate 的 `config.mk`（見
    `build_config_hook_wiring()`），本函式只負責組出 `docker run` 的
    argv，不寫 config、不啟動 process。
    """
    argv = [
        "docker", "run", "--rm", "-v", f"{flow_dir_mount}:{container_flow_path}",
        docker_image, "bash", "-c",
        f"cd {container_flow_path} && source ../env.sh && "
        f"make DESIGN_CONFIG={design_config_relpath} FLOW_VARIANT={variant}",
    ]
    return {"argv": argv, "executed": False,
            "note": "generated command only; this package launches no process"}


# ---------------------------------------------------------------------------
# 兩個有界 OpenROAD 行程的單次驗證方案（不經 make／不跑完整 ORFS flow）。
#
# 設計要求：不執行完整 ORFS flow，改成兩個有界、各自獨立
# 記錄的 OpenROAD 行程——Operation 1（modify and save）／Operation 2
# （independent readback）——都在同一個 container 裡依序完成，但**行程數量
# 分開算**，不得報成「一次工具執行」。本節所有函式與上面既有的
# `build_candidate_flow_argv()`／`build_config_hook_wiring()` 一樣，回傳值
# 一律 `executed: False`；不啟動任何 process。
#
# 真實命令來源（逐一核對過 pinned 原始碼，不是猜測）：
# - Operation 1 的載入方式沿用 `flow/scripts/load.tcl::load_design`（真實
#   pipeline stage 例如 `resize.tcl` 自己在用的同一個 proc，見模組
#   docstring）——不是 `flow/scripts/open.tcl`（那是 `make open_<file>` 的
#   GUI 檢視用途，liberty 只在 `GUI_TIMING=1` 才載入、預設不讀 SDC，跟真實
#   pipeline stage 的載入行為不同，確認過但決定不採用）。
# - OpenROAD 執行檔與參數沿用 `flow/scripts/variables.mk`：
#   `OPENROAD_EXE ?= .../tools/install/OpenROAD/bin/openroad`（容器內預設，
#   見 `flow/util/docker_shell:66`）、
#   `OPENROAD_ARGS = -no_init -threads $(NUM_CORES) $(OR_ARGS)`、
#   `OPENROAD_CMD = $(OPENROAD_EXE) -exit $(OPENROAD_ARGS)`。真實
#   `flow.sh`（`flow/scripts/flow.sh:11-15`）在每一個 step 之前還會先跑一次
#   一次性的 `noop.tcl` 暖機行程、再用 `run_command.py` 包一層 tee 記錄——
#   **刻意不重現**這兩層（暖機行程會讓「2 個 OpenROAD 行程」的額度失真、
#   `run_command.py` 只是輸出格式化，不是工具身分的一部分），直接呼叫
#   `openroad -exit -no_init -threads <n> -no_splash <script>.tcl`，如實記錄
#   這是對真實 `flow.sh` 的簡化，不是逐行複製。
# ---------------------------------------------------------------------------

#: 核准的資源上限。
OPERATION_PROCESS_TIMEOUT_SECONDS = 300
BATCH_TIMEOUT_SECONDS = 900
BATCH_CLEANUP_RESERVE_SECONDS = 120
#: 執行階段（`docker run` 本身，不含清理）的上限——900 扣掉 120 秒清理保留
#: 額度；兩個行程各自 300 秒（合計 600 秒）必須落在這個範圍內，還留一段
#: container 啟動／`env.sh`／`load_design` 開銷的餘裕。
EXECUTION_PHASE_TIMEOUT_SECONDS = BATCH_TIMEOUT_SECONDS - BATCH_CLEANUP_RESERVE_SECONDS
MAX_OPENROAD_PROCESSES = 2
MAX_DOCKER_CONTAINERS = 1

#: 容器內真實路徑（沿用 `build_candidate_flow_argv()` 已用過的 mount 慣例、
#: 以及 `flow/util/docker_shell:66` 的預設 `OPENROAD_EXE`）。
CONTAINER_FLOW_PATH = "/OpenROAD-flow-scripts/flow"
CONTAINER_OPENROAD_EXE = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"

#: 唯讀核對過的真實本機 image 身分——**不是**先前誤用的
#: `openroad/flow-ubuntu22.04-dev:aeae7d`（那個 tag 是建置用開發環境，
#: `/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad`
#: **不存在**於該 image；唯讀核對 ORFS 原始碼稽核紀錄
#: 既有紀錄後確認）。真正的 runtime image 是
#: `openroad/flow-ubuntu22.04-builder:aeae7d`，以 `docker inspect` 唯讀
#: 核對其完整 Image ID／RepoDigest，與 vendor-neutral execution contract 記錄的短 ID `8926a3cd2b26`
#: 一致（image 自 vendor-neutral execution contract 之後未變動）。
#:
#: 身分依據（不是 mtime 推論——見本模組其他地方已核對過的教訓）：本機
#: 檢出目錄下的 `build_openroad.log`（BuildKit
#: 輸出，`CONFIRMED` 由 `docker build` 產生，非 `docker pull`）第 2499／2727
#: 行的編譯期版本字串 `OpenROAD version: 26Q3-1510-g6cb3f2b704`，與 cmake
#: 編譯旗標 `-DOPENROAD_VERSION=26Q3-1510-g6cb3f2b704`（第 2704 行）——這個
#: 版本字串**與 pinned `tools/OpenROAD` 子模組 commit
#: `6cb3f2b704604a8961573218e1c8adaae9fe9937` 的短 SHA 完全相同**，是編譯期
#: 就烙進二進位的內容依據，不是時間戳推論。殘留、尚未解決的缺口：這個
#: 版本字串本身沒有透過實際啟動容器執行 `openroad -version` 重新核對過
#: （會違反「不啟動 Docker container」的限制），只信任 build log 記錄
#: 的編譯旗標；flow Tcl／Makefile 內容不是烘進 image 的，是即時 bind mount
#: 這份本機 checkout（HEAD `be0dca0b1fd41df54792b3012350cd52bccd99bb`），
#: 與 image 版本是兩件分開驗證過的事，不是同一份證據。
PINNED_DOCKER_IMAGE_REPOSITORY = "openroad/flow-ubuntu22.04-builder"
PINNED_DOCKER_IMAGE_DIGEST = (
    "sha256:8926a3cd2b269694cb4950a932d8a8b31c811cd5b819a85f326aed412d7ae28c")
PINNED_DOCKER_IMAGE_REF = f"{PINNED_DOCKER_IMAGE_REPOSITORY}@{PINNED_DOCKER_IMAGE_DIGEST}"
#: 下一次真實執行必須引用這個固定 digest 參照，不能只用可變 tag
#: （`:aeae7d` 之後如果本機重新 `docker build` 出同名 tag，內容可能已經
#: 換掉；`@sha256:...` 才是不可變的內容位址）。
_DIGEST_PINNED_IMAGE_RE = re.compile(r"^[^:@\s]+@sha256:[0-9a-f]{64}$")


class DockerImageNotDigestPinnedError(ValueError):
    """`docker_image` 不是 `repo@sha256:<64 hex>` 形式的固定 digest 參照——
    設計要求「執行須引用固定 digest／image ID，不使用可變
    tag」，這裡直接擋下，不是只寫在文件裡靠人記得。"""


def is_digest_pinned_image(docker_image: str) -> bool:
    """`docker_image` 是不是 `repo@sha256:<64 hex>` 固定 digest 參照——公開
    版本供其他模組（如 `single_instance_eco_adapter.py`）在自己的驗證點
    重用同一個規則，不用各自重新定義或伸手拿內部的
    `_DIGEST_PINNED_IMAGE_RE`。"""
    return bool(_DIGEST_PINNED_IMAGE_RE.match(docker_image))


def build_standalone_modify_and_save_tcl(
    *, target_instance: str, expected_cell: str, replacement_cell: str,
    candidate_odb_output_path: str, library: str = LIBRARY_NANGATE45,
    source_design_file: str = "3_4_place_resized.odb",
    source_sdc_file: str = "2_floorplan.sdc",
    expected_target_pin_net: list[str] | None = None,
) -> str:
    """Operation 1（modify and save）的獨立 Tcl 腳本內容（**不執行**）。

    不經 `make`、不 source `resize.tcl`——只 source 真實
    `flow/scripts/load.tcl` 並呼叫它定義的 `load_design`（與 `resize.tcl`
    自己第一行做的事完全一樣：`source $::env(SCRIPTS_DIR)/load.tcl` 見該檔
    第 1 行；呼叫慣例 `load_design 3_3_place_gp.odb 2_floorplan.sdc` 見該檔
    第 4 行）。本函式預設 `source_design_file="3_4_place_resized.odb"`——
    沿用已核對來源身分的既有真實輸出，**不是** `3_3_place_gp.odb`
    （resize stage 原本的輸入）；SDC 沿用同一次 run 的 `2_floorplan.sdc`
    （resize 階段不寫新 SDC，時序限制不變，見 `load.tcl`／`resize.tcl` 的
    呼叫方式）。呼叫端必須把 `RESULTS_DIR` 設成來源 ODB／SDC 所在目錄
    （唯讀）；寫出的新 ODB 由本函式自己以真實 `write_db` 指令寫到
    `candidate_odb_output_path`（呼叫端提供的獨立可寫路徑），**不經過**
    `orfs_write_db`（那個 wrapper 靠 `WRITE_ODB_AND_SDC_EACH_STAGE` env 開關
    控制是否寫檔，見 `util.tcl:298-303`；本函式不依賴那個開關，直接呼叫底層
    `write_db`，如實記錄這是一個刻意的簡化）。

    核對／修改／再核對／寫證據的邏輯與 `build_post_resize_hook_tcl()` 是
    **同一個** `_swap_and_verify_core_tcl()`，不是另一份測試專用實作。

    `load_design` 之後、真的核對／修改**之前**，先寫出
    `TOOL_VERSION_FILENAME`（見該常數 docstring）——真實執行發現
    `openroad_version`／`openroad_git_describe` 在本機 image 裡不是有效
    指令，已改成固定寫 `"unavailable"`＋原因，不呼叫任何版本查詢指令，
    不猜替代 API，不為了探測版本另外啟動一個 OpenROAD 行程。
    """
    lib_cell = qualified_lib_cell(library, replacement_cell)
    return (
        "# terminux gcd.single_instance_buffer_swap.v1 — Operation 1"
        " (modify and save).\n"
        "# GENERATED, NOT EXECUTED. Standalone script, NOT via make/resize.tcl.\n"
        "# Loader reuses the real flow/scripts/load.tcl::load_design proc\n"
        "# (same proc resize.tcl itself calls); the original source ODB/SDC\n"
        "# directory (RESULTS_DIR) is only ever read, never written.\n"
        "source $::env(SCRIPTS_DIR)/load.tcl\n"
        f"load_design {source_design_file} {source_sdc_file}\n"
        "\n"
        "# ---- save tool version info from this same process, before any"
        " modification ----\n"
        "# 真實執行發現 `openroad_version`/`openroad_git_describe`\n"
        "# 在本機 pinned image 裡是 invalid command name（雖然 SWIG binding\n"
        "# 原始碼裡有定義，實際這個 build 沒有註冊進 Tcl）——這裡不猜替代\n"
        "# API、不為了查版本另外啟動探測；本函式固定用 -no_splash（見\n"
        "# build_two_operation_batch_argv()），OpenROAD 唯一會印版本字串的\n"
        "# 地方是 showSplash()（Main.cc:574-598，只在沒有 -no_splash 時才會\n"
        "# 呼叫），所以這個行程的既有 log 本來就不含版本資訊——如實記成\n"
        "# unavailable，不是留白也不是猜一個值。\n"
        f"set version_fp [open [file join $::env(REPORTS_DIR)"
        f" {TOOL_VERSION_FILENAME}] w]\n"
        "puts $version_fp \"\\{\"\n"
        "puts $version_fp \"  \\\"tool_version\\\": \\\"unavailable\\\",\"\n"
        "puts $version_fp \"  \\\"reason\\\": \\\"openroad_version/"
        "openroad_git_describe are not registered Tcl commands in this "
        "build; -no_splash suppresses the only log line that would "
        "otherwise contain the version string\\\"\"\n"
        "puts $version_fp \"\\}\"\n"
        "close $version_fp\n"
        "\n"
        f"set target_instance {{{target_instance}}}\n"
        f"set expected_cell {{{expected_cell}}}\n"
        f"set replacement_cell {{{replacement_cell}}}\n"
        f"set replacement_lib_cell {{{lib_cell}}}\n"
        "\n"
        + _swap_and_verify_core_tcl(
            target_instance=target_instance, expected_cell=expected_cell,
            replacement_cell=replacement_cell, replacement_lib_cell=lib_cell,
            expected_target_pin_net=expected_target_pin_net)
        + "\n"
        "# ---- write the modified design to an independent candidate path ----\n"
        "# 直接呼叫底層 write_db，不經 orfs_write_db 的 env 開關（見上方\n"
        "# docstring）；來源目錄本身完全沒有被寫入。\n"
        f"write_db {{{candidate_odb_output_path}}}\n"
    )


def build_standalone_readback_verification_tcl(
    *, target_instance: str, replacement_cell: str, candidate_odb_path: str,
) -> str:
    """Operation 2（independent readback）的獨立 Tcl 腳本內容（**不執行**）。

    只在 Operation 1 成功寫出候選 ODB **之後**才會被啟動（見下方
    `build_two_operation_batch_argv()` 的 `set -e` 串接，Operation 1 失敗時
    shell 直接中止，不會走到這裡）；是全新、獨立的 OpenROAD 行程，只
    `read_db` Operation 1 寫出的新 ODB，不做任何修改。

    載入方式對照真實 `flow/scripts/open.tcl` 的 `.odb` 分支（`make
    open_<file>` 用的既有唯讀檢視路徑，見該檔第 31-32 行：`set input_file
    $::env(ODB_FILE); log_cmd read_db {*}[hier_options] $input_file`）——這裡
    本來就是「唯讀載入既有存檔」的場景，用這個既有 harness 比借用
    `load_design`（那是給要繼續往下寫的 pipeline stage 用的，會多要求
    liberty／SDC）更貼切；只是不依賴 Makefile 的 `ODB_FILE` env 變數，直接
    把路徑寫進腳本內容（沿用同一個 `read_db {*}[hier_options] ...` 呼叫）。

    只做讀回核對：confirm target 的新 cell、target 的 pin→net 連接、以及
    target 所接 net 上其他 pin 的清單，寫成與 Operation 1 相同格式的
    `READBACK_*` 檔名；核對失敗（目標 cell 不是預期的 replacement_cell）
    直接 `error`。兩份輸出（Operation 1 的 `NEIGHBOR_SNAPSHOT_FILENAME` 與
    這裡的 `READBACK_NEIGHBOR_SNAPSHOT_FILENAME`）的比對在 Python 端做（見
    `compare_operation1_and_readback_snapshots()`），不在 Tcl 裡手刻 JSON
    parser。
    """
    return (
        "# terminux gcd.single_instance_buffer_swap.v1 — Operation 2"
        " (independent readback).\n"
        "# GENERATED, NOT EXECUTED. Independent OpenROAD process; no"
        " modification.\n"
        "source $::env(SCRIPTS_DIR)/util.tcl\n"
        f"log_cmd read_db {{*}}[hier_options] {candidate_odb_path}\n"
        "\n"
        f"set target_instance {{{target_instance}}}\n"
        f"set expected_cell {{{replacement_cell}}}\n"
        "\n"
        "if { [catch {get_property [get_cells $target_instance] ref_name}"
        " observed_ref] } {\n"
        "    error \"readback cannot confirm the cell for instance"
        " $target_instance: $observed_ref\"\n"
        "}\n"
        "if { $observed_ref ne $expected_cell } {\n"
        "    error \"readback mismatch: candidate ODB has $observed_ref"
        " for $target_instance, expected $expected_cell\"\n"
        "}\n"
        "\n"
        "set readback_pin_net {}\n"
        "set readback_neighbor_nets [dict create]\n"
        "foreach p [get_pins -of_objects [get_cells $target_instance]] {\n"
        "    set pin_full [get_full_name $p]\n"
        "    set pin_leaf [lindex [split $pin_full \"/\"] end]\n"
        "    set net [get_nets -of_objects $p]\n"
        "    set net_name \"\"\n"
        "    if { $net ne \"\" } {\n"
        "        set net_name [get_full_name $net]\n"
        "        set other_pins {}\n"
        "        foreach np [get_pins -of_objects $net] {\n"
        "            set np_full [get_full_name $np]\n"
        "            if { $np_full ne $pin_full } {\n"
        "                lappend other_pins $np_full\n"
        "            }\n"
        "        }\n"
        "        dict set readback_neighbor_nets $net_name [lsort $other_pins]\n"
        "    }\n"
        "    lappend readback_pin_net \"$pin_leaf=$net_name\"\n"
        "}\n"
        "\n"
        f"set snapshot_fp [open [file join $::env(REPORTS_DIR) "
        f"{READBACK_INSTANCE_SNAPSHOT_FILENAME}] w]\n"
        "puts $snapshot_fp \"\\{\"\n"
        "puts $snapshot_fp \"  \\\"instances\\\": \\{\"\n"
        "puts $snapshot_fp \"    \\\"$target_instance\\\":"
        " \\{\\\"cell\\\": \\\"$observed_ref\\\"\\}\"\n"
        "puts $snapshot_fp \"  \\},\"\n"
        "set rpn_json {}\n"
        "foreach e $readback_pin_net { lappend rpn_json \"\\\"$e\\\"\" }\n"
        "puts $snapshot_fp \"  \\\"target_pin_net\\\": \\[[join $rpn_json {, }]\\]\"\n"
        "puts $snapshot_fp \"\\}\"\n"
        "close $snapshot_fp\n"
        f"set neighbor_fp [open [file join $::env(REPORTS_DIR) "
        f"{READBACK_NEIGHBOR_SNAPSHOT_FILENAME}] w]\n"
        "puts $neighbor_fp \"\\{\"\n"
        "puts $neighbor_fp \"  \\\"nets\\\": \\{\"\n"
        "set rn_names [lsort [dict keys $readback_neighbor_nets]]\n"
        "set rn_count [llength $rn_names]\n"
        "set rn_i 0\n"
        "foreach net_name $rn_names {\n"
        "    incr rn_i\n"
        "    set other_pins [dict get $readback_neighbor_nets $net_name]\n"
        "    set pins_json {}\n"
        "    foreach pin $other_pins { lappend pins_json \"\\\"$pin\\\"\" }\n"
        "    set rn_comma \",\"\n"
        "    if { $rn_i == $rn_count } { set rn_comma \"\" }\n"
        "    puts $neighbor_fp \"    \\\"$net_name\\\":"
        " \\[[join $pins_json {, }]\\]$rn_comma\"\n"
        "}\n"
        "puts $neighbor_fp \"  \\}\"\n"
        "puts $neighbor_fp \"\\}\"\n"
        "close $neighbor_fp\n"
    )


#: 批次容器的名稱與 label 都由 `batch_id` 決定；single-instance ECO 正式路徑以 Kernel
#: run id 當 `batch_id`，因此容器身分在 daemon 建立 run（child 派發前）時
#: 就已經 durable 可推導，不必等 docker client 寫出 cidfile。
BATCH_CONTAINER_NAME_PREFIX = "terminux-b3-eco-"
BATCH_LABEL_KEY = "terminux.b3_eco_batch"
#: 容器內看到取消標記時的結束碼（不是 OpenROAD 的結束碼）。
CANCELLED_BEFORE_OPERATION_EXIT_CODE = 125


def batch_container_name(batch_id: str) -> str:
    """由 `batch_id` 推導唯一的批次容器名稱。"""
    return f"{BATCH_CONTAINER_NAME_PREFIX}{batch_id}"


def build_two_operation_batch_argv(
    *, docker_image: str, batch_id: str, flow_scripts_mount: str,
    source_results_dir_container: str, platform_dir_container: str,
    lib_files_container: str, candidate_dir_host: str,
    candidate_dir_container: str, cidfile_path: str,
    op1_script_container_path: str, op2_script_container_path: str,
    op1_reports_dir_container: str, op2_reports_dir_container: str,
    num_cores: int = 1, docker_executable: str = "docker",
    cancel_marker_container_path: str | None = None,
) -> dict[str, Any]:
    """組出單一 `docker run` 內依序完成兩個有界 OpenROAD 行程的 argv（**不
    執行**）——不是完整 ORFS flow，不跑 `make`。

    `docker_image` 必須是 `repo@sha256:<64 hex>` 固定 digest 參照——直接
    `DockerImageNotDigestPinnedError`，不接受可變 tag（`PINNED_DOCKER_IMAGE_REF`
    是唯讀核對過的真實值）。

    容器與行程層級的限制（呼叫端／實際執行時必須遵守，本函式只組文字）：
    - 只有 1 個 container（`--name`／`--label`／`--cidfile` 給這個 container
      一個可核對的身分，`--rm` 處理正常結束時的自動清理；`cidfile_path`
      是 `run_two_operation_batch()` 在 timeout／client 中斷時用來找到真正
      container id 的唯一依據，見該函式與 `MAX_DOCKER_CONTAINERS`）。
    - 容器內依序啟動剛好 2 個 OpenROAD 行程（`MAX_OPENROAD_PROCESSES`），
      各自前面加 `timeout OPERATION_PROCESS_TIMEOUT_SECONDS`；shell 用
      `set -e` 串接，Operation 1 的 `openroad -exit ...` 若因為
      `_swap_and_verify_core_tcl()` 裡任何一個 `error` 而回傳非 0，`set -e`
      會讓整個 `bash -c` 在還沒跑到 Operation 2 之前就中止——這是「Operation
      1 失敗時 Operation 2 不得啟動」的實際機制，不是額外包一層 Python
      判斷；這個前提本身（Tcl `error` → OpenROAD 非零 exit code）已核對過
      pinned 原始碼（`Main.cc:465-471`／`StaMain.cc:96-109`／
      `Util.tcl:377,394-475`），見模組其他地方的說明，不需要修 wrapper。
    - 兩個行程各自的 `REPORTS_DIR` 不同（`op1_reports_dir_container`／
      `op2_reports_dir_container`），對應各自獨立的候選輸出區。
    - `flow_scripts_mount` 用 `:ro` 掛載——容器只能讀取原始 ODB／SDC／
      library／腳本，寫不了來源目錄；新產物與日誌的唯一可寫掛載是
      `candidate_dir_host:candidate_dir_container`（分開給 host／container
      各自的路徑，不假設兩邊路徑字串相同）。沒有為了繞過 loader 的路徑
      問題放寬過任何歷史輸出樹的權限（來源目錄為
      `755`／`644` 標準檔案權限，容器內用 root 身分即可讀取，不需要
      chmod；見 ORFS 原始碼稽核紀錄錄
      的既有 root-in-container 執行方式，本函式沿用同一個身分，不新增
      `-u` 或改權限）。
    - 這裡**沒有**重現 `flow/scripts/flow.sh` 每個 step 前的 `noop.tcl` 暖機
      行程與 `run_command.py` tee 包裝（見本節開頭的說明）；如實記錄成
      「2 個 OpenROAD 行程」，不是「4 個」也不是「1 次工具執行」。
    - `export OPENROAD_HIERARCHICAL=0`：`load.tcl`／`util.tcl` 的
      `hier_options()`（`util.tcl:215-224`）對這個 env var 是直接
      `$::env(OPENROAD_HIERARCHICAL)` 解參照，不是用
      `env_var_exists_and_non_empty` 這種容忍未設定的 helper；正常經
      `make` 執行時這個變數由 `variables.mk`／`variables.yaml` 的
      `default: 0` 機制自動補上，這裡繞過 `make`，所以必須自己顯式
      export，否則兩個腳本一 source `util.tcl`／`load.tcl` 就會直接
      Tcl 例外——這是核對 pinned 原始碼時抓到的真實必要前置，不是
      臆測補上的防禦性程式碼。

    批次層級的清理（cidfile／timeout／force-remove）由
    `run_two_operation_batch()` 負責，不是本函式的事——本函式只組
    `argv`／`cidfile_path`，不執行、不清理任何東西。

    single-instance ECO 取消交錯：`cancel_marker_container_path` 有提供時，每個 OpenROAD
    行程啟動前先檢查該路徑（位於唯一可寫的 candidate 掛載內）；存在就以
    `CANCELLED_BEFORE_OPERATION_EXIT_CODE` 結束、不啟動 OpenROAD。管理端在
    取消時先寫這個標記、再依名稱移除容器——即使 dockerd 在 client 被殺之後
    才完成建立容器，這個遲到的容器也不會做任何工具操作，`--rm` 會在它結束
    時移除它。`None` 時 argv 與既有 single-instance ECO 內容相同。
    """
    if not _DIGEST_PINNED_IMAGE_RE.match(docker_image):
        raise DockerImageNotDigestPinnedError(
            f"docker_image must be repo@sha256:<64 hex>, not a mutable tag: "
            f"{docker_image!r}; use PINNED_DOCKER_IMAGE_REF")
    container_name = batch_container_name(batch_id)
    container_label = f"{BATCH_LABEL_KEY}={batch_id}"
    cancel_guard = (
        f"test ! -e {cancel_marker_container_path}"
        f" || exit {CANCELLED_BEFORE_OPERATION_EXIT_CODE}; "
        if cancel_marker_container_path else "")
    inner = (
        "set -e; "
        f"export SCRIPTS_DIR={CONTAINER_FLOW_PATH}/scripts; "
        f"export RESULTS_DIR={source_results_dir_container}; "
        f"export PLATFORM_DIR={platform_dir_container}; "
        f"export PLATFORM={LIBRARY_NANGATE45}; "
        f"export LIB_FILES={lib_files_container}; "
        "export OPENROAD_HIERARCHICAL=0; "
        f"mkdir -p {op1_reports_dir_container} {op2_reports_dir_container}; "
        f"export REPORTS_DIR={op1_reports_dir_container}; "
        f"{cancel_guard}"
        f"timeout {OPERATION_PROCESS_TIMEOUT_SECONDS} {CONTAINER_OPENROAD_EXE}"
        f" -exit -no_init -threads {num_cores} -no_splash"
        f" {op1_script_container_path}; "
        f"export REPORTS_DIR={op2_reports_dir_container}; "
        f"{cancel_guard}"
        f"timeout {OPERATION_PROCESS_TIMEOUT_SECONDS} {CONTAINER_OPENROAD_EXE}"
        f" -exit -no_init -threads {num_cores} -no_splash"
        f" {op2_script_container_path}"
    )
    argv = [
        docker_executable, "run", "--rm",
        "--name", container_name,
        "--label", container_label,
        "--cidfile", cidfile_path,
        "-v", f"{flow_scripts_mount}:{CONTAINER_FLOW_PATH}:ro",
        "-v", f"{candidate_dir_host}:{candidate_dir_container}",
        docker_image, "bash", "-c", inner,
    ]
    return {
        "argv": argv, "executed": False,
        "container_name": container_name, "container_label": container_label,
        "cidfile_path": cidfile_path,
        "openroad_process_count": MAX_OPENROAD_PROCESSES,
        "docker_container_count": MAX_DOCKER_CONTAINERS,
        "note": ("generated command only; this package launches no process; "
                 "counts Operation 1 and Operation 2 as two separate OpenROAD "
                 "processes even though both run inside one container"),
    }


# ---------------------------------------------------------------------------
# 有界 container 清理——不能只靠 `docker run --rm`（那個只在容器正常結束
# 時起作用；client 被外層 timeout 用 SIGKILL 中止時容器可能變孤兒）。
#
# 架構：`DockerOps` 是清理邏輯依賴的最小介面；`SubprocessDockerOps` 是真正
# 會呼叫 `subprocess`／`docker` 的實作（**目前不會被呼叫**——這個流程不啟動任何
# container／OpenROAD 行程，這個類別只是把「之後真的要執行時要呼叫什麼」
# 寫清楚）；測試用替身
# 模擬正常／Operation 1 失敗／timeout 三條路徑，不啟動真實 Docker 或 EDA。
# ---------------------------------------------------------------------------

class OperationBatchTimeout(Exception):
    """`DockerOps.run()` 必須在 argv 執行超過 timeout 時拋出這個例外——
    介面不綁死在 `subprocess.TimeoutExpired` 上，`SubprocessDockerOps`
    只是把它包起來轉譯，測試替身可以直接拋這個。"""


@dataclass(frozen=True)
class BatchRunResult:
    """`DockerOps.run()` 的回傳形狀（不管真的跑 subprocess 還是替身）。"""

    returncode: int
    stdout: str
    stderr: str


class DockerOps:
    """清理邏輯需要的最小 docker 操作介面（duck-typed protocol，用類別只是
    給個型別名稱；`SubprocessDockerOps` 與測試替身都實作同一組方法簽章）。
    """

    def run(self, argv: list[str], *, timeout: float) -> BatchRunResult:
        raise NotImplementedError

    def read_cidfile(self, path: str) -> str | None:
        raise NotImplementedError

    def remove_file(self, path: str) -> None:
        raise NotImplementedError

    def is_running(self, container_id: str) -> bool | None:
        """`True`／`False`／`None`（查不到，視為已經不存在）。"""
        raise NotImplementedError

    def stop(self, container_id: str, *, timeout: float) -> bool:
        raise NotImplementedError

    def remove_force(self, container_id: str) -> bool:
        raise NotImplementedError

    def container_label(self, container_ref: str, label_key: str) -> tuple[str, str | None]:
        """依 id 或名稱查容器：回傳 `(state, label_value)`。

        `state` 只有三種：`"present"`（查得到，附上 `label_key` 的值）、
        `"absent"`（dockerd 明確回報沒有這個容器）、`"unknown"`（查詢本身
        失敗，不能當成已不存在）。"""
        raise NotImplementedError


class SubprocessDockerOps(DockerOps):
    """真正呼叫 `subprocess`／`docker` 的實作。`run()` 不再啟動新的
    container（`LEGACY_DOCKER_EXECUTION_DISABLED`）；查詢、停止與移除保留，
    只用來收尾已在執行中的舊 container。

    `docker_executable` 與 adapter 使用同一個 host 綁定的 docker 執行檔
    （絕對路徑），查詢／停止／移除不再依賴 PATH 上的 `docker`。每一個指令都
    經 `run_legacy_stop_only()` 核對形狀；形狀不符（例如 id 以 `-` 開頭）時
    不呼叫 docker，當成查詢失敗。"""

    def __init__(self, docker_executable: str = "docker") -> None:
        self._docker = docker_executable

    def _stop_only(self, argv: list[str], *, timeout: float) -> Any:
        from terminux_engine.orchestration.legacy_docker_execution import (
            LegacyExecutionDisabled,
            run_legacy_stop_only,
        )

        try:
            return run_legacy_stop_only(self._docker, argv, timeout=timeout)
        except LegacyExecutionDisabled:
            return None

    def run(self, argv: list[str], *, timeout: float) -> BatchRunResult:
        """container 模式不再啟動新的 container；查詢、停止與移除不受影響。"""
        from terminux_engine.orchestration.legacy_docker_execution import (
            PATH_SINGLE_INSTANCE_ECO,
            LegacyDockerExecutionDisabled,
        )

        del argv, timeout
        raise LegacyDockerExecutionDisabled(PATH_SINGLE_INSTANCE_ECO)

    def read_cidfile(self, path: str) -> str | None:
        import os
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
        return content or None

    def remove_file(self, path: str) -> None:
        import os
        if os.path.exists(path):
            os.remove(path)

    def is_running(self, container_id: str) -> bool | None:
        import subprocess as _subprocess
        try:
            completed = self._stop_only(
                ["inspect", "--format", "{{.State.Running}}", container_id], timeout=10)
        except _subprocess.TimeoutExpired:
            return None
        if completed is None or completed.returncode != 0:
            return None
        return completed.stdout.strip() == "true"

    def stop(self, container_id: str, *, timeout: float) -> bool:
        import subprocess as _subprocess
        try:
            completed = self._stop_only(
                ["stop", "--time", str(int(timeout)), container_id], timeout=timeout + 5)
        except _subprocess.TimeoutExpired:
            return False
        return completed is not None and completed.returncode == 0

    def remove_force(self, container_id: str) -> bool:
        import subprocess as _subprocess
        try:
            completed = self._stop_only(["rm", "-f", container_id], timeout=15)
        except _subprocess.TimeoutExpired:
            return False
        return completed is not None and completed.returncode == 0

    def container_label(self, container_ref: str, label_key: str) -> tuple[str, str | None]:
        import json as _json
        import subprocess as _subprocess
        try:
            completed = self._stop_only(
                ["container", "inspect", "--format", "{{json .Config.Labels}}",
                 container_ref], timeout=10)
        except (_subprocess.TimeoutExpired, OSError):
            return "unknown", None
        if completed is None:
            return "unknown", None
        if completed.returncode != 0:
            # 只有 dockerd 明確說「沒有這個容器」才算 absent；其他錯誤（例如
            # daemon 連不上）是 unknown，不得當成已清掉。
            text = (completed.stderr or "").lower()
            return ("absent", None) if "no such" in text else ("unknown", None)
        try:
            labels = _json.loads(completed.stdout.strip() or "null") or {}
        except ValueError:
            return "unknown", None
        return "present", (labels.get(label_key) if isinstance(labels, dict) else None)


def _cleanup_batch_container(
    ops: DockerOps, *, cidfile_path: str, stop_grace_seconds: float,
) -> dict[str, Any]:
    """清理只針對這個 argv 自己啟動、透過 `cidfile_path` 記下身分的那個
    container 做——不掃描、不動任何其他 container。用 `ops.is_running()`
    的真實觀測結果決定要不要動手，不是靠「是不是 timeout 觸發的」這種
    呼叫端猜測：成功／失敗結束的容器已經被 `--rm` 清掉（`is_running`
    回 `None`），timeout／client 中斷留下的孤兒容器則真的還在跑
    （`is_running` 回 `True`），兩者用同一段邏輯統一處理。
    """
    container_id = ops.read_cidfile(cidfile_path)
    ops.remove_file(cidfile_path)  # cidfile 本身無論後面清不清得掉都先清
    if container_id is None:
        return {"attempted": False, "ok": True,
                "detail": "no cidfile found; nothing to clean up"}
    running = ops.is_running(container_id)
    if running is not True:
        return {"attempted": False, "ok": True,
                "detail": f"container {container_id} already gone "
                          "(exited normally or --rm already applied)"}
    stopped = ops.stop(container_id, timeout=stop_grace_seconds)
    if stopped:
        return {"attempted": True, "ok": True,
                "detail": f"container {container_id} stopped gracefully"}
    removed = ops.remove_force(container_id)
    if removed:
        return {"attempted": True, "ok": True,
                "detail": f"container {container_id} force-removed after "
                          "graceful stop failed"}
    return {"attempted": True, "ok": False,
            "detail": f"container {container_id} could not be stopped or "
                      "force-removed; manual intervention required"}


def cleanup_batch_container(
    ops: DockerOps, *, cidfile_path: str, stop_grace_seconds: float = 10.0,
) -> dict[str, Any]:
    """`_cleanup_batch_container()` 的公開包裝：既有 container 的 cidfile-based
    清理（只停止與移除，不啟動）。

    container 模式不再啟動新的 batch（`run_two_operation_batch()` 一律拒絕）；
    本函式留給已在執行中的舊 run 收尾——client 行程被中止後可能留下的孤兒
    container，以 cidfile 記下的身分精確停止，不掃描、不動其他 container。
    """
    return _cleanup_batch_container(
        ops, cidfile_path=cidfile_path, stop_grace_seconds=stop_grace_seconds)


#: 依名稱清理時的確認期限與輪詢間隔（遠小於 `BATCH_CLEANUP_RESERVE_SECONDS`）。
RELEASE_DEADLINE_SECONDS = 60.0
RELEASE_POLL_INTERVAL_SECONDS = 1.0


def release_batch_container(
    ops: DockerOps, *, batch_id: str, deadline_seconds: float | None = None,
    poll_interval_seconds: float | None = None, required_absent_observations: int = 2,
    clock: Any = None, sleep: Any = None,
) -> dict[str, Any]:
    """只依 `batch_id` 推導的名稱／label 清理**這一個**批次容器。

    給管理端（daemon）在 runner 已被 `SIGKILL`、runner 自己的 `finally`
    不可能執行時使用；也給 adapter 的正常 `cleanup()` 使用，兩者同一套邏輯。
    容器名稱在 child 派發前就能由 durable run config 推導，不依賴 docker
    client 事後才寫出的 cidfile。

    - 只動名稱等於 `batch_container_name(batch_id)`、且 label
      `BATCH_LABEL_KEY` 等於 `batch_id` 的容器；名稱相同但 label 不符時不移除，
      記為失敗（不是本 run 的容器）。
    - 反覆「查詢→必要時 `rm -f`」，直到**連續** `required_absent_observations`
      次都由 dockerd 明確回報不存在才算完成；client 被殺時仍在 dockerd 內
      進行中的建立請求，會在後續輪次被看到並移除。
    - 期限內無法確認就回報 `ok=False`，由呼叫端獨立記錄；本函式不重試派工、
      不改寫任何工具結果。
    """
    import time as _time

    now = clock or _time.monotonic
    pause = sleep or _time.sleep
    if deadline_seconds is None:
        deadline_seconds = RELEASE_DEADLINE_SECONDS
    if poll_interval_seconds is None:
        poll_interval_seconds = RELEASE_POLL_INTERVAL_SECONDS
    name = batch_container_name(batch_id)
    deadline = now() + deadline_seconds
    observations: list[dict[str, Any]] = []
    removals = 0
    consecutive_absent = 0
    while True:
        state, label = ops.container_label(name, BATCH_LABEL_KEY)
        entry: dict[str, Any] = {"state": state}
        if state == "absent":
            consecutive_absent += 1
        else:
            consecutive_absent = 0
            if state == "present":
                if label != batch_id:
                    observations.append({**entry, "label": label})
                    return {"attempted": True, "ok": False, "container_name": name,
                            "removals": removals, "observations": observations,
                            "detail": "a container with this name does not carry "
                                      "this run's batch label; it was not removed"}
                entry["removed"] = ops.remove_force(name)
                removals += 1
        observations.append(entry)
        if consecutive_absent >= required_absent_observations:
            return {"attempted": removals > 0, "ok": True, "container_name": name,
                    "removals": removals, "observations": observations,
                    "detail": ("container removed and confirmed absent" if removals
                               else "no container with this run's name exists")}
        if now() >= deadline:
            return {"attempted": removals > 0, "ok": False, "container_name": name,
                    "removals": removals, "observations": observations,
                    "detail": "container absence could not be confirmed before "
                              "the cleanup deadline; manual intervention required"}
        pause(poll_interval_seconds)


def run_two_operation_batch(
    *, argv: list[str], cidfile_path: str, ops: DockerOps,
    execution_timeout_seconds: float = EXECUTION_PHASE_TIMEOUT_SECONDS,
    stop_grace_seconds: float = 10.0,
) -> dict[str, Any]:
    """container 模式的兩段 OpenROAD batch 入口；不再啟動新的 container。

    呼叫 `ops` 之前一律以 `LEGACY_DOCKER_EXECUTION_DISABLED` 拒絕——沒有
    container 被建立，因此也沒有需要清理的資源。既有 container 的停止與
    清理使用 `cleanup_batch_container()`／`release_batch_container()`；
    host-native 的兩段 OpenROAD 由 `single_instance_eco_native` 執行。
    """
    from terminux_engine.orchestration.legacy_docker_execution import (
        PATH_SINGLE_INSTANCE_ECO,
        LegacyDockerExecutionDisabled,
    )

    del argv, cidfile_path, ops, execution_timeout_seconds, stop_grace_seconds
    raise LegacyDockerExecutionDisabled(PATH_SINGLE_INSTANCE_ECO)


def compare_operation1_and_readback_snapshots(
    *, op1_neighbor_snapshot: dict[str, Any],
    readback_neighbor_snapshot: dict[str, Any],
    op1_instance_snapshot: dict[str, Any],
    op1_eco_diff: dict[str, Any],
    readback_instance_snapshot: dict[str, Any],
    target_instance: str,
) -> tuple[bool, tuple[str, ...]]:
    """比較 Operation 1 寫出的證據與 Operation 2 獨立讀回的證據（純 Python，
    不在 Tcl 裡手刻 JSON parser——三邊都已經是解析好的 dict）。

    `op1_instance_snapshot` 是 `INSTANCE_SNAPSHOT_FILENAME` 的內容（只有
    `instances.<target>.cell`）；target 的 pin→net 清單在 `op1_eco_diff`
    （`ECO_DIFF_FILENAME` 的 `target_pin_net_after` 欄位，新增的真實
    比較結果，見 `_swap_and_verify_core_tcl()`），不是 instance_snapshot 的
    欄位——兩份檔案的職責本來就不同，這裡各取各自真正有的欄位，不是
    schema 對不上的臨時拼湊。

    回傳 `(ok, mismatches)`；`ok=False` 時 `mismatches` 逐條列出不一致的
    net 名稱或欄位，呼叫端必須拒絕把這次修改標為「已獨立驗證」。
    """
    mismatches: list[str] = []
    op1_cell = (op1_instance_snapshot.get("instances", {})
                .get(target_instance, {}).get("cell"))
    readback_cell = (readback_instance_snapshot.get("instances", {})
                      .get(target_instance, {}).get("cell"))
    if op1_cell != readback_cell:
        mismatches.append(
            f"target_instance cell: operation1={op1_cell!r} "
            f"readback={readback_cell!r}")

    op1_pin_net = sorted(op1_eco_diff.get("target_pin_net_after", []) or [])
    readback_pin_net = sorted(
        readback_instance_snapshot.get("target_pin_net", []) or [])
    if op1_pin_net != readback_pin_net:
        mismatches.append(
            f"target_pin_net: operation1={op1_pin_net} "
            f"readback={readback_pin_net}")

    op1_nets = op1_neighbor_snapshot.get("nets", {})
    readback_nets = readback_neighbor_snapshot.get("nets", {})
    for net_name in sorted(set(op1_nets) | set(readback_nets)):
        op1_pins = sorted(op1_nets.get(net_name, []))
        readback_pins = sorted(readback_nets.get(net_name, []))
        if op1_pins != readback_pins:
            mismatches.append(
                f"net {net_name!r}: operation1={op1_pins} "
                f"readback={readback_pins}")

    return (not mismatches), tuple(mismatches)


#: `place` 之後、依 ORFS 固定順序必須視為失效並完整重跑的階段。目前沒有
#: 可靠的 incremental 相依規則能判斷「哪一段可以跳過」，所以列出「這個
#: stage 之後全部」，不猜測、不宣稱支援最小重跑。
DOWNSTREAM_STAGES_REQUIRING_FULL_RERUN: dict[str, tuple[str, ...]] = {
    STAGE_PLACE: ("cts", "route", "final"),
}


def downstream_stages_to_invalidate(stage: str) -> tuple[str, ...]:
    """給定換型發生的 stage，回傳必須完整重跑（不得沿用舊結果）的後續階段。

    不支援的 stage 回傳空 tuple——呼叫端理應已經在
    `validate_single_instance_swap_change()` 擋下不支援的 stage，這裡不重
    複驗證，只回答「這個 stage 之後有哪些」。
    """
    return DOWNSTREAM_STAGES_REQUIRING_FULL_RERUN.get(stage, ())


INVALIDATION_REASON_CODE = "SINGLE_INSTANCE_ECO_NETLIST_CHANGED"


def build_downstream_invalidation_record(
    *, invalidation_id: str, declared_at: float, authorized_by: str,
    authorization_reference: str, source_candidate_reference: str, stage: str,
    orchestration_run_ids: tuple[str, ...] = (),
    dqe_rows: tuple[dict[str, Any], ...] = (),
    artifact_manifests: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """組一筆既有 `evidence_invalidation` 格式的作廢紀錄（不新建作廢機制）。

    直接呼叫 `evidence_integrity.invalidation.build_invalidation_record()`——
    既有、已測過的 additive invalidation record；這裡只負責組出
    這個方法特有的 `reason_code`／`statement`，並要求呼叫端明確列出至少
    一項要作廢的 target（沿用該模組「必須指名至少一個 target」的既有規則，
    不在這裡放寬）。

    只組出 record payload（含可重算的 digest），**不寫入** EventStore——
    寫入是呼叫端的事，這裡不假設有哪一個 store 可寫。
    """
    from terminux_engine.evidence_integrity import invalidation as INV

    return INV.build_invalidation_record(
        invalidation_id=invalidation_id, reason_code=INVALIDATION_REASON_CODE,
        declared_at=declared_at, authorized_by=authorized_by,
        authorization_reference=authorization_reference,
        statement=(
            f"a single-instance buffer swap changed the netlist at stage "
            f"{stage!r} for source candidate {source_candidate_reference!r}; "
            f"timing/quality/proof evidence computed on the pre-swap netlist "
            f"for the following downstream stages must not be reused for the "
            f"new candidate without rerunning them: "
            f"{list(downstream_stages_to_invalidate(stage))}"),
        orchestration_run_ids=orchestration_run_ids,
        dqe_rows=dqe_rows, artifact_manifests=artifact_manifests)


__all__ = [
    "BATCH_CLEANUP_RESERVE_SECONDS", "BATCH_CONTAINER_NAME_PREFIX",
    "BATCH_LABEL_KEY", "BATCH_TIMEOUT_SECONDS", "BatchRunResult",
    "CANCELLED_BEFORE_OPERATION_EXIT_CODE",
    "CHANGE_KIND", "CONTAINER_FLOW_PATH", "CONTAINER_OPENROAD_EXE",
    "DockerImageNotDigestPinnedError", "DockerOps",
    "DOWNSTREAM_STAGES_REQUIRING_FULL_RERUN",
    "ECO_DIFF_FILENAME", "EXECUTION_PHASE_TIMEOUT_SECONDS",
    "INSTANCE_SNAPSHOT_FILENAME",
    "INVALIDATION_REASON_CODE", "LIBRARY_NAME_IN_LIB", "LIBRARY_NANGATE45",
    "MATERIALIZE_OPERATION", "MAX_DOCKER_CONTAINERS",
    "MAX_OPENROAD_PROCESSES", "METHOD_ID", "METHOD_VERSION",
    "NEIGHBOR_SNAPSHOT_FILENAME", "NON_CLOCK_BUFFER_FAMILY",
    "NON_CLOCK_BUFFER_FAMILY_SET", "OPERATION_PROCESS_TIMEOUT_SECONDS",
    "OperationBatchTimeout",
    "PINNED_DOCKER_IMAGE_DIGEST", "PINNED_DOCKER_IMAGE_REF",
    "PINNED_DOCKER_IMAGE_REPOSITORY",
    "PLACE_STAGE_DEPENDENCY_CHAIN", "PARAMETER_NAME",
    "READBACK_INSTANCE_SNAPSHOT_FILENAME", "READBACK_NEIGHBOR_SNAPSHOT_FILENAME",
    "RELEASE_DEADLINE_SECONDS", "RELEASE_POLL_INTERVAL_SECONDS",
    "REASON_CELL_NOT_IN_ALLOWED_FAMILY", "REASON_CHANGE_FORMAT_INVALID",
    "REASON_EVIDENCE_REFERENCE_MISSING", "REASON_INSTANCE_INVALID",
    "REASON_INSTANCE_NOT_FOUND", "REASON_KIND_UNSUPPORTED",
    "REASON_LIBRARY_UNSUPPORTED", "REASON_NO_OP_REPLACEMENT", "REASON_OK",
    "REASON_REASON_MISSING", "REASON_SOURCE_CANDIDATE_INVALID",
    "REASON_SOURCE_DRIFT", "REASON_STAGE_UNSUPPORTED",
    "REQUIRED_CHANGE_KEYS", "REQUIRED_SOURCE_CANDIDATE_KEYS",
    "SubprocessDockerOps", "STAGE_PLACE",
    "SUPPORTED_LIBRARIES", "SUPPORTED_STAGES", "TOOL_VERSION_FILENAME",
    "ProposalCheck", "acceptance_to_attempt_outcome",
    "batch_container_name", "build_candidate_flow_argv", "build_config_hook_wiring",
    "build_downstream_invalidation_record", "build_post_resize_hook_tcl",
    "build_standalone_modify_and_save_tcl",
    "build_standalone_readback_verification_tcl",
    "build_two_operation_batch_argv", "check_source_drift", "cleanup_batch_container",
    "is_digest_pinned_image",
    "compare_operation1_and_readback_snapshots",
    "downstream_stages_to_invalidate", "qualified_lib_cell",
    "release_batch_container", "run_two_operation_batch",
    "validate_single_instance_swap_change",
]
