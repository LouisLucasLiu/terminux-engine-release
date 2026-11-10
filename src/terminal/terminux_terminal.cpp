// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/terminux_terminal.h"

#include "terminal/chat_query_message.h"
#include "terminal/notification_bar.h"
#include "terminal/pty_manager.h"
#include "terminal/terminal_surface.h"
#include "terminal/vterm_bridge.h"

#include "ipc/jsonrpc_client.h"

#include <QApplication>
#include <QDebug>
#include <QDateTime>
#include <QDesktopServices>
#include <QDir>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonObject>
#include <QMetaObject>
#include <QProcess>
#include <QRegularExpression>
#include <QResizeEvent>
#include <QScrollBar>
#include <QSettings>
#include <QThread>
#include <QTimer>
#include <QUrl>

#include <algorithm>
#include <thread>

#include <unistd.h>
#include <pwd.h>

namespace terminux {

namespace {
constexpr int kInitialRows = 24;
constexpr int kInitialCols = 80;
// 每個 GUI 行程僅顯示一次 csh／tcsh hook 提示。
bool tcshHookHintShown = false;

// preferred language 的 QSettings key（main.cpp 已設 org/app name）。daemon 的
// preferences.json 為單一真實來源；QSettings 僅作本地快取 / daemon 離線 fallback。
constexpr const char* kSettingsKeyLanguage = "preferences/language";
constexpr const char* kDefaultLanguage     = "zh-Hant";
}  // namespace

TerminuxTerminal::TerminuxTerminal(QWidget* parent)
    : QWidget(parent)
{
    pty_       = new PtyManager(this);
    bridge_    = new VTermBridge(kInitialRows, kInitialCols, this);
    surface_   = new TerminalSurface(bridge_, this);
    scrollBar_ = new QScrollBar(Qt::Vertical, this);

    // preferred language：先用 QSettings 快取設 context menu 勾選狀態（即時，
    // 不需等 daemon）。setHookConfig() 後會再向 daemon 查 preferences.get_language
    // 校正（daemon 為單一真實來源）。
    {
        QSettings s;
        surface_->setCurrentLanguage(
            s.value(QString::fromLatin1(kSettingsKeyLanguage),
                    QString::fromLatin1(kDefaultLanguage)).toString());
    }

    // NotificationBar 移植（overlay，parent = TerminuxTerminal，不參與 layout）
    notificationBar_ = new NotificationBar(this);
    notificationBar_->hide();
    connect(notificationBar_, &NotificationBar::actionTriggered,
            this, &TerminuxTerminal::invokePackageAction);

    // 強制全 child NoFocus（QScrollArea default StrongFocus 會搶 keyboard
    // focus，使用者 click NotificationBar 內文後打字會斷。spike 已驗證此 fix）
    notificationBar_->setFocusPolicy(Qt::NoFocus);
    for (QWidget* w : notificationBar_->findChildren<QWidget*>()) {
        w->setFocusPolicy(Qt::NoFocus);
    }

    // layout：QHBoxLayout，surface 左 + scrollbar 右
    auto* layout = new QHBoxLayout(this);
    layout->setContentsMargins(0, 0, 0, 0);
    layout->setSpacing(0);
    layout->addWidget(surface_, /*stretch=*/1);
    layout->addWidget(scrollBar_, /*stretch=*/0);

    scrollBar_->setMinimum(0);
    scrollBar_->setMaximum(0);   // scrollback 還沒長
    scrollBar_->setValue(0);
    scrollBar_->setPageStep(bridge_->rows());
    scrollBar_->setSingleStep(1);

    // PTY 輸出 → libvterm 解析
    connect(pty_, &PtyManager::dataReady,
            bridge_, &VTermBridge::feedData);
    // 同份 PTY data 也送 ring buffer（給 lineage.post_exec stdout_tail）
    connect(pty_, &PtyManager::dataReady,
            this,    &TerminuxTerminal::onPtyDataForBuffer);

    // surface 鍵盤輸入 → PTY
    connect(surface_, &TerminalSurface::keyInput,
            pty_, &PtyManager::writeData);

    // forward signals
    connect(surface_, &TerminalSurface::chatQueryDetected,
            this,     &TerminuxTerminal::chatQueryDetected);
    connect(pty_,     &PtyManager::finished,
            this,     &TerminuxTerminal::shellFinished);

    // Enter 後一般命令送 lineage.pre_exec
    connect(surface_, &TerminalSurface::commandSubmitted,
            this,     &TerminuxTerminal::onCommandSubmitted);

    // @@ → ecc.chat_query + NotificationBar 顯示問答
    connect(surface_, &TerminalSurface::chatQueryDetected,
            this,     &TerminuxTerminal::onChatQuery);

    // scrollback wiring
    connect(bridge_,    &VTermBridge::damageReceived,
            this,       &TerminuxTerminal::onBridgeDamage);
    connect(bridge_,    &VTermBridge::termResized,
            this,       [this](int, int) { syncScrollBarRange(); });
    connect(scrollBar_, &QScrollBar::valueChanged,
            this,       &TerminuxTerminal::onScrollBarValueChanged);
    connect(surface_,   &TerminalSurface::scrollRequested,
            this,       &TerminuxTerminal::onSurfaceScrollRequested);
    connect(surface_,   &TerminalSurface::surfaceResized,
            this,       &TerminuxTerminal::onSurfaceResized);

    // title forward 給 MainWindow setWindowTitle 用；BEL → QApplication::beep
    connect(bridge_, &VTermBridge::titleChanged,
            this,    &TerminuxTerminal::titleChanged);
    connect(bridge_, &VTermBridge::bellRing,
            this,    []() { QApplication::beep(); });

    // OSC 133 prompt markers（CommandEnd → 送 lineage.post_exec 帶 stdout）
    connect(bridge_, &VTermBridge::promptMarker,
            this,    &TerminuxTerminal::onPromptMarker);

    // context menu「Clear Terminal」→ 送 Ctrl+L 給 PTY
    connect(surface_, &TerminalSurface::clearTerminalRequested,
            this,     [this]() {
                if (pty_) pty_->writeData(QByteArrayLiteral("\x0c"));
            });

    // context menu「Notification Bar → Show」→ 重新顯示 NotificationBar（✕ 隱藏後喚回）
    connect(surface_, &TerminalSurface::showNotificationBarRequested,
            this,     [this]() {
                if (notificationBar_) notificationBar_->showBar();
            });

    // context menu「Notification Bar → 移除最舊 N 筆」
    connect(surface_, &TerminalSurface::trimOldestNotificationsRequested,
            this,     [this](int count) {
                if (notificationBar_) notificationBar_->removeOldest(count);
            });

    // context menu「Notification Bar → 移除全部」
    connect(surface_, &TerminalSurface::clearNotificationsRequested,
            this,     [this]() {
                if (notificationBar_) notificationBar_->clearAll();
            });

    // context menu「Language → 選擇語言」→ 持久化 + RPC 通知 daemon
    connect(surface_, &TerminalSurface::languageChangeRequested,
            this,     &TerminuxTerminal::onLanguageChangeRequested);
}

TerminuxTerminal::~TerminuxTerminal()
{
    notifyPollStop_.store(true);
    if (notifyPollThread_.joinable()) {
        notifyPollThread_.join();
    }
}

void TerminuxTerminal::notifyPollLoop()
{
    // 每 ~1.2s 依本 GUI 的 cursor 讀取通知；斷線期間保留 cursor。
    const std::string socketPath = hookConfig_.has_value()
        ? hookConfig_->socket_path : std::string();
    if (socketPath.empty()) {
        return;
    }
    using namespace std::chrono;
    // lineage server 可達性：daemon 剛啟動 ~20s 內跑 _init_ecc、尚未 listen 時連不上
    // （= 暖機中）。以 poll 的 transport_ok 追蹤，變化時通知 MainWindow 顯示提示。
    // 初始未知，確保第一次 poll 一定會把實際狀態通知 MainWindow。
    bool lastAvailable = false;
    qint64 afterSeq = 0;
    QString instanceId;
    while (!notifyPollStop_.load()) {
        // 分段 sleep 讓 dtor 能快速停止。
        for (int i = 0; i < 12 && !notifyPollStop_.load(); ++i) {
            std::this_thread::sleep_for(milliseconds(100));
        }
        if (notifyPollStop_.load()) {
            break;
        }
        terminux::ipc::JsonRpcClientOptions opts;
        opts.socket_path = socketPath;
        // 短 timeout：本機 socket 往返 <50ms 就夠；上限也界定了關閉時 dtor join() 的最長
        // 等待（send_request 阻塞期間無法檢查 notifyPollStop_）。
        opts.timeout_ms  = 1500;
        terminux::ipc::JsonRpcClient client(opts);

        QJsonObject params;
        params[QStringLiteral("terminal_id")] = QStringLiteral("default");
        if (!instanceId.isEmpty()) {
            params[QStringLiteral("after_seq")] = afterSeq;
        }
        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("id")]      = 1;
        req[QStringLiteral("method")]  = QStringLiteral("notify.poll");
        req[QStringLiteral("params")]  = params;

        const auto result = client.send_request(req);
        const bool available = result.transport_ok;
        if (available != lastAvailable) {
            lastAvailable = available;
            // 跨 thread emit：Qt 對不同 thread 的接收者自動改用 queued，安全。
            emit notifyServiceAvailableChanged(available);
        }
        if (available
            && !result.response.contains(QStringLiteral("error"))) {
            const QJsonObject res = result.response
                .value(QStringLiteral("result")).toObject();
            const QString responseInstance =
                res.value(QStringLiteral("instance_id")).toString();
            const QJsonValue latestValue = res.value(QStringLiteral("latest_seq"));
            if (responseInstance.isEmpty() || !latestValue.isDouble()) continue;
            const qint64 latestSeq = latestValue.toInteger(-1);
            if (latestSeq < 0) continue;
            if (instanceId.isEmpty() || instanceId != responseInstance) {
                instanceId = responseInstance;
                afterSeq = latestSeq;
                continue;
            }
            dispatchAgentAsync(res);
            afterSeq = latestSeq;
        }
    }
}

bool TerminuxTerminal::start(const QString& shellProgram)
{
    QString selectedShell = shellProgram;
    if (selectedShell.isEmpty()) {
        selectedShell = qEnvironmentVariable("SHELL");
    }
    if (selectedShell.isEmpty()) {
        const struct passwd* pw = getpwuid(getuid());
        selectedShell = pw && pw->pw_shell && pw->pw_shell[0] != '\0'
            ? QString::fromLocal8Bit(pw->pw_shell) : QStringLiteral("/bin/bash");
    }
    int rows = kInitialRows, cols = kInitialCols;
    calcTermSize(width(), height(), rows, cols);
    bridge_->resize(rows, cols);
    if (!pty_->start(rows, cols, selectedShell)) {
        return false;
    }
    hookMarkerReceived_ = false;
    const QString shellName = QFileInfo(selectedShell).fileName();
    if (shellName == QStringLiteral("tcsh") || shellName == QStringLiteral("csh")) {
        QTimer::singleShot(10000, this, [this]() {
            if (hookMarkerReceived_ || tcshHookHintShown || !pty_->isRunning()) {
                return;
            }
            tcshHookHintShown = true;
            showNotification(NotificationType::Hint, QStringLiteral(
                "啟動後 10 秒內未收到 Terminux hook 的 OSC 133 標記。\n"
                "有 ~/.tcshrc 就加在 ~/.tcshrc，沒有才加在 ~/.cshrc：\n"
                "if ( $?TERMINUX_ACTIVE ) source ~/.terminux/tcsh_hooks.csh"));
        });
    }
    return true;
}

void TerminuxTerminal::writeData(const QByteArray& data)
{
    pty_->writeData(data);
}

void TerminuxTerminal::onSurfaceResized(QSize newSize)
{
    // resize 同步在 surface 真正被 layout 過後才觸發，避免 layout 順序問題
    int rows = bridge_->rows(), cols = bridge_->cols();
    calcTermSize(newSize.width(), newSize.height(), rows, cols);
    if (rows != bridge_->rows() || cols != bridge_->cols()) {
        bridge_->resize(rows, cols);
        pty_->resize(rows, cols);
    }
    scrollBar_->setPageStep(bridge_->rows());

    // NotificationBar overlay 跟著 parent resize 重新定位
    if (notificationBar_ && notificationBar_->isVisible()) {
        notificationBar_->repositionInParent();
        notificationBar_->raise();
    }
}

void TerminuxTerminal::calcTermSize(int widgetW, int widgetH,
                                    int& rows, int& cols) const
{
    if (!surface_ || surface_->cellWidth() <= 0 || surface_->cellHeight() <= 0) {
        return;  // 用呼叫方傳入的預設值
    }
    rows = std::max(1, static_cast<int>(widgetH / surface_->cellHeight()));
    // 右邊 2 字元會被遮：cellWidth 是 qreal，但 paint 時 cell
    // 邊界在 sub-pixel rendering 下會位移擴展（粗體 / italic / glyph 寬於
    // advance 之差），cumulative 造成最後 1-2 cell 跑出 widget right edge。
    // 先 trim 2 cells 做右邊安全 margin（~16px），避免裁切；犧牲 2 cols 可接受。
    const int rawCols = static_cast<int>(widgetW / surface_->cellWidth());
    cols = std::max(1, rawCols - 2);
}

// ── scrollback handlers ─────────────────────────────────────────────────────

void TerminuxTerminal::onBridgeDamage()
{
    // 每次 damage（不論是 scrollback push 還是 cell 更新）都同步 range
    // bridge.scrollbackLineCount() 是 push 後的 latest 值。
    syncScrollBarRange();
}

void TerminuxTerminal::syncScrollBarRange()
{
    const int sbCount = bridge_->scrollbackLineCount();
    const bool wasAtBottom =
        (scrollBar_->value() >= scrollBar_->maximum());

    // 暫時 block signal 避免在這裡改 max 時也觸發 valueChanged 鏈
    const QSignalBlocker blocker(scrollBar_);
    scrollBar_->setMaximum(sbCount);
    scrollBar_->setPageStep(bridge_->rows());

    if (wasAtBottom) {
        scrollBar_->setValue(sbCount);   // 跟隨底部
    }
    // 結尾統一 push offset 到 surface（max 變了即使 value 沒動，offset 也變）
    surface_->setScrollOffset(scrollBar_->maximum() - scrollBar_->value());
}

void TerminuxTerminal::onScrollBarValueChanged(int value)
{
    // user 拖曳 / 程式 setValue 都會走這裡
    surface_->setScrollOffset(scrollBar_->maximum() - value);
}

void TerminuxTerminal::onSurfaceScrollRequested(int linesUp)
{
    // linesUp > 0：滾上去看舊內容 → scrollBar value 減少
    // linesUp < 0：滾回新內容    → scrollBar value 增加
    scrollBar_->setValue(scrollBar_->value() - linesUp);
}

void TerminuxTerminal::setCoreSessionId(const QString& sessionId)
{
    coreSessionId_ = sessionId;
}

void TerminuxTerminal::setHookConfig(const TerminuxHookConfig& cfg)
{
    hookConfig_ = cfg;

    // 啟動 notify poll 背景 thread（socket 已知）；只啟一次。
    if (!notifyPollThread_.joinable()) {
        notifyPollStop_.store(false);
        notifyPollThread_ = std::thread([this]() { notifyPollLoop(); });
    }

    // daemon 連上後查 preferences.get_language 校正 context menu 勾選狀態
    // （daemon 的 preferences.json 為單一真實來源；QSettings 只是離線快取）。
    std::thread([this, cfg]() {
        terminux::ipc::JsonRpcClientOptions opts;
        opts.socket_path = cfg.socket_path;
        opts.timeout_ms  = 3000;
        terminux::ipc::JsonRpcClient client(opts);

        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("id")]      = 1;
        req[QStringLiteral("method")]  = QStringLiteral("preferences.get_language");
        req[QStringLiteral("params")]  = QJsonObject{};

        const auto result = client.send_request(req);
        if (!result.transport_ok
            || result.response.contains(QStringLiteral("error"))) {
            return;   // daemon 不可達 → 維持 QSettings 快取值
        }
        const QString lang = result.response
            .value(QStringLiteral("result")).toObject()
            .value(QStringLiteral("language")).toString();
        if (lang.isEmpty()) return;

        QMetaObject::invokeMethod(this, [this, lang]() {
            if (surface_) surface_->setCurrentLanguage(lang);
            QSettings s;
            s.setValue(QString::fromLatin1(kSettingsKeyLanguage), lang);
            emit languageChanged(lang);
        }, Qt::QueuedConnection);
    }).detach();
}

void TerminuxTerminal::onLanguageChangeRequested(const QString& languageCode)
{
    if (languageCode.isEmpty()) return;

    // 本地即時生效：更新勾選狀態 + QSettings 快取（即使 daemon 離線也記住）。
    if (surface_) surface_->setCurrentLanguage(languageCode);
    {
        QSettings s;
        s.setValue(QString::fromLatin1(kSettingsKeyLanguage), languageCode);
    }
    emit languageChanged(languageCode);

    if (!hookConfig_.has_value()) return;   // 無 IPC：僅本地快取
    const TerminuxHookConfig cfg = hookConfig_.value();

    // 背景送 preferences.set_language（daemon 寫自己 HOME 的 preferences.json）。
    std::thread([languageCode, cfg]() {
        terminux::ipc::JsonRpcClientOptions opts;
        opts.socket_path = cfg.socket_path;
        opts.timeout_ms  = 5000;
        terminux::ipc::JsonRpcClient client(opts);

        QJsonObject params;
        params[QStringLiteral("language")] = languageCode;

        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("id")]      = 1;
        req[QStringLiteral("method")]  = QStringLiteral("preferences.set_language");
        req[QStringLiteral("params")]  = params;

        client.send_request(req);   // 結果非關鍵：本地已生效，daemon 失敗下次再同步
    }).detach();
}

QString TerminuxTerminal::currentCwd() const
{
    if (!pty_) return QDir::currentPath();
    const pid_t pid = pty_->childPid();
    if (pid <= 0) return QDir::currentPath();
    char buf[4096];
    const QByteArray link =
        QStringLiteral("/proc/%1/cwd").arg(static_cast<int>(pid)).toLocal8Bit();
    const ssize_t n = ::readlink(link.constData(), buf, sizeof buf - 1);
    if (n > 0) {
        return QString::fromLocal8Bit(buf, static_cast<int>(n));
    }
    return QDir::currentPath();
}

void TerminuxTerminal::onCommandSubmitted(const QString& command)
{
    // 先記錄 lastCommand_，下次 OSC 133;D 用此 command 送 post_exec
    lastCommand_ = command;

    if (!hookConfig_.has_value()) return;
    if (command.isEmpty())         return;

    // 對齊舊 TerminalWidget post-notification：Enter 已送 PTY，shell 已開跑；
    // 這裡只做事後 lineage check，warn/block 透過 signal 顯示在 NotificationBar。
    const TerminuxHookConfig cfg = hookConfig_.value();
    const QString cwd = currentCwd();

    std::thread([this, command, cwd, cfg]() {
        terminux::ipc::JsonRpcClientOptions opts;
        opts.socket_path = cfg.socket_path;
        opts.timeout_ms  = 30000;
        terminux::ipc::JsonRpcClient client(opts);

        // 手動組 request 帶 source="qt_terminal_typed" — 此處的 command 來自
        // surface::commandSubmitted signal（bridge_->cursorLogicalLineText()
        // strip prompt 後的結果，方案 B）。但**未** 經 shell 展開（tcsh history
        // / alias / glob），所以視為不權威。daemon 端見此 source 會跳過 lineage
        // check 與 nudge，避免用 `!982` 這種未展開字串去匹配 EDA tool。真正的
        // 權威 pre_exec 由 shell hook（bash DEBUG / tcsh postcmd）以
        // source=shell_hook 送出。
        QJsonObject params;
        params[QStringLiteral("command")]     = command;
        params[QStringLiteral("cwd")]         = cwd;
        params[QStringLiteral("ledger_path")] =
            QString::fromStdString(cfg.ledger_path);
        if (cfg.tool_signatures_path.has_value()) {
            params[QStringLiteral("tool_signatures_path")] =
                QString::fromStdString(*cfg.tool_signatures_path);
        }
        if (cfg.lineage_rules_path.has_value()) {
            params[QStringLiteral("lineage_rules_path")] =
                QString::fromStdString(*cfg.lineage_rules_path);
        }
        params[QStringLiteral("source")] = QStringLiteral("qt_terminal_typed");

        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("id")]      = 1;
        req[QStringLiteral("method")]  = QStringLiteral("lineage.pre_exec");
        req[QStringLiteral("params")]  = params;

        const auto result = client.send_request(req);

        QString action;
        QString message;
        QString aiExplanation;   // AI explainer 生成的友善說明（若 daemon 有跑）
        if (result.transport_ok
            && !result.response.contains(QStringLiteral("error")))
        {
            const QJsonObject res = result.response
                .value(QStringLiteral("result")).toObject();
            action  = res.value(QStringLiteral("action")).toString();
            message = res.value(QStringLiteral("message")).toString();
            // explanation 是 {summary, likely_cause, suggested_actions[]}
            // null（explainer 沒開）時 toObject() 回空 object，下面組空字串
            const QJsonValue expVal = res.value(QStringLiteral("explanation"));
            if (expVal.isObject()) {
                const QJsonObject exp = expVal.toObject();
                const QString summary = exp.value(QStringLiteral("summary")).toString();
                const QString cause   = exp.value(QStringLiteral("likely_cause")).toString();
                const QJsonArray acts = exp.value(QStringLiteral("suggested_actions")).toArray();
                QStringList parts;
                if (!summary.isEmpty()) parts << summary;
                if (!cause.isEmpty())   parts << QStringLiteral("Cause: ") + cause;
                if (!acts.isEmpty()) {
                    QStringList actLines;
                    for (const auto& v : acts) actLines << QStringLiteral("• ") + v.toString();
                    parts << QStringLiteral("Suggested:\n") + actLines.join(QStringLiteral("\n"));
                }
                aiExplanation = parts.join(QStringLiteral("\n\n"));
            }
        }

        // 結果回 main thread，emit signals + 顯示 NotificationBar
        QMetaObject::invokeMethod(this,
            [this, command, action, message, aiExplanation]() {
                if (action == QStringLiteral("block")) {
                    showNotification(NotificationType::Warn,
                                     QStringLiteral("[BLOCKED] %1").arg(message));
                    emit preExecBlocked(command, message);
                } else if (action == QStringLiteral("warn")) {
                    if (!message.isEmpty()) {
                        showNotification(NotificationType::Warn, message);
                    }
                    emit preExecWarned(command, message);
                } else {
                    emit preExecAllowed(command);
                }
                // AI 解釋（若有）獨立一條 NotificationBar 訊息（AI 類型）
                if (!aiExplanation.isEmpty()) {
                    showNotification(NotificationType::AI, aiExplanation);
                }
            },
            Qt::QueuedConnection);
    }).detach();
}

void TerminuxTerminal::onChatQuery(const QString& query, ChatQueryOrigin origin)
{
    if (query.isEmpty()) return;

    // 即時回饋：顯示使用者的問題
    showNotification(NotificationType::Hint,
                     QStringLiteral("Q: %1").arg(query));

    // 沒有 IPC config（測試或 daemon 不在線）→ 顯示佔位回覆，不送 IPC
    if (!hookConfig_.has_value()) {
        showNotification(NotificationType::AI,
                         QStringLiteral("(IPC 未啟用 — 無法送 chat query)"));
        return;
    }

    const TerminuxHookConfig cfg = hookConfig_.value();
    const QString cwd = currentCwd();
    // `ecc.chat_query` 是 context-required method；把 Core
    // session.init 取得的 session id（由 MainWindow::setCoreSessionId 傳入）
    // 帶進 request 的 terminux_context.session_id。沒有 session（Core 未連線
    // 或尚未完成 session.init）時不帶這個欄位，daemon 端會回明確的
    // SESSION_CONTEXT_REQUIRED 錯誤，不在 client 端猜測或造假。
    const QString coreSessionId = coreSessionId_;
    // 來源在 GUI 端就定案：daemon 只在 at_command 時把 `/` 開頭文字當作本機指令。
    const QString originName = chatQueryOriginName(origin);

    std::thread([this, query, cwd, cfg, coreSessionId, originName]() {
        terminux::ipc::JsonRpcClientOptions opts;
        opts.socket_path = cfg.socket_path;
        opts.timeout_ms  = 120000;   // chat query 可能要等 LLM
        terminux::ipc::JsonRpcClient client(opts);

        QJsonObject params;
        params[QStringLiteral("query")]  = query;
        params[QStringLiteral("cwd")]    = cwd;
        params[QStringLiteral("origin")] = originName;

        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("id")]      = 1;
        req[QStringLiteral("method")]  = QStringLiteral("ecc.chat_query");
        req[QStringLiteral("params")]  = params;
        if (!coreSessionId.isEmpty()) {
            QJsonObject terminuxContext;
            terminuxContext[QStringLiteral("session_id")] = coreSessionId;
            req[QStringLiteral("terminux_context")] = terminuxContext;
        }

        const auto result = client.send_request(req);

        // 連不上 daemon／JSON-RPC error／answer 為空 各自顯示不同訊息。
        const terminux::ChatQueryOutcome outcome =
            terminux::classifyChatQueryResponse(result);
        const QString answer = outcome.message;
        // 先前對話未納入本次 AI 請求的提示獨立成一則，排在回答之前；它不是回答的一部分。
        const QString contextNotice = outcome.contextNotice;
        if (outcome.kind == terminux::ChatQueryOutcome::Kind::Answer
            || outcome.kind == terminux::ChatQueryOutcome::Kind::EmptyAnswer)
        {
            const QJsonObject& res = outcome.result;
            // 報表 HTML 自動開啟：daemon 生成報表（如 lineage graph）後回
            // open_path（本機絕對路徑），在使用者 GUI display 用預設瀏覽器開啟。
            // 用 QUrl::fromLocalFile 處理路徑編碼（空白等）。
            const QString openPath =
                res.value(QStringLiteral("open_path")).toString();
            if (!openPath.isEmpty()) {
                QMetaObject::invokeMethod(this, [openPath]() {
                    QDesktopServices::openUrl(QUrl::fromLocalFile(openPath));
                }, Qt::QueuedConnection);
            }

            const QString viewerError = launchViewer(
                res.value(QStringLiteral("launch_viewer")).toObject(),
                QString::fromStdString(cfg.socket_path));
            if (!viewerError.isEmpty()) {
                QMetaObject::invokeMethod(this, [this, viewerError]() {
                    showNotification(NotificationType::Warn, viewerError);
                }, Qt::QueuedConnection);
            }
        }

        QMetaObject::invokeMethod(this,
            [this, answer, contextNotice]() {
                if (!contextNotice.isEmpty()) {
                    showNotification(NotificationType::Hint, contextNotice);
                }
                showNotification(NotificationType::AI, answer);
            },
            Qt::QueuedConnection);
    }).detach();
}

void TerminuxTerminal::showNotification(NotificationType type,
                                        const QString& message)
{
    if (!notificationBar_) return;
    notificationBar_->appendMessage(type, message);
    // appendMessage 內部會 expand_=true + repositionInParent + show + raise
}

QString TerminuxTerminal::launchViewer(const QJsonObject& request,
                                       const QString& socketPath)
{
    if (request.isEmpty()) return {};
    QString viewerBin = qEnvironmentVariable("TERMINUX_DQE_VIEWER_BIN");
    if (viewerBin.isEmpty()) {
        viewerBin = QDir(QCoreApplication::applicationDirPath()).filePath(QStringLiteral("dqe_viewer"));
    }
    const QFileInfo viewerInfo(viewerBin);
    if (viewerBin.isEmpty() || !viewerInfo.isFile() || !viewerInfo.isExecutable()) {
        return QStringLiteral(
            "找不到可執行的 DQE Viewer：%1（可用 TERMINUX_DQE_VIEWER_BIN 覆寫）").arg(viewerBin);
    }
    const QString dbPath = request.value(QStringLiteral("db")).toString();
    const QString temporaryDbPath =
        request.value(QStringLiteral("temporary_db_path")).toString();
    if (dbPath.isEmpty() || temporaryDbPath.isEmpty() || socketPath.isEmpty()) {
        return QStringLiteral("DQE Viewer 啟動參數不完整");
    }
    const QStringList arguments{
        QStringLiteral("--db"), dbPath,
        QStringLiteral("--temporary-db-path"), temporaryDbPath,
        QStringLiteral("--ipc-endpoint"),
        socketPath,
    };
    if (!QProcess::startDetached(viewerInfo.absoluteFilePath(), arguments)) {
        return QStringLiteral("DQE Viewer 無法啟動");
    }
    return {};
}

// ── notify.poll notifications → NotificationBar(AI) ───────────────────
//
// notifications shape（來自 notify.poll）：
//   [{ "answer": str,            // LLM 自然語言回覆（顯示主體）
//      "trigger": str,           // 起源（目前主要是 "command_buffer"）
//      "task_id": str,           // 對應 event_store agent_analysis
//      "evidence_count": int,    // 用了幾個 tool
//      "elapsed_sec": float }]   // 跑了幾秒
//
// 空陣列表示目前沒有新通知；結構化通知與 Agent 回答共用此陣列。
void TerminuxTerminal::dispatchAgentAsync(const QJsonObject& rpcResult)
{
    if (!rpcResult.contains(QStringLiteral("notifications"))) return;

    const QJsonArray asyncArr =
        rpcResult.value(QStringLiteral("notifications")).toArray();
    if (asyncArr.isEmpty()) return;

    for (const QJsonValue& val : asyncArr) {
        const QJsonObject item = val.toObject();
        if (item.value(QStringLiteral("type")).toString()
            == QStringLiteral("notification"))
        {
            dispatchStructuredNotification(item);
            continue;
        }

        const QString answer = item.value(QStringLiteral("answer")).toString();
        if (answer.isEmpty()) continue;

        const int evidenceCount =
            item.value(QStringLiteral("evidence_count")).toInt(0);
        const double elapsed =
            item.value(QStringLiteral("elapsed_sec")).toDouble(0.0);

        QString displayText = answer;
        if (evidenceCount > 0 || elapsed > 0.0) {
            displayText = QStringLiteral("[%1 tools, %2s] %3")
                .arg(evidenceCount)
                .arg(elapsed, 0, 'f', 1)
                .arg(answer);
        }

        // 從任意 thread 呼叫都安全：queue 到 main thread 跑 showNotification
        // （showNotification 直接動 QWidget，必須在 main thread）。
        QMetaObject::invokeMethod(this,
            [this, displayText]() {
                showNotification(NotificationType::AI, displayText);
            },
            Qt::QueuedConnection);
    }
}

NotificationType TerminuxTerminal::notificationTypeFromLevel(const QString& level)
{
    if (level == QStringLiteral("error")) return NotificationType::Warn;
    if (level == QStringLiteral("warning")) return NotificationType::Warn;
    if (level == QStringLiteral("info")) return NotificationType::Hint;
    return NotificationType::AI;
}

void TerminuxTerminal::dispatchStructuredNotification(const QJsonObject& item)
{
    const QString message = item.value(QStringLiteral("message")).toString();
    if (message.isEmpty()) return;

    QList<NotificationAction> actions;
    const QJsonArray rawActions = item.value(QStringLiteral("actions")).toArray();
    for (const QJsonValue& rawActionValue : rawActions) {
        const QJsonObject rawAction = rawActionValue.toObject();
        const QString label = rawAction.value(QStringLiteral("label")).toString();
        if (label.isEmpty()) continue;

        NotificationAction action;
        action.label = label;
        const QJsonValue actionValue = rawAction.value(QStringLiteral("action"));
        action.action = actionValue.isString() ? actionValue.toString() : QString();
        const QJsonValue argsValue = rawAction.value(QStringLiteral("args"));
        action.args = argsValue.isObject() ? argsValue.toObject() : QJsonObject{};
        actions.append(action);
    }

    const QString title = item.value(QStringLiteral("title")).toString();
    const QString displayText = title.isEmpty()
        ? message
        : QStringLiteral("%1\n%2").arg(title, message);
    const QString notificationId =
        item.value(QStringLiteral("notification_id")).toString();
    const NotificationType type =
        notificationTypeFromLevel(item.value(QStringLiteral("level")).toString());

    QMetaObject::invokeMethod(this,
        [this, type, displayText, notificationId, actions]() {
            if (!notificationBar_) return;
            if (actions.isEmpty()) {
                notificationBar_->appendMessage(type, displayText);
            } else {
                notificationBar_->appendMessageWithActions(
                    type, displayText, notificationId, actions);
            }
        },
        Qt::QueuedConnection);
}

void TerminuxTerminal::invokePackageAction(const QString& notificationId,
                                           const QString& action,
                                           const QJsonObject& args)
{
    if (notificationId.isEmpty() || action.isEmpty()) return;
    if (!hookConfig_.has_value()) {
        showNotification(NotificationType::Warn,
                         QStringLiteral("IPC 未啟用，無法執行通知動作：%1").arg(action));
        return;
    }

    const TerminuxHookConfig cfg = hookConfig_.value();
    std::thread([this, cfg, notificationId, action, args]() {
        terminux::ipc::JsonRpcClientOptions opts;
        opts.socket_path = cfg.socket_path;
        opts.timeout_ms  = 30000;
        terminux::ipc::JsonRpcClient client(opts);

        QJsonObject params;
        params[QStringLiteral("notification_id")] = notificationId;
        params[QStringLiteral("action")] = action;
        params[QStringLiteral("args")] = args;

        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("id")]      = 1;
        req[QStringLiteral("method")]  = QStringLiteral("package.invoke_action");
        req[QStringLiteral("params")]  = params;

        const auto result = client.send_request(req);
        QString message;
        NotificationType type = NotificationType::Hint;
        if (!result.transport_ok) {
            type = NotificationType::Warn;
            message = QStringLiteral("通知動作失敗：%1")
                .arg(QString::fromStdString(result.error_message));
        } else if (result.response.contains(QStringLiteral("error"))) {
            type = NotificationType::Warn;
            const QJsonObject err = result.response.value(QStringLiteral("error")).toObject();
            message = QStringLiteral("通知動作被拒：%1")
                .arg(err.value(QStringLiteral("message")).toString());
        } else {
            const QJsonObject res = result.response.value(QStringLiteral("result")).toObject();
            const QString status = res.value(QStringLiteral("status")).toString();
            if (status == QStringLiteral("ok")) {
                const QString viewerError = launchViewer(
                    res.value(QStringLiteral("launch_viewer")).toObject(),
                    QString::fromStdString(cfg.socket_path));
                if (!viewerError.isEmpty()) {
                    type = NotificationType::Warn;
                    message = viewerError;
                } else {
                    message = QStringLiteral("通知動作已執行：%1").arg(action);
                }
            } else {
                type = NotificationType::Warn;
                const QString reason = res.value(QStringLiteral("reason")).toString();
                message = QStringLiteral("通知動作未執行：%1").arg(
                    reason.isEmpty() ? status : reason);
            }
        }
        QMetaObject::invokeMethod(this,
            [this, type, message]() {
                if (!message.isEmpty()) showNotification(type, message);
            },
            Qt::QueuedConnection);
    }).detach();
}

// ── PTY ring buffer + OSC 133 prompt markers ────────────────────────────────

void TerminuxTerminal::onPtyDataForBuffer(const QByteArray& data)
{
    // 簡易 ANSI strip（regex；需要時可改為 state machine）
    //   CSI sequences：\x1b[ ... letter
    //   OSC sequences：\x1b] ... (\x07 | \x1b\\)
    //   也含 OSC 133 marker 自己（不應出現在 ring buffer）
    static const QRegularExpression ansiRe(
        QStringLiteral(
            R"(\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))"
        )
    );

    QString text = QString::fromUtf8(data);
    text.remove(ansiRe);

    for (QChar ch : text) {
        const ushort u = ch.unicode();
        if (u == '\n') {
            stdoutRingBuffer_.append(currentStdoutLine_);
            currentStdoutLine_.clear();
            while (stdoutRingBuffer_.size() > kMaxStdoutLines) {
                stdoutRingBuffer_.removeFirst();
            }
        } else if (u == '\r') {
            // CR：回行首（覆寫模式，下次寫入會從頭蓋）
            currentStdoutLine_.clear();
        } else if (u == '\b') {
            if (!currentStdoutLine_.isEmpty()) currentStdoutLine_.chop(1);
        } else if (u >= 0x20 || u == '\t') {
            currentStdoutLine_ += ch;
        }
        // 其他控制字元跳過
    }
}

void TerminuxTerminal::onPromptMarker(VTermBridge::PromptMarkerType type,
                                      int exitCode)
{
    hookMarkerReceived_ = true;
    using PMT = VTermBridge::PromptMarkerType;
    switch (type) {
    case PMT::CommandBegin:
        // user 確定執行命令 → 清 ring buffer，新命令的 stdout 從頭累積
        stdoutRingBuffer_.clear();
        currentStdoutLine_.clear();
        break;
    case PMT::CommandEnd:
        // 命令結束 → 把 ring buffer 連同 lastCommand_ + exitCode 送 lineage.post_exec
        sendPostExecWithStdout(exitCode);
        break;
    case PMT::PrePrompt:
    default:
        // 不做事
        break;
    }
}

void TerminuxTerminal::sendPostExecWithStdout(int exitCode)
{
    if (!hookConfig_.has_value()) {
        // 無 IPC config → 仍要清 buffer（下個命令乾淨）
        stdoutRingBuffer_.clear();
        currentStdoutLine_.clear();
        return;
    }
    if (lastCommand_.isEmpty()) {
        stdoutRingBuffer_.clear();
        currentStdoutLine_.clear();
        return;
    }

    // snapshot（容入 thread）
    const TerminuxHookConfig cfg = hookConfig_.value();
    const QString  command = lastCommand_;
    const QString  cwd     = currentCwd();
    QStringList    stdout_tail = stdoutRingBuffer_;
    if (!currentStdoutLine_.isEmpty()) {
        stdout_tail.append(currentStdoutLine_);
    }

    // 一次性消費 lastCommand_：清空，避免同一命令被重複送 post_exec。
    // shell-integration 每畫一次 prompt 都會吐 OSC 133;D，空 Enter（不觸發
    // commandSubmitted → lastCommand_ 不更新）會讓殘餘 D marker 拿舊命令重送，
    // 造成「每按一次 Enter 就多一筆 record」。清空後多餘 D marker 撞上方
    // lastCommand_.isEmpty() guard 而 skip。
    lastCommand_.clear();

    // 立即清空 buffer（已經 snapshot 給 thread；下一個命令的 stdout 從頭累積）
    stdoutRingBuffer_.clear();
    currentStdoutLine_.clear();

    std::thread([this, command, cwd, cfg, exitCode, stdout_tail]() {
        terminux::ipc::JsonRpcClientOptions opts;
        opts.socket_path = cfg.socket_path;
        opts.timeout_ms  = 30000;
        terminux::ipc::JsonRpcClient client(opts);

        // 建 stdout_tail QJsonArray（daemon 端期望 list[str]）
        QJsonArray jsonStdout;
        for (const QString& line : stdout_tail) jsonStdout.append(line);

        QJsonObject params;
        params[QStringLiteral("command")]     = command;
        params[QStringLiteral("cwd")]         = cwd;
        params[QStringLiteral("exit_code")]   = exitCode;
        params[QStringLiteral("ledger_path")] =
            QString::fromStdString(cfg.ledger_path);
        if (cfg.tool_signatures_path.has_value()) {
            params[QStringLiteral("tool_signatures_path")] =
                QString::fromStdString(*cfg.tool_signatures_path);
        }
        params[QStringLiteral("stdout_tail")] = jsonStdout;
        params[QStringLiteral("source")]      = QStringLiteral("qt_terminal");
        params[QStringLiteral("ts")] =
            static_cast<double>(QDateTime::currentMSecsSinceEpoch()) / 1000.0;

        QJsonObject req;
        req[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
        req[QStringLiteral("id")]      = 1;
        req[QStringLiteral("method")]  = QStringLiteral("lineage.post_exec");
        req[QStringLiteral("params")]  = params;

        const auto result = client.send_request(req);

        QString eccAction, eccMessage, eccNudge;
        if (result.transport_ok
            && !result.response.contains(QStringLiteral("error")))
        {
            const QJsonObject res = result.response
                .value(QStringLiteral("result")).toObject();
            eccAction  = res.value(QStringLiteral("ecc_action")).toString();
            eccMessage = res.value(QStringLiteral("ecc_message")).toString();
            eccNudge   = res.value(QStringLiteral("ecc_nudge")).toString();
        }

        // 結果回 main thread → NotificationBar 顯示
        QMetaObject::invokeMethod(this,
            [this, eccAction, eccMessage, eccNudge]() {
                if (eccAction == QStringLiteral("warn") && !eccMessage.isEmpty()) {
                    showNotification(NotificationType::Gate, eccMessage);
                }
                // nudge 獨立顯示（即使 gate pass 也可能有建議）
                if (!eccNudge.isEmpty()
                    && !eccMessage.contains(eccNudge))   // pipeline 內已合併時不重覆
                {
                    showNotification(NotificationType::Hint, eccNudge);
                }
            },
            Qt::QueuedConnection);
    }).detach();
}

}  // namespace terminux
