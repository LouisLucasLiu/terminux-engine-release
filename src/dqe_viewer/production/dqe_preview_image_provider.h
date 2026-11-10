// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// memory-only QImage store；token 不編碼 backend ref 或檔案路徑。

#pragma once

#include <QHash>
#include <QImage>
#include <QQuickImageProvider>
#include <QReadWriteLock>
#include <QString>

namespace terminux::dqe::production {

class DqePreviewImageStore final
{
public:
    static DqePreviewImageStore &instance();

    QString insert(QImage image);
    QImage image(const QString &token) const;
    void remove(const QString &token);

private:
    DqePreviewImageStore() = default;

    mutable QReadWriteLock m_lock;
    QHash<QString, QImage> m_images;
    quint64 m_nextToken = 1;
};

class DqePreviewImageProvider final : public QQuickImageProvider
{
public:
    DqePreviewImageProvider();
    QImage requestImage(const QString &id, QSize *size, const QSize &requestedSize) override;
};

} // namespace terminux::dqe::production
