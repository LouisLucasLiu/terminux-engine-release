// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 影像只停留在 process memory，QML 以 opaque image provider token 讀取。

#include "dqe_viewer/production/dqe_preview_image_provider.h"

#include <QReadLocker>
#include <QWriteLocker>

namespace terminux::dqe::production {

DqePreviewImageStore &DqePreviewImageStore::instance()
{
    static DqePreviewImageStore store;
    return store;
}

QString DqePreviewImageStore::insert(QImage image)
{
    if (image.isNull()) {
        return {};
    }
    const QWriteLocker lock(&m_lock);
    const QString token = QStringLiteral("img-%1")
                              .arg(m_nextToken++, 8, 10, QLatin1Char('0'));
    m_images.insert(token, std::move(image));
    return token;
}

QImage DqePreviewImageStore::image(const QString &token) const
{
    const QReadLocker lock(&m_lock);
    return m_images.value(token);
}

void DqePreviewImageStore::remove(const QString &token)
{
    if (token.isEmpty()) {
        return;
    }
    const QWriteLocker lock(&m_lock);
    m_images.remove(token);
}

DqePreviewImageProvider::DqePreviewImageProvider()
    : QQuickImageProvider(QQuickImageProvider::Image)
{
}

QImage DqePreviewImageProvider::requestImage(const QString &id, QSize *size,
                                              const QSize &requestedSize)
{
    QImage result = DqePreviewImageStore::instance().image(id);
    if (size) {
        *size = result.size();
    }
    if (!result.isNull() && requestedSize.isValid()) {
        result = result.scaled(requestedSize, Qt::KeepAspectRatio, Qt::SmoothTransformation);
    }
    return result;
}

} // namespace terminux::dqe::production
