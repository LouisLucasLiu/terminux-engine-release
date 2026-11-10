// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// manual purge response 的 allow-list mapper；confirmation token 與 backend detail 保持 private。

#pragma once

#include <QJsonObject>
#include <QStringList>

namespace terminux::dqe::production {

struct PurgePreviewResult {
    QString state;
    QString failureCode;
    QString message;
    QString privateConfirmationToken;
    int candidateJobCount = 0;
    int candidateRowCount = 0;
    int candidateAttachmentBlobCount = 0;
    QStringList jobLabels;
    QStringList warnings;
};

struct PurgeCommitResult {
    QString state;
    QString failureCode;
    QString message;
    int deletedJobCount = 0;
    int deletedRowCount = 0;
    QStringList warnings;
};

class PurgeDtoMapper
{
public:
    static PurgePreviewResult mapPreviewResponse(const QJsonObject &response, const QString &mode,
                                                 const QString &selectedJobLabel);
    static PurgeCommitResult mapCommitResponse(const QJsonObject &response, const QString &mode);
};

} // 命名空間 terminux::dqe::production
