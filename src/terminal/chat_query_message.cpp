// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/chat_query_message.h"

#include <QJsonValue>
#include <QStringList>

namespace terminux {

namespace {

// daemon 端（jsonrpc_server.py）定義的 JSON-RPC error code。
constexpr int kInvalidRequest         = -32600;
constexpr int kMethodNotFound         = -32601;
constexpr int kInvalidParams          = -32602;
constexpr int kInternalError          = -32603;
constexpr int kSessionContextRequired = -32010;
constexpr int kInvalidSessionContext  = -32011;
constexpr int kPeerUidNotAuthorized   = -32012;

// jsonrpc_client.cpp 逾時時固定寫入的 error_message。
constexpr const char* kRecvTimeout = "recv(): timeout";

constexpr const char* kContextStatusSchema = "terminux.chat_context_status.v1";
constexpr int kMaxContextCount = 100000;

// 讀取 0..kMaxContextCount 的整數；不合法時回傳 -1。
int contextCount(const QJsonValue& value)
{
    if (!value.isDouble()) return -1;
    const double number = value.toDouble();
    if (number < 0 || number > kMaxContextCount) return -1;
    const int count = static_cast<int>(number);
    return static_cast<double>(count) == number ? count : -1;
}

QString rpcErrorText(int code)
{
    switch (code) {
    case kSessionContextRequired:
        return QStringLiteral("尚未連上 Terminal Core，請稍候或重新開啟分頁");
    case kInvalidSessionContext:
        return QStringLiteral("Terminal Core 連線已失效，請重新開啟分頁");
    case kPeerUidNotAuthorized:
        return QStringLiteral("這個 daemon 不屬於目前的使用者，已拒絕連線");
    case kMethodNotFound:
        return QStringLiteral("daemon 不支援這個查詢，可能與 GUI 版本不一致");
    case kInvalidRequest:
    case kInvalidParams:
        return QStringLiteral("daemon 拒絕了這筆查詢的格式");
    case kInternalError:
        return QStringLiteral("daemon 處理查詢時發生內部錯誤，細節請看 daemon log");
    default:
        return QStringLiteral("daemon 回報錯誤");
    }
}

}  // namespace

QString contextStatusNotice(const QJsonObject& status)
{
    if (status.value(QStringLiteral("schema")).toString() != QLatin1String(kContextStatusSchema)) {
        return {};
    }
    const int total = contextCount(status.value(QStringLiteral("history_withheld_turns")));
    const QJsonValue withheldValue = status.value(QStringLiteral("withheld"));
    if (total <= 0 || !withheldValue.isObject()) return {};
    const QJsonObject withheld = withheldValue.toObject();

    struct Reason { const char* key; const char* label; };
    static const Reason kReasons[] = {
        {"context_revoked", "AI 上下文授權已撤銷（Run 綁定、Profile 或外送政策變更）"},
        {"policy_changed", "外送政策或目的地已變更"},
        {"content_policy", "內容不符外送政策"},
        {"limit", "超過歷史上限"},
        {"not_exportable", "非可外送來源"},
    };
    QStringList parts;
    int sum = 0;
    int revoked = 0;
    for (const Reason& reason : kReasons) {
        const QJsonValue value = withheld.value(QLatin1String(reason.key));
        if (value.isUndefined()) continue;
        const int count = contextCount(value);
        if (count < 0) return {};
        if (count == 0) continue;
        sum += count;
        if (QLatin1String(reason.key) == QLatin1String("context_revoked")) revoked = count;
        parts << QStringLiteral("%1 %2").arg(QString::fromUtf8(reason.label)).arg(count);
    }
    // 各原因的合計必須等於總數；不一致代表欄位不可信，不顯示。
    if (sum != total) return {};
    const QString tail = revoked > 0
        ? QStringLiteral("先前的回答仍顯示在本機，但不是目前 Run 的證據。")
        : QStringLiteral("這些訊息仍顯示在本機。");
    return QStringLiteral("AI 上下文：%1 則先前對話未納入這次 AI 請求（%2）。%3 [CONTEXT_HISTORY_WITHHELD]")
        .arg(total).arg(parts.join(QStringLiteral("、")), tail);
}

ChatQueryOutcome classifyChatQueryResponse(const ipc::JsonRpcCallResult& call)
{
    ChatQueryOutcome out;

    if (!call.transport_ok) {
        out.kind = ChatQueryOutcome::Kind::TransportFailed;
        // error_message 只用來分辨逾時，不顯示原文。
        out.message = call.error_message == kRecvTimeout
            ? QStringLiteral("(query failed — daemon 沒有在時限內回應，請稍後再試)")
            : QStringLiteral("(query failed — 無法連線到 Terminux daemon，請確認 daemon 已啟動)");
        return out;
    }

    if (call.response.contains(QStringLiteral("error"))) {
        out.kind = ChatQueryOutcome::Kind::RpcError;
        const QJsonValue code = call.response.value(QStringLiteral("error"))
            .toObject().value(QStringLiteral("code"));
        // daemon 的 message 可能含路徑或例外內容，一律不轉貼。
        if (code.isDouble()) {
            const int value = code.toInt();
            out.message = QStringLiteral("(query failed — %1 [%2])")
                .arg(rpcErrorText(value)).arg(value);
        } else {
            out.message = QStringLiteral("(query failed — %1)").arg(rpcErrorText(0));
        }
        return out;
    }

    out.result = call.response.value(QStringLiteral("result")).toObject();
    out.contextNotice = contextStatusNotice(
        out.result.value(QStringLiteral("context_status")).toObject());
    const QString answer = out.result.value(QStringLiteral("answer")).toString();
    if (answer.isEmpty()) {
        out.kind = ChatQueryOutcome::Kind::EmptyAnswer;
        out.message = QStringLiteral("(daemon 已回應，但這次沒有回覆內容)");
        return out;
    }
    out.kind = ChatQueryOutcome::Kind::Answer;
    out.message = answer;
    return out;
}

}  // namespace terminux
