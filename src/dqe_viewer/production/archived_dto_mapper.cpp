// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 所有 sensitive JSON path/ref 僅用於 privateRef，絕不寫入 UI DTO 欄位。

#include "dqe_viewer/production/archived_dto_mapper.h"

#include <QJsonArray>
#include <QJsonValue>

namespace terminux::dqe::production {
namespace {

QString text(const QJsonObject &object, const char *key)
{
    const QJsonValue value = object.value(QLatin1String(key));
    return value.isString() ? value.toString() : QString();
}

QString nestedText(const QJsonObject &object, const char *outer, const char *inner)
{
    return text(object.value(QLatin1String(outer)).toObject(), inner);
}

QString contextValue(const QJsonObject &group, const char *name)
{
    return nestedText(group.value(QStringLiteral("context")).toObject(), "primary_fields", name);
}

QString contextDisplayValue(const QJsonObject &group, const char *name)
{
    const QJsonObject fields = group.value(QStringLiteral("context")).toObject()
                                  .value(QStringLiteral("primary_fields")).toObject();
    return text(fields.value(QLatin1String(name)).toObject(), "display_value");
}

QString groupRef(const QJsonObject &group)
{
    return group.value(QStringLiteral("domain_extension")).toObject()
        .value(QStringLiteral("dqe_capabilities")).toObject()
        .value(QStringLiteral("group_ref")).toString();
}

BackendAttachmentRecord mapAttachment(const QJsonObject &attachment, const QString &fallbackRef)
{
    BackendAttachmentRecord mapped;
    const QJsonObject location = attachment.value(QStringLiteral("location")).toObject();
    mapped.privateRef = location.value(QStringLiteral("uri")).toString();
    if (mapped.privateRef.isEmpty()) {
        mapped.privateRef = fallbackRef;
    }
    mapped.displayName = text(attachment, "label");
    mapped.role = text(attachment, "attachment_role");
    mapped.state = text(attachment, "state");
    mapped.mimeHint = text(attachment, "mime_hint");
    mapped.sizeBytes = attachment.value(QStringLiteral("size_hint")).toVariant();
    mapped.previewAvailability = text(attachment, "preview_capability");
    mapped.integrityState = mapped.state == QLatin1String("error") ? QStringLiteral("incomplete")
                                                                        : QStringLiteral("complete");
    return mapped;
}

BackendSourceRecord mapGroup(const QJsonObject &group)
{
    BackendSourceRecord source;
    source.privateRef = groupRef(group);
    source.sourceKind = QStringLiteral("archived");
    source.displayName = nestedText(group, "identity", "display_name");
    source.jobOrVersion = contextDisplayValue(group, "version");
    source.block = contextDisplayValue(group, "block");
    source.flow = contextDisplayValue(group, "flow");
    source.dateTime = nestedText(group, "identity", "primary_time");
    source.availability = QStringLiteral("available");
    source.lifecycleState = nestedText(group, "domain_extension", "dqe_lifecycle_state");

    const QJsonArray items = group.value(QStringLiteral("items")).toArray();
    int itemIndex = 0;
    for (const QJsonValue &value : items) {
        const QJsonObject item = value.toObject();
        const QString backendItemId = text(item, "item_id");
        if (backendItemId.isEmpty() || source.privateRef.isEmpty()) {
            continue;
        }
        BackendItemRecord mapped;
        mapped.privateRef = source.privateRef + QLatin1Char('\x1f') + backendItemId;
        mapped.dqeId = item.value(QStringLiteral("domain_extension")).toObject()
                           .value(QStringLiteral("dqe_id")).toString(backendItemId);
        mapped.title = text(item, "title");
        mapped.rawValue = item.value(QStringLiteral("primary_value")).toVariant();
        mapped.unit = text(item, "unit");
        mapped.status = text(item, "status");
        mapped.statusRaw = text(item, "status_raw");
        mapped.description = text(item, "description");
        mapped.reference = text(item, "reference");
        mapped.dateTime = text(item, "updated_at");
        mapped.block = source.block;
        mapped.flow = source.flow;
        int attachmentIndex = 0;
        for (const QJsonValue &attachmentValue : item.value(QStringLiteral("attachments")).toArray()) {
            mapped.attachments.append(mapAttachment(
                attachmentValue.toObject(),
                mapped.privateRef + QStringLiteral("/attachment-%1").arg(attachmentIndex++)));
        }
        source.items.append(mapped);
        ++itemIndex;
    }
    return source;
}

bool hasIssueCode(const QJsonArray &issues, const QString &code)
{
    for (const QJsonValue &value : issues) {
        if (text(value.toObject(), "code") == code) {
            return true;
        }
    }
    return false;
}

TransportReply mapGroups(const QJsonArray &groups, const QJsonArray &issues)
{
    TransportReply reply;
    for (const QJsonValue &value : groups) {
        BackendSourceRecord source = mapGroup(value.toObject());
        if (!source.privateRef.isEmpty()) {
            reply.sources.append(source);
        }
    }
    if (reply.sources.isEmpty()) {
        reply.state = hasIssueCode(issues, QStringLiteral("source_empty")) || issues.isEmpty()
                        ? RepositoryState::Empty : RepositoryState::Error;
    } else {
        reply.state = issues.isEmpty() ? RepositoryState::Ready : RepositoryState::Partial;
    }
    return reply;
}

} // namespace

TransportReply ArchivedDtoMapper::mapListResult(const QJsonObject &result)
{
    if (!result.value(QStringLiteral("groups")).isArray() || !result.value(QStringLiteral("issues")).isArray()) {
        return {RepositoryState::Error, {}};
    }
    return mapGroups(result.value(QStringLiteral("groups")).toArray(),
                     result.value(QStringLiteral("issues")).toArray());
}

TransportReply ArchivedDtoMapper::mapGroupResult(const QJsonObject &result)
{
    if (!result.value(QStringLiteral("group")).isObject() || !result.value(QStringLiteral("issues")).isArray()) {
        return {RepositoryState::Error, {}};
    }
    return mapGroups({result.value(QStringLiteral("group"))},
                     result.value(QStringLiteral("issues")).toArray());
}

} // namespace terminux::dqe::production
