// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "dqe_viewer/dqe_viewer_client.h"

#include <QJsonValue>

DqeViewerClient::DqeViewerClient(QString ipcEndpoint, QObject *parent)
    : QObject(parent),
      m_ipcEndpoint(std::move(ipcEndpoint)),
      m_status(m_ipcEndpoint.isEmpty() ? Status::NotConfigured : Status::NotConnected),
      m_nextRequestId(1)
{
    // 包裝既有 JsonRpcClient。endpoint 由 --ipc-endpoint 明確提供；
    // 沒有 endpoint 時保持 not_configured，不假造 connected。
}

QString DqeViewerClient::ipcEndpoint() const
{
    return m_ipcEndpoint;
}

DqeViewerClient::Status DqeViewerClient::status() const
{
    return m_status;
}

QString DqeViewerClient::statusText() const
{
    switch (m_status) {
    case Status::NotConfigured:
        return QStringLiteral("not configured");
    case Status::NotConnected:
        return QStringLiteral("not connected");
    case Status::Connected:
        return QStringLiteral("connected");
    case Status::Error:
        return QStringLiteral("error");
    }
    return QStringLiteral("not configured");
}

QString DqeViewerClient::lastError() const
{
    return m_lastError;
}

DqeViewerCallResult DqeViewerClient::health()
{
    auto response = call(QStringLiteral("dqe_viewer.health"), QJsonObject{});
    if (!response.ok) {
        return response;
    }

    const auto service = response.result.value(QStringLiteral("service")).toString();
    const auto status = response.result.value(QStringLiteral("status")).toString();
    if (service != QStringLiteral("dqe_viewer") || status != QStringLiteral("ok")) {
        setError(QStringLiteral("DQE Viewer service mismatch"));
        return DqeViewerCallResult{
            false,
            response.result,
            m_lastError,
        };
    }

    m_status = Status::Connected;
    m_lastError.clear();
    return response;
}

DqeViewerCallResult DqeViewerClient::listArchivedGroups(const QString &versionDir)
{
    QJsonObject params;
    params[QStringLiteral("version_dir")] = versionDir;
    return call(QStringLiteral("dqe_viewer.list_archived_groups"), params);
}

DqeViewerCallResult DqeViewerClient::listTemporaryGroups(const QString &dbPath, int retentionDays)
{
    QJsonObject params;
    if (!dbPath.isEmpty()) {
        params[QStringLiteral("db_path")] = dbPath;
    } else {
        params[QStringLiteral("db_path")] = QJsonValue::Null;
    }
    if (retentionDays > 0) {
        params[QStringLiteral("retention_days")] = retentionDays;
    }
    return call(QStringLiteral("dqe_viewer.list_temporary_groups"), params);
}

DqeViewerCallResult DqeViewerClient::loadGroup(const QString &groupRef)
{
    QJsonObject params;
    params[QStringLiteral("group_ref")] = groupRef;
    return call(QStringLiteral("dqe_viewer.load_group"), params);
}

DqeViewerCallResult DqeViewerClient::checkinJob(const QJsonObject &request)
{
    return call(QStringLiteral("dqe_viewer.checkin_job"), request);
}

DqeViewerCallResult DqeViewerClient::getRetention()
{
    return call(QStringLiteral("dqe_viewer.get_retention"), QJsonObject{});
}

DqeViewerCallResult DqeViewerClient::setRetention(int retentionDays, const QString &updatedBy)
{
    QJsonObject params;
    params[QStringLiteral("retention_days")] = retentionDays;
    if (!updatedBy.isEmpty()) {
        params[QStringLiteral("updated_by")] = updatedBy;
    }
    return call(QStringLiteral("dqe_viewer.set_retention"), params);
}

DqeViewerCallResult DqeViewerClient::loadAttachment(const QString &attachmentRef, const QString &previewRequestId)
{
    QJsonObject params;
    params[QStringLiteral("attachment_ref")] = attachmentRef;
    if (!previewRequestId.isEmpty()) {
        params[QStringLiteral("preview_request_id")] = previewRequestId;
    }
    return call(QStringLiteral("dqe_viewer.load_attachment"), params);
}

DqeViewerCallResult DqeViewerClient::previewPurge(const QJsonObject &request)
{
    return call(QStringLiteral("dqe_viewer.preview_purge"), request);
}

DqeViewerCallResult DqeViewerClient::purgeJobs(const QJsonObject &request)
{
    return call(QStringLiteral("dqe_viewer.purge_jobs"), request);
}

DqeViewerCallResult DqeViewerClient::call(const QString &method, const QJsonObject &params)
{
    if (m_ipcEndpoint.isEmpty()) {
        m_status = Status::NotConfigured;
        m_lastError = QStringLiteral("IPC endpoint is not configured");
        return DqeViewerCallResult{false, {}, m_lastError};
    }

    terminux::ipc::JsonRpcClient client(
        terminux::ipc::JsonRpcClientOptions{m_ipcEndpoint.toStdString(), 3000});

    QJsonObject req;
    req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    req[QStringLiteral("id")] = nextRequestId();
    req[QStringLiteral("method")] = method;
    req[QStringLiteral("params")] = params;

    const auto transport = client.send_request(req);
    if (!transport.transport_ok) {
        setError(QString::fromStdString(transport.error_message));
        return DqeViewerCallResult{false, {}, m_lastError};
    }

    if (transport.response.contains(QStringLiteral("error"))) {
        const auto error = transport.response.value(QStringLiteral("error")).toObject();
        const int errorCode = error.value(QStringLiteral("code")).toInt();
        setError(error.value(QStringLiteral("message")).toString(QStringLiteral("JSON-RPC error")));
        return DqeViewerCallResult{false, {}, m_lastError, errorCode};
    }

    if (!transport.response.contains(QStringLiteral("result")) ||
        !transport.response.value(QStringLiteral("result")).isObject()) {
        setError(QStringLiteral("Malformed JSON-RPC response"));
        return DqeViewerCallResult{false, {}, m_lastError};
    }

    m_lastError.clear();
    return DqeViewerCallResult{
        true,
        transport.response.value(QStringLiteral("result")).toObject(),
        {},
    };
}

void DqeViewerClient::setError(const QString &message)
{
    m_status = Status::Error;
    m_lastError = message;
}

int DqeViewerClient::nextRequestId()
{
    return m_nextRequestId++;
}
