// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// opaque ID 永遠由本 process 的遞增序號產生，不編碼 backend ref。

#include "dqe_viewer/production/opaque_id_registry.h"

#include <QList>

namespace terminux::dqe::production {

OpaqueIdRegistry::Bucket &OpaqueIdRegistry::bucket(Kind kind)
{
    switch (kind) {
    case Kind::Source:
        return m_sources;
    case Kind::Item:
        return m_items;
    case Kind::Attachment:
        return m_attachments;
    }
    return m_sources;
}

const OpaqueIdRegistry::Bucket &OpaqueIdRegistry::bucket(Kind kind) const
{
    switch (kind) {
    case Kind::Source:
        return m_sources;
    case Kind::Item:
        return m_items;
    case Kind::Attachment:
        return m_attachments;
    }
    return m_sources;
}

QString OpaqueIdRegistry::prefix(Kind kind)
{
    switch (kind) {
    case Kind::Source:
        return QStringLiteral("src");
    case Kind::Item:
        return QStringLiteral("item");
    case Kind::Attachment:
        return QStringLiteral("att");
    }
    return QStringLiteral("opaque");
}

QString OpaqueIdRegistry::idFor(Kind kind, const QString &privateRef)
{
    Bucket &current = bucket(kind);
    const auto existing = current.privateToOpaque.constFind(privateRef);
    if (existing != current.privateToOpaque.cend()) {
        return *existing;
    }

    const QString opaqueId = QStringLiteral("%1-%2")
                                 .arg(prefix(kind))
                                 .arg(current.nextValue++, 6, 10, QLatin1Char('0'));
    current.privateToOpaque.insert(privateRef, opaqueId);
    current.opaqueToPrivate.insert(opaqueId, privateRef);
    return opaqueId;
}

QString OpaqueIdRegistry::privateRefFor(const QString &opaqueId) const
{
    for (const Bucket *current : {&m_sources, &m_items, &m_attachments}) {
        const auto found = current->opaqueToPrivate.constFind(opaqueId);
        if (found != current->opaqueToPrivate.cend()) {
            return *found;
        }
    }
    return {};
}

bool OpaqueIdRegistry::contains(Kind kind, const QString &opaqueId) const
{
    return bucket(kind).opaqueToPrivate.contains(opaqueId);
}

bool OpaqueIdRegistry::invalidate(Kind kind, const QString &privateRef)
{
    Bucket &current = bucket(kind);
    const auto found = current.privateToOpaque.find(privateRef);
    if (found == current.privateToOpaque.end()) {
        return false;
    }
    current.opaqueToPrivate.remove(found.value());
    current.privateToOpaque.erase(found);
    return true;
}

void OpaqueIdRegistry::retainOnly(Kind kind, const QSet<QString> &privateRefs)
{
    Bucket &current = bucket(kind);
    const QList<QString> knownRefs = current.privateToOpaque.keys();
    for (const QString &privateRef : knownRefs) {
        if (!privateRefs.contains(privateRef)) {
            invalidate(kind, privateRef);
        }
    }
}

} // namespace terminux::dqe::production
