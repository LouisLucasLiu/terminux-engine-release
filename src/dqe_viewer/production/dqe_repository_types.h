// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// QML repository 的後端中立型別。只定義 private transport 與 UI-safe DTO 的邊界。

#pragma once

#include <QList>
#include <QJsonObject>
#include <QString>
#include <QVariant>

#include <functional>

namespace terminux::dqe::production {

using RequestToken = quint64;

enum class RepositoryState {
    Idle,
    Loading,
    Ready,
    Empty,
    Unavailable,
    Error,
    Partial,
};

enum class RequestDomain {
    SourceList,
    GroupLoad,
    AttachmentPreview,
    Checkin,
    RetentionLoad,
    RetentionSave,
    PurgePreview,
    PurgeCommit,
};

QString repositoryStateName(RepositoryState state);
QString requestDomainName(RequestDomain domain);

// 以下三種 record 僅能存在於 production transport/controller；privateRef 絕不可送到 QML。
struct BackendAttachmentRecord {
    QString privateRef;
    QString displayName;
    QString role;
    QString state;
    QString mimeHint;
    QVariant sizeBytes;
    QString previewAvailability;
    QString integrityState;
};

struct BackendItemRecord {
    QString privateRef;
    QString dqeId;
    QString title;
    QVariant rawValue;
    QString unit;
    QString status;
    QString statusRaw;
    QString description;
    QString reference;
    QString dateTime;
    QString block;
    QString flow;
    QList<BackendAttachmentRecord> attachments;
};

struct BackendSourceRecord {
    QString privateRef;
    QString sourceKind;
    QString displayName;
    QString jobOrVersion;
    QString block;
    QString flow;
    QString dateTime;
    QString availability;
    QString lifecycleState;
    QVariant retentionDays;
    QString expiresAt;
    bool isExpired = false;
    // Temporary group 的 backend capability；只有明確 true 才能啟用 operation。
    bool canCheckin = false;
    QList<BackendItemRecord> items;
};

struct TransportRequest {
    RequestToken token = 0;
    RequestDomain domain = RequestDomain::SourceList;
    // 僅供 transport 使用的 private backend capability，QML 不可見。
    QString privateTargetRef;
    // operation request；只有 transport/worker 可以讀取，QML 無法取得。
    QJsonObject operationParameters;
    // Temporary list/load 重新計算 expiry 的 private policy context，QML 無法取得 request。
    int temporaryRetentionDays = 0;
};

struct TransportReply {
    RepositoryState state = RepositoryState::Error;
    QList<BackendSourceRecord> sources;
    // 僅 worker→controller 的 private JSON；絕不可直接送到 QML。
    QJsonObject attachmentResponse;
    // 僅 worker→operation controller 的 private JSON response。
    QJsonObject operationResponse;
    // 僅 worker→operation controller 的 private retention JSON response。
    QJsonObject retentionResponse;
    // 僅 worker→operation controller 的 private purge JSON response；token 不可進 QML。
    QJsonObject purgeResponse;
};

struct ArchivedLaunchContext {
    QString ipcEndpoint;
    QString archivedVersionDir;
    QString workspaceDisplayContext;
    QString versionDisplayContext;
};

struct TemporaryLaunchContext {
    QString ipcEndpoint;
    QString temporaryDbPath;
    int retentionDays = 14;
    QString jobKeyToOpen;
};

using TransportCompletion = std::function<void(TransportReply)>;

} // namespace terminux::dqe::production

Q_DECLARE_METATYPE(terminux::dqe::production::TransportReply)
