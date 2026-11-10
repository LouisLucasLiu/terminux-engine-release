// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// transport 不解析 DTO、不記錄 private ref；只維持 queued completion。

#include "dqe_viewer/production/dqe_viewer_transport.h"

#include "dqe_viewer/production/dqe_rpc_worker.h"

#include <QMetaObject>

namespace terminux::dqe::production {

DqeViewerTransport::DqeViewerTransport(ArchivedLaunchContext context, QObject *parent)
    : QObject(parent)
{
    qRegisterMetaType<TransportReply>();
    m_worker = new DqeRpcWorker(std::move(context));
    m_worker->moveToThread(&m_thread);
    connect(&m_thread, &QThread::finished, m_worker, &QObject::deleteLater);
    connect(m_worker, &DqeRpcWorker::requestCompleted, this,
            [this](quint64 token, int, TransportReply reply) {
                const auto completion = m_completions.take(token);
                if (completion) {
                    completion(std::move(reply));
                }
            }, Qt::QueuedConnection);
    m_thread.start();
}

DqeViewerTransport::DqeViewerTransport(TemporaryLaunchContext context, QObject *parent)
    : QObject(parent)
{
    qRegisterMetaType<TransportReply>();
    m_worker = new DqeRpcWorker(std::move(context));
    m_worker->moveToThread(&m_thread);
    connect(&m_thread, &QThread::finished, m_worker, &QObject::deleteLater);
    connect(m_worker, &DqeRpcWorker::requestCompleted, this,
            [this](quint64 token, int, TransportReply reply) {
                const auto completion = m_completions.take(token);
                if (completion) {
                    completion(std::move(reply));
                }
            }, Qt::QueuedConnection);
    m_thread.start();
}

DqeViewerTransport::~DqeViewerTransport()
{
    m_completions.clear();
    m_thread.quit();
    m_thread.wait();
}

void DqeViewerTransport::request(const TransportRequest &requestValue, TransportCompletion completion)
{
    m_completions.insert(requestValue.token, std::move(completion));
    QMetaObject::invokeMethod(m_worker, "runRequest", Qt::QueuedConnection,
                              Q_ARG(quint64, requestValue.token),
                              Q_ARG(int, static_cast<int>(requestValue.domain)),
                              Q_ARG(QString, requestValue.privateTargetRef),
                              Q_ARG(QJsonObject, requestValue.operationParameters),
                              Q_ARG(int, requestValue.temporaryRetentionDays));
}

bool DqeViewerTransport::cancelQueued(RequestToken token)
{
    return m_completions.remove(token) > 0;
}

QThread *DqeViewerTransport::workerThread()
{
    return &m_thread;
}

} // namespace terminux::dqe::production
