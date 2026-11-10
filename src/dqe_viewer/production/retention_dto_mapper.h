// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// retention service response 的 allow-list mapper；不讓 policy path 或原始訊息離開 production layer。

#pragma once

#include <QJsonObject>

namespace terminux::dqe::production {

struct RetentionOperationResult {
    QString state;
    QString failureCode;
    QString message;
    int retentionDays = 0;
    int recordsDeleted = 0;
};

class RetentionDtoMapper
{
public:
    static RetentionOperationResult mapLoadResponse(const QJsonObject &response);
    static RetentionOperationResult mapSaveResponse(const QJsonObject &response);
};

} // 命名空間 terminux::dqe::production
