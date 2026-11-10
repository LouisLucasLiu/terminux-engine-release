// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// production layer 私有的非同步 transport 抽象；不包含任何 IPC/RPC 實作。

#pragma once

#include "dqe_viewer/production/dqe_repository_types.h"

namespace terminux::dqe::production {

class DqeRepositoryTransport
{
public:
    virtual ~DqeRepositoryTransport() = default;

    virtual void request(const TransportRequest &request, TransportCompletion completion) = 0;

    // 僅取消尚未開始的工作；已執行工作由 controller 的 generation 規則丟棄結果。
    virtual bool cancelQueued(RequestToken token) = 0;
};

} // namespace terminux::dqe::production
