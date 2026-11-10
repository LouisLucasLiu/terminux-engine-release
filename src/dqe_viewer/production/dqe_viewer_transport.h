// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// controller transport；將 request 排入 RPC worker，不在 GUI thread 連 IPC。

#pragma once

#include "dqe_viewer/production/dqe_repository_transport.h"

#include <QHash>
#include <QObject>
#include <QThread>

namespace terminux::dqe::production {

class DqeRpcWorker;

class DqeViewerTransport final : public QObject, public DqeRepositoryTransport
{
    Q_OBJECT

public:
    explicit DqeViewerTransport(ArchivedLaunchContext context, QObject *parent = nullptr);
    explicit DqeViewerTransport(TemporaryLaunchContext context, QObject *parent = nullptr);
    ~DqeViewerTransport() override;

    void request(const TransportRequest &request, TransportCompletion completion) override;
    bool cancelQueued(RequestToken token) override;

    QThread *workerThread();

private:
    QThread m_thread;
    DqeRpcWorker *m_worker = nullptr;
    QHash<RequestToken, TransportCompletion> m_completions;
};

} // namespace terminux::dqe::production
