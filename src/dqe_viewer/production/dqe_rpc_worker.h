// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 唯一可建立 DqeViewerClient 的 QObject，必須移入 worker QThread。

#pragma once

#include "dqe_viewer/production/dqe_repository_types.h"

#include <QObject>

#include <variant>

namespace terminux::dqe::production {

class DqeRpcWorker final : public QObject
{
    Q_OBJECT

public:
    explicit DqeRpcWorker(ArchivedLaunchContext context, QObject *parent = nullptr);
    explicit DqeRpcWorker(TemporaryLaunchContext context, QObject *parent = nullptr);

public slots:
    void runRequest(quint64 token, int domain, const QString &privateTargetRef,
                    const QJsonObject &operationParameters = {}, int temporaryRetentionDays = 0);

signals:
    void requestCompleted(quint64 token, int domain, TransportReply reply);

private:
    std::variant<ArchivedLaunchContext, TemporaryLaunchContext> m_context;
};

} // namespace terminux::dqe::production
