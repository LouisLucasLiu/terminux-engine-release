// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "ipc/jsonrpc_client.h"

#include <cerrno>
#include <cstring>
#include <string>

#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

namespace terminux::ipc {

// ─── 建構子 ──────────────────────────────────────────────────────────────────

JsonRpcClient::JsonRpcClient(JsonRpcClientOptions options)
    : options_(std::move(options)) {}


// ─── 內部 helper：以 POSIX socket 完成一次 req/resp 交換 ─────────────────────

static JsonRpcCallResult posix_roundtrip(
    const std::string& socket_path,
    int timeout_ms,
    const QByteArray& payload   // compact JSON + '\n'
) {
    JsonRpcCallResult result;

    // ── 1. 路徑長度檢查 ────────────────────────────────────────────────────
    constexpr std::size_t kMaxPath = sizeof(sockaddr_un::sun_path) - 1;
    if (socket_path.size() > kMaxPath) {
        result.error_message = "Socket path too long (max " +
                                std::to_string(kMaxPath) + " bytes)";
        return result;
    }

    // ── 2. 建立 socket ─────────────────────────────────────────────────────
    int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        result.error_message = std::string("socket(): ") + std::strerror(errno);
        return result;
    }

    // ── 3. 設定 send / recv timeout ────────────────────────────────────────
    struct timeval tv;
    tv.tv_sec  = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    // ── 4. 連線 ────────────────────────────────────────────────────────────
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, socket_path.c_str(), kMaxPath);

    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        result.error_message = std::string("connect(): ") + std::strerror(errno);
        ::close(fd);
        return result;
    }

    // ── 5. 送出請求（payload 已含結尾 '\n'）──────────────────────────────
    const char* ptr  = payload.constData();
    int         left = payload.size();
    while (left > 0) {
        ssize_t sent = ::send(fd, ptr, static_cast<std::size_t>(left), 0);
        if (sent <= 0) {
            result.error_message = std::string("send(): ") +
                                    (sent == 0 ? "connection closed" : std::strerror(errno));
            ::close(fd);
            return result;
        }
        ptr  += sent;
        left -= static_cast<int>(sent);
    }

    // ── 6. 接收一行回應（直到 '\n'）──────────────────────────────────────
    std::string buf;
    buf.reserve(4096);

    while (true) {
        // poll 等待可讀或 timeout
        struct pollfd pfd{};
        pfd.fd     = fd;
        pfd.events = POLLIN;
        int rc = ::poll(&pfd, 1, timeout_ms);
        if (rc == 0) {
            result.error_message = "recv(): timeout";
            ::close(fd);
            return result;
        }
        if (rc < 0) {
            result.error_message = std::string("poll(): ") + std::strerror(errno);
            ::close(fd);
            return result;
        }

        char tmp[4096];
        ssize_t n = ::recv(fd, tmp, sizeof(tmp), 0);
        if (n < 0) {
            result.error_message = std::string("recv(): ") + std::strerror(errno);
            ::close(fd);
            return result;
        }
        if (n == 0) {
            result.error_message = "recv(): connection closed before newline";
            ::close(fd);
            return result;
        }

        buf.append(tmp, static_cast<std::size_t>(n));

        // 找到第一個 '\n'，截取該行
        auto nl = buf.find('\n');
        if (nl != std::string::npos) {
            buf.resize(nl);
            break;
        }
    }
    ::close(fd);

    // ── 7. 解析 JSON ──────────────────────────────────────────────────────
    QJsonParseError parseErr;
    auto doc = QJsonDocument::fromJson(QByteArray::fromStdString(buf), &parseErr);
    if (parseErr.error != QJsonParseError::NoError || !doc.isObject()) {
        result.error_message = "JSON parse error: " +
                                parseErr.errorString().toStdString();
        return result;
    }

    result.transport_ok = true;
    result.response     = doc.object();
    return result;
}


// ─── send_request ─────────────────────────────────────────────────────────────

JsonRpcCallResult JsonRpcClient::send_request(const QJsonObject& request) const {
    QByteArray payload =
        QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n';
    return posix_roundtrip(options_.socket_path, options_.timeout_ms, payload);
}


// ─── call_pre_exec ────────────────────────────────────────────────────────────

JsonRpcCallResult JsonRpcClient::call_pre_exec(
    const std::string& command,
    const std::string& cwd,
    const std::string& ledger_path,
    const std::optional<std::string>& tool_signatures_path,
    const std::optional<std::string>& lineage_rules_path,
    int request_id,
    const std::string& source
) const {
    QJsonObject params;
    params["command"]     = QString::fromStdString(command);
    params["cwd"]         = QString::fromStdString(cwd);
    params["ledger_path"] = QString::fromStdString(ledger_path);

    if (tool_signatures_path.has_value())
        params["tool_signatures_path"] =
            QString::fromStdString(tool_signatures_path.value());

    if (lineage_rules_path.has_value())
        params["lineage_rules_path"] =
            QString::fromStdString(lineage_rules_path.value());

    params["source"] = QString::fromStdString(source);

    QJsonObject req;
    req["jsonrpc"] = "2.0";
    req["id"]      = request_id;
    req["method"]  = "lineage.pre_exec";
    req["params"]  = params;

    return send_request(req);
}


// ─── call_post_exec ───────────────────────────────────────────────────────────

JsonRpcCallResult JsonRpcClient::call_post_exec(
    const std::string& command,
    const std::string& cwd,
    int exit_code,
    const std::string& ledger_path,
    const std::optional<std::string>& tool_signatures_path,
    const QStringList& stdout_tail,
    int request_id,
    std::optional<int> shell_pid,
    const std::string& source
) const {
    QJsonObject params;
    params["command"]     = QString::fromStdString(command);
    params["cwd"]         = QString::fromStdString(cwd);
    params["exit_code"]   = exit_code;
    params["ledger_path"] = QString::fromStdString(ledger_path);

    if (tool_signatures_path.has_value())
        params["tool_signatures_path"] =
            QString::fromStdString(tool_signatures_path.value());

    if (!stdout_tail.isEmpty()) {
        QJsonArray arr;
        for (const auto& line : stdout_tail)
            arr.append(line);
        params["stdout_tail"] = arr;
    }

    params["source"] = QString::fromStdString(source);

    // ancestor fallback PID
    if (shell_pid.has_value()) {
        params["pid"] = shell_pid.value();
    }

    QJsonObject req;
    req["jsonrpc"] = "2.0";
    req["id"]      = request_id;
    req["method"]  = "lineage.post_exec";
    req["params"]  = params;

    return send_request(req);
}


// ─── call_chat_query ─────────────────────────────────────────────────────────

JsonRpcCallResult JsonRpcClient::call_chat_query(
    const std::string& query,
    const std::string& cwd,
    const std::string& ask_id,
    int request_id,
    const std::string& origin
) const {
    QJsonObject params;
    params[QStringLiteral("query")] = QString::fromStdString(query);
    params[QStringLiteral("cwd")]   = QString::fromStdString(cwd);

    if (!ask_id.empty())
        params[QStringLiteral("ask_id")] = QString::fromStdString(ask_id);
    if (!origin.empty())
        params[QStringLiteral("origin")] = QString::fromStdString(origin);

    QJsonObject req;
    req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    req[QStringLiteral("id")]      = request_id;
    req[QStringLiteral("method")]  = QStringLiteral("ecc.chat_query");
    req[QStringLiteral("params")]  = params;

    return send_request(req);
}

}  // namespace terminux::ipc
