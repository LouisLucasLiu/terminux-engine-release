// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// attachment payload response 只在 controller 內轉為 safe text/QImage。

#pragma once

#include <QImage>
#include <QJsonObject>
#include <QString>

namespace terminux::dqe::production {

struct AttachmentPreviewResult {
    QString state = QStringLiteral("error");
    QString previewKind = QStringLiteral("error");
    QString text;
    QImage image;
};

class AttachmentDtoMapper final
{
public:
    static AttachmentPreviewResult mapLoadResponse(const QJsonObject &response,
                                                   const QString &integrityState);
};

} // namespace terminux::dqe::production
