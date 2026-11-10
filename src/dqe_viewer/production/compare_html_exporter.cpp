// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "dqe_viewer/production/compare_html_exporter.h"

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QStandardPaths>

namespace terminux::dqe::production {

CompareHtmlExporter::CompareHtmlExporter(QObject *parent)
    : QObject(parent)
{
}

QString CompareHtmlExporter::writeHtml(const QUrl &fileUrl, const QString &html)
{
    const QString path = fileUrl.isLocalFile() ? fileUrl.toLocalFile() : fileUrl.toString();
    if (path.isEmpty()) {
        return QStringLiteral("invalid file path");
    }
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return QStringLiteral("cannot open file (%1)").arg(file.errorString());
    }
    const QByteArray bytes = html.toUtf8();
    if (file.write(bytes) != bytes.size()) {
        const QString err = file.errorString();
        file.close();
        return QStringLiteral("write failed (%1)").arg(err);
    }
    file.close();
    return QString();
}

QString CompareHtmlExporter::suggestedFileName() const
{
    return QStringLiteral("dqe_compare_%1.html")
        .arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd_HHmmss")));
}

QUrl CompareHtmlExporter::suggestedFileUrl() const
{
    QString dir = QStandardPaths::writableLocation(QStandardPaths::DocumentsLocation);
    if (dir.isEmpty()) {
        dir = QStandardPaths::writableLocation(QStandardPaths::HomeLocation);
    }
    if (dir.isEmpty()) {
        dir = QDir::homePath();
    }
    return QUrl::fromLocalFile(QDir(dir).filePath(suggestedFileName()));
}

} // namespace terminux::dqe::production
