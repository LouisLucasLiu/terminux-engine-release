# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Tool-neutral timing path evidence：單一 setup path 的逐級資料與診斷特徵。

本 package 只定義資料形狀、完整性規則、特徵演算法與對外投影；它不讀任何
report、不認識任何 STA 工具的輸出格式，也不含 library cell 的命名規則。
Report 解析、pin／net 列辨識與 cell role 判定由各 tool 的 adapter package 負責，
再以 `TimingPathEvidence` 交給這裡。
"""
