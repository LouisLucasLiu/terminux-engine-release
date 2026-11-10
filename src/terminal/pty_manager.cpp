// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/pty_manager.h"

#include <QDir>
#include <QFileInfo>
#include <QSocketNotifier>
#include <QUuid>

#include <chrono>
#include <cerrno>
#include <cstdlib>

#include <fcntl.h>
#include <pty.h>           // forkpty (Linux glibc, link with -lutil)
#include <pwd.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>

namespace terminux {

PtyManager::PtyManager(QObject* parent) : QObject(parent) {}

PtyManager::~PtyManager() {
    cleanup();
}

bool PtyManager::start(int rows, int cols, const QString& shellProgram) {
    if (ptyFd_ >= 0) return false;  // 已啟動

    rows_ = rows;
    cols_ = cols;

    // ── 決定 shell 路徑（三段 fallback）──────────────────────────────────
    QByteArray shellPath = shellProgram.toLocal8Bit();
    if (shellPath.isEmpty()) {
        if (const char* env = std::getenv("SHELL"); env && env[0] != '\0') {
            shellPath = QByteArray(env);
        } else if (const struct passwd* pw = getpwuid(getuid());
                   pw && pw->pw_shell && pw->pw_shell[0] != '\0') {
            shellPath = QByteArray(pw->pw_shell);
        } else {
            shellPath = QByteArrayLiteral("/bin/bash");
        }
    }

    // ── 預先計算注入到 child 的環境變數值（parent 端執行）────────────────
    // Trace ID chain（與 terminal_widget.cpp:62-91 等價邏輯，非照抄 Qt API）
    traceRoot_ = QStringLiteral("sess_%1")
        .arg(QUuid::createUuid().toString(QUuid::Id128))
        .toLocal8Bit();
    const auto epochNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    traceSelf_ = QStringLiteral("inv_%1_%2")
        .arg(static_cast<qlonglong>(epochNs)).arg(getpid())
        .toLocal8Bit();

    if (const char* orig = std::getenv("BASH_ENV"); orig && orig[0] != '\0') {
        origBashEnv_ = QByteArray(orig);
    } else {
        origBashEnv_.clear();
    }
    bashEnv_ = QStringLiteral("%1/.terminux/hooks/bash_env")
        .arg(QDir::homePath())
        .toLocal8Bit();
    const QByteArray bashRcfile = QDir::home().filePath(
        QStringLiteral(".terminux/hooks/bash_rcfile")).toLocal8Bit();
    const bool useBashRcfile = QFileInfo(QString::fromLocal8Bit(shellPath)).fileName()
                                  == QStringLiteral("bash")
                              && QFileInfo::exists(QString::fromLocal8Bit(bashRcfile));

    // ── forkpty ──────────────────────────────────────────────────────────
    struct winsize ws;
    ws.ws_row    = static_cast<unsigned short>(rows);
    ws.ws_col    = static_cast<unsigned short>(cols);
    ws.ws_xpixel = 0;
    ws.ws_ypixel = 0;

    pid_t pid = forkpty(&ptyFd_, nullptr, nullptr, &ws);
    if (pid < 0) {
        ptyFd_ = -1;
        return false;
    }

    if (pid == 0) {
        // ── child ────────────────────────────────────────────────────────
        setupChildEnvironment();
        if (useBashRcfile) {
            ::execlp(shellPath.constData(), shellPath.constData(), "--rcfile",
                     bashRcfile.constData(), static_cast<char*>(nullptr));
            _exit(127);
        }
        ::execlp(shellPath.constData(), shellPath.constData(),
                 static_cast<char*>(nullptr));
        _exit(127);  // exec 失敗
    }

    // ── parent ───────────────────────────────────────────────────────────
    childPid_         = pid;
    finishedEmitted_  = false;

    // PTY master 設 non-blocking，QSocketNotifier 可放心 drain loop
    if (int flags = ::fcntl(ptyFd_, F_GETFL, 0); flags >= 0) {
        ::fcntl(ptyFd_, F_SETFL, flags | O_NONBLOCK);
    }

    readNotifier_ = new QSocketNotifier(ptyFd_, QSocketNotifier::Read, this);
    connect(readNotifier_, &QSocketNotifier::activated,
            this, &PtyManager::onReadReady);

    return true;
}

void PtyManager::setupChildEnvironment() {
    // POSIX setenv，所有變數 overwrite=1
    ::setenv("TERMINUX_ACTIVE", "1", 1);
    ::setenv("TERMINUX_TRACE_ROOT",   traceRoot_.constData(), 1);
    ::setenv("TERMINUX_TRACE_SELF",   traceSelf_.constData(), 1);
    ::setenv("TERMINUX_TRACE_QUEUE",  "", 1);
    ::setenv("TERMINUX_TRACE_JOB",    "", 1);
    ::setenv("TERMINUX_TRACE_PARENT", "", 1);
    if (!origBashEnv_.isEmpty()) {
        ::setenv("_TERMINUX_ORIG_BASH_ENV", origBashEnv_.constData(), 1);
    }
    ::setenv("BASH_ENV", bashEnv_.constData(), 1);
}

void PtyManager::writeData(const QByteArray& data) {
    if (ptyFd_ < 0 || data.isEmpty()) return;
    ::write(ptyFd_, data.constData(), static_cast<size_t>(data.size()));
}

void PtyManager::resize(int rows, int cols) {
    rows_ = rows;
    cols_ = cols;
    if (ptyFd_ < 0) return;
    struct winsize ws;
    ws.ws_row    = static_cast<unsigned short>(rows);
    ws.ws_col    = static_cast<unsigned short>(cols);
    ws.ws_xpixel = 0;
    ws.ws_ypixel = 0;
    ::ioctl(ptyFd_, TIOCSWINSZ, &ws);
}

bool PtyManager::isRunning() const {
    return ptyFd_ >= 0 && childPid_ > 0 && !finishedEmitted_;
}

pid_t PtyManager::childPid() const {
    return childPid_;
}

void PtyManager::onReadReady() {
    char buf[4096];
    for (;;) {
        ssize_t n = ::read(ptyFd_, buf, sizeof buf);
        if (n > 0) {
            emit dataReady(QByteArray(buf, static_cast<int>(n)));
            continue;  // drain non-blocking buffer
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            return;  // 沒資料了
        }
        // EOF（n == 0）或其他 error（多半是 EIO，表 slave 已關）
        if (readNotifier_) readNotifier_->setEnabled(false);
        if (!finishedEmitted_) {
            finishedEmitted_ = true;
            int exitCode = -1;
            if (childPid_ > 0) {
                int status = 0;
                pid_t r = ::waitpid(childPid_, &status, WNOHANG);
                if (r == 0) {
                    // 還活著 → 短暫 blocking wait 撈 status
                    r = ::waitpid(childPid_, &status, 0);
                }
                if (r == childPid_ && WIFEXITED(status)) {
                    exitCode = WEXITSTATUS(status);
                }
            }
            emit finished(exitCode);
        }
        return;
    }
}

void PtyManager::cleanup() {
    if (readNotifier_) {
        readNotifier_->setEnabled(false);
        delete readNotifier_;
        readNotifier_ = nullptr;
    }
    if (ptyFd_ >= 0) {
        ::close(ptyFd_);
        ptyFd_ = -1;
    }
    if (childPid_ > 0) {
        ::kill(childPid_, SIGHUP);
        // 200ms grace（每 10ms 輪詢一次）
        for (int i = 0; i < 20; ++i) {
            int status = 0;
            pid_t r = ::waitpid(childPid_, &status, WNOHANG);
            if (r != 0) {
                childPid_ = -1;
                return;
            }
            ::usleep(10 * 1000);
        }
        // grace 內沒結束 → SIGKILL + blocking reap
        ::kill(childPid_, SIGKILL);
        ::waitpid(childPid_, nullptr, 0);
        childPid_ = -1;
    }
}

}  // namespace terminux
