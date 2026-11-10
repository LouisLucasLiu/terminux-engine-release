// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Temporary check-in 的唯一 QML operation 邊界；所有 backend context 維持 private。

#pragma once

#include "dqe_viewer/production/dqe_repository_controller.h"
#include "dqe_viewer/production/dqe_repository_transport.h"

#include <QObject>
#include <QHash>
#include <QPointer>
#include <QStringList>
#include <QVariantList>

#include <memory>

namespace terminux::dqe::production {

class DqeOperationController final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(bool checkinEnabled READ checkinEnabled NOTIFY eligibilityChanged)
    Q_PROPERTY(bool retentionEnabled READ retentionEnabled NOTIFY eligibilityChanged)
    Q_PROPERTY(bool running READ running NOTIFY operationChanged)
    Q_PROPERTY(QString operationState READ operationState NOTIFY operationChanged)
    Q_PROPERTY(QString operationPhase READ operationPhase NOTIFY operationChanged)
    Q_PROPERTY(bool confirmationReady READ confirmationReady NOTIFY operationChanged)
    Q_PROPERTY(QString jobKey READ jobKey NOTIFY operationChanged)
    Q_PROPERTY(QString workspaceRoot READ workspaceRoot NOTIFY operationChanged)
    Q_PROPERTY(QString targetVersionPath READ targetVersionPath NOTIFY operationChanged)
    Q_PROPERTY(QString lastResult READ lastResult NOTIFY operationChanged)
    Q_PROPERTY(QString failureCode READ failureCode NOTIFY operationChanged)
    Q_PROPERTY(QString operationMessage READ operationMessage NOTIFY operationChanged)
    Q_PROPERTY(bool partial READ partial NOTIFY operationChanged)
    Q_PROPERTY(QVariantList files READ files NOTIFY operationChanged)
    Q_PROPERTY(QVariantList conflicts READ conflicts NOTIFY operationChanged)
    Q_PROPERTY(QStringList warnings READ warnings NOTIFY operationChanged)
    Q_PROPERTY(bool retentionEditorReady READ retentionEditorReady NOTIFY operationChanged)
    Q_PROPERTY(int authoritativeRetentionDays READ authoritativeRetentionDays NOTIFY operationChanged)
    Q_PROPERTY(int recordsDeleted READ recordsDeleted NOTIFY operationChanged)
    Q_PROPERTY(bool purgeSelectedEnabled READ purgeSelectedEnabled NOTIFY eligibilityChanged)
    Q_PROPERTY(bool purgeExpiredEnabled READ purgeExpiredEnabled NOTIFY eligibilityChanged)
    Q_PROPERTY(bool purgePreviewReady READ purgePreviewReady NOTIFY operationChanged)
    Q_PROPERTY(QString purgePreviewMode READ purgePreviewMode NOTIFY operationChanged)
    Q_PROPERTY(int purgeCandidateJobCount READ purgeCandidateJobCount NOTIFY operationChanged)
    Q_PROPERTY(int purgeCandidateRowCount READ purgeCandidateRowCount NOTIFY operationChanged)
    Q_PROPERTY(int purgeCandidateAttachmentBlobCount READ purgeCandidateAttachmentBlobCount NOTIFY operationChanged)
    Q_PROPERTY(QStringList purgeJobLabels READ purgeJobLabels NOTIFY operationChanged)
    Q_PROPERTY(QStringList purgeWarnings READ purgeWarnings NOTIFY operationChanged)

public:
    DqeOperationController(DqeRepositoryController *repository,
                           std::unique_ptr<DqeRepositoryTransport> transport,
                           QObject *parent = nullptr);
    ~DqeOperationController() override;

    bool checkinEnabled() const;
    bool retentionEnabled() const;
    bool running() const;
    QString operationState() const;
    QString operationPhase() const;
    bool confirmationReady() const;
    QString jobKey() const;
    QString workspaceRoot() const;
    QString targetVersionPath() const;
    QString lastResult() const;
    QString failureCode() const;
    QString operationMessage() const;
    bool partial() const;
    QVariantList files() const;
    QVariantList conflicts() const;
    QStringList warnings() const;
    bool retentionEditorReady() const;
    int authoritativeRetentionDays() const;
    int recordsDeleted() const;
    bool purgeSelectedEnabled() const;
    bool purgeExpiredEnabled() const;
    bool purgePreviewReady() const;
    QString purgePreviewMode() const;
    int purgeCandidateJobCount() const;
    int purgeCandidateRowCount() const;
    int purgeCandidateAttachmentBlobCount() const;
    QStringList purgeJobLabels() const;
    QStringList purgeWarnings() const;

    Q_INVOKABLE bool beginCheckinDryRun(const QString &workspaceRoot,
                                        const QString &targetVersionPath);
    Q_INVOKABLE bool confirmCheckin();
    Q_INVOKABLE void cancelConfirmation();
    Q_INVOKABLE bool beginRetentionEditor();
    Q_INVOKABLE bool saveRetentionPolicy(const QString &retentionDays);
    Q_INVOKABLE void cancelRetentionEditor();
    Q_INVOKABLE bool beginPurgePreview(const QString &mode);
    Q_INVOKABLE bool confirmPurge();
    Q_INVOKABLE void cancelPurgePreview();

signals:
    void eligibilityChanged();
    void operationChanged();
    // QML/provider 只 refresh 指定 opaque source，不變更目前使用者 selection。
    void refreshRequested(const QString &sourceId);
    // 全域 policy 成功後，repository 已重列所有 Temporary jobs；QML 只更新顯示。
    void retentionPolicyRefreshed(int retentionDays);
    // Preview 因 selection 變更或 commit terminal result 而失效；QML 只能關閉安全確認畫面。
    void purgePreviewInvalidated();

private:
    struct SelectedJob {
        QString sourceId;
        QString jobKey;
        bool eligible = false;
        bool purgeable = false;
    };

    SelectedJob selectedJob() const;
    bool validInput(const QString &workspaceRoot, const QString &targetVersionPath);
    void startRequest(bool dryRun);
    void completeRequest(RequestToken token, TransportReply reply);
    void startRetentionRequest(RequestDomain domain, int retentionDays = 0);
    void completeRetentionRequest(RequestDomain domain, RequestToken token, TransportReply reply);
    void startPurgeRequest(RequestDomain domain);
    void completePurgeRequest(RequestDomain domain, RequestToken token, TransportReply reply);
    void invalidatePurgePreview(const QString &message = QString());
    bool purgeSelectionStillMatches() const;
    void setRefused(const QString &message, const QString &failureCode);
    void resetTerminalForNewDryRun();
    void clearConfirmation();
    bool selectionStillMatches() const;
    static int domainKey(RequestDomain domain);

    QPointer<DqeRepositoryController> m_repository;
    std::unique_ptr<DqeRepositoryTransport> m_transport;
    RequestToken m_nextToken = 0;
    // operation domain 各自保有 current generation；不得由另一 domain 誤淘汰。
    QHash<int, RequestToken> m_currentTokens;
    bool m_running = false;
    bool m_dryRun = false;
    bool m_confirmationReady = false;
    bool m_retentionEditorReady = false;
    QString m_operationState = QStringLiteral("idle");
    QString m_operationPhase = QStringLiteral("idle");
    QString m_sourceId;
    QString m_jobKey;
    QString m_workspaceRoot;
    QString m_targetVersionPath;
    QString m_lastResult = QStringLiteral("not_run");
    QString m_failureCode;
    QString m_operationMessage;
    bool m_partial = false;
    QVariantList m_files;
    QVariantList m_conflicts;
    QStringList m_warnings;
    int m_authoritativeRetentionDays = 0;
    int m_recordsDeleted = 0;
    bool m_purgePreviewReady = false;
    bool m_purgeSelectionChanged = false;
    QString m_purgeMode;
    QString m_privatePurgeToken;
    QString m_purgeSourceId;
    QString m_purgeJobKey;
    quint64 m_purgeSourceGeneration = 0;
    int m_purgeCandidateJobCount = 0;
    int m_purgeCandidateRowCount = 0;
    int m_purgeCandidateAttachmentBlobCount = 0;
    QStringList m_purgeJobLabels;
    QStringList m_purgeWarnings;
};

} // 命名空間 terminux::dqe::production
