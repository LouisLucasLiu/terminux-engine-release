// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QString>

namespace terminux {

/**
 * @brief `ecc.chat_query` 的輸入來源。
 *
 * GUI 在送出前就區分「使用者以 `@@` 打字送出」與「右鍵選取文字後 Ask AI」：
 * 前者可能帶本機控制指令（例如 `/bind-run`），後者永遠只是資料。daemon 依
 * `origin` 欄位決定是否把 `/` 開頭的文字當作指令；欄位本身不授予任何權限。
 */
enum class ChatQueryOrigin {
    AtCommand,   ///< 終端輸入行以 `@@` 開頭送出
    Selection,   ///< 右鍵選取文字 → "Ask AI about this"
};

/// wire 上的固定字串，與 daemon `_CHAT_ORIGINS` 值域一致。
inline QString chatQueryOriginName(ChatQueryOrigin origin)
{
    switch (origin) {
    case ChatQueryOrigin::AtCommand: return QStringLiteral("at_command");
    case ChatQueryOrigin::Selection: return QStringLiteral("selection");
    }
    return QString();
}

}  // namespace terminux
