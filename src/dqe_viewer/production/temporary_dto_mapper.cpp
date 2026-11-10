// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 只 allow-list Temporary JSON 欄位；DB path、record id 與 adapter ref 僅作 privateRef。

#include "dqe_viewer/production/temporary_dto_mapper.h"

#include <QJsonArray>
#include <QJsonValue>

#include <algorithm>

namespace terminux::dqe::production {
namespace {

QString text(const QJsonObject &object, const char *key)
{
    const QJsonValue value = object.value(QLatin1String(key));
    return value.isString() ? value.toString() : QString();
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

QString canonicalStatus(const QString &status)
{
    const QString normalized = status.trimmed().toLower();
    if (normalized == QLatin1String("pass") || normalized == QLatin1String("passed")) {
        return QStringLiteral("pass");
    }
    if (normalized == QLatin1String("warn") || normalized == QLatin1String("warning")) {
        return QStringLiteral("warn");
    }
    if (normalized == QLatin1String("fail") || normalized == QLatin1String("failed")) {
        return QStringLiteral("fail");
    }
    // not_applicable 不擴充 UI status；維持 raw status，canonical 顯示 Unknown。
    return QStringLiteral("unknown");
}

BackendAttachmentRecord mapAttachment(const QJsonObject &attachment, const QString &fallbackRef)
{
    BackendAttachmentRecord mapped;
    const QJsonObject location = attachment.value(QStringLiteral("location")).toObject();
    mapped.privateRef = location.value(QStringLiteral("db_ref")).toString();
    if (mapped.privateRef.isEmpty()) {
        mapped.privateRef = location.value(QStringLiteral("uri")).toString();
    }
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

BackendSourceRecord mapGroup(const QJsonObject &group, int fallbackRetentionDays)
{
    BackendSourceRecord source;
    const QJsonObject extension = group.value(QStringLiteral("domain_extension")).toObject();
    const QJsonObject retention = extension.value(QStringLiteral("retention")).toObject();
    source.privateRef = groupRef(group);
    source.sourceKind = QStringLiteral("temporary");
    source.displayName = text(extension, "job_key");
    if (source.displayName.isEmpty()) {
        source.displayName = text(group.value(QStringLiteral("identity")).toObject(), "display_name");
    }
    source.jobOrVersion = source.displayName;
    source.block = contextDisplayValue(group, "block");
    source.flow = contextDisplayValue(group, "flow");
    source.dateTime = text(group.value(QStringLiteral("identity")).toObject(), "primary_time");
    source.availability = QStringLiteral("available");
    source.lifecycleState = text(extension, "dqe_lifecycle_state");
    source.retentionDays = retention.value(QStringLiteral("retention_days")).toVariant();
    if (!source.retentionDays.isValid() || source.retentionDays.isNull()) {
        source.retentionDays = fallbackRetentionDays;
    }
    source.expiresAt = text(retention, "expires_at");
    source.isExpired = retention.value(QStringLiteral("is_expired")).toBool(false)
                       || source.lifecycleState == QLatin1String("expired");
    // capability 不可由 lifecycle 或 job 名稱推斷；缺值一律視為不可 check-in。
    source.canCheckin = extension.value(QStringLiteral("checkin")).toObject()
                            .value(QStringLiteral("can_checkin")).toBool(false);

    for (const QJsonValue &value : group.value(QStringLiteral("items")).toArray()) {
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
        mapped.statusRaw = text(item, "status_raw");
        if (mapped.statusRaw.isEmpty()) {
            mapped.statusRaw = text(item, "status");
        }
        mapped.status = canonicalStatus(text(item, "status"));
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

TransportReply mapGroups(const QJsonArray &groups, const QJsonArray &issues, int retentionDays)
{
    TransportReply reply;
    for (const QJsonValue &value : groups) {
        BackendSourceRecord source = mapGroup(value.toObject(), retentionDays);
        if (!source.privateRef.isEmpty()) {
            reply.sources.append(source);
        }
    }
    std::sort(reply.sources.begin(), reply.sources.end(), [](const BackendSourceRecord &left,
                                                               const BackendSourceRecord &right) {
        if (left.dateTime != right.dateTime) {
            return left.dateTime > right.dateTime;
        }
        return left.jobOrVersion > right.jobOrVersion;
    });
    if (reply.sources.isEmpty()) {
        if (hasIssueCode(issues, QStringLiteral("source_empty")) || issues.isEmpty()) {
            reply.state = RepositoryState::Empty;
        } else if (hasIssueCode(issues, QStringLiteral("source_not_found"))
                   || hasIssueCode(issues, QStringLiteral("source_unavailable"))) {
            reply.state = RepositoryState::Unavailable;
        } else {
            reply.state = RepositoryState::Error;
        }
    } else {
        reply.state = issues.isEmpty() ? RepositoryState::Ready : RepositoryState::Partial;
    }
    return reply;
}

} // namespace

TransportReply TemporaryDtoMapper::mapListResult(const QJsonObject &result, int retentionDays)
{
    if (!result.value(QStringLiteral("groups")).isArray() || !result.value(QStringLiteral("issues")).isArray()) {
        return {RepositoryState::Error, {}};
    }
    return mapGroups(result.value(QStringLiteral("groups")).toArray(),
                     result.value(QStringLiteral("issues")).toArray(), retentionDays);
}

TransportReply TemporaryDtoMapper::mapGroupResult(const QJsonObject &result, int retentionDays)
{
    if (!result.value(QStringLiteral("group")).isObject() || !result.value(QStringLiteral("issues")).isArray()) {
        return {RepositoryState::Error, {}};
    }
    return mapGroups({result.value(QStringLiteral("group"))},
                     result.value(QStringLiteral("issues")).toArray(), retentionDays);
}

} // namespace terminux::dqe::production
