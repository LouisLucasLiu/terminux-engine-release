// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// dry-run 是 actual check-in 的必要前置；不自動重試，也不更動 repository selection。

#include "dqe_viewer/production/dqe_operation_controller.h"

#include "dqe_viewer/production/checkin_dto_mapper.h"
#include "dqe_viewer/production/purge_dto_mapper.h"
#include "dqe_viewer/production/retention_dto_mapper.h"

#include <QDir>
#include <QJsonArray>

#include <utility>

namespace terminux::dqe::production {

DqeOperationController::DqeOperationController(DqeRepositoryController *repository,
                                                 std::unique_ptr<DqeRepositoryTransport> transport,
                                                 QObject *parent)
    : QObject(parent)
    , m_repository(repository)
    , m_transport(std::move(transport))
{
    if (m_repository) {
        connect(m_repository, &DqeRepositoryController::selectedSourceChanged,
                this, &DqeOperationController::eligibilityChanged);
        connect(m_repository, &DqeRepositoryController::sourcesChanged,
                this, &DqeOperationController::eligibilityChanged);
        connect(m_repository, &DqeRepositoryController::selectedSourceChanged, this, [this]() {
            if (m_purgePreviewReady) {
                invalidatePurgePreview(QStringLiteral("The selection changed; request a new purge preview."));
            } else if (m_running && !m_purgeMode.isEmpty()) {
                // 已送出的 preview 無法宣稱取消；completion 只會安全丟棄它的 token。
                m_purgeSelectionChanged = true;
            }
        });
        connect(m_repository, &DqeRepositoryController::requestStarted, this,
                [this](quint64, const QString &domainName) {
            // 明確 Refresh（亦是 reconnect recovery 的唯一 read path）會使 preview plan 過期；
            // 不可讓舊 token 在新 source snapshot 抵達前被 commit。
            if (domainName == QLatin1String("list")
                && (m_purgePreviewReady || (m_running && !m_purgeMode.isEmpty()))) {
                if (m_purgePreviewReady) {
                    invalidatePurgePreview(
                        QStringLiteral("The source was refreshed; request a new purge preview."));
                } else {
                    m_purgeSelectionChanged = true;
                }
            }
        });
        connect(m_repository, &DqeRepositoryController::sourcesChanged, this, [this]() {
            // source generation 改變後，即使 opaque selection 仍可見，也不能沿用舊 purge plan。
            if (m_purgePreviewReady) {
                invalidatePurgePreview(
                    QStringLiteral("The source data changed; request a new purge preview."));
            } else if (m_running && !m_purgeMode.isEmpty()) {
                m_purgeSelectionChanged = true;
            }
        });
    }
}

DqeOperationController::~DqeOperationController()
{
    if (m_transport) {
        for (const RequestToken token : std::as_const(m_currentTokens)) {
            if (token != 0) {
                m_transport->cancelQueued(token);
            }
        }
    }
}

bool DqeOperationController::checkinEnabled() const
{
    return !m_running && !m_retentionEditorReady && !m_purgePreviewReady && selectedJob().eligible;
}

bool DqeOperationController::retentionEnabled() const
{
    return !m_running && !m_confirmationReady && !m_purgePreviewReady;
}

bool DqeOperationController::running() const { return m_running; }
QString DqeOperationController::operationState() const { return m_operationState; }
QString DqeOperationController::operationPhase() const { return m_operationPhase; }
bool DqeOperationController::confirmationReady() const { return m_confirmationReady; }
QString DqeOperationController::jobKey() const { return m_jobKey; }
QString DqeOperationController::workspaceRoot() const { return m_workspaceRoot; }
QString DqeOperationController::targetVersionPath() const { return m_targetVersionPath; }
QString DqeOperationController::lastResult() const { return m_lastResult; }
QString DqeOperationController::failureCode() const { return m_failureCode; }
QString DqeOperationController::operationMessage() const { return m_operationMessage; }
bool DqeOperationController::partial() const { return m_partial; }
QVariantList DqeOperationController::files() const { return m_files; }
QVariantList DqeOperationController::conflicts() const { return m_conflicts; }
QStringList DqeOperationController::warnings() const { return m_warnings; }
bool DqeOperationController::retentionEditorReady() const { return m_retentionEditorReady; }
int DqeOperationController::authoritativeRetentionDays() const { return m_authoritativeRetentionDays; }
int DqeOperationController::recordsDeleted() const { return m_recordsDeleted; }
bool DqeOperationController::purgeSelectedEnabled() const
{
    return !m_running && !m_confirmationReady && !m_retentionEditorReady && !m_purgePreviewReady
           && selectedJob().purgeable;
}
bool DqeOperationController::purgeExpiredEnabled() const
{
    return !m_running && !m_confirmationReady && !m_retentionEditorReady && !m_purgePreviewReady
           && m_repository;
}
bool DqeOperationController::purgePreviewReady() const { return m_purgePreviewReady; }
QString DqeOperationController::purgePreviewMode() const { return m_purgeMode; }
int DqeOperationController::purgeCandidateJobCount() const { return m_purgeCandidateJobCount; }
int DqeOperationController::purgeCandidateRowCount() const { return m_purgeCandidateRowCount; }
int DqeOperationController::purgeCandidateAttachmentBlobCount() const
{
    return m_purgeCandidateAttachmentBlobCount;
}
QStringList DqeOperationController::purgeJobLabels() const { return m_purgeJobLabels; }
QStringList DqeOperationController::purgeWarnings() const { return m_purgeWarnings; }

bool DqeOperationController::beginCheckinDryRun(const QString &workspaceRoot,
                                                  const QString &targetVersionPath)
{
    if (m_running || m_confirmationReady || m_retentionEditorReady || !m_transport) {
        return false;
    }
    const SelectedJob selected = selectedJob();
    if (!selected.eligible) {
        setRefused(QStringLiteral("Select an eligible Temporary job before check-in."),
                    QStringLiteral("invalid_request"));
        return false;
    }
    if (!validInput(workspaceRoot, targetVersionPath)) {
        return false;
    }

    resetTerminalForNewDryRun();
    m_sourceId = selected.sourceId;
    m_jobKey = selected.jobKey;
    m_workspaceRoot = workspaceRoot.trimmed();
    m_targetVersionPath = targetVersionPath.trimmed();
    startRequest(true);
    return true;
}

bool DqeOperationController::confirmCheckin()
{
    if (m_running || !m_confirmationReady || !selectionStillMatches() || !m_transport) {
        if (m_confirmationReady && !selectionStillMatches()) {
            setRefused(QStringLiteral("The selected job changed; run a new dry-run before check-in."),
                        QStringLiteral("invalid_request"));
            clearConfirmation();
        }
        return false;
    }
    m_confirmationReady = false;
    startRequest(false);
    return true;
}

void DqeOperationController::cancelConfirmation()
{
    if (!m_confirmationReady || m_running) {
        return;
    }
    clearConfirmation();
    m_operationState = QStringLiteral("idle");
    m_operationPhase = QStringLiteral("idle");
    m_operationMessage.clear();
    emit operationChanged();
}

bool DqeOperationController::beginRetentionEditor()
{
    if (!retentionEnabled() || !m_transport) {
        return false;
    }
    m_retentionEditorReady = false;
    startRetentionRequest(RequestDomain::RetentionLoad);
    return true;
}

bool DqeOperationController::saveRetentionPolicy(const QString &retentionDays)
{
    if (m_running || !m_retentionEditorReady || !m_transport) {
        return false;
    }
    bool parsed = false;
    const int days = retentionDays.trimmed().toInt(&parsed);
    if (!parsed || days < 1 || days > 3650) {
        setRefused(QStringLiteral("Retention policy must be an integer from 1 to 3650 days."),
                    QStringLiteral("invalid_retention_days"));
        return false;
    }
    startRetentionRequest(RequestDomain::RetentionSave, days);
    return true;
}

void DqeOperationController::cancelRetentionEditor()
{
    if (m_running || !m_retentionEditorReady) {
        return;
    }
    m_retentionEditorReady = false;
    m_operationState = QStringLiteral("idle");
    m_operationPhase = QStringLiteral("idle");
    m_operationMessage.clear();
    emit eligibilityChanged();
    emit operationChanged();
}

bool DqeOperationController::beginPurgePreview(const QString &mode)
{
    if ((!purgeSelectedEnabled() && mode == QLatin1String("selected_jobs"))
        || (!purgeExpiredEnabled() && mode == QLatin1String("expired_now")) || !m_transport) {
        return false;
    }
    if (mode != QLatin1String("selected_jobs") && mode != QLatin1String("expired_now")) {
        setRefused(QStringLiteral("Only selected jobs or jobs expired now can be purged."),
                    QStringLiteral("invalid_purge_mode"));
        return false;
    }
    const SelectedJob selected = selectedJob();
    if (mode == QLatin1String("selected_jobs") && !selected.purgeable) {
        setRefused(QStringLiteral("Select one Temporary job before requesting a purge preview."),
                    QStringLiteral("invalid_purge_selection"));
        return false;
    }
    invalidatePurgePreview();
    m_purgeMode = mode;
    m_purgeSourceId = m_repository ? m_repository->selectedSourceId() : QString();
    m_purgeJobKey = mode == QLatin1String("selected_jobs") ? selected.jobKey : QString();
    m_purgeSourceGeneration = m_repository ? m_repository->sourceGeneration() : 0;
    m_purgeSelectionChanged = false;
    startPurgeRequest(RequestDomain::PurgePreview);
    return true;
}

bool DqeOperationController::confirmPurge()
{
    if (m_running || !m_purgePreviewReady || m_privatePurgeToken.isEmpty() || !m_transport) {
        return false;
    }
    if (!purgeSelectionStillMatches()) {
        invalidatePurgePreview(QStringLiteral("The selection changed; request a new purge preview."));
        return false;
    }
    startPurgeRequest(RequestDomain::PurgeCommit);
    return true;
}

void DqeOperationController::cancelPurgePreview()
{
    if (!m_running && m_purgePreviewReady) {
        invalidatePurgePreview();
        m_operationState = QStringLiteral("idle");
        m_operationPhase = QStringLiteral("idle");
        m_operationMessage.clear();
        emit eligibilityChanged();
        emit operationChanged();
    }
}

DqeOperationController::SelectedJob DqeOperationController::selectedJob() const
{
    if (!m_repository) {
        return {};
    }
    const QString selectedId = m_repository->selectedSourceId();
    for (const QVariant &value : m_repository->sources()) {
        const QVariantMap source = value.toMap();
        if (source.value(QStringLiteral("sourceId")).toString() != selectedId) {
            continue;
        }
        SelectedJob selected;
        selected.sourceId = selectedId;
        selected.jobKey = source.value(QStringLiteral("jobOrVersion")).toString();
        selected.eligible = source.value(QStringLiteral("sourceKind")).toString()
                            == QLatin1String("temporary")
                            && source.value(QStringLiteral("canCheckin")).toBool()
                            && !selected.jobKey.isEmpty();
        selected.purgeable = source.value(QStringLiteral("sourceKind")).toString()
                            == QLatin1String("temporary") && !selected.jobKey.isEmpty();
        return selected;
    }
    return {};
}

bool DqeOperationController::validInput(const QString &workspaceRoot,
                                        const QString &targetVersionPath)
{
    const QString workspace = workspaceRoot.trimmed();
    const QString version = targetVersionPath.trimmed();
    if (workspace.isEmpty()) {
        setRefused(QStringLiteral("Select an existing workspace before check-in."),
                    QStringLiteral("target_workspace_missing"));
        return false;
    }
    if (version.isEmpty() || QDir::isAbsolutePath(version)
        || version.split(QLatin1Char('/')).contains(QStringLiteral(".."))) {
        setRefused(QStringLiteral("Provide a safe relative target version path."),
                    QStringLiteral("target_version_dir_missing"));
        return false;
    }
    return true;
}

void DqeOperationController::startRequest(bool dryRun)
{
    m_running = true;
    m_dryRun = dryRun;
    m_operationState = QStringLiteral("running");
    m_operationPhase = dryRun ? QStringLiteral("dry_run") : QStringLiteral("actual");
    const RequestToken token = ++m_nextToken;
    m_currentTokens.insert(domainKey(RequestDomain::Checkin), token);
    emit eligibilityChanged();
    emit operationChanged();

    QJsonObject parameters{
        {QStringLiteral("job_key"), m_jobKey},
        {QStringLiteral("workspace_root"), m_workspaceRoot},
        {QStringLiteral("target_version_path"), m_targetVersionPath},
        {QStringLiteral("dry_run"), dryRun},
    };
    const QPointer<DqeOperationController> self(this);
    m_transport->request({token, RequestDomain::Checkin, {}, parameters},
                         [self, token](TransportReply reply) {
        if (self) {
            self->completeRequest(token, std::move(reply));
        }
    });
}

void DqeOperationController::completeRequest(RequestToken token, TransportReply reply)
{
    if (token != m_currentTokens.value(domainKey(RequestDomain::Checkin))) {
        return;
    }
    m_running = false;
    const bool wasDryRun = m_dryRun;
    m_dryRun = false;
    m_currentTokens.insert(domainKey(RequestDomain::Checkin), 0);

    if (reply.state != RepositoryState::Ready) {
        m_operationState = QStringLiteral("failed");
        m_operationPhase = QStringLiteral("idle");
        m_confirmationReady = false;
        m_lastResult = QStringLiteral("failed");
        m_failureCode = QStringLiteral("operation_failed");
        m_operationMessage = QStringLiteral("The check-in operation failed.");
        m_partial = false;
        m_files.clear();
        m_conflicts.clear();
        m_warnings.clear();
        emit eligibilityChanged();
        emit operationChanged();
        return;
    }

    const CheckinOperationResult mapped = CheckinDtoMapper::mapResponse(reply.operationResponse);
    m_lastResult = mapped.lastResult;
    m_failureCode = mapped.failureCode;
    m_operationMessage = mapped.message;
    m_partial = mapped.partial;
    m_files = mapped.files;
    m_conflicts = mapped.conflicts;
    m_warnings = mapped.warnings;
    if (wasDryRun && mapped.state == QLatin1String("succeeded") && selectionStillMatches()) {
        m_operationState = QStringLiteral("idle");
        m_operationPhase = QStringLiteral("awaiting_confirmation");
        m_confirmationReady = true;
    } else {
        m_operationState = mapped.state;
        m_operationPhase = QStringLiteral("idle");
        m_confirmationReady = false;
        if (!wasDryRun && (mapped.state == QLatin1String("succeeded")
                            || mapped.state == QLatin1String("partial"))) {
            // refresh 不選取 source；selection 已變時也不能被 operation completion 覆寫。
            emit refreshRequested(m_sourceId);
        }
    }
    emit eligibilityChanged();
    emit operationChanged();
}

void DqeOperationController::startRetentionRequest(RequestDomain domain, int retentionDays)
{
    m_running = true;
    m_operationState = QStringLiteral("running");
    m_operationPhase = domain == RequestDomain::RetentionLoad
                           ? QStringLiteral("retention_load") : QStringLiteral("retention_save");
    const RequestToken token = ++m_nextToken;
    m_currentTokens.insert(domainKey(domain), token);
    emit eligibilityChanged();
    emit operationChanged();

    QJsonObject parameters;
    if (domain == RequestDomain::RetentionSave) {
        parameters.insert(QStringLiteral("retention_days"), retentionDays);
    }
    const QPointer<DqeOperationController> self(this);
    TransportRequest request;
    request.token = token;
    request.domain = domain;
    request.operationParameters = parameters;
    m_transport->request(request, [self, domain, token](TransportReply reply) {
        if (self) {
            self->completeRetentionRequest(domain, token, std::move(reply));
        }
    });
}

void DqeOperationController::completeRetentionRequest(RequestDomain domain, RequestToken token,
                                                       TransportReply reply)
{
    if (token != m_currentTokens.value(domainKey(domain))) {
        return;
    }
    m_running = false;
    m_currentTokens.insert(domainKey(domain), 0);
    m_partial = false;
    m_files.clear();
    m_conflicts.clear();
    m_warnings.clear();

    if (reply.state != RepositoryState::Ready) {
        m_operationState = QStringLiteral("failed");
        m_operationPhase = domain == RequestDomain::RetentionSave && m_retentionEditorReady
                               ? QStringLiteral("retention_editing") : QStringLiteral("idle");
        m_lastResult = QStringLiteral("failed");
        m_failureCode = QStringLiteral("operation_failed");
        m_operationMessage = QStringLiteral("The retention policy request failed.");
        emit eligibilityChanged();
        emit operationChanged();
        return;
    }

    const RetentionOperationResult mapped = domain == RequestDomain::RetentionLoad
                                                ? RetentionDtoMapper::mapLoadResponse(reply.retentionResponse)
                                                : RetentionDtoMapper::mapSaveResponse(reply.retentionResponse);
    m_operationState = mapped.state;
    m_failureCode = mapped.failureCode;
    m_operationMessage = mapped.message;
    m_lastResult = mapped.state == QLatin1String("succeeded")
                       ? QStringLiteral("succeeded") : QStringLiteral("failed");
    if (mapped.state != QLatin1String("succeeded")) {
        m_operationPhase = domain == RequestDomain::RetentionSave && m_retentionEditorReady
                               ? QStringLiteral("retention_editing") : QStringLiteral("idle");
        emit eligibilityChanged();
        emit operationChanged();
        return;
    }

    m_authoritativeRetentionDays = mapped.retentionDays;
    m_recordsDeleted = mapped.recordsDeleted;
    if (domain == RequestDomain::RetentionLoad) {
        m_retentionEditorReady = true;
        m_operationState = QStringLiteral("idle");
        m_operationPhase = QStringLiteral("retention_editing");
    } else {
        m_retentionEditorReady = false;
        m_operationPhase = QStringLiteral("idle");
        if (m_repository) {
            m_repository->setTemporaryRetentionDays(mapped.retentionDays);
            m_repository->requestSources();
        }
        emit retentionPolicyRefreshed(mapped.retentionDays);
    }
    emit eligibilityChanged();
    emit operationChanged();
}

void DqeOperationController::startPurgeRequest(RequestDomain domain)
{
    m_running = true;
    m_operationState = QStringLiteral("running");
    m_operationPhase = domain == RequestDomain::PurgePreview
                           ? QStringLiteral("purge_preview") : QStringLiteral("purge_commit");
    const RequestToken token = ++m_nextToken;
    m_currentTokens.insert(domainKey(domain), token);
    QJsonObject parameters{{QStringLiteral("mode"), m_purgeMode}};
    if (domain == RequestDomain::PurgePreview && m_purgeMode == QLatin1String("selected_jobs")) {
        parameters.insert(QStringLiteral("job_keys"), QJsonArray{m_purgeJobKey});
    } else if (domain == RequestDomain::PurgeCommit) {
        parameters.insert(QStringLiteral("confirmation_token"), m_privatePurgeToken);
    }
    emit eligibilityChanged();
    emit operationChanged();
    const QPointer<DqeOperationController> self(this);
    TransportRequest request;
    request.token = token;
    request.domain = domain;
    request.operationParameters = parameters;
    m_transport->request(request, [self, domain, token](TransportReply reply) {
        if (self) {
            self->completePurgeRequest(domain, token, std::move(reply));
        }
    });
}

void DqeOperationController::completePurgeRequest(RequestDomain domain, RequestToken token,
                                                   TransportReply reply)
{
    if (token != m_currentTokens.value(domainKey(domain))) {
        return;
    }
    m_running = false;
    m_currentTokens.insert(domainKey(domain), 0);
    if (domain == RequestDomain::PurgePreview) {
        if (m_purgeSelectionChanged || !purgeSelectionStillMatches()) {
            invalidatePurgePreview(QStringLiteral("The selection changed; request a new purge preview."));
            return;
        }
        if (reply.state != RepositoryState::Ready) {
            invalidatePurgePreview();
            m_operationState = QStringLiteral("failed");
            m_operationPhase = QStringLiteral("idle");
            m_lastResult = QStringLiteral("failed");
            m_failureCode = QStringLiteral("purge_preview_failed");
            m_operationMessage = QStringLiteral("The purge preview could not be created.");
            emit eligibilityChanged();
            emit operationChanged();
            return;
        }
        const PurgePreviewResult mapped = PurgeDtoMapper::mapPreviewResponse(
            reply.purgeResponse, m_purgeMode, m_purgeJobKey);
        m_operationState = mapped.state;
        m_lastResult = mapped.state == QLatin1String("succeeded") ? QStringLiteral("succeeded")
                                                                     : QStringLiteral("failed");
        m_failureCode = mapped.failureCode;
        m_operationMessage = mapped.message;
        m_purgeWarnings = mapped.warnings;
        if (mapped.state == QLatin1String("succeeded")) {
            m_privatePurgeToken = mapped.privateConfirmationToken;
            m_purgeCandidateJobCount = mapped.candidateJobCount;
            m_purgeCandidateRowCount = mapped.candidateRowCount;
            m_purgeCandidateAttachmentBlobCount = mapped.candidateAttachmentBlobCount;
            m_purgeJobLabels = mapped.jobLabels;
            m_purgePreviewReady = true;
            m_operationState = QStringLiteral("idle");
            m_operationPhase = QStringLiteral("awaiting_purge_confirmation");
        } else {
            m_operationPhase = QStringLiteral("idle");
        }
        emit eligibilityChanged();
        emit operationChanged();
        return;
    }

    const QString completedMode = m_purgeMode;
    const QString selectedSourceId = m_purgeSourceId;
    if (reply.state != RepositoryState::Ready) {
        invalidatePurgePreview();
        m_operationState = QStringLiteral("failed");
        m_operationPhase = QStringLiteral("idle");
        m_lastResult = QStringLiteral("failed");
        m_failureCode = QStringLiteral("purge_failed");
        m_operationMessage = QStringLiteral("The purge operation failed.");
        emit eligibilityChanged();
        emit operationChanged();
        return;
    }
    const PurgeCommitResult mapped = PurgeDtoMapper::mapCommitResponse(reply.purgeResponse, completedMode);
    invalidatePurgePreview();
    m_operationState = mapped.state;
    m_operationPhase = QStringLiteral("idle");
    m_lastResult = mapped.state == QLatin1String("succeeded") ? QStringLiteral("succeeded")
                 : mapped.state == QLatin1String("partial") ? QStringLiteral("partial")
                                                               : QStringLiteral("failed");
    m_failureCode = mapped.failureCode;
    m_operationMessage = mapped.message;
    m_partial = mapped.state == QLatin1String("partial");
    m_purgeWarnings = mapped.warnings;
    if ((mapped.state == QLatin1String("succeeded") || mapped.state == QLatin1String("partial"))
        && m_repository) {
        // selected_jobs 的成功刪除不可暫時保留已知不存在的 selection。
        if (completedMode == QLatin1String("selected_jobs") && mapped.deletedJobCount > 0
            && m_repository->selectedSourceId() == selectedSourceId) {
            m_repository->setSelectedSource({});
        }
        m_repository->requestSources();
    }
    emit eligibilityChanged();
    emit operationChanged();
}

void DqeOperationController::invalidatePurgePreview(const QString &message)
{
    const bool hadPreview = m_purgePreviewReady || !m_privatePurgeToken.isEmpty();
    m_purgePreviewReady = false;
    m_privatePurgeToken.clear();
    m_purgeMode.clear();
    m_purgeSourceId.clear();
    m_purgeJobKey.clear();
    m_purgeSourceGeneration = 0;
    m_purgeCandidateJobCount = 0;
    m_purgeCandidateRowCount = 0;
    m_purgeCandidateAttachmentBlobCount = 0;
    m_purgeJobLabels.clear();
    m_purgeWarnings.clear();
    m_purgeSelectionChanged = false;
    if (!message.isEmpty()) {
        m_operationState = QStringLiteral("refused");
        m_operationPhase = QStringLiteral("idle");
        m_lastResult = QStringLiteral("failed");
        m_failureCode = QStringLiteral("purge_preview_stale");
        m_operationMessage = message;
    }
    if (hadPreview) {
        emit purgePreviewInvalidated();
    }
    emit eligibilityChanged();
    emit operationChanged();
}

bool DqeOperationController::purgeSelectionStillMatches() const
{
    if (!m_repository || m_purgeSelectionChanged) {
        return false;
    }
    if (m_repository->sourceGeneration() != m_purgeSourceGeneration) {
        return false;
    }
    if (m_purgeMode == QLatin1String("selected_jobs")) {
        const SelectedJob selected = selectedJob();
        return selected.purgeable && selected.sourceId == m_purgeSourceId
               && selected.jobKey == m_purgeJobKey;
    }
    // expired_now 也綁定 preview 時的 selection；變更後需重新取得 backend 當下的 plan。
    return m_repository->selectedSourceId() == m_purgeSourceId;
}

void DqeOperationController::setRefused(const QString &message, const QString &failureCode)
{
    m_operationState = QStringLiteral("refused");
    m_operationPhase = QStringLiteral("idle");
    m_confirmationReady = false;
    m_lastResult = QStringLiteral("failed");
    m_failureCode = failureCode;
    m_operationMessage = message;
    m_partial = false;
    m_files.clear();
    m_conflicts.clear();
    m_warnings.clear();
    emit operationChanged();
}

void DqeOperationController::resetTerminalForNewDryRun()
{
    m_operationState = QStringLiteral("idle");
    m_operationPhase = QStringLiteral("idle");
    m_confirmationReady = false;
    m_lastResult = QStringLiteral("not_run");
    m_failureCode.clear();
    m_operationMessage.clear();
    m_partial = false;
    m_files.clear();
    m_conflicts.clear();
    m_warnings.clear();
}

void DqeOperationController::clearConfirmation()
{
    m_confirmationReady = false;
}

bool DqeOperationController::selectionStillMatches() const
{
    const SelectedJob selected = selectedJob();
    return selected.eligible && selected.sourceId == m_sourceId && selected.jobKey == m_jobKey;
}

int DqeOperationController::domainKey(RequestDomain domain)
{
    return static_cast<int>(domain);
}

} // 命名空間 terminux::dqe::production
