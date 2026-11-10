# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""真實 ORFS GCD（Nangate45）finish report 的 `report_checks -path_delay max` section 摘錄。

這是教 parser 與診斷特徵用的**靜態資料**，不是可供 `/bind-run` 綁定的 receipt／EventStore
投影；它不帶任何執行身分。

- 來源 artifact：ORFS `6_finish.rpt`（design `gcd`、platform `nangate45`、單一 typical liberty），
  由 `flow/scripts/report_metrics.tcl` 的 `report_checks -path_delay max -fields {slew cap input
  net fanout} -format full_clock_expanded` 產生；沒有 `-digits`，所有時間為 0.01 ns 解析度。
- 來源檔案 SHA-256：`SOURCE_REPORT_SHA256`（33573 bytes，658 行）。
- 摘錄範圍：來源檔第 128 至 288 行（整個 section：兩個 path group 各一條 path）。
  摘錄內第 N 行 ＝ 來源檔第 N + LINE_OFFSET 行。
- 摘錄本身的 SHA-256：`EXCERPT_SHA256`。
- GCD 設計與 Nangate45 library 均為公開材料（見 THIRD_PARTY_NOTICES.md）；摘錄只含
  OpenSTA 報告文字，不含任何私有路徑或執行身分。

手算基準（core_clock path，摘錄第 5–93 行 ＝ 來源第 132–220 行）：
data path delay 0.47 − 0.07 = 0.40 ns；launch CK→Q 0.12 ns（30%）；最大組合 cell arc 0.04 ns
（10%，以相鄰 Time 欄差計算時與 BUF_X8 的 0.03 同值，排名有歧義）；cell arc 合計 0.39、
net arc 顯示合計 0.00、殘差 0.01；Q→D 共 15 個 cell arc（含 buffer ×1、inverter ×2）；
path clock skew (0.32 − 0.25) − (0.07 − 0.00) + 0.00 = 0.00 ns。
"""

from __future__ import annotations

SOURCE_REPORT_SHA256 = "f6dcc72a3b150fa26ae20067e36b6af3e67ff48a20fe0fb598ac3eb6ac86fcae"
SOURCE_REPORT_BYTES = 33573
SOURCE_LINE_RANGE = (128, 288)
LINE_OFFSET = 127
EXCERPT_SHA256 = "02641f6cdbedb06e9a2a0f468eb566dd58880ed1419a54f10239f6d9cc10ea9d"

#: 來源檔的 1-indexed 行號（core_clock path block：Startpoint 列到 slack 列）。
CORE_CLOCK_BLOCK_LINES = (132, 220)
#: 來源檔的 1-indexed 行號（vclk_core_clock path block）。
VCLK_BLOCK_LINES = (223, 286)
#: core_clock path 的 launch CK pin 列、CK→Q 列、最大組合 cell 列、BUF_X8 列、endpoint D 列。
CORE_CLOCK_KEY_LINES = {
    "launch_ck": 151, "ck_to_q": 152, "aoi21": 182, "buf_x8": 185, "endpoint_d": 199,
    "capture_ck": 212, "slack": 220,
}


def source_line(excerpt_line_no: int) -> int:
    """摘錄內行號 → 來源檔行號。"""
    return excerpt_line_no + LINE_OFFSET


def excerpt_line(source_line_no: int) -> int:
    """來源檔行號 → 摘錄內行號。"""
    return source_line_no - LINE_OFFSET


FINISH_REPORT_PATH_SECTION = """\

==========================================================================
finish report_checks -path_delay max
--------------------------------------------------------------------------
Startpoint: dpath.a_reg.out[2]$_DFFE_PP_
            (rising edge-triggered flip-flop clocked by core_clock)
Endpoint: dpath.a_reg.out[5]$_DFFE_PP_
          (rising edge-triggered flip-flop clocked by core_clock)
Path Group: core_clock
Path Type: max

Fanout     Cap    Slew   Delay    Time   Description
-----------------------------------------------------------------------------
                          0.00    0.00   clock core_clock (rise edge)
                          0.00    0.00   clock source latency
     1    4.09    0.00    0.00    0.00 ^ clk (in)
                                         clk (net)
                  0.00    0.00    0.00 ^ clkbuf_0_clk/A (CLKBUF_X3)
     4    9.53    0.01    0.03    0.03 ^ clkbuf_0_clk/Z (CLKBUF_X3)
                                         clknet_0_clk (net)
                  0.01    0.00    0.03 ^ clkbuf_2_1__f_clk/A (CLKBUF_X3)
     9   14.84    0.01    0.04    0.07 ^ clkbuf_2_1__f_clk/Z (CLKBUF_X3)
                                         clknet_2_1__leaf_clk (net)
                  0.01    0.00    0.07 ^ dpath.a_reg.out[2]$_DFFE_PP_/CK (DFF_X1)
     4   13.62    0.03    0.12    0.19 ^ dpath.a_reg.out[2]$_DFFE_PP_/Q (DFF_X1)
                                         dpath.a_lt_b$in0[2] (net)
                  0.03    0.00    0.19 ^ _517_/A (INV_X2)
     2    9.42    0.01    0.02    0.21 v _517_/ZN (INV_X2)
                                         _072_ (net)
                  0.01    0.00    0.21 v _519_/A1 (NAND2_X4)
     1    7.52    0.01    0.02    0.22 ^ _519_/ZN (NAND2_X4)
                                         _074_ (net)
                  0.01    0.00    0.22 ^ _520_/A1 (NAND2_X4)
     2    9.09    0.01    0.01    0.24 v _520_/ZN (NAND2_X4)
                                         _075_ (net)
                  0.01    0.00    0.24 v _536_/A1 (NOR2_X4)
     2    8.42    0.02    0.02    0.26 ^ _536_/ZN (NOR2_X4)
                                         _090_ (net)
                  0.02    0.00    0.26 ^ _539_/A1 (NAND2_X4)
     1    5.86    0.01    0.01    0.28 v _539_/ZN (NAND2_X4)
                                         _093_ (net)
                  0.01    0.00    0.28 v _543_/A1 (NAND2_X4)
     3   11.11    0.01    0.02    0.29 ^ _543_/ZN (NAND2_X4)
                                         _097_ (net)
                  0.01    0.00    0.29 ^ _736_/A1 (NAND2_X4)
     1    7.21    0.01    0.01    0.31 v _736_/ZN (NAND2_X4)
                                         _278_ (net)
                  0.01    0.00    0.31 v _738_/A1 (NAND2_X4)
     2    9.96    0.01    0.02    0.32 ^ _738_/ZN (NAND2_X4)
                                         _280_ (net)
                  0.01    0.00    0.32 ^ _745_/A1 (NAND2_X4)
     5   22.36    0.01    0.02    0.34 v _745_/ZN (NAND2_X4)
                                         _284_ (net)
                  0.01    0.00    0.35 v _840_/B1 (AOI21_X4)
     3   12.54    0.03    0.04    0.38 ^ _840_/ZN (AOI21_X4)
                                         _364_ (net)
                  0.03    0.00    0.38 ^ place168/A (BUF_X8)
     7   36.05    0.01    0.03    0.41 ^ place168/Z (BUF_X8)
                                         net168 (net)
                  0.01    0.00    0.42 ^ _903_/A1 (NAND2_X2)
     1    4.17    0.01    0.01    0.43 v _903_/ZN (NAND2_X2)
                                         _418_ (net)
                  0.01    0.00    0.43 v _906_/A1 (NAND3_X2)
     1    3.85    0.01    0.02    0.45 ^ _906_/ZN (NAND3_X2)
                                         _421_ (net)
                  0.01    0.00    0.45 ^ _908_/A1 (NAND2_X2)
     1    1.95    0.01    0.01    0.46 v _908_/ZN (NAND2_X2)
                                         _423_ (net)
                  0.01    0.00    0.46 v _909_/A (INV_X1)
     1    2.45    0.01    0.01    0.47 ^ _909_/ZN (INV_X1)
                                         _043_ (net)
                  0.01    0.00    0.47 ^ dpath.a_reg.out[5]$_DFFE_PP_/D (DFF_X1)
                                  0.47   data arrival time

                          0.25    0.25   clock core_clock (rise edge)
                          0.00    0.25   clock source latency
     1    4.09    0.00    0.00    0.25 ^ clk (in)
                                         clk (net)
                  0.00    0.00    0.25 ^ clkbuf_0_clk/A (CLKBUF_X3)
     4    9.53    0.01    0.03    0.28 ^ clkbuf_0_clk/Z (CLKBUF_X3)
                                         clknet_0_clk (net)
                  0.01    0.00    0.28 ^ clkbuf_2_1__f_clk/A (CLKBUF_X3)
     9   14.84    0.01    0.04    0.32 ^ clkbuf_2_1__f_clk/Z (CLKBUF_X3)
                                         clknet_2_1__leaf_clk (net)
                  0.01    0.00    0.32 ^ dpath.a_reg.out[5]$_DFFE_PP_/CK (DFF_X1)
                          0.00    0.32   clock reconvergence pessimism
                         -0.03    0.29   library setup time
                                  0.29   data required time
-----------------------------------------------------------------------------
                                  0.29   data required time
                                 -0.47   data arrival time
-----------------------------------------------------------------------------
                                 -0.18   slack (VIOLATED)


Startpoint: dpath.a_reg.out[5]$_DFFE_PP_
            (rising edge-triggered flip-flop clocked by core_clock)
Endpoint: resp_msg[14] (output port clocked by vclk_core_clock)
Path Group: vclk_core_clock
Path Type: max

Fanout     Cap    Slew   Delay    Time   Description
-----------------------------------------------------------------------------
                          0.00    0.00   clock core_clock (rise edge)
                          0.00    0.00   clock source latency
     1    4.09    0.00    0.00    0.00 ^ clk (in)
                                         clk (net)
                  0.00    0.00    0.00 ^ clkbuf_0_clk/A (CLKBUF_X3)
     4    9.53    0.01    0.03    0.03 ^ clkbuf_0_clk/Z (CLKBUF_X3)
                                         clknet_0_clk (net)
                  0.01    0.00    0.03 ^ clkbuf_2_1__f_clk/A (CLKBUF_X3)
     9   14.84    0.01    0.04    0.07 ^ clkbuf_2_1__f_clk/Z (CLKBUF_X3)
                                         clknet_2_1__leaf_clk (net)
                  0.01    0.00    0.07 ^ dpath.a_reg.out[5]$_DFFE_PP_/CK (DFF_X1)
     4   10.03    0.02    0.11    0.18 ^ dpath.a_reg.out[5]$_DFFE_PP_/Q (DFF_X1)
                                         dpath.a_lt_b$in0[5] (net)
                  0.02    0.00    0.18 ^ _533_/A1 (NAND2_X2)
     4   12.74    0.02    0.03    0.21 v _533_/ZN (NAND2_X2)
                                         _088_ (net)
                  0.02    0.00    0.21 v _534_/A2 (NAND2_X4)
     2    9.21    0.01    0.02    0.23 ^ _534_/ZN (NAND2_X4)
                                         _089_ (net)
                  0.01    0.00    0.23 ^ _672_/A2 (NOR2_X4)
     3    7.13    0.01    0.01    0.24 v _672_/ZN (NOR2_X4)
                                         _220_ (net)
                  0.01    0.00    0.24 v rebuffer380/A (BUF_X1)
     1    1.70    0.01    0.03    0.27 v rebuffer380/Z (BUF_X1)
                                         net380 (net)
                  0.01    0.00    0.27 v _673_/B1 (AOI21_X1)
     1    4.50    0.03    0.04    0.31 ^ _673_/ZN (AOI21_X1)
                                         _221_ (net)
                  0.03    0.00    0.31 ^ _675_/A2 (NAND2_X2)
     3    9.95    0.01    0.03    0.33 v _675_/ZN (NAND2_X2)
                                         _223_ (net)
                  0.01    0.00    0.34 v _676_/A2 (NAND2_X1)
     1    4.17    0.01    0.03    0.36 ^ _676_/ZN (NAND2_X1)
                                         _224_ (net)
                  0.01    0.00    0.36 ^ _677_/A2 (NAND2_X2)
     1    3.99    0.01    0.02    0.38 v _677_/ZN (NAND2_X2)
                                         _225_ (net)
                  0.01    0.00    0.38 v _679_/A (XNOR2_X2)
     2    6.24    0.01    0.04    0.41 v _679_/ZN (XNOR2_X2)
                                         net42 (net)
                  0.01    0.00    0.41 v output42/A (BUF_X1)
     1    0.90    0.00    0.03    0.44 v output42/Z (BUF_X1)
                                         resp_msg[14] (net)
                  0.00    0.00    0.44 v resp_msg[14] (out)
                                  0.44   data arrival time

                  0.00    0.25    0.25   clock vclk_core_clock (rise edge)
                          0.07    0.32   clock network delay (ideal)
                          0.00    0.32   clock reconvergence pessimism
                         -0.05    0.27   output external delay
                                  0.27   data required time
-----------------------------------------------------------------------------
                                  0.27   data required time
                                 -0.44   data arrival time
-----------------------------------------------------------------------------
                                 -0.17   slack (VIOLATED)


"""

__all__ = [
    "CORE_CLOCK_BLOCK_LINES", "CORE_CLOCK_KEY_LINES", "EXCERPT_SHA256", "FINISH_REPORT_PATH_SECTION",
    "LINE_OFFSET", "SOURCE_LINE_RANGE", "SOURCE_REPORT_BYTES", "SOURCE_REPORT_SHA256",
    "VCLK_BLOCK_LINES", "excerpt_line", "source_line",
]
