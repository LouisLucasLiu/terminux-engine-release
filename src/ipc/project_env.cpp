// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "ipc/project_env.h"

#include <QByteArray>

namespace terminux {

namespace {

const char* kPrimaryEnv = "TERMINUX_PROJECT_ID";
const char* kLegacyEnv  = "TERMINUX_PROJECT";
constexpr int kMaxProjectIdChars = 128;

bool isValidProjectId(const QString& value)
{
    if (value.size() > kMaxProjectIdChars) return false;
    for (const QChar ch : value) {
        if (ch.isSpace() || ch.unicode() < 32 || ch.unicode() == 127) return false;
    }
    return true;
}

ProjectDeclaration invalid(const QString& envName, const QString& value)
{
    ProjectDeclaration d;
    d.ok = false;
    d.code = QStringLiteral("TERM_PROJECT_ENV_INVALID");
    d.message = QStringLiteral("%1=\"%2\" is not a valid project id: it must not contain "
                               "whitespace or control characters and must be at most %3 "
                               "characters")
                    .arg(envName, value).arg(kMaxProjectIdChars);
    return d;
}

} // namespace

ProjectDeclaration resolveProjectDeclaration(const QString& primaryRaw,
                                             const QString& legacyRaw)
{
    const QString primary = primaryRaw.trimmed();
    const QString legacy  = legacyRaw.trimmed();

    if (!primary.isEmpty() && !legacy.isEmpty() && primary != legacy) {
        ProjectDeclaration d;
        d.ok = false;
        d.code = QStringLiteral("TERM_PROJECT_ENV_CONFLICT");
        d.message = QStringLiteral("%1=\"%2\" and %3=\"%4\" disagree; unset one of them "
                                   "(the supported name is %1)")
                        .arg(QLatin1String(kPrimaryEnv), primary,
                             QLatin1String(kLegacyEnv), legacy);
        return d;
    }
    if (!primary.isEmpty()) {
        if (!isValidProjectId(primary)) return invalid(QLatin1String(kPrimaryEnv), primary);
        ProjectDeclaration d;
        d.declared = true;
        d.projectId = primary;
        d.sourceEnv = QLatin1String(kPrimaryEnv);
        return d;
    }
    if (!legacy.isEmpty()) {
        if (!isValidProjectId(legacy)) return invalid(QLatin1String(kLegacyEnv), legacy);
        ProjectDeclaration d;
        d.declared = true;
        d.projectId = legacy;
        d.sourceEnv = QLatin1String(kLegacyEnv);
        d.warning = QStringLiteral("%1 is a legacy name; it still works but please switch to %2")
                        .arg(QLatin1String(kLegacyEnv), QLatin1String(kPrimaryEnv));
        return d;
    }
    ProjectDeclaration d;   // 未宣告：ok=true, declared=false
    return d;
}

ProjectDeclaration resolveProjectDeclarationFromEnvironment()
{
    return resolveProjectDeclaration(QString::fromLocal8Bit(qgetenv(kPrimaryEnv)),
                                     QString::fromLocal8Bit(qgetenv(kLegacyEnv)));
}

} // namespace terminux
