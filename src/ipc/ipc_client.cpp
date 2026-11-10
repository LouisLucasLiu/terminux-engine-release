// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "ipc/ipc_client.h"

#include <QJsonDocument>
#include <QJsonValue>

// ── Constructor / Destructor ─────────────────────────────────────────────────

IpcClient::IpcClient(QObject* parent)
    : QObject(parent)
    , socket_(new QLocalSocket(this))
{
    connect(socket_, &QLocalSocket::connected,
            this,    &IpcClient::onSocketConnected);
    connect(socket_, &QLocalSocket::disconnected,
            this,    &IpcClient::onSocketDisconnected);
    connect(socket_, &QLocalSocket::errorOccurred,
            this,    &IpcClient::onSocketError);
    connect(socket_, &QLocalSocket::readyRead,
            this,    &IpcClient::onReadyRead);
}

IpcClient::~IpcClient()
{
    // Stop all pending timers to avoid post-destruction callbacks.
    // The timer objects themselves are owned by this (parent = this)
    // and will be deleted by Qt's parent-child mechanism.
    for (auto& pr : pendingRequests_) {
        if (pr.timer) pr.timer->stop();
    }
    pendingRequests_.clear();
}

// ── Connection management ─────────────────────────────────────────────────────

void IpcClient::connectToCore(const QString& socketPath)
{
    // 只從 Unconnected 發起連線：已連線或連線中（含定時重連輪詢期間）直接略過，
    // 避免對正在 connecting 的 socket 重複 connectToServer 而觸發警告。
    if (socket_->state() != QLocalSocket::UnconnectedState) return;
    socket_->connectToServer(socketPath);
}

void IpcClient::disconnectFromCore()
{
    if (!isConnected()) return;

    // Fire-and-forget session.end (write directly, not via sendRequest).
    if (hasSession()) {
        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("method")]  = QStringLiteral("session.end");
        QJsonObject params;
        params[QStringLiteral("session_id")] = sessionId_;
        req[QStringLiteral("params")] = params;
        req[QStringLiteral("id")]     = nextRequestId_++;

        QByteArray data = QJsonDocument(req).toJson(QJsonDocument::Compact);
        data.append('\n');
        socket_->write(data);
    }

    socket_->disconnectFromServer();
    // onSocketDisconnected() will fire, cleaning up pending requests and emitting disconnected().
}

// ── State queries ─────────────────────────────────────────────────────────────

bool IpcClient::isConnected() const
{
    return socket_ && socket_->state() == QLocalSocket::ConnectedState;
}

bool IpcClient::hasSession() const
{
    return !sessionId_.isEmpty();
}

QString IpcClient::sessionId() const
{
    return sessionId_;
}

void IpcClient::setRequestTimeout(int milliseconds)
{
    requestTimeoutMs_ = milliseconds;
}

// ── Public sending methods ────────────────────────────────────────────────────

void IpcClient::sessionInit(const QString& memberId,
                             const QString& projectId,
                             const QString& shell,
                             const QString& workDirectory)
{
    if (!isConnected()) {
        emit notReady(QStringLiteral("sessionInit"));
        return;
    }
    QJsonObject params;
    params[QStringLiteral("member_id")]      = memberId;
    // projectId 為空 ⇒ **不送** project_id（session 未宣告
    // project，daemon 標成 project_source=unset）。client 永不送 project_source。
    if (!projectId.isEmpty()) {
        params[QStringLiteral("project_id")] = projectId;
    }
    params[QStringLiteral("shell")]          = shell;
    params[QStringLiteral("work_directory")] = workDirectory;
    sendRequest(QStringLiteral("session.init"), params);
}

void IpcClient::sessionEnd()
{
    if (!isConnected()) { emit notReady(QStringLiteral("sessionEnd")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("sessionEnd")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")] = sessionId_;
    sendRequest(QStringLiteral("session.end"), params);
}

void IpcClient::commandSubmit(const QString& command,
                               const QString& workDirectory,
                               const QString& timestamp,
                               const QString& commandType,
                               const QStringList& targets,
                               const QJsonObject& envSnapshot)
{
    if (!isConnected()) { emit notReady(QStringLiteral("commandSubmit")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("commandSubmit")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")]     = sessionId_;
    params[QStringLiteral("command")]        = command;
    params[QStringLiteral("work_directory")] = workDirectory;
    params[QStringLiteral("timestamp")]      = timestamp;
    params[QStringLiteral("command_type")]   = commandType;
    params[QStringLiteral("targets")]        = QJsonArray::fromStringList(targets);
    params[QStringLiteral("env_snapshot")]   = envSnapshot;
    sendRequest(QStringLiteral("command.submit"), params);
}

void IpcClient::commandOutput(const QString& stream,
                               const QString& content,
                               const QString& timestamp)
{
    if (!isConnected()) { emit notReady(QStringLiteral("commandOutput")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("commandOutput")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")] = sessionId_;
    params[QStringLiteral("stream")]     = stream;
    params[QStringLiteral("content")]    = content;
    params[QStringLiteral("timestamp")]  = timestamp;
    sendRequest(QStringLiteral("command.output"), params);
}

void IpcClient::commandComplete(int exitCode,
                                 int durationMs,
                                 const QString& timestamp,
                                 const QStringList& targetsConfirmed,
                                 const QStringList& artifactsDetected)
{
    if (!isConnected()) { emit notReady(QStringLiteral("commandComplete")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("commandComplete")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")]          = sessionId_;
    params[QStringLiteral("exit_code")]           = exitCode;
    params[QStringLiteral("duration_ms")]         = durationMs;
    params[QStringLiteral("timestamp")]           = timestamp;
    params[QStringLiteral("targets_confirmed")]   = QJsonArray::fromStringList(targetsConfirmed);
    params[QStringLiteral("artifacts_detected")]  = QJsonArray::fromStringList(artifactsDetected);
    sendRequest(QStringLiteral("command.complete"), params);
}

void IpcClient::chatQuery(const QString& query, const QString& timestamp)
{
    if (!isConnected()) { emit notReady(QStringLiteral("chatQuery")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("chatQuery")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")] = sessionId_;
    params[QStringLiteral("query")]      = query;
    params[QStringLiteral("timestamp")]  = timestamp;
    sendRequest(QStringLiteral("chat.query"), params);
}

void IpcClient::eccQuery(const QJsonObject& filters)
{
    if (!isConnected()) { emit notReady(QStringLiteral("eccQuery")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("eccQuery")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")] = sessionId_;
    params[QStringLiteral("filters")]    = filters;
    sendRequest(QStringLiteral("ecc.query"), params);
}

void IpcClient::provenanceWriteSpec(const QString& step,
                                     const QString& memberId,
                                     const QJsonObject& data,
                                     const QString& source)
{
    if (!isConnected()) { emit notReady(QStringLiteral("provenanceWriteSpec")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("provenanceWriteSpec")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")] = sessionId_;
    params[QStringLiteral("step")]       = step;
    params[QStringLiteral("member_id")]  = memberId;
    params[QStringLiteral("data")]       = data;
    params[QStringLiteral("source")]     = source;
    sendRequest(QStringLiteral("provenance.write_spec"), params);
}

void IpcClient::provenanceDiff(const QString& step, const QString& memberId)
{
    if (!isConnected()) { emit notReady(QStringLiteral("provenanceDiff")); return; }
    if (sessionId_.isEmpty()) { emit notReady(QStringLiteral("provenanceDiff")); return; }

    QJsonObject params;
    params[QStringLiteral("session_id")] = sessionId_;
    params[QStringLiteral("step")]       = step;
    params[QStringLiteral("member_id")]  = memberId;
    sendRequest(QStringLiteral("provenance.diff"), params);
}

// ── Private slots ─────────────────────────────────────────────────────────────

void IpcClient::onSocketConnected()
{
    emit connected();
}

void IpcClient::onSocketDisconnected()
{
    sessionId_.clear();
    clearPendingWithError(QStringLiteral("Connection lost"));
    readBuffer_.clear();
    emit disconnected();
}

void IpcClient::onSocketError(QLocalSocket::LocalSocketError error)
{
    emit socketError(error, socket_->errorString());
}

void IpcClient::onReadyRead()
{
    readBuffer_.append(socket_->readAll());

    while (true) {
        int newlinePos = readBuffer_.indexOf('\n');
        if (newlinePos < 0) break;

        QByteArray message = readBuffer_.left(newlinePos);
        readBuffer_.remove(0, newlinePos + 1);

        // Parse JSON
        QJsonParseError parseErr;
        QJsonDocument doc = QJsonDocument::fromJson(message, &parseErr);
        if (parseErr.error != QJsonParseError::NoError || !doc.isObject()) {
            emit protocolError(
                QStringLiteral("JSON parse failed: ")
                + QString::fromUtf8(message.left(200))
            );
            continue;
        }

        QJsonObject root = doc.object();

        // Validate jsonrpc field
        if (root[QStringLiteral("jsonrpc")].toString() != QStringLiteral("2.0")) {
            emit protocolError(QStringLiteral("Missing or invalid jsonrpc field"));
            continue;
        }

        // Dispatch: response (has non-null id) vs notification (no id or null id)
        QJsonValue idVal = root[QStringLiteral("id")];
        if (!idVal.isUndefined() && !idVal.isNull()) {
            dispatchResponse(root);
        } else {
            dispatchNotification(root);
        }
    }
}

// ── Private methods ───────────────────────────────────────────────────────────

void IpcClient::sendRequest(const QString& rpcMethod, const QJsonObject& params)
{
    int id = nextRequestId_;

    QJsonObject request;
    request[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    request[QStringLiteral("method")]  = rpcMethod;
    request[QStringLiteral("params")]  = params;
    request[QStringLiteral("id")]      = id;

    QByteArray data = QJsonDocument(request).toJson(QJsonDocument::Compact);
    data.append('\n');
    socket_->write(data);

    // Create timeout timer
    auto* timer = new QTimer(this);
    timer->setSingleShot(true);
    timer->setInterval(requestTimeoutMs_);
    connect(timer, &QTimer::timeout, this, [this, id, rpcMethod]() {
        onRequestTimeout(id, rpcMethod);
    });
    timer->start();

    PendingRequest pr;
    pr.id     = id;
    pr.method = rpcMethod;
    pr.timer  = timer;
    pendingRequests_[id] = pr;

    ++nextRequestId_;
}

void IpcClient::dispatchResponse(const QJsonObject& root)
{
    int id = root[QStringLiteral("id")].toInt();

    auto it = pendingRequests_.find(id);
    if (it == pendingRequests_.end()) {
        emit protocolError(
            QStringLiteral("Received response for unknown id: ") + QString::number(id)
        );
        return;
    }

    PendingRequest pr = it.value();
    pr.timer->stop();
    pr.timer->deleteLater();
    pendingRequests_.erase(it);

    // Handle error response
    if (root.contains(QStringLiteral("error"))) {
        QJsonObject err = root[QStringLiteral("error")].toObject();
        emit rpcError(pr.method,
                      err[QStringLiteral("code")].toInt(),
                      err[QStringLiteral("message")].toString());
        return;
    }

    // Handle success response
    if (!root.contains(QStringLiteral("result"))) {
        emit protocolError(QStringLiteral("Response missing both result and error"));
        return;
    }

    QJsonObject result = root[QStringLiteral("result")].toObject();

    if (pr.method == QStringLiteral("session.init")) {
        sessionId_ = result[QStringLiteral("session_id")].toString();
        emit sessionInitResult(sessionId_);
    } else if (pr.method == QStringLiteral("session.end")) {
        emit sessionEndResult();
    } else if (pr.method == QStringLiteral("command.submit")) {
        emit commandSubmitResult();
    } else if (pr.method == QStringLiteral("command.output")) {
        emit commandOutputResult();
    } else if (pr.method == QStringLiteral("command.complete")) {
        emit commandCompleteResult(result[QStringLiteral("event_id")].toString());
    } else if (pr.method == QStringLiteral("chat.query")) {
        emit chatQueryResult(
            result[QStringLiteral("answer")].toString(),
            jsonArrayToStringList(result[QStringLiteral("references")].toArray())
        );
    } else if (pr.method == QStringLiteral("ecc.query")) {
        emit eccQueryResult(
            result[QStringLiteral("events")].toArray(),
            result[QStringLiteral("total")].toInt()
        );
    } else if (pr.method == QStringLiteral("provenance.write_spec")) {
        emit provenanceWriteSpecResult();
    } else if (pr.method == QStringLiteral("provenance.diff")) {
        emit provenanceDiffResult(
            result[QStringLiteral("mismatches")].toArray(),
            result[QStringLiteral("has_mismatch")].toBool()
        );
    } else {
        emit protocolError(
            QStringLiteral("Unknown method in pending request: ") + pr.method
        );
    }
}

void IpcClient::dispatchNotification(const QJsonObject& root)
{
    QString method = root[QStringLiteral("method")].toString();
    if (method != QStringLiteral("plugin.notify")) {
        emit protocolError(
            QStringLiteral("Unexpected notification method: ") + method
        );
        return;
    }

    QJsonObject params = root[QStringLiteral("params")].toObject();
    emit notificationReceived(
        params[QStringLiteral("notify_type")].toString(),
        params[QStringLiteral("title")].toString(),
        params[QStringLiteral("content")].toString(),
        params[QStringLiteral("detail")].toString(),
        params[QStringLiteral("timestamp")].toString()
    );
}

void IpcClient::onRequestTimeout(int id, const QString& rpcMethod)
{
    auto it = pendingRequests_.find(id);
    if (it == pendingRequests_.end()) return;  // response arrived first (race condition)

    PendingRequest pr = it.value();
    pr.timer->deleteLater();
    pendingRequests_.erase(it);

    emit requestTimeout(rpcMethod, id);
}

void IpcClient::clearPendingWithError(const QString& errorMessage)
{
    for (auto& pr : pendingRequests_) {
        if (pr.timer) {
            pr.timer->stop();
            pr.timer->deleteLater();
        }
        emit rpcError(pr.method, -32000, errorMessage);
    }
    pendingRequests_.clear();
}

QStringList IpcClient::jsonArrayToStringList(const QJsonArray& arr) const
{
    QStringList result;
    result.reserve(arr.size());
    for (const QJsonValue& v : arr) {
        result.append(v.toString());
    }
    return result;
}
