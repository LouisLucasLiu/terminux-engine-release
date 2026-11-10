// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 只輸出全域 policy 天數、零刪除證據與安全化 operation 狀態。

#include "dqe_viewer/production/retention_dto_mapper.h"

namespace terminux::dqe::production {
namespace {

constexpr int kMinimumRetentionDays = 1;
constexpr int kMaximumRetentionDays = 3650;

bool isValidRetentionDays(const QJsonValue &value)
{
    if (!value.isDouble()) {
        return false;
    }
    const double rawDays = value.toDouble();
    const int days = value.toInt();
    return rawDays == static_cast<double>(days) && days >= kMinimumRetentionDays
           && days <= kMaximumRetentionDays;
}

RetentionOperationResult invalidResponse()
{
    return {
        QStringLiteral("failed"),
        QStringLiteral("retention_contract_error"),
        QStringLiteral("The retention policy response was invalid."),
        0,
        0,
    };
}

} // 匿名命名空間

RetentionOperationResult RetentionDtoMapper::mapLoadResponse(const QJsonObject &response)
{
    const QJsonValue retentionDays = response.value(QStringLiteral("retention_days"));
    if (!isValidRetentionDays(retentionDays)) {
        return invalidResponse();
    }
    return {
        QStringLiteral("succeeded"),
        {},
        QStringLiteral("Temporary retention policy loaded."),
        retentionDays.toInt(),
        0,
    };
}

RetentionOperationResult RetentionDtoMapper::mapSaveResponse(const QJsonObject &response)
{
    const QJsonValue retentionDays = response.value(QStringLiteral("retention_days"));
    const QJsonValue recordsDeleted = response.value(QStringLiteral("records_deleted"));
    if (!isValidRetentionDays(retentionDays) || !recordsDeleted.isDouble()
        || recordsDeleted.toDouble() != 0.0) {
        return invalidResponse();
    }
    return {
        QStringLiteral("succeeded"),
        {},
        QStringLiteral("Temporary retention policy saved. No Temporary records were deleted."),
        retentionDays.toInt(),
        0,
    };
}

} // 命名空間 terminux::dqe::production
