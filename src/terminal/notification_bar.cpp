// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/notification_bar.h"

#include <QApplication>
#include <QCursor>
#include <QDateTime>
#include <QMouseEvent>
#include <QScrollBar>
#include <QSettings>
#include <QToolButton>
#include <cstdlib>

namespace {
constexpr const char* kSettingsKeyUserHeight = "notificationBar/userHeight";
}

// ── Constructor ──────────────────────────────────────────────────────────────

NotificationBar::NotificationBar(QWidget* parent)
    : QFrame(parent)
{
    setFrameShape(QFrame::NoFrame);
    setAutoFillBackground(false);

    // 基本 stylesheet（背景色在 updateDisplay 中依類型動態設定）
    setStyleSheet(QStringLiteral(
        "NotificationBar {"
        "  border-bottom: 2px solid #555;"
        "  border-radius: 0 0 4px 4px;"
        "}"
    ));

    // ── Main layout ──────────────────────────────────────────────────────────
    mainLayout_ = new QVBoxLayout(this);
    mainLayout_->setContentsMargins(8, 4, 8, 4);
    mainLayout_->setSpacing(2);

    // ── Header row ───────────────────────────────────────────────────────────
    headerLayout_ = new QHBoxLayout();
    headerLayout_->setContentsMargins(0, 0, 0, 0);
    headerLayout_->setSpacing(6);

    iconLabel_ = new QLabel(this);
    iconLabel_->setFixedWidth(24);
    iconLabel_->setAlignment(Qt::AlignCenter);
    iconLabel_->setStyleSheet(QStringLiteral("font-size: 14pt; background: transparent;"));

    titleLabel_ = new QLabel(this);
    titleLabel_->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    titleLabel_->setStyleSheet(QStringLiteral(
        "color: #eee; font-family: Monospace; font-size: 11pt; background: transparent;"
    ));
    titleLabel_->setWordWrap(false);
    // 通知與建議命令皆以原始文字顯示，保留 <、> 與重新導向符號。
    titleLabel_->setTextFormat(Qt::PlainText);
    titleLabel_->setTextInteractionFlags(Qt::TextSelectableByMouse);

    badgeLabel_ = new QLabel(this);
    badgeLabel_->setAlignment(Qt::AlignCenter);
    badgeLabel_->setStyleSheet(QStringLiteral(
        "background-color: #e74c3c;"
        "color: white;"
        "font-size: 9pt;"
        "font-weight: bold;"
        "border-radius: 8px;"
        "padding: 1px 5px;"
        "min-width: 16px;"
    ));
    badgeLabel_->hide();

    closeButton_ = new QToolButton(this);
    closeButton_->setText(QStringLiteral("\u2715"));  // ✕
    closeButton_->setFixedSize(20, 20);
    closeButton_->setStyleSheet(QStringLiteral(
        "QToolButton {"
        "  color: #aaa; background: transparent; border: none; font-size: 12pt;"
        "}"
        "QToolButton:hover { color: #fff; }"
    ));

    headerLayout_->addWidget(iconLabel_);
    headerLayout_->addWidget(titleLabel_);
    headerLayout_->addWidget(badgeLabel_);
    headerLayout_->addWidget(closeButton_);

    mainLayout_->addLayout(headerLayout_);

    // ── Body area（scroll） ──────────────────────────────────────────────────
    bodyArea_ = new QScrollArea(this);
    bodyArea_->setWidgetResizable(true);
    bodyArea_->setFrameShape(QFrame::NoFrame);
    bodyArea_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    bodyArea_->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    bodyArea_->setStyleSheet(QStringLiteral(
        "QScrollArea { background: transparent; }"
        "QScrollBar:vertical {"
        "  background: rgba(50,50,50,100); width: 8px; border-radius: 4px;"
        "}"
        "QScrollBar::handle:vertical {"
        "  background: rgba(150,150,150,120); border-radius: 4px; min-height: 20px;"
        "}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
    ));

    bodyLabel_ = new QLabel();
    bodyLabel_->setAlignment(Qt::AlignTop | Qt::AlignLeft);
    bodyLabel_->setWordWrap(true);
    bodyLabel_->setTextFormat(Qt::PlainText);
    bodyLabel_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    bodyLabel_->setStyleSheet(QStringLiteral(
        "color: #eee; font-family: Monospace; font-size: 11pt;"
        "background: transparent; padding: 4px 0;"
    ));

    bodyArea_->setWidget(bodyLabel_);
    bodyArea_->hide();
    mainLayout_->addWidget(bodyArea_);

    actionsWidget_ = new QWidget(this);
    actionsLayout_ = new QHBoxLayout(actionsWidget_);
    actionsLayout_->setContentsMargins(0, 2, 0, 2);
    actionsLayout_->setSpacing(6);
    actionsLayout_->addStretch();
    actionsWidget_->hide();
    mainLayout_->addWidget(actionsWidget_);

    // ── Connections ──────────────────────────────────────────────────────────
    // ✕：只隱藏、保留訊息歷史（可由右鍵選單「顯示通知列」喚回，
    // 或下一則通知自動重新展開）。
    connect(closeButton_, &QToolButton::clicked, this, [this]() {
        autoCollapseTimer_.stop();
        hide();
        emit closeRequested();
    });

    // header 點擊切換展開/收合（透過 eventFilter 攔截 MouseButtonRelease）
    titleLabel_->installEventFilter(this);
    iconLabel_->installEventFilter(this);

    // autoCollapse timer
    autoCollapseTimer_.setSingleShot(true);
    connect(&autoCollapseTimer_, &QTimer::timeout,
            this, &NotificationBar::onAutoCollapse);

    // ── Resize handle（底部可拖曳，6px）────────────────────────────────────
    // 用獨立子 widget：bodyArea_ / bodyLabel_ 等子 widget 會攔截 mouse event，
    // 在 frame 層 override 只能在 4px margin 觸發。獨立 handle 自己 own events。
    resizeHandle_ = new QWidget(this);
    resizeHandle_->setObjectName(QStringLiteral("notificationBarResizeHandle"));
    resizeHandle_->setCursor(Qt::SizeVerCursor);
    resizeHandle_->setMouseTracking(true);
    resizeHandle_->setStyleSheet(QStringLiteral(
        "QWidget#notificationBarResizeHandle {"
        "  background-color: transparent;"
        "}"
        "QWidget#notificationBarResizeHandle:hover {"
        "  background-color: rgba(255, 255, 255, 40);"
        "}"
    ));
    resizeHandle_->installEventFilter(this);
    resizeHandle_->hide();  // 預設隱藏，expanded 才顯示

    // 載入跨 session 的使用者偏好高度
    loadUserHeight();
}

// ── Public API ───────────────────────────────────────────────────────────────

void NotificationBar::appendMessage(NotificationType type, const QString& text)
{
    NotificationMessage msg;
    msg.type = type;
    msg.text = text;
    msg.timestamp = QDateTime::currentMSecsSinceEpoch();
    messages_.append(msg);

    expanded_ = true;
    updateDisplay();
    repositionInParent();
    show();
    raise();

    // 重置自動收合計時。使用者已手動拉伸時不啟動（明確的高度承諾，
    // 5 秒自動縮回會很意外）；可改由 header 點擊手動收合。
    if (userHeight_ == -1) {
        autoCollapseTimer_.start(kAutoCollapseMs);
    }
}

void NotificationBar::appendMessageWithActions(
    NotificationType type,
    const QString& text,
    const QString& notificationId,
    const QList<NotificationAction>& actions)
{
    NotificationMessage msg;
    msg.type = type;
    msg.text = text;
    msg.timestamp = QDateTime::currentMSecsSinceEpoch();
    msg.notificationId = notificationId;
    msg.actions = actions;
    messages_.append(msg);

    expanded_ = true;
    updateDisplay();
    repositionInParent();
    show();
    raise();

    if (userHeight_ == -1) {
        autoCollapseTimer_.start(kAutoCollapseMs);
    }
}

int NotificationBar::messageCount() const
{
    return messages_.size();
}

void NotificationBar::clearAll()
{
    messages_.clear();
    expanded_ = false;
    autoCollapseTimer_.stop();
    hide();
}

void NotificationBar::removeOldest(int count)
{
    if (count <= 0 || messages_.isEmpty())
        return;

    const int n = qMin(count, static_cast<int>(messages_.size()));
    messages_.erase(messages_.begin(), messages_.begin() + n);

    if (messages_.isEmpty()) {
        // 全部移除完 → 等同 clearAll：收合並隱藏
        expanded_ = false;
        autoCollapseTimer_.stop();
        hide();
        return;
    }

    // 仍有訊息：刷新顯示（保持目前展開/收合狀態）
    updateDisplay();
    repositionInParent();
}

void NotificationBar::showBar()
{
    expanded_ = true;

    if (messages_.isEmpty()) {
        // 無歷史訊息：顯示一次性提示（不寫進 messages_，避免污染歷史與 badge
        // 計數）。5 秒後 autoCollapse → updateDisplay 見空訊息自動 hide。
        clearActionButtons();
        iconLabel_->setText(iconForType(NotificationType::Hint));
        setStyleSheet(QStringLiteral(
            "NotificationBar {"
            "  background-color: %1;"
            "  border-bottom: 2px solid %2;"
            "  border-radius: 0 0 4px 4px;"
            "}"
        ).arg(bgColorForType(NotificationType::Hint),
              borderColorForType(NotificationType::Hint)));
        titleLabel_->setText(tr("目前沒有通知"));
        badgeLabel_->hide();
        bodyArea_->hide();
        repositionInParent();
        show();
        raise();
        autoCollapseTimer_.start(kAutoCollapseMs);
        return;
    }

    updateDisplay();
    repositionInParent();
    show();
    raise();
    // 與 appendMessage 一致：未手動拉伸時，啟動 5 秒自動收合為 badge。
    if (userHeight_ == -1) {
        autoCollapseTimer_.start(kAutoCollapseMs);
    }
}

bool NotificationBar::isExpanded() const
{
    return isVisible() && expanded_;
}

bool NotificationBar::isCollapsed() const
{
    return isVisible() && !expanded_;
}

// ── Protected ────────────────────────────────────────────────────────────────

void NotificationBar::enterEvent(QEnterEvent* /*event*/)
{
    // hover 時暫停自動收合
    autoCollapseTimer_.stop();
}

void NotificationBar::leaveEvent(QEvent* /*event*/)
{
    // 離開時重新啟動自動收合（僅在展開且未手動拉伸時）
    if (expanded_ && !messages_.isEmpty() && userHeight_ == -1) {
        autoCollapseTimer_.start(kAutoCollapseMs);
    }
    if (!dragging_) {
        unsetCursor();
    }
}

bool NotificationBar::eventFilter(QObject* obj, QEvent* event)
{
    // ── resize handle：press / move / release / dblclick ─────────────────
    if (obj == resizeHandle_) {
        switch (event->type()) {
        case QEvent::MouseButtonPress: {
            auto* me = static_cast<QMouseEvent*>(event);
            if (me->button() != Qt::LeftButton) break;
            dragging_         = true;
            dragMoved_        = false;
            dragStartGlobal_  = me->globalPosition().toPoint();
            dragStartHeight_  = height();
            autoCollapseTimer_.stop();
            return true;
        }
        case QEvent::MouseMove: {
            auto* me = static_cast<QMouseEvent*>(event);
            if (!dragging_ || !(me->buttons() & Qt::LeftButton)) break;
            const int delta =
                me->globalPosition().toPoint().y() - dragStartGlobal_.y();
            if (std::abs(delta) > kDragThresholdPx) {
                dragMoved_ = true;
            }
            // 直接寫入 unclamped 原值；clamp 在 computeExpandedHeight 做
            userHeight_ = dragStartHeight_ + delta;
            repositionInParent();
            return true;
        }
        case QEvent::MouseButtonRelease: {
            auto* me = static_cast<QMouseEvent*>(event);
            if (me->button() != Qt::LeftButton) break;
            const bool wasMoved = dragMoved_;
            dragging_ = false;
            if (wasMoved) {
                // 把當下實際 clamped 的高度作為使用者偏好寫入。
                // 注意：repositionInParent 已 clamp 過，記憶體中 userHeight_
                // 可能比實際 height() 大（small parent 暫時 clamp 場景），
                // 我們選擇寫入「使用者本次拖到的目標」=> 就是 userHeight_，
                // 但需要再 clamp 一次以免存入超過合理上限的離譜值。
                if (auto* p = parentWidget()) {
                    const int maxUser = p->height() * 9 / 10;
                    userHeight_ = qBound(kMinHeight, userHeight_, maxUser);
                }
                saveUserHeight();
            }
            // release 後重新評估 cursor（可能已離開 handle 區）
            unsetCursor();
            return true;
        }
        case QEvent::MouseButtonDblClick: {
            auto* me = static_cast<QMouseEvent*>(event);
            if (me->button() != Qt::LeftButton) break;
            resetUserHeight();
            repositionInParent();
            return true;
        }
        default:
            break;
        }
        return QFrame::eventFilter(obj, event);
    }

    // ── header 區域點擊 → 切換展開/收合 ──────────────────────────────────
    if (event->type() == QEvent::MouseButtonRelease
        && (obj == titleLabel_ || obj == iconLabel_))
    {
        // 拖曳剛結束時手指漂移到 title 上 release，不可被當成 toggle
        if (dragging_ || dragMoved_) {
            dragMoved_ = false;
            return false;
        }
        toggleExpanded();
        return true;
    }
    return QFrame::eventFilter(obj, event);
}

// ── Private slots ────────────────────────────────────────────────────────────

void NotificationBar::toggleExpanded()
{
    expanded_ = !expanded_;
    updateDisplay();
    repositionInParent();
}

void NotificationBar::onAutoCollapse()
{
    if (!expanded_) return;
    expanded_ = false;
    updateDisplay();
    repositionInParent();
}

// ── Private helpers ──────────────────────────────────────────────────────────

void NotificationBar::updateDisplay()
{
    if (messages_.isEmpty()) {
        clearActionButtons();
        hide();
        return;
    }

    const auto& latest = messages_.last();

    // 設定圖示
    iconLabel_->setText(iconForType(latest.type));

    // 背景色依類型
    QString bg = bgColorForType(latest.type);
    QString border = borderColorForType(latest.type);
    setStyleSheet(QStringLiteral(
        "NotificationBar {"
        "  background-color: %1;"
        "  border-bottom: 2px solid %2;"
        "  border-radius: 0 0 4px 4px;"
        "}"
    ).arg(bg, border));

    if (expanded_) {
        // 展開：title 顯示最新訊息第一行，body 顯示完整歷史
        QString firstLine = latest.text.section(QLatin1Char('\n'), 0, 0);
        if (firstLine.length() > 80)
            firstLine = firstLine.left(77) + QStringLiteral("...");
        titleLabel_->setText(firstLine);

        badgeLabel_->hide();
        bodyArea_->show();

        // 尋找最後一則包含 action 的訊息以保留互動按鈕（避免後續純文字/AI 通知沖掉按鈕）
        NotificationMessage actionSource = latest;
        if (actionSource.actions.isEmpty()) {
            for (auto it = messages_.rbegin(); it != messages_.rend(); ++it) {
                if (!it->actions.isEmpty()) {
                    actionSource = *it;
                    break;
                }
            }
        }
        rebuildActionButtons(actionSource);

        // 組合所有訊息（最新在底部）
        QString body;
        for (int i = 0; i < messages_.size(); ++i) {
            if (i > 0)
                body += QStringLiteral("\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n");
            body += QStringLiteral("%1 %2").arg(
                iconForType(messages_[i].type), messages_[i].text);
        }
        bodyLabel_->setText(body);

        // 捲動到底部（最新訊息）
        QScrollBar* vbar = bodyArea_->verticalScrollBar();
        if (vbar) vbar->setValue(vbar->maximum());

    } else {
        // 收合：badge 顯示數量
        int count = messages_.size();
        QString summary = QStringLiteral("%1 %2 notification%3")
            .arg(iconForType(latest.type))
            .arg(count)
            .arg(count > 1 ? QStringLiteral("s") : QString());
        titleLabel_->setText(summary);

        badgeLabel_->setText(QString::number(count));
        badgeLabel_->show();
        bodyArea_->hide();
        clearActionButtons();
    }
}

void NotificationBar::clearActionButtons()
{
    if (!actionsLayout_ || !actionsWidget_) return;
    while (QLayoutItem* item = actionsLayout_->takeAt(0)) {
        if (QWidget* widget = item->widget()) {
            widget->deleteLater();
        }
        delete item;
    }
    actionsLayout_->addStretch();
    actionsWidget_->hide();
}

void NotificationBar::rebuildActionButtons(const NotificationMessage& message)
{
    clearActionButtons();
    if (!actionsLayout_ || !actionsWidget_ || message.actions.isEmpty()) return;

    for (const NotificationAction& notificationAction : message.actions) {
        if (notificationAction.label.isEmpty()) continue;
        auto* button = new QToolButton(actionsWidget_);
        button->setText(notificationAction.label);
        button->setAutoRaise(false);
        button->setStyleSheet(QStringLiteral(
            "QToolButton {"
            "  color: #fff;"
            "  background-color: rgba(80, 120, 180, 180);"
            "  border: 1px solid rgba(180, 210, 255, 180);"
            "  border-radius: 4px;"
            "  padding: 2px 8px;"
            "}"
            "QToolButton:hover {"
            "  background-color: rgba(100, 145, 210, 220);"
            "}"
        ));
        const QString notificationId = message.notificationId;
        const QString action = notificationAction.action;
        const QJsonObject args = notificationAction.args;
        connect(button, &QToolButton::clicked, this, [this, notificationId, action, args]() {
            if (action.isEmpty()) {
                hide();
                return;
            }
            emit actionTriggered(notificationId, action, args);
            hide();
        });
        actionsLayout_->insertWidget(actionsLayout_->count() - 1, button);
    }

    actionsWidget_->show();
}

void NotificationBar::repositionInParent()
{
    QWidget* p = parentWidget();
    if (!p) return;

    int barH;
    if (expanded_) {
        barH = computeExpandedHeight(p->height());
    } else {
        barH = headerLayout_->sizeHint().height() + mainLayout_->contentsMargins().top()
               + mainLayout_->contentsMargins().bottom() + 4;
    }

    setGeometry(0, 0, p->width(), barH);

    // resize handle 只在 expanded 時可見、可拖（collapsed 時 header 高度太薄
    // 與 close button 重疊變 click trap）
    if (resizeHandle_) {
        if (expanded_) {
            resizeHandle_->setGeometry(
                0, barH - kResizeHandleHeight, p->width(), kResizeHandleHeight);
            resizeHandle_->show();
            resizeHandle_->raise();
        } else {
            resizeHandle_->hide();
        }
    }
}

int NotificationBar::computeExpandedHeight(int parentH) const
{
    // 兩個獨立上限：
    //   auto-fit  → 40% parent（防第一則長訊息灌滿整個畫面）
    //   user-drag → 90% parent（給使用者最大彈性）
    const int maxAuto = parentH * 2 / 5;
    const int maxUser = parentH * 9 / 10;
    // -1 是 sentinel = 從未拖過；其他任何值都視為使用者意圖（即使是負值，
    // 例如拖過頭的中間態），clamp 由 qBound 處理。
    if (userHeight_ != -1) {
        return qBound(kMinHeight, userHeight_, maxUser);
    }
    const int hint = sizeHint().height();
    int h = qMin(hint, maxAuto);
    if (h < kMinHeight) h = kMinHeight;
    return h;
}

QString NotificationBar::iconForType(NotificationType type) const
{
    switch (type) {
    case NotificationType::Warn: return QStringLiteral("\u26A0");       // ⚠
    case NotificationType::AI:   return QStringLiteral("\U0001F916");   // 🤖
    case NotificationType::Gate: return QStringLiteral("\U0001F6A7");   // 🚧
    case NotificationType::Hint: return QStringLiteral("\U0001F4A1");   // 💡
    }
    return QStringLiteral("?");
}

QString NotificationBar::bgColorForType(NotificationType type) const
{
    switch (type) {
    case NotificationType::Warn: return QStringLiteral("rgba(50, 48, 5, 220)");
    case NotificationType::AI:   return QStringLiteral("rgba(20, 35, 60, 220)");
    case NotificationType::Gate: return QStringLiteral("rgba(50, 48, 5, 220)");
    case NotificationType::Hint: return QStringLiteral("rgba(20, 50, 30, 220)");
    }
    return QStringLiteral("rgba(30, 30, 30, 220)");
}

QString NotificationBar::borderColorForType(NotificationType type) const
{
    switch (type) {
    case NotificationType::Warn: return QStringLiteral("#f0d000");
    case NotificationType::AI:   return QStringLiteral("#4a9eff");
    case NotificationType::Gate: return QStringLiteral("#f0d000");
    case NotificationType::Hint: return QStringLiteral("#27ae60");
    }
    return QStringLiteral("#555");
}

// ── QSettings 持久化 ─────────────────────────────────────────────────────────

void NotificationBar::loadUserHeight()
{
    QSettings s;
    userHeight_ = s.value(QString::fromLatin1(kSettingsKeyUserHeight), -1).toInt();
}

void NotificationBar::saveUserHeight()
{
    QSettings s;
    s.setValue(QString::fromLatin1(kSettingsKeyUserHeight), userHeight_);
}

void NotificationBar::resetUserHeight()
{
    userHeight_ = -1;
    QSettings s;
    s.remove(QString::fromLatin1(kSettingsKeyUserHeight));
}
