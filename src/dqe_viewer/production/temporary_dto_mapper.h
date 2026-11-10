// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Temporary JSON-RPC response 的 safe DTO mapper；private DB ref 不離開 production layer。

#pragma once

#include "dqe_viewer/production/dqe_repository_types.h"

#include <QJsonObject>

namespace terminux::dqe::production {

class TemporaryDtoMapper
{
public:
    static TransportReply mapListResult(const QJsonObject &result, int retentionDays);
    static TransportReply mapGroupResult(const QJsonObject &result, int retentionDays);
};

} // namespace terminux::dqe::production
