// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QString>

#include <sys/types.h>

/**
 * @file agent_socket_path.h
 * @brief per-user agent socket 的唯一路徑規則（C++ 端）。
 *
 * 與 Python 端 `terminux_engine.ipc.agent_socket` 是**同一組規則**：
 *
 *   <目前執行帳號的 home>/.terminux/run/<nodename>/terminux.sock
 *
 * - home 由 process 自己的 effective UID 查系統帳號資料（getpwuid_r）取得，
 *   **不讀 $HOME**；TERMINUX_HOME_DIR 可覆寫為目前 UID 私有的 0700 目錄。
 *   uid 不由設定或使用者輸入提供。
 * - home 必須存在、是目錄、owner 為目前 UID（不要求 0700）。
 * - `<home>/.terminux/` 必須存在、是目錄、不是 symlink、owner 為目前 UID、
 *   group／other 不可寫。client 端只檢查，不建立。
 * - socket 路徑上若有非 socket 的東西（普通檔、symlink、目錄）一律拒絕，
 *   不刪除、不跟隨。
 * - 不依賴 /run/user/<uid>、logind 或 XDG_RUNTIME_DIR；沒有 /tmp/terminux.sock
 *   或舊 /run/user/<uid>/terminux/terminux.sock fallback。`TERMINUX_CORE_SOCK`
 *   是明確的隔離部署覆寫（整合測試用）：必須是絕對路徑、不得是 legacy 路徑、
 *   父目錄必須是本 uid 私有的 0700 目錄。
 * - 錯誤訊息含目前 uid 與預期路徑，不含其他帳號的任何資訊。
 */
namespace terminux {

struct AgentSocketResolution {
    bool    ok = false;
    QString path;       ///< 可用時：要連的 socket 路徑
    QString source;     ///< "per_user" 或 "override"
    QString code;       ///< 失敗時：TERM_HOME_DIR_* / TERM_RUNTIME_DIR_* 等
    QString message;    ///< 失敗時：人可讀原因（含 uid 與預期路徑）
    uid_t   uid = 0;
    QString expectedPath;
};

/** TERMINUX_HOME_DIR 優先，否則以 uid 查帳號資料；不合法或查不到回空字串。 */
QString homeForUid(uid_t uid);

/** POSIX 主機名稱，或 TERMINUX_NODENAME 的明確覆寫；不合法時回空字串。 */
QString localNodename();

/** 純字串規則（含一次帳號查詢），不碰檔案系統。home 查不到時回佔位形狀。 */
QString formatAgentSocketPath(uid_t uid);

/** 純字串的本機 lineage 預設路徑。 */
QString formatLineageSocketPath(uid_t uid);

/** 機器全域的 legacy 路徑常數（只用來拒絕覆寫與提示）。 */
QString legacyAgentSocketPath();

/** 舊版的 per-user runtime dir legacy 路徑（只用來拒絕覆寫與提示）。 */
QString legacyRuntimeDirSocketPath(uid_t uid);

/**
 * 檢查一個目錄是否符合 socket 目錄的安全條件：存在、非 symlink、是目錄、
 * owner 為 uid、mode 不含 `forbiddenModeBits`（`0077` = 嚴格 0700；
 * `0022` = 只擋 group／other 可寫）。
 * 回傳空字串代表通過；否則回傳錯誤碼（TERM_RUNTIME_DIR_*）。
 * `message` 會被填入人可讀原因。抽出來是為了讓測試能對任意目錄驗規則。
 */
QString checkRuntimeDirectory(const QString& directory, uid_t uid, QString* message,
                              mode_t forbiddenModeBits);

/** 舊簽名：等同 `forbiddenModeBits = 0077`（覆寫父目錄的嚴格規則）。 */
QString checkRuntimeDirectory(const QString& directory, uid_t uid, QString* message);

/**
 * 檢查 socket 路徑上是否被非 socket 占用。回傳空字串代表「沒有東西或是 socket」；
 * 否則回傳 TERM_AGENT_SOCKET_PATH_OCCUPIED 並填 `message`。永不刪除、不跟隨 symlink。
 */
QString checkSocketPathOccupant(const QString& path, QString* message);

/** 依目前 process UID（與 TERMINUX_CORE_SOCK 覆寫）解析 agent socket。 */
AgentSocketResolution resolveAgentSocketPath();

/** TERMINUX_SOCK 優先；GUI 可在目錄尚未建立時只驗證路徑。 */
AgentSocketResolution resolveLineageSocketPath(bool checkDirectory = true);

} // namespace terminux
