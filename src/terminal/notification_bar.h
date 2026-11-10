// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QFrame>
#include <QHBoxLayout>
#include <QJsonObject>
#include <QLabel>
#include <QPoint>
#include <QScrollArea>
#include <QTimer>
#include <QToolButton>
#include <QVBoxLayout>

// 通知類型
enum class NotificationType { Warn, AI, Gate, Hint };

struct NotificationAction {
    QString label;
    QString action;
    QJsonObject args;
};

// 單筆通知訊息
struct NotificationMessage {
    NotificationType type;
    QString text;
    qint64 timestamp;  // QDateTime::currentMSecsSinceEpoch()
    QString notificationId;
    QList<NotificationAction> actions;
};

class TestNotificationBar;  // friend：測試需直接存取 userHeight_ / resizeHandle_

/**
 * @brief 半透明 overlay 通知列，疊在 TerminuxTerminal 上方。
 *
 * 狀態機：Hidden -> Expanded -> Collapsed(badge) -> Expanded / Hidden
 *   - appendMessage() → 自動展開
 *   - 5 秒自動收合為 badge（使用者拖過後此計時停用）
 *   - hover 時暫停收合計時
 *   - close button → Hidden
 *
 * 高度模式：
 *   - userHeight_ < 0：auto-fit（max 40% parent，min 60px）
 *   - userHeight_ > 0：使用者拖曳值（clamp 到 60 ~ 90% parent）
 *   - 跨 session 透過 QSettings("notificationBar/userHeight") 持久化
 *   - 雙擊底部 resize handle 還原為 auto-fit（移除 QSettings key）
 */
class NotificationBar : public QFrame {
    Q_OBJECT
    friend class ::TestNotificationBar;

public:
    explicit NotificationBar(QWidget* parent = nullptr);

    /** 新增一筆通知，自動展開 */
    void appendMessage(NotificationType type, const QString& text);

    /** 新增一筆帶 action 的結構化通知，自動展開 */
    void appendMessageWithActions(NotificationType type,
                                  const QString& text,
                                  const QString& notificationId,
                                  const QList<NotificationAction>& actions);

    /** 訊息數量 */
    int messageCount() const;

    /** 清除所有訊息並隱藏 */
    void clearAll();

    /** 移除最舊的 count 筆訊息。移除後若仍有訊息則刷新顯示，全部移除完則等同
     *  clearAll（收合並隱藏）。count <= 0 或無訊息時為 no-op。 */
    void removeOldest(int count);

    /** 重新顯示通知列（保留現有訊息歷史）。供 ✕ 隱藏後由右鍵選單
     *  「顯示通知列」喚回；無歷史訊息時顯示一次性提示，5 秒後自動消失。 */
    void showBar();

    /** 目前是否展開（bodyArea 可見） */
    bool isExpanded() const;

    /** 目前是否收合（badge 狀態，header 可見但 body 隱藏） */
    bool isCollapsed() const;

    /** 重新定位到 parent 底部（供 parent resizeEvent 呼叫） */
    void repositionInParent();

signals:
    void closeRequested();
    void actionTriggered(const QString& notificationId,
                         const QString& action,
                         const QJsonObject& args);

protected:
    void enterEvent(QEnterEvent* event) override;
    void leaveEvent(QEvent* event) override;
    bool eventFilter(QObject* obj, QEvent* event) override;

private slots:
    void toggleExpanded();
    void onAutoCollapse();

private:
    void updateDisplay();
    void rebuildActionButtons(const NotificationMessage& message);
    void clearActionButtons();
    QString iconForType(NotificationType type) const;
    QString bgColorForType(NotificationType type) const;
    QString borderColorForType(NotificationType type) const;

    /** 計算 expanded 狀態下的 bar 高度（純函式，便於測試） */
    int computeExpandedHeight(int parentH) const;

    /** QSettings 持久化 */
    void loadUserHeight();
    void saveUserHeight();
    void resetUserHeight();

    QList<NotificationMessage> messages_;
    bool expanded_ = false;

    // ── 子 widget ──
    QVBoxLayout* mainLayout_ = nullptr;
    QHBoxLayout* headerLayout_ = nullptr;
    QLabel* iconLabel_ = nullptr;
    QLabel* titleLabel_ = nullptr;
    QLabel* badgeLabel_ = nullptr;
    QToolButton* closeButton_ = nullptr;
    QScrollArea* bodyArea_ = nullptr;
    QLabel* bodyLabel_ = nullptr;
    QHBoxLayout* actionsLayout_ = nullptr;
    QWidget* actionsWidget_ = nullptr;

    // ── 拉伸 handle（底部 6px，使用者可拖曳）──
    QWidget* resizeHandle_ = nullptr;
    int      userHeight_      = -1;     // -1 sentinel = auto-fit
    bool     dragging_        = false;
    bool     dragMoved_       = false;  // click-vs-drag 判別（>= 4px 才算 drag）
    QPoint   dragStartGlobal_;
    int      dragStartHeight_ = 0;

    QTimer autoCollapseTimer_;

    static constexpr int kAutoCollapseMs     = 5000;
    static constexpr int kResizeHandleHeight = 6;
    static constexpr int kMinHeight          = 60;
    static constexpr int kDragThresholdPx    = 4;
};
