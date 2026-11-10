// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

// composite widget。把 PtyManager + VTermBridge + TerminalSurface
// 串成一個 end-to-end 可用的 terminal。
// 接上 IPC（lineage.pre_exec via background thread + QueuedConnection）。
// 行為對齊舊 TerminalWidget：post-notification（Enter 已送 PTY 後才做 lineage check，
// warn/block 為事後通知，顯示在 NotificationBar）。

#include "terminal/chat_query_origin.h"
#include "terminal/notification_bar.h"
#include "terminal/vterm_bridge.h"

#include <QByteArray>
#include <QSize>
#include <QJsonObject>
#include <QString>
#include <QStringList>
#include <QWidget>

#include <atomic>
#include <optional>
#include <string>
#include <thread>

class QScrollBar;

namespace terminux {

class PtyManager;
class VTermBridge;
class TerminalSurface;

/// 給 TerminuxTerminal 用的 IPC config（與 PreExecHookConfig 同義，但獨立定義
/// 以避免 transitive 拉入舊 TerminalWidget 的 header）。
struct TerminuxHookConfig {
    std::string                socket_path;
    std::string                ledger_path;
    std::optional<std::string> tool_signatures_path;
    std::optional<std::string> lineage_rules_path;
};

class TerminuxTerminal : public QWidget {
    Q_OBJECT
public:
    explicit TerminuxTerminal(QWidget* parent = nullptr);
    ~TerminuxTerminal() override;

    /// 啟動 shell。shellProgram 為空走 PtyManager 三段 fallback。
    bool start(const QString& shellProgram = {});

    /// 直接寫資料到 PTY（外部 IPC 注入用）
    void writeData(const QByteArray& data);

    /// 設定 lineage check IPC config（MainWindow 在建構後呼叫一次）
    void setHookConfig(const TerminuxHookConfig& cfg);

    /// MainWindow 在 IpcClient::sessionInitResult（Core socket
    /// session.init 成功）時呼叫一次，把 Core-issued session id 傳入；
    /// Core 斷線時傳空字串清空。`ecc.chat_query`（`@@`）request 會帶上這個
    /// id 作為 terminux_context.session_id，讓 daemon 端解析出 canonical
    /// request context。
    void setCoreSessionId(const QString& sessionId);

    /// 給 NotificationBar / 測試讀 terminal state
    VTermBridge*     bridge()           const { return bridge_;          }
    TerminalSurface* surface()          const { return surface_;         }
    QScrollBar*      scrollBar()        const { return scrollBar_;       }
    NotificationBar* notificationBar()  const { return notificationBar_; }

    /// 對外公開的通知 API（IPC 結果 / 外部組件可用）
    void showNotification(NotificationType type, const QString& message);

    /// parse notify.poll response 中的 notifications 陣列，dispatch 到
    /// NotificationBar(AI)。可從任何 thread 呼叫——內部用
    /// QMetaObject::invokeMethod(Qt::QueuedConnection) queue 回 main thread。
    /// rpcResult 是 response 的 "result" 物件（不是整個 response）。
    void dispatchAgentAsync(const QJsonObject& rpcResult);

signals:
    void chatQueryDetected(const QString& query, ChatQueryOrigin origin);
    void shellFinished(int exitCode);

    /// IPC pre-exec 結果回 main thread 後 emit（供 NotificationBar 使用）
    void preExecAllowed(const QString& command);
    void preExecWarned (const QString& command, const QString& message);
    void preExecBlocked(const QString& command, const QString& message);

    /// window title（OSC 0/2）forward 自 VTermBridge
    void titleChanged(const QString& title);

    /// preferred language 變更（context menu 選擇 or daemon 校正後）→ 供 MainWindow
    /// 更新依語言變化的 UI 文字（如 title bar 的 Core 未連線警告）。
    void languageChanged(const QString& languageCode);

    /// notify.poll 對 lineage server 的可達性變化（daemon 啟動 ~20s 內 _init_ecc 尚未
    /// listen 時連不上 = 暖機中）。供 MainWindow 在 title bar 顯示「服務初始化中」。
    void notifyServiceAvailableChanged(bool available);

private slots:
    void onCommandSubmitted(const QString& command);
    void onChatQuery(const QString& query, ChatQueryOrigin origin);   // @@／選字 → ecc.chat_query

    /// context menu「Language → 選擇語言」→ QSettings 持久化 + preferences.set_language RPC
    void onLanguageChangeRequested(const QString& languageCode);

    // scrollback 與 resize
    void onSurfaceResized(QSize newSize);
    void onBridgeDamage();              // 同步 scrollbar range / auto-bottom
    void onScrollBarValueChanged(int value);
    void onSurfaceScrollRequested(int linesUp);

    // PTY ring buffer 與 OSC 133 prompt markers
    void onPtyDataForBuffer(const QByteArray& data);
    void onPromptMarker(VTermBridge::PromptMarkerType type, int exitCode);

private:
    void    calcTermSize(int widgetW, int widgetH, int& rows, int& cols) const;
    QString currentCwd() const;
    void    syncScrollBarRange();

    // 定期向 daemon poll pending 通知（不依賴 OSC 133;D 的 lineage.post_exec），
    // 讓 dc_shell 等因 prompt marker 未穩定送達而漏掉的即時通知在固定時間內顯示。
    void    notifyPollLoop();

    // helpers
    void    sendPostExecWithStdout(int exitCode);
    void    invokePackageAction(const QString& notificationId,
                                const QString& action,
                                const QJsonObject& args);
    void    dispatchStructuredNotification(const QJsonObject& item);
    static QString launchViewer(const QJsonObject& request,
                                const QString& socketPath);
    static NotificationType notificationTypeFromLevel(const QString& level);
    static  constexpr int kMaxStdoutLines = 50;

    PtyManager*      pty_             = nullptr;
    VTermBridge*     bridge_          = nullptr;
    TerminalSurface* surface_         = nullptr;
    QScrollBar*      scrollBar_       = nullptr;
    NotificationBar* notificationBar_ = nullptr;

    std::optional<TerminuxHookConfig> hookConfig_;
    QString coreSessionId_;   // 見 setCoreSessionId()
    bool hookMarkerReceived_ = false;

    // PTY output ring buffer + 上一個命令名（給 lineage.post_exec）
    QStringList stdoutRingBuffer_;
    QString     currentStdoutLine_;
    QString     lastCommand_;

    // notify poll 背景 thread
    std::thread       notifyPollThread_;
    std::atomic<bool> notifyPollStop_{false};
};

}  // namespace terminux
