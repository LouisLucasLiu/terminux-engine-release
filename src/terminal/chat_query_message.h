// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QJsonObject>
#include <QString>

#include "ipc/jsonrpc_client.h"

namespace terminux {

/**
 * @brief `ecc.chat_query` 回應 → NotificationBar 顯示內容。
 *
 * 只做判斷與文案，不碰 GUI，因此可以不啟動視窗單獨測試。
 * 所有失敗文案都由 client 端固定字串組成：**不**轉貼 daemon 的
 * error message 或 transport 細節，避免帶出展開後的路徑（DQE Viewer cutover 原則）。
 */
struct ChatQueryOutcome {
    enum class Kind {
        Answer,           ///< 回應成功且有 answer
        TransportFailed,  ///< 連不上 daemon、逾時或回應無法解析
        RpcError,         ///< daemon 回傳 JSON-RPC error
        EmptyAnswer,      ///< 回應成功但 answer 是空的
    };

    Kind kind = Kind::TransportFailed;
    /// 要顯示在 NotificationBar 的文字（Answer 時就是 answer 本身）。
    QString message;
    /// 成功回應的 `result` 物件（Answer／EmptyAnswer 時才有內容），
    /// caller 用它處理 agent_async 與 open_path。
    QJsonObject result;
    /// 先前對話未納入本次 AI 請求時的提示（只由 `context_status` 的計數組成）；
    /// 沒有被排除的回合或欄位不合法時為空字串。
    QString contextNotice;
};

ChatQueryOutcome classifyChatQueryResponse(const ipc::JsonRpcCallResult& call);

/**
 * @brief `result.context_status` → 「先前對話未納入 AI 上下文」提示。
 *
 * 只讀取已知原因的整數計數，文案完全由 client 端組成，不轉貼 daemon 的任何文字；
 * schema 不符、計數不是 0..100000 的整數或合計為 0 時回傳空字串。
 */
QString contextStatusNotice(const QJsonObject& status);

}  // namespace terminux
