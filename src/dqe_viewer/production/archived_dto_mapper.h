// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Archived JSON-RPC response 到 production-private record 的 allow-list mapper。

#pragma once

#include "dqe_viewer/production/dqe_repository_types.h"

#include <QJsonObject>

namespace terminux::dqe::production {

class ArchivedDtoMapper
{
public:
    static TransportReply mapListResult(const QJsonObject &result);
    static TransportReply mapGroupResult(const QJsonObject &result);
};

} // namespace terminux::dqe::production
