// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// private backend ref 與 QML opaque ID 的 process-local 對照表。

#pragma once

#include <QHash>
#include <QSet>
#include <QString>

namespace terminux::dqe::production {

class OpaqueIdRegistry
{
public:
    enum class Kind {
        Source,
        Item,
        Attachment,
    };

    QString idFor(Kind kind, const QString &privateRef);
    QString privateRefFor(const QString &opaqueId) const;
    bool contains(Kind kind, const QString &opaqueId) const;
    bool invalidate(Kind kind, const QString &privateRef);
    void retainOnly(Kind kind, const QSet<QString> &privateRefs);

private:
    struct Bucket {
        QHash<QString, QString> privateToOpaque;
        QHash<QString, QString> opaqueToPrivate;
        quint64 nextValue = 1;
    };

    Bucket &bucket(Kind kind);
    const Bucket &bucket(Kind kind) const;
    static QString prefix(Kind kind);

    Bucket m_sources;
    Bucket m_items;
    Bucket m_attachments;
};

} // namespace terminux::dqe::production
