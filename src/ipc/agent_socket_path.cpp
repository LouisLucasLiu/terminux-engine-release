// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "ipc/agent_socket_path.h"

#include <QByteArray>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>

#include <pwd.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <vector>

namespace terminux {

namespace {

// 與 Python 端 `socket_lifecycle.AF_UNIX_PATH_MAX_BYTES` 相同：sun_path 108，
// 最後一個 byte 是 NUL。
constexpr int kAfUnixPathMaxBytes = 107;

const QString kSocketDirName    = QStringLiteral(".terminux");
const QString kSocketFileName   = QStringLiteral("terminux.sock");
const QString kLegacySocketPath = QStringLiteral("/tmp/terminux.sock");
const char*   kOverrideEnv      = "TERMINUX_CORE_SOCK";

// 既有 .terminux 目錄：只擋 group／other 可寫。
constexpr mode_t kSocketDirForbidden     = 0022;
// 覆寫路徑的父目錄：嚴格 0700。
constexpr mode_t kOverrideParentForbidden = 0077;

AgentSocketResolution fail(uid_t uid, const QString& expected,
                           const QString& code, const QString& message)
{
    AgentSocketResolution r;
    r.ok = false;
    r.uid = uid;
    r.expectedPath = expected;
    r.code = code;
    r.message = QStringLiteral("%1: %2 (uid=%3, expected=%4)")
                    .arg(code, message).arg(static_cast<qulonglong>(uid)).arg(expected);
    return r;
}

QString checkLength(const QString& path, QString* message)
{
    const int bytes = path.toUtf8().size();
    if (bytes > kAfUnixPathMaxBytes) {
        if (message) {
            *message = QStringLiteral("%1 is %2 bytes; the platform limit is %3")
                           .arg(path).arg(bytes).arg(kAfUnixPathMaxBytes);
        }
        return QStringLiteral("TERM_SOCKET_PATH_TOO_LONG");
    }
    return QString();
}

// home：存在、是目錄（允許 symlink 指向目錄）、owner 為 uid。不要求 0700。
QString checkHomeDirectory(const QString& home, uid_t uid, QString* message)
{
    struct stat st{};
    const QByteArray native = QFile::encodeName(home);
    if (::stat(native.constData(), &st) != 0) {
        if (message) {
            *message = QStringLiteral("home directory %1 does not exist or cannot be inspected")
                           .arg(home);
        }
        return QStringLiteral("TERM_HOME_DIR_UNAVAILABLE");
    }
    if (!S_ISDIR(st.st_mode)) {
        if (message) *message = QStringLiteral("home directory %1 is not a directory").arg(home);
        return QStringLiteral("TERM_HOME_DIR_UNAVAILABLE");
    }
    if (st.st_uid != uid) {
        if (message) {
            *message = QStringLiteral("home directory %1 is owned by uid %2, expected %3")
                           .arg(home)
                           .arg(static_cast<qulonglong>(st.st_uid))
                           .arg(static_cast<qulonglong>(uid));
        }
        return QStringLiteral("TERM_HOME_DIR_UNAVAILABLE");
    }
    return QString();
}

} // namespace

QString homeForUid(uid_t uid)
{
    if (qEnvironmentVariableIsSet("TERMINUX_HOME_DIR")) {
        const QString home = QString::fromLocal8Bit(qgetenv("TERMINUX_HOME_DIR"));
        QString message;
        if (!QDir::isAbsolutePath(home)
            || !checkRuntimeDirectory(home, uid, &message).isEmpty()) return QString();
        return home;
    }
    // 只看系統帳號資料，不看 $HOME（避免切換帳號後繼承別人的 HOME）。
    std::vector<char> buffer(16384);
    struct passwd pw{};
    struct passwd* result = nullptr;
    const int rc = ::getpwuid_r(uid, &pw, buffer.data(), buffer.size(), &result);
    if (rc != 0 || result == nullptr || result->pw_dir == nullptr || *result->pw_dir == '\0') {
        return QString();
    }
    return QFile::decodeName(result->pw_dir);
}

QString localNodename()
{
    QByteArray raw = qgetenv("TERMINUX_NODENAME");
    if (raw.isEmpty()) {
        char name[256]{};
        if (::gethostname(name, sizeof(name) - 1) != 0) return QString();
        raw = name;
    }
    if (raw == "." || raw == "..") return QString();
    for (char ch : raw) {
        if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')
              || (ch >= '0' && ch <= '9') || ch == '.' || ch == '_' || ch == '-'))
            return QString();
    }
    return QString::fromLatin1(raw);
}

QString formatAgentSocketPath(uid_t uid)
{
    QString home = homeForUid(uid);
    if (home.isEmpty()) {
        home = QStringLiteral("<home of uid %1>").arg(static_cast<qulonglong>(uid));
    }
    return home + QStringLiteral("/.terminux/run/") + localNodename()
           + QLatin1Char('/') + kSocketFileName;
}

QString formatLineageSocketPath(uid_t uid)
{
    return QFileInfo(formatAgentSocketPath(uid)).path() + QStringLiteral("/lineage.sock");
}

QString legacyAgentSocketPath()
{
    return kLegacySocketPath;
}

QString legacyRuntimeDirSocketPath(uid_t uid)
{
    return QStringLiteral("/run/user/%1/terminux/terminux.sock")
        .arg(static_cast<qulonglong>(uid));
}

QString checkRuntimeDirectory(const QString& directory, uid_t uid, QString* message,
                              mode_t forbiddenModeBits)
{
    struct stat st{};
    const QByteArray native = QFile::encodeName(directory);
    if (::lstat(native.constData(), &st) != 0) {
        if (message) {
            *message = QStringLiteral("%1 does not exist or cannot be inspected").arg(directory);
        }
        return QStringLiteral("TERM_RUNTIME_DIR_MISSING");
    }
    if (S_ISLNK(st.st_mode)) {
        if (message) *message = QStringLiteral("%1 is a symlink").arg(directory);
        return QStringLiteral("TERM_RUNTIME_DIR_IS_SYMLINK");
    }
    if (!S_ISDIR(st.st_mode)) {
        if (message) *message = QStringLiteral("%1 is not a directory").arg(directory);
        return QStringLiteral("TERM_RUNTIME_DIR_NOT_A_DIRECTORY");
    }
    if (st.st_uid != uid) {
        if (message) {
            *message = QStringLiteral("%1 is owned by uid %2, expected %3")
                           .arg(directory)
                           .arg(static_cast<qulonglong>(st.st_uid))
                           .arg(static_cast<qulonglong>(uid));
        }
        return QStringLiteral("TERM_RUNTIME_DIR_WRONG_OWNER");
    }
    if ((st.st_mode & forbiddenModeBits) != 0) {
        if (message) {
            *message = QStringLiteral("%1 has mode %2; it must not be writable by group or other")
                           .arg(directory)
                           .arg(static_cast<uint>(st.st_mode & 07777), 4, 8, QLatin1Char('0'));
        }
        return QStringLiteral("TERM_RUNTIME_DIR_TOO_PERMISSIVE");
    }
    return QString();
}

QString checkRuntimeDirectory(const QString& directory, uid_t uid, QString* message)
{
    return checkRuntimeDirectory(directory, uid, message, kOverrideParentForbidden);
}

QString checkSocketPathOccupant(const QString& path, QString* message)
{
    struct stat st{};
    const QByteArray native = QFile::encodeName(path);
    if (::lstat(native.constData(), &st) != 0) {
        return QString();  // 沒有東西：daemon 未啟動或可以 bind
    }
    if (S_ISSOCK(st.st_mode)) {
        return QString();  // 是 socket：交給 connect／bind 判定
    }
    QString what;
    if (S_ISLNK(st.st_mode))      what = QStringLiteral("a symlink (not followed)");
    else if (S_ISREG(st.st_mode)) what = QStringLiteral("a regular file");
    else if (S_ISDIR(st.st_mode)) what = QStringLiteral("a directory");
    else                          what = QStringLiteral("not a socket");
    if (message) {
        *message = QStringLiteral("%1 is %2 (owner uid %3); not removed")
                       .arg(path, what).arg(static_cast<qulonglong>(st.st_uid));
    }
    return QStringLiteral("TERM_AGENT_SOCKET_PATH_OCCUPIED");
}

static AgentSocketResolution resolveSocketPath(const char* overrideEnv, const QString& filename,
                                                bool checkDirectory)
{
    const uid_t uid = ::geteuid();
    const QString expected = QFileInfo(formatAgentSocketPath(uid)).path() + QLatin1Char('/') + filename;
    if (qEnvironmentVariableIsSet("TERMINUX_HOME_DIR")) {
        const QString home = QString::fromLocal8Bit(qgetenv("TERMINUX_HOME_DIR"));
        if (!QDir::isAbsolutePath(home))
            return fail(uid, expected, QStringLiteral("TERM_AGENT_SOCKET_OVERRIDE_INVALID"),
                        QStringLiteral("TERMINUX_HOME_DIR must be an absolute path"));
        QString message;
        const QString code = checkRuntimeDirectory(home, uid, &message);
        if (!code.isEmpty()) return fail(uid, expected, code, message);
    }
    if (localNodename().isEmpty())
        return fail(uid, expected, QStringLiteral("TERM_NODENAME_INVALID"), QStringLiteral("主機名稱不合法"));

    // ── 明確覆寫（隔離部署／整合測試）。不是 fallback：未設定就走 per-user。──
    const QByteArray overrideRaw = qgetenv(overrideEnv);
    QString overridePath = QString::fromLocal8Bit(overrideRaw).trimmed();
    if (!overridePath.isEmpty()) {
        const QString home = homeForUid(uid);
        const QString oldDefault = home + QStringLiteral("/.terminux/") + filename;
        if (!home.isEmpty() && QDir::cleanPath(overridePath) == oldDefault) {
            static std::atomic_bool warnedCore{false};
            static std::atomic_bool warnedLineage{false};
            std::atomic_bool& warned = filename == kSocketFileName ? warnedCore : warnedLineage;
            if (!warned.exchange(true)) {
                qWarning().noquote() << "TERM_LEGACY_SOCKET_OVERRIDE_IGNORED: ignored"
                                     << overrideEnv << overridePath;
            }
            overridePath.clear();
        }
    }
    if (!overridePath.isEmpty()) {
        if (!overridePath.startsWith(QLatin1Char('/'))) {
            return fail(uid, expected, QStringLiteral("TERM_AGENT_SOCKET_OVERRIDE_INVALID"),
                        QStringLiteral("%1 must be an absolute path: %2")
                            .arg(QLatin1String(overrideEnv), overridePath));
        }
        const QString normalized = QDir::cleanPath(overridePath);
        if (normalized == kLegacySocketPath || normalized == legacyRuntimeDirSocketPath(uid)) {
            return fail(uid, expected, QStringLiteral("TERM_AGENT_SOCKET_LEGACY_PATH_REJECTED"),
                        QStringLiteral("%1 may not point at a retired socket path (%2); "
                                       "the agent socket lives under the user's home")
                            .arg(QLatin1String(overrideEnv), normalized));
        }
        const QString parent = QFileInfo(normalized).path();
        QString message;
        QString code = checkLength(normalized, &message);
        if (!code.isEmpty()) return fail(uid, expected, code, message);
        code = checkDirectory ? checkRuntimeDirectory(parent, uid, &message, kOverrideParentForbidden) : QString();
        if (!code.isEmpty()) return fail(uid, expected, code, message);
        code = checkSocketPathOccupant(normalized, &message);
        if (!code.isEmpty()) return fail(uid, expected, code, message);
        code = checkLength(normalized, &message);
        if (!code.isEmpty()) return fail(uid, expected, code, message);

        AgentSocketResolution r;
        r.ok = true;
        r.uid = uid;
        r.expectedPath = expected;
        r.path = normalized;
        r.source = QStringLiteral("override");
        return r;
    }

    // 本帳號的私有主機目錄；client 只檢查，不建立。
    const QString home = homeForUid(uid);
    if (home.isEmpty()) {
        return fail(uid, expected, QStringLiteral("TERM_HOME_DIR_UNRESOLVED"),
                    QStringLiteral("no home directory is recorded for uid %1 in the "
                                   "account database")
                        .arg(static_cast<qulonglong>(uid)));
    }
    QString message;
    QString code = checkHomeDirectory(home, uid, &message);
    if (!code.isEmpty()) return fail(uid, expected, code, message);

    const QString base = home + QLatin1Char('/') + kSocketDirName;
    const QString run = base + QStringLiteral("/run");
    const QString sockets = run + QLatin1Char('/') + localNodename();
    const QString path = sockets + QLatin1Char('/') + filename;
    code = checkLength(path, &message);
    if (!code.isEmpty()) return fail(uid, expected, code, message);
    if (checkDirectory) {
        for (const QString& dir : {base, run, sockets}) {
            code = checkRuntimeDirectory(dir, uid, &message,
                dir == base ? kSocketDirForbidden : kOverrideParentForbidden);
            if (!code.isEmpty()) break;
        }
    }
    if (!code.isEmpty()) {
        if (code == QLatin1String("TERM_RUNTIME_DIR_MISSING")) {
            message += QStringLiteral("; the daemon for this uid has not created it yet");
        }
        return fail(uid, expected, code, message);
    }
    code = checkSocketPathOccupant(path, &message);
    if (!code.isEmpty()) return fail(uid, expected, code, message);
    code = checkLength(path, &message);
    if (!code.isEmpty()) return fail(uid, expected, code, message);

    AgentSocketResolution r;
    r.ok = true;
    r.uid = uid;
    r.expectedPath = expected;
    r.path = path;
    r.source = QStringLiteral("per_user");
    return r;
}

AgentSocketResolution resolveAgentSocketPath()
{
    return resolveSocketPath(kOverrideEnv, kSocketFileName, true);
}

AgentSocketResolution resolveLineageSocketPath(bool checkDirectory)
{
    return resolveSocketPath("TERMINUX_SOCK", QStringLiteral("lineage.sock"), checkDirectory);
}

} // namespace terminux
