// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 將 source generation cache 的 raw items 聚合為 input shape；不做數值計算或判讀。

#include "dqe_viewer/production/compare_snapshot_aggregator.h"

#include "dqe_viewer/production/dqe_repository_controller.h"

#include <QHash>
#include <QSet>

#include <utility>

namespace terminux::dqe::production {
namespace {

QString text(const QVariantMap &map, const char *key)
{
    return map.value(QLatin1String(key)).toString();
}

QVariantMap safeAttachment(const QVariantMap &attachment)
{
    return {
        {QStringLiteral("attachmentId"), attachment.value(QStringLiteral("attachmentId"))},
        {QStringLiteral("displayName"), attachment.value(QStringLiteral("displayName"))},
        {QStringLiteral("role"), attachment.value(QStringLiteral("role"))},
        {QStringLiteral("state"), attachment.value(QStringLiteral("state"))},
        {QStringLiteral("mimeHint"), attachment.value(QStringLiteral("mimeHint"))},
        {QStringLiteral("sizeBytes"), attachment.value(QStringLiteral("sizeBytes"))},
        {QStringLiteral("previewAvailability"), attachment.value(QStringLiteral("previewAvailability"))},
        {QStringLiteral("integrityState"), attachment.value(QStringLiteral("integrityState"))},
    };
}

} // 匿名命名空間

CompareSnapshotAggregator::CompareSnapshotAggregator(QObject *parent)
    : QObject(parent)
{
}

QVariantList CompareSnapshotAggregator::sources() const { return m_sources; }
QVariantList CompareSnapshotAggregator::series() const { return m_series; }

void CompareSnapshotAggregator::addRepository(DqeRepositoryController *repository)
{
    if (!repository || m_repositories.contains(repository)) {
        return;
    }
    m_repositories.append(repository);
    connect(repository, &DqeRepositoryController::sourcesChanged,
            this, &CompareSnapshotAggregator::rebuild);
    connect(repository, &QObject::destroyed, this, &CompareSnapshotAggregator::rebuild);
    rebuild();
}

QVariantList CompareSnapshotAggregator::attachmentsForPoint(const QString &sourceId, const QString &dqeId) const
{
    const CachedSource cached = m_cache.value(sourceId);
    QVariantList attachments;
    for (const QVariant &itemValue : cached.items) {
        const QVariantMap item = itemValue.toMap();
        if (item.value(QStringLiteral("dqeId")).toString() != dqeId) {
            continue;
        }
        for (const QVariant &attachmentValue : item.value(QStringLiteral("attachments")).toList()) {
            attachments.append(safeAttachment(attachmentValue.toMap()));
        }
    }
    return attachments;
}

void CompareSnapshotAggregator::rebuild()
{
    QHash<QString, CachedSource> nextCache;
    QStringList nextOrder;
    for (const QPointer<DqeRepositoryController> &repository : std::as_const(m_repositories)) {
        if (!repository) {
            continue;
        }
        const quint64 generation = repository->sourceGeneration();
        for (const QVariant &snapshotValue : repository->compareSourceSnapshots()) {
            const QVariantMap snapshot = snapshotValue.toMap();
            const QString repositorySourceId = snapshot.value(QStringLiteral("sourceId")).toString();
            if (repositorySourceId.isEmpty()) {
                continue;
            }
            const QString compareId = compareIdFor(repository, repositorySourceId);
            CachedSource cached;
            cached.repository = repository;
            cached.repositorySourceId = repositorySourceId;
            cached.generation = generation;
            cached.source = snapshot;
            cached.items = snapshot.value(QStringLiteral("items")).toList();
            nextCache.insert(compareId, cached);
            nextOrder.append(compareId);
        }
    }
    m_cache = std::move(nextCache);
    m_sourceOrder = nextOrder;

    QVariantList sources;
    QVariantList series;
    QHash<QString, QVariantMap> seriesByDqe;
    QStringList dqeOrder;
    for (const QString &compareId : std::as_const(m_sourceOrder)) {
        const CachedSource cached = m_cache.value(compareId);
        const QVariantMap source = cached.source;
        const QString block = text(source, "block");
        const QString flow = text(source, "flow");
        const QString displayName = text(source, "displayName");
        sources.append(QVariantMap{
            {QStringLiteral("sourceId"), compareId},
            {QStringLiteral("sourceKind"), source.value(QStringLiteral("sourceKind"))},
            {QStringLiteral("displayName"), displayName},
            {QStringLiteral("jobKey"), source.value(QStringLiteral("jobOrVersion"))},
            {QStringLiteral("block"), block},
            {QStringLiteral("flow"), flow},
            {QStringLiteral("shortLabel"), displayName},
            {QStringLiteral("subtitle"), block + QStringLiteral(" · ") + flow},
        });

        QHash<QString, QVariantList> rowsByDqe;
        // QHash 的 iteration order 不可作為 UI contract；保留已載入 source item 的原始順序。
        QStringList sourceDqeOrder;
        for (const QVariant &itemValue : cached.items) {
            const QVariantMap item = itemValue.toMap();
            const QString dqeId = text(item, "dqeId");
            if (!dqeId.isEmpty()) {
                if (!rowsByDqe.contains(dqeId)) {
                    sourceDqeOrder.append(dqeId);
                }
                rowsByDqe[dqeId].append(item);
            }
        }
        for (const QString &dqeId : std::as_const(sourceDqeOrder)) {
            const QVariantList rows = rowsByDqe.value(dqeId);
            if (!seriesByDqe.contains(dqeId)) {
                const QVariantMap first = rows.constFirst().toMap();
                seriesByDqe.insert(dqeId, QVariantMap{
                    {QStringLiteral("dqeId"), dqeId},
                    {QStringLiteral("title"), first.value(QStringLiteral("title"))},
                    {QStringLiteral("description"), first.value(QStringLiteral("description"))},
                    {QStringLiteral("unit"), first.value(QStringLiteral("unit"))},
                    {QStringLiteral("block"), first.value(QStringLiteral("block"))},
                    {QStringLiteral("points"), QVariantList{}},
                });
                dqeOrder.append(dqeId);
            }
            QVariantMap current = seriesByDqe.value(dqeId);
            QVariantList points = current.value(QStringLiteral("points")).toList();
            if (rows.size() != 1) {
                QVariantList duplicateRows;
                for (const QVariant &rowValue : rows) {
                    const QVariantMap row = rowValue.toMap();
                    duplicateRows.append(QVariantMap{
                        {QStringLiteral("title"), row.value(QStringLiteral("title"))},
                        {QStringLiteral("rawValue"), row.value(QStringLiteral("rawValue"))},
                        {QStringLiteral("unit"), row.value(QStringLiteral("unit"))},
                        {QStringLiteral("status"), row.value(QStringLiteral("status"))},
                        {QStringLiteral("statusRaw"), row.value(QStringLiteral("statusRaw"))},
                    });
                }
                points.append(QVariantMap{
                    {QStringLiteral("sourceId"), compareId},
                    {QStringLiteral("value"), QVariant()},
                    {QStringLiteral("unit"), QVariant()},
                    {QStringLiteral("status"), QVariant()},
                    {QStringLiteral("statusRaw"), QVariant()},
                    {QStringLiteral("comparisonIssue"), QStringLiteral("duplicate_dqe_id")},
                    {QStringLiteral("duplicateRows"), duplicateRows},
                });
            } else {
                const QVariantMap row = rows.constFirst().toMap();
                points.append(QVariantMap{
                    {QStringLiteral("sourceId"), compareId},
                    {QStringLiteral("value"), row.value(QStringLiteral("rawValue"))},
                    {QStringLiteral("unit"), row.value(QStringLiteral("unit"))},
                    // status 與 statusRaw 都是已接受 DTO 的原值；不得以其中一個覆寫另一個。
                    {QStringLiteral("status"), row.value(QStringLiteral("status"))},
                    {QStringLiteral("statusRaw"), row.value(QStringLiteral("statusRaw"))},
                });
            }
            current.insert(QStringLiteral("points"), points);
            seriesByDqe.insert(dqeId, current);
        }
    }
    for (const QString &dqeId : std::as_const(dqeOrder)) {
        series.append(seriesByDqe.value(dqeId));
    }
    m_sources = sources;
    m_series = series;
    emit snapshotsChanged();
}

QString CompareSnapshotAggregator::compareIdFor(DqeRepositoryController *repository, const QString &sourceId)
{
    const QString key = repositorySourceKey(repository, sourceId);
    auto found = m_compareIdsByRepositorySource.constFind(key);
    if (found != m_compareIdsByRepositorySource.cend()) {
        return *found;
    }
    const QString compareId = QStringLiteral("cmp-src-%1").arg(m_nextCompareId++, 6, 10, QLatin1Char('0'));
    m_compareIdsByRepositorySource.insert(key, compareId);
    return compareId;
}

QString CompareSnapshotAggregator::repositorySourceKey(DqeRepositoryController *repository, const QString &sourceId)
{
    return QString::number(reinterpret_cast<quintptr>(repository), 16) + QLatin1Char(':') + sourceId;
}

} // 命名空間 terminux::dqe::production
