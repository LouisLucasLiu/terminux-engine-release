// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// check-in service response 的 allow-list mapper；不讓 backend path/message 離開 production layer。

#pragma once

#include <QJsonObject>
#include <QVariantList>

namespace terminux::dqe::production {

struct CheckinOperationResult {
    QString state;
    QString lastResult;
    QString failureCode;
    QString message;
    bool dryRun = false;
    bool partial = false;
    QVariantList files;
    QVariantList conflicts;
    QStringList warnings;
};

class CheckinDtoMapper
{
public:
    static CheckinOperationResult mapResponse(const QJsonObject &response);
};

} // 命名空間 terminux::dqe::production
