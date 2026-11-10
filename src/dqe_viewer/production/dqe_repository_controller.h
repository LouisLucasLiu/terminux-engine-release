// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// QML 只看見此 controller 的 allow-list DTO 與 canonical read state。

#pragma once

#include "dqe_viewer/production/dqe_repository_transport.h"
#include "dqe_viewer/production/opaque_id_registry.h"

#include <QHash>
#include <QObject>
#include <QVariantList>

#include <memory>

namespace terminux::dqe::production {

class DqeRepositoryController final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(QVariantList sources READ sources NOTIFY sourcesChanged)
    Q_PROPERTY(QString repositoryState READ repositoryState NOTIFY repositoryStateChanged)
    Q_PROPERTY(QString stateDetail READ stateDetail NOTIFY repositoryStateChanged)
    Q_PROPERTY(QString selectedSourceId READ selectedSourceId NOTIFY selectedSourceChanged)
    Q_PROPERTY(QString selectedItemId READ selectedItemId NOTIFY selectedItemChanged)
    Q_PROPERTY(QString selectedAttachmentId READ selectedAttachmentId NOTIFY selectedAttachmentChanged)
    Q_PROPERTY(QString previewState READ previewState NOTIFY previewChanged)
    Q_PROPERTY(QString previewKind READ previewKind NOTIFY previewChanged)
    Q_PROPERTY(QString previewText READ previewText NOTIFY previewChanged)
    Q_PROPERTY(QString previewImageUrl READ previewImageUrl NOTIFY previewChanged)
    Q_PROPERTY(QString previewAttachmentId READ previewAttachmentId NOTIFY previewChanged)

public:
    explicit DqeRepositoryController(std::unique_ptr<DqeRepositoryTransport> transport,
                                     QObject *parent = nullptr);
    ~DqeRepositoryController() override;

    QVariantList sources() const;
    QString repositoryState() const;
    QString stateDetail() const;
    QString selectedSourceId() const;
    QString selectedItemId() const;
    QString selectedAttachmentId() const;
    QString previewState() const;
    QString previewKind() const;
    QString previewText() const;
    QString previewImageUrl() const;
    QString previewAttachmentId() const;
    // Compare 聚合器的 safe snapshot API；不 expose private refs，generation 僅供 C++ cache invalidation。
    quint64 sourceGeneration() const;
    QVariantList compareSourceSnapshots() const;

    Q_INVOKABLE QVariantList itemsForSource(const QString &sourceId) const;
    Q_INVOKABLE QVariantList attachmentsForItem(const QString &itemId) const;
    Q_INVOKABLE QString stateForRequestDomain(const QString &domainName) const;

    Q_INVOKABLE RequestToken requestSources();
    Q_INVOKABLE RequestToken requestItems(const QString &sourceId);
    // 成功儲存後只由 operation controller 更新；供下一次 Temporary list/load 重算 expiry。
    void setTemporaryRetentionDays(int retentionDays);
    // 只能對目前 selected opaque attachment 明確要求 memory-only preview。
    Q_INVOKABLE RequestToken requestPreviewState(const QString &attachmentId);
    Q_INVOKABLE void clearPreview();

    Q_INVOKABLE void setSelectedSource(const QString &sourceId);
    Q_INVOKABLE void setSelectedItem(const QString &sourceId, const QString &itemId);
    Q_INVOKABLE void setSelectedAttachment(const QString &attachmentId);

signals:
    void sourcesChanged();
    void itemsChanged(const QString &sourceId);
    void attachmentsChanged(const QString &itemId);
    void repositoryStateChanged();
    void selectedSourceChanged();
    void selectedItemChanged();
    void selectedAttachmentChanged();
    void previewChanged();
    void requestStarted(quint64 token, const QString &domainName);
    void requestFinished(quint64 token, const QString &state, const QString &detail);

private:
    RequestToken beginRequest(RequestDomain domain, const QString &privateTargetRef = {});
    void completeRequest(RequestDomain domain, RequestToken token, TransportReply reply);
    void applySourceSnapshot(const QList<BackendSourceRecord> &sourceRecords,
                             bool preserveCurrentGroupRequest = false);
    void applyGroupSnapshot(const QList<BackendSourceRecord> &sourceRecords);
    void invalidateDomainRequest(RequestDomain domain, RepositoryState state = RepositoryState::Idle);
    void validateSelections();
    void applyAttachmentPreview(const QJsonObject &response);
    void clearPreviewPayload(bool invalidateRequest);
    void invalidatePreviewRequest();
    void clearSelectedSource();
    void clearSelectedItem();
    void clearSelectedAttachment();
    void setDomainState(RequestDomain domain, RepositoryState state);
    RepositoryState domainState(RequestDomain domain) const;
    static int domainKey(RequestDomain domain);
    static QString safeStateDetail(RepositoryState state);

    std::unique_ptr<DqeRepositoryTransport> m_transport;
    OpaqueIdRegistry m_ids;
    RequestToken m_nextToken = 0;
    QHash<int, RequestToken> m_currentTokens;
    QHash<int, RepositoryState> m_domainStates;
    QVariantList m_sources;
    QHash<QString, QVariantList> m_itemsBySource;
    QHash<QString, QVariantList> m_attachmentsByItem;
    QHash<QString, QString> m_sourcePrivateRefs;
    QHash<QString, QString> m_itemSourceIds;
    QHash<QString, QString> m_attachmentItemIds;
    QHash<QString, QString> m_attachmentIntegrityStates;
    QList<BackendSourceRecord> m_backendSources;
    QString m_selectedSourceId;
    QString m_selectedItemId;
    QString m_selectedAttachmentId;
    QString m_previewState = QStringLiteral("idle");
    QString m_previewKind = QStringLiteral("metadata_only");
    QString m_previewText;
    QString m_previewImageToken;
    QString m_previewAttachmentId;
    int m_temporaryRetentionDays = 0;
    quint64 m_sourceGeneration = 0;
};

} // namespace terminux::dqe::production
