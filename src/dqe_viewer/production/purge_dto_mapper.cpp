// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 只保留 purge preview/commit 的安全數量與通用文字，絕不轉送 token、DB path 或原始警告。

#include "dqe_viewer/production/purge_dto_mapper.h"

#include <QJsonArray>
#include <QJsonValue>

namespace terminux::dqe::production {
namespace {

constexpr auto kSelectedJobs = "selected_jobs";
constexpr auto kExpiredNow = "expired_now";

bool isSupportedMode(const QString &mode)
{
    return mode == QLatin1String(kSelectedJobs) || mode == QLatin1String(kExpiredNow);
}

bool isNonNegativeInteger(const QJsonValue &value)
{
    if (!value.isDouble()) {
        return false;
    }
    const double number = value.toDouble();
    return number >= 0 && number == static_cast<double>(value.toInt());
}

int safeCount(const QJsonObject &response, const char *key)
{
    const QJsonValue value = response.value(QLatin1String(key));
    return isNonNegativeInteger(value) ? value.toInt() : 0;
}

bool hasTokenProblem(const QJsonObject &response)
{
    const QStringList warnings = response.value(QStringLiteral("warnings")).toVariant().toStringList();
    for (const QString &warning : warnings) {
        if (warning.contains(QStringLiteral("confirmation token"), Qt::CaseInsensitive)) {
            return true;
        }
    }
    return false;
}

QStringList safeWarnings(const QJsonObject &response)
{
    const QJsonValue warnings = response.value(QStringLiteral("warnings"));
    return warnings.isArray() && !warnings.toArray().isEmpty()
               ? QStringList{QStringLiteral("The backend reported a safe-to-review purge warning.")}
               : QStringList{};
}

PurgePreviewResult invalidPreview()
{
    return {QStringLiteral("failed"), QStringLiteral("purge_preview_contract_error"),
            QStringLiteral("The purge preview response was invalid."), {}, 0, 0, 0, {}, {}};
}

} // 匿名命名空間

PurgePreviewResult PurgeDtoMapper::mapPreviewResponse(const QJsonObject &response, const QString &mode,
                                                       const QString &selectedJobLabel)
{
    if (!isSupportedMode(mode) || response.value(QStringLiteral("mode")).toString() != mode) {
        return invalidPreview();
    }
    if (response.value(QStringLiteral("status")).toString() != QLatin1String("ready")) {
        return {QStringLiteral("failed"), QStringLiteral("purge_preview_failed"),
                QStringLiteral("The purge preview could not be created."), {}, 0, 0, 0, {},
                safeWarnings(response)};
    }
    const QString token = response.value(QStringLiteral("confirmation_token")).toString();
    if (token.isEmpty() || !isNonNegativeInteger(response.value(QStringLiteral("candidate_job_count")))
        || !isNonNegativeInteger(response.value(QStringLiteral("candidate_row_count")))
        || !isNonNegativeInteger(response.value(QStringLiteral("candidate_attachment_blob_count")))) {
        return invalidPreview();
    }
    QStringList labels;
    if (mode == QLatin1String(kSelectedJobs) && !selectedJobLabel.isEmpty()) {
        labels.append(selectedJobLabel);
    } else if (mode == QLatin1String(kExpiredNow)) {
        labels.append(QStringLiteral("Expired jobs at preview time"));
    }
    return {QStringLiteral("succeeded"), {}, QStringLiteral("Purge preview is ready for confirmation."),
            token, safeCount(response, "candidate_job_count"), safeCount(response, "candidate_row_count"),
            safeCount(response, "candidate_attachment_blob_count"), labels, safeWarnings(response)};
}

PurgeCommitResult PurgeDtoMapper::mapCommitResponse(const QJsonObject &response, const QString &mode)
{
    if (!isSupportedMode(mode) || response.value(QStringLiteral("mode")).toString() != mode) {
        return {QStringLiteral("failed"), QStringLiteral("purge_commit_contract_error"),
                QStringLiteral("The purge result was invalid."), 0, 0, {}};
    }
    const QString status = response.value(QStringLiteral("status")).toString();
    if (status == QLatin1String("purged") || status == QLatin1String("partial")) {
        if (!isNonNegativeInteger(response.value(QStringLiteral("deleted_row_count")))) {
            return {QStringLiteral("failed"), QStringLiteral("purge_commit_contract_error"),
                    QStringLiteral("The purge result was invalid."), 0, 0, {}};
        }
        const QJsonValue jobs = response.value(QStringLiteral("deleted_job_count"));
        const int jobCount = isNonNegativeInteger(jobs) ? jobs.toInt() : 0;
        const bool partial = status == QLatin1String("partial");
        return {partial ? QStringLiteral("partial") : QStringLiteral("succeeded"), {},
                partial ? QStringLiteral("Purge completed only partially. Review the refreshed jobs.")
                        : QStringLiteral("Purge completed."),
                jobCount, safeCount(response, "deleted_row_count"), safeWarnings(response)};
    }
    if (status == QLatin1String("failed") && hasTokenProblem(response)) {
        return {QStringLiteral("refused"), QStringLiteral("purge_preview_stale"),
                QStringLiteral("The purge preview is stale or expired. Request a new preview."), 0, 0, {}};
    }
    return {QStringLiteral("failed"), QStringLiteral("purge_failed"),
            QStringLiteral("The purge operation failed."), 0, 0, safeWarnings(response)};
}

} // 命名空間 terminux::dqe::production
