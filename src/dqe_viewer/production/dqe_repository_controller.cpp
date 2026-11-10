// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 所有 DTO 都逐欄建立；private ref 不會進入 QVariantMap 或 log。

#include "dqe_viewer/production/dqe_repository_controller.h"

#include "dqe_viewer/production/attachment_dto_mapper.h"
#include "dqe_viewer/production/dqe_preview_image_provider.h"

#include <QPointer>
#include <QSet>

#include <utility>

namespace terminux::dqe::production {
namespace {

QVariantMap sourceDto(const BackendSourceRecord &source, const QString &sourceId)
{
    return {
        {QStringLiteral("sourceId"), sourceId},
        {QStringLiteral("sourceKind"), source.sourceKind},
        {QStringLiteral("displayName"), source.displayName},
        {QStringLiteral("jobOrVersion"), source.jobOrVersion},
        {QStringLiteral("block"), source.block},
        {QStringLiteral("flow"), source.flow},
        {QStringLiteral("dateTime"), source.dateTime},
        {QStringLiteral("availability"), source.availability},
        {QStringLiteral("lifecycleState"), source.lifecycleState},
        {QStringLiteral("retentionDays"), source.retentionDays},
        {QStringLiteral("expiresAt"), source.expiresAt},
        {QStringLiteral("isExpired"), source.isExpired},
        {QStringLiteral("canCheckin"), source.canCheckin},
    };
}

QVariantMap attachmentDto(const BackendAttachmentRecord &attachment, const QString &attachmentId)
{
    return {
        {QStringLiteral("attachmentId"), attachmentId},
        {QStringLiteral("displayName"), attachment.displayName},
        {QStringLiteral("role"), attachment.role},
        {QStringLiteral("state"), attachment.state},
        {QStringLiteral("mimeHint"), attachment.mimeHint},
        {QStringLiteral("sizeBytes"), attachment.sizeBytes},
        {QStringLiteral("previewAvailability"), attachment.previewAvailability},
        {QStringLiteral("integrityState"), attachment.integrityState},
    };
}

QVariantMap itemDto(const BackendItemRecord &item, const QString &itemId,
                    const QVariantList &attachmentIds)
{
    return {
        {QStringLiteral("itemId"), itemId},
        {QStringLiteral("dqeId"), item.dqeId},
        {QStringLiteral("title"), item.title},
        {QStringLiteral("rawValue"), item.rawValue},
        {QStringLiteral("unit"), item.unit},
        {QStringLiteral("status"), item.status},
        {QStringLiteral("statusRaw"), item.statusRaw},
        {QStringLiteral("description"), item.description},
        {QStringLiteral("reference"), item.reference},
        {QStringLiteral("dateTime"), item.dateTime},
        {QStringLiteral("block"), item.block},
        {QStringLiteral("flow"), item.flow},
        {QStringLiteral("attachmentIds"), attachmentIds},
    };
}

} // namespace

QString repositoryStateName(RepositoryState state)
{
    switch (state) {
    case RepositoryState::Idle:
        return QStringLiteral("idle");
    case RepositoryState::Loading:
        return QStringLiteral("loading");
    case RepositoryState::Ready:
        return QStringLiteral("ready");
    case RepositoryState::Empty:
        return QStringLiteral("empty");
    case RepositoryState::Unavailable:
        return QStringLiteral("unavailable");
    case RepositoryState::Error:
        return QStringLiteral("error");
    case RepositoryState::Partial:
        return QStringLiteral("partial");
    }
    return QStringLiteral("error");
}

QString requestDomainName(RequestDomain domain)
{
    switch (domain) {
    case RequestDomain::SourceList:
        return QStringLiteral("list");
    case RequestDomain::GroupLoad:
        return QStringLiteral("load");
    case RequestDomain::AttachmentPreview:
        return QStringLiteral("preview");
    case RequestDomain::Checkin:
        return QStringLiteral("checkin");
    case RequestDomain::RetentionLoad:
        return QStringLiteral("retention_load");
    case RequestDomain::RetentionSave:
        return QStringLiteral("retention_save");
    case RequestDomain::PurgePreview:
        return QStringLiteral("purge_preview");
    case RequestDomain::PurgeCommit:
        return QStringLiteral("purge_commit");
    }
    return QStringLiteral("list");
}

DqeRepositoryController::DqeRepositoryController(std::unique_ptr<DqeRepositoryTransport> transport,
                                                   QObject *parent)
    : QObject(parent)
    , m_transport(std::move(transport))
{
    for (const RequestDomain domain : {RequestDomain::SourceList, RequestDomain::GroupLoad,
                                       RequestDomain::AttachmentPreview, RequestDomain::Checkin}) {
        m_domainStates.insert(domainKey(domain), RepositoryState::Idle);
        m_currentTokens.insert(domainKey(domain), 0);
    }
}

DqeRepositoryController::~DqeRepositoryController()
{
    clearPreviewPayload(false);
    if (!m_transport) {
        return;
    }
    for (const RequestToken token : std::as_const(m_currentTokens)) {
        if (token != 0) {
            m_transport->cancelQueued(token);
        }
    }
}

QVariantList DqeRepositoryController::sources() const
{
    return m_sources;
}

QString DqeRepositoryController::repositoryState() const
{
    return repositoryStateName(domainState(RequestDomain::SourceList));
}

QString DqeRepositoryController::stateDetail() const
{
    return safeStateDetail(domainState(RequestDomain::SourceList));
}

QString DqeRepositoryController::selectedSourceId() const
{
    return m_selectedSourceId;
}

QString DqeRepositoryController::selectedItemId() const
{
    return m_selectedItemId;
}

QString DqeRepositoryController::selectedAttachmentId() const
{
    return m_selectedAttachmentId;
}

QString DqeRepositoryController::previewState() const
{
    return m_previewState;
}

QString DqeRepositoryController::previewKind() const
{
    return m_previewKind;
}

QString DqeRepositoryController::previewText() const
{
    return m_previewText;
}

QString DqeRepositoryController::previewImageUrl() const
{
    return m_previewImageToken.isEmpty()
             ? QString()
             : QStringLiteral("image://dqe-preview/%1").arg(m_previewImageToken);
}

QString DqeRepositoryController::previewAttachmentId() const
{
    return m_previewAttachmentId;
}

quint64 DqeRepositoryController::sourceGeneration() const
{
    return m_sourceGeneration;
}

QVariantList DqeRepositoryController::compareSourceSnapshots() const
{
    QVariantList snapshots;
    for (const QVariant &sourceValue : m_sources) {
        QVariantMap source = sourceValue.toMap();
        const QString sourceId = source.value(QStringLiteral("sourceId")).toString();
        QVariantList items;
        for (const QVariant &itemValue : m_itemsBySource.value(sourceId)) {
            QVariantMap item = itemValue.toMap();
            item.insert(QStringLiteral("attachments"),
                        m_attachmentsByItem.value(item.value(QStringLiteral("itemId")).toString()));
            items.append(item);
        }
        source.insert(QStringLiteral("items"), items);
        snapshots.append(source);
    }
    return snapshots;
}

QVariantList DqeRepositoryController::itemsForSource(const QString &sourceId) const
{
    return m_itemsBySource.value(sourceId);
}

QVariantList DqeRepositoryController::attachmentsForItem(const QString &itemId) const
{
    return m_attachmentsByItem.value(itemId);
}

QString DqeRepositoryController::stateForRequestDomain(const QString &domainName) const
{
    if (domainName == QLatin1String("load")) {
        return repositoryStateName(domainState(RequestDomain::GroupLoad));
    }
    if (domainName == QLatin1String("preview")) {
        return repositoryStateName(domainState(RequestDomain::AttachmentPreview));
    }
    return repositoryStateName(domainState(RequestDomain::SourceList));
}

RequestToken DqeRepositoryController::requestSources()
{
    return beginRequest(RequestDomain::SourceList);
}

RequestToken DqeRepositoryController::requestItems(const QString &sourceId)
{
    const QString privateRef = m_sourcePrivateRefs.value(sourceId);
    return privateRef.isEmpty() ? 0 : beginRequest(RequestDomain::GroupLoad, privateRef);
}

void DqeRepositoryController::setTemporaryRetentionDays(int retentionDays)
{
    if (retentionDays >= 1 && retentionDays <= 3650) {
        m_temporaryRetentionDays = retentionDays;
    }
}

RequestToken DqeRepositoryController::requestPreviewState(const QString &attachmentId)
{
    const QString privateRef = m_ids.privateRefFor(attachmentId);
    // opaque ID 本身不足以讀取；必須是目前 explicit-selected attachment。
    if (privateRef.isEmpty() || attachmentId != m_selectedAttachmentId) {
        return 0;
    }
    clearPreviewPayload(false);
    m_previewAttachmentId = attachmentId;
    m_previewState = QStringLiteral("loading");
    m_previewKind = QStringLiteral("loading");
    emit previewChanged();
    return beginRequest(RequestDomain::AttachmentPreview, privateRef);
}

void DqeRepositoryController::clearPreview()
{
    clearPreviewPayload(true);
}

void DqeRepositoryController::setSelectedSource(const QString &sourceId)
{
    if (sourceId.isEmpty() || !m_sourcePrivateRefs.contains(sourceId)) {
        invalidateDomainRequest(RequestDomain::GroupLoad);
        clearSelectedSource();
        return;
    }
    if (m_selectedSourceId == sourceId) {
        return;
    }
    // 切換 source 後，舊 group load 不得覆寫新來源的 rows／selection。
    invalidateDomainRequest(RequestDomain::GroupLoad);
    m_selectedSourceId = sourceId;
    emit selectedSourceChanged();
    clearSelectedItem();
}

void DqeRepositoryController::setSelectedItem(const QString &sourceId, const QString &itemId)
{
    if (sourceId.isEmpty() || itemId.isEmpty() || m_itemSourceIds.value(itemId) != sourceId) {
        clearSelectedItem();
        return;
    }
    if (m_selectedSourceId != sourceId) {
        // setSelectedItem 亦可隱式切換 source，必須套用相同 stale-discard 規則。
        invalidateDomainRequest(RequestDomain::GroupLoad);
        m_selectedSourceId = sourceId;
        emit selectedSourceChanged();
    }
    if (m_selectedItemId == itemId) {
        return;
    }
    m_selectedItemId = itemId;
    emit selectedItemChanged();
    clearSelectedAttachment();
}

void DqeRepositoryController::setSelectedAttachment(const QString &attachmentId)
{
    if (attachmentId.isEmpty() || m_attachmentItemIds.value(attachmentId) != m_selectedItemId) {
        clearSelectedAttachment();
        return;
    }
    if (m_selectedAttachmentId == attachmentId) {
        return;
    }
    clearPreviewPayload(true);
    m_selectedAttachmentId = attachmentId;
    emit selectedAttachmentChanged();
}

RequestToken DqeRepositoryController::beginRequest(RequestDomain domain, const QString &privateTargetRef)
{
    if (!m_transport) {
        return 0;
    }
    const int key = domainKey(domain);
    const RequestToken previousToken = m_currentTokens.value(key);
    if (previousToken != 0) {
        m_transport->cancelQueued(previousToken);
    }

    const RequestToken token = ++m_nextToken;
    m_currentTokens.insert(key, token);
    setDomainState(domain, RepositoryState::Loading);
    emit requestStarted(token, requestDomainName(domain));

    const QPointer<DqeRepositoryController> self(this);
    TransportRequest request;
    request.token = token;
    request.domain = domain;
    request.privateTargetRef = privateTargetRef;
    if (domain == RequestDomain::SourceList || domain == RequestDomain::GroupLoad) {
        request.temporaryRetentionDays = m_temporaryRetentionDays;
    }
    m_transport->request(request, [self, domain, token](TransportReply reply) {
        if (self) {
            self->completeRequest(domain, token, std::move(reply));
        }
    });
    return token;
}

void DqeRepositoryController::completeRequest(RequestDomain domain, RequestToken token,
                                              TransportReply reply)
{
    if (m_currentTokens.value(domainKey(domain)) != token) {
        return;
    }

    if (domain == RequestDomain::SourceList
        && (reply.state == RepositoryState::Ready || reply.state == RepositoryState::Empty
            || reply.state == RepositoryState::Partial)) {
        applySourceSnapshot(reply.sources);
    } else if (domain == RequestDomain::GroupLoad
               && (reply.state == RepositoryState::Ready || reply.state == RepositoryState::Partial)) {
        applyGroupSnapshot(reply.sources);
    } else if (domain == RequestDomain::AttachmentPreview) {
        if (reply.state == RepositoryState::Ready
            && m_previewAttachmentId == m_selectedAttachmentId
            && !m_previewAttachmentId.isEmpty()) {
            applyAttachmentPreview(reply.attachmentResponse);
        } else if (reply.state == RepositoryState::Error) {
            clearPreviewPayload(false);
            m_previewAttachmentId = m_selectedAttachmentId;
            m_previewState = QStringLiteral("error");
            m_previewKind = QStringLiteral("error");
            emit previewChanged();
        }
    }
    setDomainState(domain, reply.state);
    emit requestFinished(token, repositoryStateName(reply.state), safeStateDetail(reply.state));
}

void DqeRepositoryController::applySourceSnapshot(const QList<BackendSourceRecord> &sourceRecords,
                                                   bool preserveCurrentGroupRequest)
{
    // 新 list snapshot 代表新的 source identity generation。舊 group reply 不可把已刪 source
    // 或舊 rows 混入；attachment preview 也不可在 refresh 後復原舊 Evidence payload。
    if (!preserveCurrentGroupRequest) {
        invalidateDomainRequest(RequestDomain::GroupLoad);
    }
    clearPreviewPayload(true);

    QVariantList mappedSources;
    QHash<QString, QVariantList> mappedItems;
    QHash<QString, QVariantList> mappedAttachments;
    QHash<QString, QString> sourcePrivateRefs;
    QHash<QString, QString> itemSourceIds;
    QHash<QString, QString> attachmentItemIds;
    QHash<QString, QString> attachmentIntegrityStates;
    QSet<QString> sourceRefs;
    QSet<QString> itemRefs;
    QSet<QString> attachmentRefs;

    for (const BackendSourceRecord &source : sourceRecords) {
        sourceRefs.insert(source.privateRef);
        const QString sourceId = m_ids.idFor(OpaqueIdRegistry::Kind::Source, source.privateRef);
        sourcePrivateRefs.insert(sourceId, source.privateRef);
        mappedSources.append(sourceDto(source, sourceId));

        QVariantList items;
        for (const BackendItemRecord &item : source.items) {
            itemRefs.insert(item.privateRef);
            const QString itemId = m_ids.idFor(OpaqueIdRegistry::Kind::Item, item.privateRef);
            itemSourceIds.insert(itemId, sourceId);

            QVariantList attachmentIds;
            QVariantList attachments;
            for (const BackendAttachmentRecord &attachment : item.attachments) {
                attachmentRefs.insert(attachment.privateRef);
                const QString attachmentId = m_ids.idFor(OpaqueIdRegistry::Kind::Attachment,
                                                         attachment.privateRef);
                attachmentIds.append(attachmentId);
                attachments.append(attachmentDto(attachment, attachmentId));
                attachmentItemIds.insert(attachmentId, itemId);
                attachmentIntegrityStates.insert(attachmentId, attachment.integrityState);
            }
            mappedAttachments.insert(itemId, attachments);
            items.append(itemDto(item, itemId, attachmentIds));
        }
        mappedItems.insert(sourceId, items);
    }

    // 先完成新 snapshot mapping，再讓被移除 ref 的 opaque ID 失效。
    m_ids.retainOnly(OpaqueIdRegistry::Kind::Source, sourceRefs);
    m_ids.retainOnly(OpaqueIdRegistry::Kind::Item, itemRefs);
    m_ids.retainOnly(OpaqueIdRegistry::Kind::Attachment, attachmentRefs);

    m_backendSources = sourceRecords;
    m_sources = mappedSources;
    m_itemsBySource = mappedItems;
    m_attachmentsByItem = mappedAttachments;
    m_sourcePrivateRefs = sourcePrivateRefs;
    m_itemSourceIds = itemSourceIds;
    m_attachmentItemIds = attachmentItemIds;
    m_attachmentIntegrityStates = attachmentIntegrityStates;
    ++m_sourceGeneration;
    validateSelections();
    emit sourcesChanged();
}

void DqeRepositoryController::applyGroupSnapshot(const QList<BackendSourceRecord> &sourceRecords)
{
    QList<BackendSourceRecord> updated = m_backendSources;
    for (const BackendSourceRecord &incoming : sourceRecords) {
        bool replaced = false;
        for (BackendSourceRecord &existing : updated) {
            if (existing.privateRef == incoming.privateRef) {
                BackendSourceRecord merged = incoming;
                // Temporary load_group 不帶 caller 的 retention context；必須沿用 list 時已
                // 驗證的 context，不能因 reload 改回 service 預設值或改變 expiry 顯示。
                if (existing.sourceKind == QLatin1String("temporary")
                    && merged.sourceKind == QLatin1String("temporary")) {
                    merged.retentionDays = existing.retentionDays;
                    merged.expiresAt = existing.expiresAt;
                    merged.isExpired = existing.isExpired;
                    merged.lifecycleState = existing.lifecycleState;
                }
                existing = std::move(merged);
                replaced = true;
                break;
            }
        }
        if (!replaced) {
            updated.append(incoming);
        }
    }
    // 這是 current group request 自己的 accepted reply；不可在套用時讓它把自己視為 stale。
    applySourceSnapshot(updated, true);
}

void DqeRepositoryController::invalidateDomainRequest(RequestDomain domain, RepositoryState state)
{
    if (!m_transport) {
        return;
    }
    const int key = domainKey(domain);
    const RequestToken previousToken = m_currentTokens.value(key);
    if (previousToken != 0) {
        m_transport->cancelQueued(previousToken);
    }
    // 每個 domain 自行前進 generation；不影響其他 domain 的 current request。
    m_currentTokens.insert(key, ++m_nextToken);
    setDomainState(domain, state);
}

void DqeRepositoryController::validateSelections()
{
    if (!m_selectedSourceId.isEmpty() && !m_sourcePrivateRefs.contains(m_selectedSourceId)) {
        clearSelectedSource();
        return;
    }
    if (!m_selectedItemId.isEmpty()
        && m_itemSourceIds.value(m_selectedItemId) != m_selectedSourceId) {
        clearSelectedItem();
        return;
    }
    if (!m_selectedAttachmentId.isEmpty()
        && m_attachmentItemIds.value(m_selectedAttachmentId) != m_selectedItemId) {
        clearSelectedAttachment();
    }
}

void DqeRepositoryController::applyAttachmentPreview(const QJsonObject &response)
{
    const AttachmentPreviewResult mapped = AttachmentDtoMapper::mapLoadResponse(
        response, m_attachmentIntegrityStates.value(m_previewAttachmentId));
    clearPreviewPayload(false);
    m_previewAttachmentId = m_selectedAttachmentId;
    m_previewState = mapped.state;
    m_previewKind = mapped.previewKind;
    m_previewText = mapped.text;
    if (!mapped.image.isNull()) {
        m_previewImageToken = DqePreviewImageStore::instance().insert(mapped.image);
        if (m_previewImageToken.isEmpty()) {
            m_previewState = QStringLiteral("error");
            m_previewKind = QStringLiteral("error");
        }
    }
    emit previewChanged();
}

void DqeRepositoryController::clearPreviewPayload(bool invalidateRequest)
{
    if (!m_previewImageToken.isEmpty()) {
        DqePreviewImageStore::instance().remove(m_previewImageToken);
    }
    const bool changed = m_previewState != QLatin1String("idle")
                         || m_previewKind != QLatin1String("metadata_only")
                         || !m_previewText.isEmpty() || !m_previewImageToken.isEmpty()
                         || !m_previewAttachmentId.isEmpty();
    m_previewState = QStringLiteral("idle");
    m_previewKind = QStringLiteral("metadata_only");
    m_previewText.clear();
    m_previewImageToken.clear();
    m_previewAttachmentId.clear();
    if (invalidateRequest) {
        invalidatePreviewRequest();
    }
    if (changed) {
        emit previewChanged();
    }
}

void DqeRepositoryController::invalidatePreviewRequest()
{
    invalidateDomainRequest(RequestDomain::AttachmentPreview);
}

void DqeRepositoryController::clearSelectedSource()
{
    const bool sourceChanged = !m_selectedSourceId.isEmpty();
    m_selectedSourceId.clear();
    if (sourceChanged) {
        emit selectedSourceChanged();
    }
    clearSelectedItem();
}

void DqeRepositoryController::clearSelectedItem()
{
    const bool itemChanged = !m_selectedItemId.isEmpty();
    m_selectedItemId.clear();
    if (itemChanged) {
        emit selectedItemChanged();
    }
    clearSelectedAttachment();
}

void DqeRepositoryController::clearSelectedAttachment()
{
    if (m_selectedAttachmentId.isEmpty()) {
        return;
    }
    clearPreviewPayload(true);
    m_selectedAttachmentId.clear();
    emit selectedAttachmentChanged();
}

void DqeRepositoryController::setDomainState(RequestDomain domain, RepositoryState state)
{
    m_domainStates.insert(domainKey(domain), state);
    if (domain == RequestDomain::SourceList) {
        emit repositoryStateChanged();
    }
}

RepositoryState DqeRepositoryController::domainState(RequestDomain domain) const
{
    return m_domainStates.value(domainKey(domain), RepositoryState::Idle);
}

int DqeRepositoryController::domainKey(RequestDomain domain)
{
    return static_cast<int>(domain);
}

QString DqeRepositoryController::safeStateDetail(RepositoryState state)
{
    switch (state) {
    case RepositoryState::Idle:
        return {};
    case RepositoryState::Loading:
        return QStringLiteral("Loading repository data.");
    case RepositoryState::Ready:
        return QStringLiteral("Repository data is ready.");
    case RepositoryState::Empty:
        return QStringLiteral("No repository data is available.");
    case RepositoryState::Unavailable:
        return QStringLiteral("The repository source is unavailable.");
    case RepositoryState::Error:
        return QStringLiteral("The repository request failed.");
    case RepositoryState::Partial:
        return QStringLiteral("Some repository records have issues.");
    }
    return QStringLiteral("The repository request failed.");
}

} // namespace terminux::dqe::production
