# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""發行版層級的 Learner Strict 設定。

Generic 發行版為 False。Learner 發行版的 source tree 只把下面這一行改成 True，再由那棵樹建置；
值隨套件內容一起進入 wheel RECORD，不讀取任何環境變數或外部設定檔。

為 True 時，`llm.task_scope` 一律視為 Profile 模式：沒有設定 Profile 等同 Profile 無效，
所有 AI 外送路徑沿用既有的 fail-closed 拒絕，不回到 Default provider。
本檔必須只包含這一個字面常數（測試會檢查）。
"""

LEARNER_STRICT = False
