// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QLocalSocket>
#include <QObject>
#include <QStringList>
#include <QTimer>

/**
 * @brief JSON-RPC 2.0 over Unix Domain Socket client.
 *
 * Manages a single connection to the Python Core process.
 * All sending methods are non-blocking; results arrive via Qt signals.
 * Call connectToCore() to initiate the connection.
 */
class IpcClient : public QObject
{
    Q_OBJECT

public:
    explicit IpcClient(QObject* parent = nullptr);
    ~IpcClient() override;

    // ── Connection management ────────────────────────────────────────────────

    /**
     * Initiate a connection. Emits connected() on success. Ignores if already connected.
     * 沒有預設路徑——呼叫端必須先用 `terminux::resolveAgentSocketPath()`
     * 依目前 UID 解析 `<home>/.terminux/terminux.sock`；`/tmp/terminux.sock`
     * 與 `/run/user/<uid>/terminux/terminux.sock` 皆已退役。
     */
    void connectToCore(const QString& socketPath);

    /**
     * Gracefully disconnect.
     * Sends session.end (fire-and-forget) if a session exists, then closes the socket.
     * Emits disconnected() after the socket closes.
     */
    void disconnectFromCore();

    // ── Session ──────────────────────────────────────────────────────────────

    /**
     * Send session.init. Emits sessionInitResult() on success.
     * `projectId` 為空字串 ⇒ 不宣告 project（params 省略 project_id）。
     */
    void sessionInit(const QString& memberId,
                     const QString& projectId,
                     const QString& shell,
                     const QString& workDirectory);

    /** Send session.end. Emits sessionEndResult() on success. */
    void sessionEnd();

    // ── Command Lifecycle ────────────────────────────────────────────────────

    /** Send command.submit. Emits commandSubmitResult() on success. */
    void commandSubmit(const QString& command,
                       const QString& workDirectory,
                       const QString& timestamp,
                       const QString& commandType,
                       const QStringList& targets,
                       const QJsonObject& envSnapshot);

    /** Send command.output. Emits commandOutputResult() on success. */
    void commandOutput(const QString& stream,
                       const QString& content,
                       const QString& timestamp);

    /** Send command.complete. Emits commandCompleteResult() on success. */
    void commandComplete(int exitCode,
                         int durationMs,
                         const QString& timestamp,
                         const QStringList& targetsConfirmed,
                         const QStringList& artifactsDetected);

    // ── Chat ─────────────────────────────────────────────────────────────────

    /** Send chat.query. Emits chatQueryResult() on success. */
    void chatQuery(const QString& query, const QString& timestamp);

    // ── ECC Query ────────────────────────────────────────────────────────────

    /** Send ecc.query. Emits eccQueryResult() on success. */
    void eccQuery(const QJsonObject& filters);

    // ── Provenance ───────────────────────────────────────────────────────────

    /** Send provenance.write_spec. Emits provenanceWriteSpecResult() on success. */
    void provenanceWriteSpec(const QString& step,
                             const QString& memberId,
                             const QJsonObject& data,
                             const QString& source);

    /** Send provenance.diff. Emits provenanceDiffResult() on success. */
    void provenanceDiff(const QString& step, const QString& memberId);

    // ── State queries ────────────────────────────────────────────────────────

    /** Returns true if socket is in ConnectedState. */
    bool isConnected() const;

    /** Returns true after session.init succeeded and sessionId_ is non-empty. */
    bool hasSession() const;

    /** Returns the current session_id, or empty string if none. */
    QString sessionId() const;

    /**
     * Set the timeout for pending requests (milliseconds).
     * Default: 10000 ms. Only affects newly created requests.
     */
    void setRequestTimeout(int milliseconds);

signals:
    // ── Connection state ─────────────────────────────────────────────────────
    void connected();
    void disconnected();
    void socketError(QLocalSocket::LocalSocketError error, const QString& errorString);

    // ── RPC results ──────────────────────────────────────────────────────────
    void sessionInitResult(const QString& sessionId);
    void sessionEndResult();
    void commandSubmitResult();
    void commandOutputResult();
    void commandCompleteResult(const QString& eventId);
    void chatQueryResult(const QString& answer, const QStringList& references);
    void eccQueryResult(const QJsonArray& events, int total);
    void provenanceWriteSpecResult();
    void provenanceDiffResult(const QJsonArray& mismatches, bool hasMismatch);

    // ── Errors ───────────────────────────────────────────────────────────────
    /** JSON-RPC error response received. method = RPC method name. */
    void rpcError(const QString& method, int code, const QString& message);

    /** Pending request exceeded timeout. method = RPC method name. */
    void requestTimeout(const QString& method, int requestId);

    /** Unparseable or structurally invalid message received. */
    void protocolError(const QString& description);

    /**
     * Called when a sending method cannot proceed.
     * method = C++ method name (e.g. "commandSubmit").
     */
    void notReady(const QString& method);

    // ── Server-push notifications ────────────────────────────────────────────
    void notificationReceived(const QString& notifyType,
                               const QString& title,
                               const QString& content,
                               const QString& detail,
                               const QString& timestamp);

private slots:
    void onSocketConnected();
    void onSocketDisconnected();
    void onSocketError(QLocalSocket::LocalSocketError socketError);
    void onReadyRead();

private:
    struct PendingRequest {
        int     id     = 0;
        QString method;        // RPC method name, e.g. "command.submit"
        QTimer* timer  = nullptr;
    };

    /**
     * Internal helper: serialise request, write to socket, start timeout timer.
     * Caller is responsible for verifying preconditions before calling this.
     * @param rpcMethod  JSON-RPC method name  (e.g. "command.submit")
     * @param params     JSON params object
     */
    void sendRequest(const QString& rpcMethod, const QJsonObject& params);

    void dispatchResponse(const QJsonObject& root);
    void dispatchNotification(const QJsonObject& root);
    void onRequestTimeout(int id, const QString& rpcMethod);

    /** Clears all pending requests, emitting rpcError(-32000, message) for each. */
    void clearPendingWithError(const QString& errorMessage);

    QStringList jsonArrayToStringList(const QJsonArray& arr) const;

    QLocalSocket*              socket_           = nullptr;
    QString                    sessionId_;
    int                        nextRequestId_    = 1;
    int                        requestTimeoutMs_ = 10000;
    QByteArray                 readBuffer_;
    QHash<int, PendingRequest> pendingRequests_;
};
