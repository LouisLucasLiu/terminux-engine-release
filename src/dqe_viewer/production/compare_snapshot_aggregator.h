// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 聚合已載入的 production raw snapshots；不新增 Compare RPC，也不做 normalization/score。

#pragma once

#include <QObject>
#include <QHash>
#include <QPointer>
#include <QStringList>
#include <QVariantList>

namespace terminux::dqe::production {

class DqeRepositoryController;

class CompareSnapshotAggregator final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(QVariantList sources READ sources NOTIFY snapshotsChanged)
    Q_PROPERTY(QVariantList series READ series NOTIFY snapshotsChanged)

public:
    explicit CompareSnapshotAggregator(QObject *parent = nullptr);

    QVariantList sources() const;
    QVariantList series() const;
    void addRepository(DqeRepositoryController *repository);
    Q_INVOKABLE QVariantList attachmentsForPoint(const QString &sourceId, const QString &dqeId) const;

signals:
    void snapshotsChanged();

private:
    struct CachedSource {
        QPointer<DqeRepositoryController> repository;
        QString repositorySourceId;
        quint64 generation = 0;
        QVariantMap source;
        QVariantList items;
    };

    void rebuild();
    QString compareIdFor(DqeRepositoryController *repository, const QString &sourceId);
    static QString repositorySourceKey(DqeRepositoryController *repository, const QString &sourceId);

    QList<QPointer<DqeRepositoryController>> m_repositories;
    QHash<QString, QString> m_compareIdsByRepositorySource;
    QHash<QString, CachedSource> m_cache;
    QStringList m_sourceOrder;
    QVariantList m_sources;
    QVariantList m_series;
    quint64 m_nextCompareId = 1;
};

} // 命名空間 terminux::dqe::production
