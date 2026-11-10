// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 同步 client call 只在 worker thread 執行，錯誤字串不送往 UI。

#include "dqe_viewer/production/dqe_rpc_worker.h"

#include "dqe_viewer/dqe_viewer_client.h"
#include "dqe_viewer/production/archived_dto_mapper.h"
#include "dqe_viewer/production/temporary_dto_mapper.h"

#include <QFileInfo>
#include <QThread>

namespace terminux::dqe::production {
namespace {

bool isArchivedVersionDirectoryValidationError(const DqeViewerCallResult &result)
{
    // 此 RPC 在本 task 僅送出 version_dir；-32602 即既有 directory validator。
    // GroupLoad 的 -32602 或其他 request domain 一律維持 safe contract error。
    return result.errorCode == -32602;
}

} // namespace

DqeRpcWorker::DqeRpcWorker(ArchivedLaunchContext context, QObject *parent)
    : QObject(parent)
    , m_context(std::move(context))
{
}

DqeRpcWorker::DqeRpcWorker(TemporaryLaunchContext context, QObject *parent)
    : QObject(parent)
    , m_context(std::move(context))
{
}

void DqeRpcWorker::runRequest(quint64 token, int domainValue, const QString &privateTargetRef,
                              const QJsonObject &operationParameters, int temporaryRetentionDays)
{
    const auto domain = static_cast<RequestDomain>(domainValue);
    TransportReply reply;
    if (const auto *archived = std::get_if<ArchivedLaunchContext>(&m_context)) {
        if (domain == RequestDomain::SourceList) {
            if (!QFileInfo(archived->archivedVersionDir).isDir()) {
                reply.state = RepositoryState::Unavailable;
            } else {
                DqeViewerClient client(archived->ipcEndpoint);
                const DqeViewerCallResult result = client.listArchivedGroups(archived->archivedVersionDir);
                reply = result.ok ? ArchivedDtoMapper::mapListResult(result.result)
                                  : TransportReply{isArchivedVersionDirectoryValidationError(result)
                                                       ? RepositoryState::Unavailable
                                                       : RepositoryState::Error,
                                                   {}};
            }
        } else if (domain == RequestDomain::GroupLoad) {
            DqeViewerClient client(archived->ipcEndpoint);
            const DqeViewerCallResult result = client.loadGroup(privateTargetRef);
            reply = result.ok ? ArchivedDtoMapper::mapGroupResult(result.result)
                              : TransportReply{RepositoryState::Error, {}};
        } else if (domain == RequestDomain::AttachmentPreview) {
            DqeViewerClient client(archived->ipcEndpoint);
            const DqeViewerCallResult result = client.loadAttachment(privateTargetRef);
            reply.state = result.ok ? RepositoryState::Ready : RepositoryState::Error;
            // service result（含 base64）只作 controller private mapping，不在 worker 解碼或 log。
            reply.attachmentResponse = result.result;
        } else if (domain == RequestDomain::Checkin) {
            // Archived 永遠不可 check-in；保持 safe error，絕不嘗試其他後端方法。
            reply.state = RepositoryState::Error;
        } else if (domain == RequestDomain::RetentionLoad || domain == RequestDomain::RetentionSave) {
            // Retention 是 Temporary 全域 policy；Archived context 不得操作。
            reply.state = RepositoryState::Error;
        } else if (domain == RequestDomain::PurgePreview || domain == RequestDomain::PurgeCommit) {
            // Purge 僅可在 Temporary context 執行，Archived 不得操作。
            reply.state = RepositoryState::Error;
        } else {
            reply.state = RepositoryState::Error;
        }
    } else if (const auto *temporary = std::get_if<TemporaryLaunchContext>(&m_context)) {
        const int effectiveRetentionDays = temporaryRetentionDays > 0
                                             ? temporaryRetentionDays : temporary->retentionDays;
        if (domain == RequestDomain::SourceList) {
            const QFileInfo dbInfo(temporary->temporaryDbPath);
            if (!dbInfo.isFile()) {
                reply.state = RepositoryState::Unavailable;
            } else {
                DqeViewerClient client(temporary->ipcEndpoint);
                const DqeViewerCallResult result = client.listTemporaryGroups(
                    temporary->temporaryDbPath, effectiveRetentionDays);
                reply = result.ok ? TemporaryDtoMapper::mapListResult(result.result,
                                                                         effectiveRetentionDays)
                                  : TransportReply{RepositoryState::Error, {}};
            }
        } else if (domain == RequestDomain::GroupLoad) {
            DqeViewerClient client(temporary->ipcEndpoint);
            const DqeViewerCallResult result = client.loadGroup(privateTargetRef);
            reply = result.ok ? TemporaryDtoMapper::mapGroupResult(result.result,
                                                                      effectiveRetentionDays)
                              : TransportReply{RepositoryState::Error, {}};
        } else if (domain == RequestDomain::AttachmentPreview) {
            DqeViewerClient client(temporary->ipcEndpoint);
            const DqeViewerCallResult result = client.loadAttachment(privateTargetRef);
            reply.state = result.ok ? RepositoryState::Ready : RepositoryState::Error;
            // service result（含 base64）只作 controller private mapping，不在 worker 解碼或 log。
            reply.attachmentResponse = result.result;
        } else if (domain == RequestDomain::Checkin) {
            QJsonObject parameters = operationParameters;
            // DB path 是 injected private context，絕不可由 QML 提供或回送。
            parameters.insert(QStringLiteral("db_path"), temporary->temporaryDbPath);
            DqeViewerClient client(temporary->ipcEndpoint);
            const DqeViewerCallResult result = client.checkinJob(parameters);
            reply.state = result.ok ? RepositoryState::Ready : RepositoryState::Error;
            reply.operationResponse = result.result;
        } else if (domain == RequestDomain::RetentionLoad) {
            DqeViewerClient client(temporary->ipcEndpoint);
            const DqeViewerCallResult result = client.getRetention();
            reply.state = result.ok ? RepositoryState::Ready : RepositoryState::Error;
            reply.retentionResponse = result.result;
        } else if (domain == RequestDomain::RetentionSave) {
            const int retentionDays = operationParameters.value(QStringLiteral("retention_days")).toInt();
            DqeViewerClient client(temporary->ipcEndpoint);
            const DqeViewerCallResult result = client.setRetention(retentionDays);
            reply.state = result.ok ? RepositoryState::Ready : RepositoryState::Error;
            reply.retentionResponse = result.result;
        } else if (domain == RequestDomain::PurgePreview) {
            QJsonObject parameters = operationParameters;
            // DB path 是 injected private context；purge 固定禁止 VACUUM。
            parameters.insert(QStringLiteral("db_path"), temporary->temporaryDbPath);
            parameters.insert(QStringLiteral("vacuum_requested"), false);
            DqeViewerClient client(temporary->ipcEndpoint);
            const DqeViewerCallResult result = client.previewPurge(parameters);
            reply.state = result.ok ? RepositoryState::Ready : RepositoryState::Error;
            reply.purgeResponse = result.result;
        } else if (domain == RequestDomain::PurgeCommit) {
            DqeViewerClient client(temporary->ipcEndpoint);
            const DqeViewerCallResult result = client.purgeJobs(operationParameters);
            reply.state = result.ok ? RepositoryState::Ready : RepositoryState::Error;
            reply.purgeResponse = result.result;
        } else {
            reply.state = RepositoryState::Error;
        }
    } else {
        reply.state = RepositoryState::Error;
    }
    emit requestCompleted(token, domainValue, reply);
}

} // namespace terminux::dqe::production
