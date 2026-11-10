// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/main_window.h"

#include "terminal/terminux_terminal.h"
#include "terminal/work_dock.h"
#include "ipc/agent_socket_path.h"
#include "ipc/project_env.h"
#include "ipc/ipc_client.h"

#include <QAction>
#include <QApplication>
#include <QDebug>
#include <QDir>
#include <QHBoxLayout>
#include <QLocalSocket>
#include <QMenu>
#include <QMenuBar>
#include <QMessageBox>
#include <QSettings>
#include <QStatusBar>
#include <QTimer>

// ── Constructor / Destructor ──────────────────────────────────────────────────

MainWindow::MainWindow(QWidget* parent)
    : QMainWindow(parent)
{
    setupUi();
    connectSignals();
    attemptReconnect();
}

MainWindow::~MainWindow() = default;

// ── UI 初始化 ──────────────────────────────────────────────────────────────────

void MainWindow::setupUi()
{
    setWindowTitle(QStringLiteral("Terminux"));
    resize(1200, 800);

    auto* central = new QWidget(this);
    setCentralWidget(central);

    auto* hbox = new QHBoxLayout(central);
    hbox->setContentsMargins(0, 0, 0, 0);
    hbox->setSpacing(0);

    // ── Terminal（libvterm + Qt 自繪）──────────────────────────────────────────
    // 使用者特定 IPC config（路徑 user-scope 避免 cross-user 寫入衝突）
    terminux::TerminuxHookConfig cfg;
    const auto lineageEndpoint = terminux::resolveLineageSocketPath(false);
    if (!lineageEndpoint.ok) qWarning("%s", qPrintable(lineageEndpoint.message));
    cfg.socket_path = lineageEndpoint.path.toStdString();
    cfg.ledger_path =
        (QDir::homePath() + QStringLiteral("/.terminux/lineage/ledger.jsonl")).toStdString();

    terminal_ = new terminux::TerminuxTerminal(central);
    terminal_->setHookConfig(cfg);
    hbox->addWidget(terminal_, /*stretch=*/1);

    // PTY 啟動 deferred 到 layout 完成後（避免 cellWidth_ 為 0 時誤算 rows/cols）
    QTimer::singleShot(0, terminal_, [this]() {
        if (terminal_) terminal_->start();
    });

    // preferred language 初值（與 TerminuxTerminal 同一 QSettings key）；語言變更時
    // 重繪 title bar 的 Core 未連線警告。
    {
        QSettings s;
        language_ = s.value(QStringLiteral("preferences/language"),
                            QStringLiteral("zh-Hant")).toString();
    }

    // window title 跟著 OSC 0/2 變（經 onTerminalTitle 與 Core 連線警告合併）
    connect(terminal_, &terminux::TerminuxTerminal::titleChanged,
            this,      &MainWindow::onTerminalTitle);
    connect(terminal_, &terminux::TerminuxTerminal::languageChanged,
            this,      &MainWindow::onLanguageChanged);
    connect(terminal_, &terminux::TerminuxTerminal::notifyServiceAvailableChanged,
            this,      &MainWindow::onNotifyServiceAvailable);

    // ── IPC client（session 級 connect / sessionInit / notification）──────────
    // Core（agent）socket 依目前 UID 查帳號資料解析成
    // <home>/.terminux/run/<nodename>/terminux.sock，沒有舊路徑 fallback。
    // TERMINUX_CORE_SOCK 只是明確的隔離部署覆寫（整合測試用）。解析失敗時
    // 不連線、在 title bar 顯示原因（含 uid 與預期路徑），並由 reconnect
    // timer 定期重新解析（daemon 起來後目錄才會出現）。
    resolveCoreSocketPath();
    ipc_ = new IpcClient(this);
    workDock_ = new WorkDock(this);
    workDock_->setLineageSocketPath(lineageEndpoint.path);
    addDockWidget(Qt::RightDockWidgetArea, workDock_);
    workDock_->hide();

    QMenu* viewMenu = menuBar()->addMenu(tr("&View"));
    QAction* workPanelAction = workDock_->toggleViewAction();
    workPanelAction->setText(tr("&Work Panel"));
    workPanelAction->setObjectName(QStringLiteral("workPanelAction"));
    viewMenu->addAction(workPanelAction);

    // Core（daemon）未啟動時每 3 秒重試；連上後於 onConnected() 停止。
    reconnectTimer_ = new QTimer(this);
    reconnectTimer_->setInterval(3000);
    connect(reconnectTimer_, &QTimer::timeout, this, &MainWindow::attemptReconnect);

    // ── Status bar ────────────────────────────────────────────────────────────
    statusBar()->showMessage(QStringLiteral("Connecting to Terminux Core…"));

    // ── Help Menu ─────────────────────────────────────────────────────────────
    QMenu* helpMenu = menuBar()->addMenu(tr("&Help"));
    QAction* aboutAction = helpMenu->addAction(tr("About &Terminux"));
    connect(aboutAction, &QAction::triggered, this, [this]() {
        QMessageBox aboutBox(this);
        aboutBox.setWindowTitle(tr("About Terminux"));
        aboutBox.setTextFormat(Qt::RichText);
        aboutBox.setText(
            QStringLiteral("<h3>Terminux %1</h3>"
                           "<p>Copyright 2026 Liu, Hsien-Ming (Louis Liu)<br>"
                           "License: PolyForm Shield 1.0.0 "
                           "(<a href=\"https://polyformproject.org/licenses/shield/1.0.0/\">"
                           "https://polyformproject.org/licenses/shield/1.0.0/</a>)<br>"
                           "Contact: <a href=\"mailto:louis.cat.work@gmail.com\">"
                           "louis.cat.work@gmail.com</a></p>").arg(QStringLiteral(TERMINUX_VERSION))
        );
        aboutBox.exec();
    });
    QAction* aboutQtAction = helpMenu->addAction(tr("About &Qt"));
    connect(aboutQtAction, &QAction::triggered, qApp, &QApplication::aboutQt);
}

void MainWindow::connectSignals()
{
    connect(ipc_, &IpcClient::connected,
            this, &MainWindow::onConnected);
    connect(ipc_, &IpcClient::sessionInitResult,
            this, &MainWindow::onSessionInitResult);
    connect(ipc_, &IpcClient::notificationReceived,
            this, &MainWindow::onNotificationReceived);
    // daemon 未啟動：connectToCore 會發 socketError（ServerNotFound / ConnectionRefused）；
    // daemon 中途掛掉：發 disconnected。兩者都視為 Core 未連線，於 title bar 提醒。
    connect(ipc_, &IpcClient::disconnected,
            this, &MainWindow::onCoreDisconnected);
    connect(ipc_, &IpcClient::socketError, this,
            [this](QLocalSocket::LocalSocketError, const QString&) { onCoreDisconnected(); });
}

// ── IpcClient → MainWindow ──────────────────────────────────────────────────

void MainWindow::onConnected()
{
    coreConnected_ = true;
    if (reconnectTimer_) reconnectTimer_->stop();
    refreshWindowTitle();

    statusBar()->showMessage(QStringLiteral("Initialising session…"));

    const QByteArray userEnv  = qgetenv("USER");
    const QByteArray shellEnv = qgetenv("SHELL");

    const QString memberId  = userEnv.isEmpty()
        ? QStringLiteral("user") : QString::fromLocal8Bit(userEnv);
    const QString shell     = shellEnv.isEmpty()
        ? QStringLiteral("/bin/bash") : QString::fromLocal8Bit(shellEnv);
    const QString workDir   = QDir::currentPath();

    // TERMINUX_PROJECT_ID（舊名 TERMINUX_PROJECT 相容）。
    // 未設定 ⇒ 不宣告 project（session 之後可採用 Profile 的 project）；兩者
    // 衝突或值不合法 ⇒ 明確回報並**不**建立 session（不猜一個值）。
    const terminux::ProjectDeclaration project = terminux::resolveProjectDeclarationFromEnvironment();
    if (!project.ok) {
        projectEnvError_ = QStringLiteral("%1: %2").arg(project.code, project.message);
        statusBar()->showMessage(projectEnvError_);
        refreshWindowTitle();
        return;
    }
    projectEnvError_.clear();
    if (!project.warning.isEmpty()) {
        statusBar()->showMessage(project.warning);
    }
    ipc_->sessionInit(memberId, project.declared ? project.projectId : QString(),
                      shell, workDir);
}

void MainWindow::onSessionInitResult(const QString& sessionId)
{
    const QByteArray userEnv = qgetenv("USER");
    const QString member = userEnv.isEmpty()
        ? QStringLiteral("user") : QString::fromLocal8Bit(userEnv);

    statusBar()->showMessage(
        QStringLiteral("[%1]  session %2…").arg(member, sessionId.left(8))
    );

    // 把 Core（agent socket）session.init 取得的 session id
    // 傳給 TerminuxTerminal，讓 lineage socket（ecc.chat_query／`@@`）的
    // request 能附上 terminux_context.session_id——daemon 端會依此 session id
    // 對照發起這條 lineage 連線的 peer UID，解析出同一份 canonical
    // request context（見 ipc/session_registry.py）。
    if (terminal_) terminal_->setCoreSessionId(sessionId);
    if (workDock_) workDock_->setSessionId(sessionId);
}

void MainWindow::onNotificationReceived(const QString& notifyType,
                                        const QString& title,
                                        const QString& content,
                                        const QString& /*detail*/,
                                        const QString& /*timestamp*/)
{
    // notify_statusbar：daemon 主動推的 status 訊息（如 plugin 告知狀態）
    if (notifyType == QStringLiteral("notify_statusbar")
          || notifyType == QStringLiteral("statusbar"))
    {
        const QString msg = title.isEmpty()
            ? content
            : QStringLiteral("%1: %2").arg(title, content);
        statusBar()->showMessage(msg, 8000);
    }
    else if (notifyType == QStringLiteral("notify_inline"))
    {
        const QString msg = title.isEmpty()
            ? content
            : QStringLiteral("%1: %2").arg(title, content);
        terminal_->showNotification(NotificationType::Warn, msg);
    }
    else if (notifyType == QStringLiteral("suggest_command"))
    {
        // 只顯示 reason 與 command，不寫入 PTY，也不建立執行按鈕。
        const QString msg = title.isEmpty()
            ? content
            : QStringLiteral("%1\n%2").arg(title, content);
        terminal_->showNotification(NotificationType::Hint, msg);
    }
}

// ── Core 連線狀態 → title bar 提醒 ────────────────────────────────────────────

void MainWindow::onCoreDisconnected()
{
    const bool wasConnected = coreConnected_;
    coreConnected_ = false;
    refreshWindowTitle();

    // Core 連線斷開時，IPCServer 端的 session 已經被清理
    // （見 ipc/server.py 的 disconnect cleanup），本地保留的 session id 不再
    // 有效；清空避免 lineage request 帶著一個已撤銷的 session id。
    if (terminal_) terminal_->setCoreSessionId(QString());
    if (workDock_) workDock_->setSessionId(QString());

    // C++ terminal 不會自動 spawn daemon；提示使用者手動啟動（隨語言）。
    QString startHint;
    if (language_ == QLatin1String("en")) {
        startHint = QStringLiteral(
            "⚠ Cannot connect to Terminux Core — start the daemon (python -m terminux_engine)");
    } else if (language_ == QLatin1String("zh-Hans")) {
        startHint = QStringLiteral(
            "⚠ 无法连接 Terminux Core：请先启动 daemon（python -m terminux_engine）");
    } else {
        startHint = QStringLiteral(
            "⚠ 無法連線 Terminux Core：請先啟動 daemon（python -m terminux_engine）");
    }
    statusBar()->showMessage(startHint);

    // 反覆的 socketError 不重複 restart；沒在跑才啟動重連輪詢。
    if (reconnectTimer_ && !reconnectTimer_->isActive()) {
        reconnectTimer_->start();
    }
    Q_UNUSED(wasConnected);
}

void MainWindow::onTerminalTitle(const QString& title)
{
    shellTitle_ = title.isEmpty() ? QStringLiteral("Terminux") : title;
    refreshWindowTitle();
}

QString MainWindow::coreDisconnectedWarning() const
{
    // 依 preferred language 回傳警告文字（預設 zh-Hant）。socket 路徑解析
    // 失敗時附上錯誤碼與預期路徑（含 uid），讓使用者知道該以哪個帳號起 daemon。
    QString detail;
    if (!coreSocketError_.isEmpty()) {
        detail = QStringLiteral(" [%1]").arg(coreSocketError_);
    }
    if (language_ == QLatin1String("en")) {
        return QStringLiteral("⚠ Terminux Core not connected (daemon not running)") + detail;
    }
    if (language_ == QLatin1String("zh-Hans")) {
        return QStringLiteral("⚠ Terminux Core 未连线（daemon 未启动）") + detail;
    }
    return QStringLiteral("⚠ Terminux Core 未連線（daemon 未啟動）") + detail;
}

void MainWindow::resolveCoreSocketPath()
{
    const terminux::AgentSocketResolution r = terminux::resolveAgentSocketPath();
    if (r.ok) {
        coreSocketPath_ = r.path;
        coreSocketError_.clear();
        return;
    }
    coreSocketPath_.clear();
    // 只帶錯誤碼與預期路徑；訊息本身已由 resolver 保證不含其他帳號的資訊。
    coreSocketError_ = QStringLiteral("%1 expected=%2").arg(r.code, r.expectedPath);
}

QString MainWindow::coreWarmingUpText() const
{
    // daemon 已連上但 lineage/通知服務仍在 _init_ecc 暖機（~20s）；提示使用者稍候。
    if (language_ == QLatin1String("en")) {
        return QStringLiteral("⏳ Terminux service initializing, please wait…");
    }
    if (language_ == QLatin1String("zh-Hans")) {
        return QStringLiteral("⏳ Terminux 服务初始化中，请稍候…");
    }
    return QStringLiteral("⏳ Terminux 服務初始化中，請稍候…");
}

void MainWindow::refreshWindowTitle()
{
    if (!projectEnvError_.isEmpty()) {
        // project 環境變數衝突／不合法：session 沒有建立，明確顯示原因。
        setWindowTitle(QStringLiteral("⚠ [%1] — %2").arg(projectEnvError_, shellTitle_));
    } else if (!coreConnected_) {
        // 連 Core（session socket）都連不上：daemon 未啟動。
        setWindowTitle(coreDisconnectedWarning() + QStringLiteral(" — ") + shellTitle_);
    } else if (!notifyServiceReady_) {
        // Core 連上但 lineage/通知服務仍在暖機（此期間 EDA 通知還不會彈）。
        setWindowTitle(coreWarmingUpText() + QStringLiteral(" — ") + shellTitle_);
    } else {
        setWindowTitle(shellTitle_);
    }
}

void MainWindow::onNotifyServiceAvailable(bool available)
{
    if (available == notifyServiceReady_) {
        return;
    }
    notifyServiceReady_ = available;
    refreshWindowTitle();
    if (available) {
        QString ready;
        if (language_ == QLatin1String("en")) {
            ready = QStringLiteral("Terminux service ready");
        } else if (language_ == QLatin1String("zh-Hans")) {
            ready = QStringLiteral("Terminux 服务已就绪");
        } else {
            ready = QStringLiteral("Terminux 服務已就緒");
        }
        statusBar()->showMessage(ready, 4000);
    } else {
        statusBar()->showMessage(coreWarmingUpText());
    }
}

void MainWindow::onLanguageChanged(const QString& languageCode)
{
    if (languageCode.isEmpty() || languageCode == language_) {
        return;
    }
    language_ = languageCode;
    refreshWindowTitle();
}

void MainWindow::attemptReconnect()
{
    if (coreConnected_) {
        reconnectTimer_->stop();
        return;
    }
    // 每次重試都重新解析：daemon 起來後 <home>/.terminux/ 與 socket 才存在。
    resolveCoreSocketPath();
    if (coreSocketPath_.isEmpty()) {
        // 解析失敗：不對任何路徑發起連線，維持 title bar 警告並等下一次重試。
        refreshWindowTitle();
        if (reconnectTimer_ && !reconnectTimer_->isActive()) reconnectTimer_->start();
        return;
    }
    ipc_->connectToCore(coreSocketPath_);
}
