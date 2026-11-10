// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QMainWindow>
#include <QString>

class IpcClient;
class QTimer;
class WorkDock;
namespace terminux { class TerminuxTerminal; }

/**
 * @brief Top-level application window.
 *
 * 整合 TerminuxTerminal（libvterm + Qt 自繪 PTY shell）與 IpcClient（與 Python
 * Core 的 session 級 IPC channel）。
 *
 * Layout：central widget = TerminuxTerminal（含 surface + scrollbar +
 *         NotificationBar overlay）。statusBar 顯示 session 狀態。
 *
 * On construction：
 *   1. 透過 IpcClient::connectToCore() 連 Python Core
 *   2. onConnected() 收到 → ipc->sessionInit(member, project, shell, cwd)
 *   3. NotificationBar / ECC pipeline 由 TerminuxTerminal 內部處理
 *      （commandSubmit / lineage.pre_exec / OSC 133 / shell hook 都在那邊）
 *
 * 終端機只有 libvterm + Qt 自繪這一條路徑。
 */
class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);
    ~MainWindow() override;

    /** 測試用：取得新 terminal instance */
    terminux::TerminuxTerminal* terminalForTest() const { return terminal_; }

private slots:
    // IpcClient session 級事件
    void onConnected();
    void onSessionInitResult(const QString& sessionId);
    void onNotificationReceived(const QString& notifyType,
                                const QString& title,
                                const QString& content,
                                const QString& detail,
                                const QString& timestamp);
    // Core（daemon）連線中斷 / 連不上：於 title bar 提醒並定時重連。
    void onCoreDisconnected();
    // shell（OSC 0/2）設定的視窗標題；保存為基底，與 Core 連線警告合併顯示。
    void onTerminalTitle(const QString& title);
    // preferred language 變更 → 依語言重繪 title bar 警告文字。
    void onLanguageChanged(const QString& languageCode);
    // lineage/通知服務可達性變化（daemon 暖機中連不上）→ 更新 title bar 提示。
    void onNotifyServiceAvailable(bool available);

private:
    void setupUi();
    void connectSignals();
    // 依 coreConnected_ 與 shellTitle_ 重新組出視窗標題（未連線時前綴警告）。
    void refreshWindowTitle();
    // 依 language_ 回傳 Core 未連線的警告文字（zh-Hant / zh-Hans / en）。
    QString coreDisconnectedWarning() const;
    // 依 language_ 回傳「服務初始化中」暖機提示文字。
    QString coreWarmingUpText() const;
    // reconnectTimer_ 觸發：未連線時重新解析 per-user socket 並嘗試重連 Core。
    void attemptReconnect();
    // 依目前 UID 查帳號資料（或 TERMINUX_CORE_SOCK 覆寫）解析 Core socket
    // <home>/.terminux/terminux.sock；失敗時
    // coreSocketPath_ 清空、coreSocketError_ 記錄錯誤碼與預期路徑。
    void resolveCoreSocketPath();

    terminux::TerminuxTerminal*  terminal_ = nullptr;
    IpcClient*                   ipc_      = nullptr;
    WorkDock*                    workDock_ = nullptr;
    QTimer*                      reconnectTimer_ = nullptr;
    QString                      shellTitle_     = QStringLiteral("Terminux");
    // 由 resolveCoreSocketPath() 填入；沒有任何預設值（不再是 /tmp/terminux.sock）。
    QString                      coreSocketPath_;
    QString                      coreSocketError_;
    // project 環境變數衝突／不合法時的錯誤（非空 ⇒ 未送 session.init）。
    QString                      projectEnvError_;
    QString                      language_       = QStringLiteral("zh-Hant");
    bool                         coreConnected_  = false;
    // lineage/通知服務是否就緒（notify.poll 連得上）。初始未知，先顯示暖機提示。
    bool                         notifyServiceReady_ = false;
};
