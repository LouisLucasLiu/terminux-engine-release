// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include "ipc/jsonrpc_client.h"

#include <QObject>
#include <QJsonArray>
#include <QJsonObject>
#include <QString>

struct DqeViewerCallResult
{
    bool ok = false;
    QJsonObject result;
    QString errorMessage;
    // 僅供 production worker 做已知 RPC contract 分類；不得傳至 QML。
    int errorCode = 0;
};

class DqeViewerClient : public QObject
{
    Q_OBJECT

public:
    enum class Status {
        NotConfigured,
        NotConnected,
        Connected,
        Error,
    };

    explicit DqeViewerClient(QString ipcEndpoint = QString(), QObject *parent = nullptr);

    QString ipcEndpoint() const;
    Status status() const;
    QString statusText() const;
    QString lastError() const;

    DqeViewerCallResult health();
    DqeViewerCallResult listArchivedGroups(const QString &versionDir);
    DqeViewerCallResult listTemporaryGroups(const QString &dbPath = QString(), int retentionDays = 0);
    DqeViewerCallResult loadGroup(const QString &groupRef);
    DqeViewerCallResult checkinJob(const QJsonObject &request);
    DqeViewerCallResult getRetention();
    DqeViewerCallResult setRetention(int retentionDays, const QString &updatedBy = QString());
    DqeViewerCallResult loadAttachment(const QString &attachmentRef, const QString &previewRequestId = QString());
    DqeViewerCallResult previewPurge(const QJsonObject &request);
    DqeViewerCallResult purgeJobs(const QJsonObject &request);

private:
    DqeViewerCallResult call(const QString &method, const QJsonObject &params);
    void setError(const QString &message);
    int nextRequestId();

    QString m_ipcEndpoint;
    Status m_status;
    QString m_lastError;
    int m_nextRequestId;
};
