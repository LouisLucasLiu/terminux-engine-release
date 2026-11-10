// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QString>

/**
 * @file project_env.h
 * @brief client 端 project 環境變數的唯一規則（C++ 端）。
 *
 * 與 Python 端 `terminux_engine.ipc.project_env` 是**同一組**規則：
 *
 *   只有 TERMINUX_PROJECT_ID          → 使用它
 *   只有舊 TERMINUX_PROJECT           → 相容採用，附一句提示
 *   兩者相同                          → 正常採用
 *   兩者非空且不同                    → 明確回報衝突（不猜）
 *   兩者都未提供有效值                → session 未宣告 project（不送 project_id）
 *
 * 明確給了值但不合法（含空白／控制字元、超過 128 字元）⇒ 報錯，不以 fallback 掩蓋。
 * client 永遠不送 project_source（來源由 daemon 決定）。
 */
namespace terminux {

struct ProjectDeclaration {
    bool    ok = true;          ///< false：衝突或不合法，見 code／message
    bool    declared = false;   ///< true：session.init 要送 project_id
    QString projectId;          ///< declared 時的值
    QString sourceEnv;          ///< "TERMINUX_PROJECT_ID" 或 "TERMINUX_PROJECT"
    QString warning;            ///< 使用舊名稱時的提示；否則空
    QString code;               ///< 失敗時：TERM_PROJECT_ENV_CONFLICT / TERM_PROJECT_ENV_INVALID
    QString message;            ///< 失敗時：人可讀原因
};

/** 從兩個環境變數的值（已由呼叫端讀出）決定宣告。純函式，方便測試。 */
ProjectDeclaration resolveProjectDeclaration(const QString& primaryRaw,
                                             const QString& legacyRaw);

/** 讀 qgetenv 後呼叫上面的純函式。 */
ProjectDeclaration resolveProjectDeclarationFromEnvironment();

} // namespace terminux
