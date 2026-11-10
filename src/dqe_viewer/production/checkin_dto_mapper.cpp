// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 只輸出 safe summary、relative planned files 與 canonical operation state。

#include "dqe_viewer/production/checkin_dto_mapper.h"

#include <QJsonArray>
#include <QSet>

namespace terminux::dqe::production {
namespace {

bool isSafeRelativePath(const QString &path)
{
    return !path.isEmpty() && !path.startsWith(QLatin1Char('/'))
           && !path.startsWith(QLatin1Char('~')) && !path.contains(QStringLiteral(".."))
           && !path.contains(QLatin1Char('\\'));
}

QString safeFailureCode(const QString &code)
{
    static const QSet<QString> allowed = {
        QStringLiteral("target_workspace_missing"), QStringLiteral("target_version_dir_missing"),
        QStringLiteral("path_escape_rejected"), QStringLiteral("invalid_request"),
        QStringLiteral("job_not_found"), QStringLiteral("db_missing"),
        QStringLiteral("db_schema_mismatch"), QStringLiteral("db_query_failed"),
        QStringLiteral("isvn_status_failed"), QStringLiteral("isvn_edit_failed"),
        QStringLiteral("isvn_checkin_failed"), QStringLiteral("partial_checkin_failed"),
        QStringLiteral("package_write_failed"), QStringLiteral("internal_error"),
    };
    return allowed.contains(code) ? code : QStringLiteral("operation_failed");
}

QString safeLastResult(const QString &result)
{
    static const QSet<QString> allowed = {
        QStringLiteral("success_new"), QStringLiteral("success_update"),
        QStringLiteral("failed"), QStringLiteral("not_run"),
    };
    return allowed.contains(result) ? result : QStringLiteral("failed");
}

QString safeMessage(const QString &state, const QString &failureCode, bool dryRun)
{
    if (state == QLatin1String("succeeded")) {
        return dryRun ? QStringLiteral("Dry-run is ready for confirmation.")
                      : QStringLiteral("Check-in completed.");
    }
    if (state == QLatin1String("partial")) {
        return QStringLiteral("Check-in completed only partially. Review files and retry manually if needed.");
    }
    if (state == QLatin1String("refused")) {
        if (failureCode == QLatin1String("target_version_dir_missing")) {
            return QStringLiteral("The selected target version does not exist.");
        }
        return QStringLiteral("The check-in request was refused by validation.");
    }
    return QStringLiteral("The check-in operation failed.");
}

bool isRefusedFailure(const QString &failureCode)
{
    return failureCode == QLatin1String("target_workspace_missing")
           || failureCode == QLatin1String("target_version_dir_missing")
           || failureCode == QLatin1String("path_escape_rejected")
           || failureCode == QLatin1String("invalid_request")
           || failureCode == QLatin1String("job_not_found");
}

} // 匿名命名空間

CheckinOperationResult CheckinDtoMapper::mapResponse(const QJsonObject &response)
{
    CheckinOperationResult mapped;
    mapped.dryRun = response.value(QStringLiteral("dry_run")).toBool(false);
    mapped.partial = response.value(QStringLiteral("partial")).toBool(false);
    mapped.lastResult = safeLastResult(response.value(QStringLiteral("last_result")).toString());
    mapped.failureCode = safeFailureCode(response.value(QStringLiteral("failure_code")).toString());

    if (mapped.partial || mapped.failureCode == QLatin1String("partial_checkin_failed")) {
        mapped.state = QStringLiteral("partial");
        mapped.partial = true;
    } else if (mapped.lastResult == QLatin1String("success_new")
               || mapped.lastResult == QLatin1String("success_update")) {
        mapped.state = QStringLiteral("succeeded");
        mapped.failureCode.clear();
    } else if (isRefusedFailure(mapped.failureCode)) {
        mapped.state = QStringLiteral("refused");
    } else {
        mapped.state = QStringLiteral("failed");
    }
    mapped.message = safeMessage(mapped.state, mapped.failureCode, mapped.dryRun);

    const QJsonArray responseFiles = response.value(QStringLiteral("files")).toArray();
    for (const QJsonValue &value : responseFiles) {
        const QJsonObject file = value.toObject();
        const QString relpath = file.value(QStringLiteral("relpath")).toString();
        if (!isSafeRelativePath(relpath)) {
            continue;
        }
        const QVariantMap safeFile{
            {QStringLiteral("relpath"), relpath},
            {QStringLiteral("kind"), file.value(QStringLiteral("kind")).toString()},
            {QStringLiteral("managedBefore"), file.value(QStringLiteral("managed_before")).toBool(false)},
            {QStringLiteral("edited"), file.value(QStringLiteral("edited")).toBool(false)},
            {QStringLiteral("written"), file.value(QStringLiteral("written")).toBool(false)},
            {QStringLiteral("checkedIn"), file.value(QStringLiteral("checked_in")).toBool(false)},
        };
        mapped.files.append(safeFile);
        if (safeFile.value(QStringLiteral("managedBefore")).toBool()) {
            // 使用既有受控檔案作為預檢衝突；只保留相對檔名與安全分類。
            mapped.conflicts.append(QVariantMap{
                {QStringLiteral("relpath"), relpath},
                {QStringLiteral("kind"), safeFile.value(QStringLiteral("kind"))},
                {QStringLiteral("summary"), QStringLiteral("Existing managed target will be updated.")},
            });
        }
    }
    if (mapped.partial && !response.value(QStringLiteral("partial_warning")).toString().isEmpty()) {
        // 不傳遞 backend 原文；partial 的通用安全警告足以指引人工 retry。
        mapped.warnings.append(QStringLiteral("Some files may already be checked in; retry manually if needed."));
    }
    return mapped;
}

} // 命名空間 terminux::dqe::production
