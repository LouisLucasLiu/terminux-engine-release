// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <optional>
#include <string>

#include <QJsonObject>
#include <QStringList>

namespace terminux::ipc {

struct JsonRpcClientOptions {
    std::string socket_path;
    int timeout_ms = 60000;  // 60s：LLM 首次載入需要較長時間
};

struct JsonRpcCallResult {
    bool        transport_ok  = false;
    QJsonObject response;
    std::string error_message;
};

/**
 * @brief Synchronous JSON-RPC 2.0 client over Unix Domain Socket.
 *
 * Each call opens a new connection, sends one request (one line of JSON),
 * reads one response (one line of JSON), then closes the socket.
 *
 * Transport failures → transport_ok = false, error_message populated.
 * JSON-RPC error responses (server returns {"error": ...}) are treated as
 * transport_ok = true; callers check response.contains("error") themselves.
 */
class JsonRpcClient {
public:
    explicit JsonRpcClient(JsonRpcClientOptions options);

    /**
     * Low-level: serialise @p request as compact JSON + \\n, send it,
     * read one line back, parse and return it.
     */
    JsonRpcCallResult send_request(const QJsonObject& request) const;

    /** Convenience wrapper for lineage.pre_exec.
     *  source 參數化（default `qt_terminal`），保留 wrapper 對未來 caller
     *  的彈性（避免重新實作為 hardcode 鎖死） */
    JsonRpcCallResult call_pre_exec(
        const std::string& command,
        const std::string& cwd,
        const std::string& ledger_path,
        const std::optional<std::string>& tool_signatures_path = std::nullopt,
        const std::optional<std::string>& lineage_rules_path   = std::nullopt,
        int request_id = 1,
        const std::string& source = "qt_terminal"
    ) const;

    /** Convenience wrapper for lineage.post_exec. */
    JsonRpcCallResult call_post_exec(
        const std::string& command,
        const std::string& cwd,
        int exit_code,
        const std::string& ledger_path,
        const std::optional<std::string>& tool_signatures_path = std::nullopt,
        const QStringList& stdout_tail = {},
        int request_id = 1,
        std::optional<int> shell_pid = std::nullopt,
        const std::string& source = "qt_terminal"
    ) const;

    /** Convenience wrapper for ecc.chat_query.
     *  `origin` 為空時不送該欄位（舊 client 行為）；否則須為 "at_command" 或 "selection"。 */
    JsonRpcCallResult call_chat_query(
        const std::string& query,
        const std::string& cwd,
        const std::string& ask_id = "",
        int request_id = 1,
        const std::string& origin = ""
    ) const;

private:
    JsonRpcClientOptions options_;
};

}  // namespace terminux::ipc
