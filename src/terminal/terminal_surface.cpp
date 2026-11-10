// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/terminal_surface.h"

#include "terminal/vterm_bridge.h"

#include <QActionGroup>
#include <QApplication>
#include <QClipboard>
#include <QDesktopServices>
#include <QRegularExpression>
#include <QUrl>
#include <QContextMenuEvent>
#include <QDesktopServices>
#include <QFontMetricsF>
#include <QInputMethodEvent>
#include <QKeyEvent>
#include <QMap>
#include <QMenu>
#include <QMouseEvent>
#include <QPaintEvent>
#include <QPainter>
#include <QPen>
#include <QRegularExpression>
#include <QResizeEvent>
#include <QStringList>
#include <QTimer>
#include <QUrl>
#include <QWheelEvent>

#include <algorithm>
#include <cmath>
#include <cstdlib>

namespace terminux {

namespace {

// 色彩取自 colorScheme_（member function）；drawEmptyCell 的 selected 路徑
// 保留一個簡單的 fallback。
constexpr int kCursorBlinkIntervalMs = 500;

}  // namespace

// ── ctor ────────────────────────────────────────────────────────────────────

TerminalSurface::TerminalSurface(VTermBridge* bridge, QWidget* parent)
    : QWidget(parent), bridge_(bridge)
{
    // 字型 fallback chain — Qt 依序找已安裝者作 primary，缺 glyph
    // 時自動 fallback 到後面 family（缺檔者直接被跳過）
    QFont font;
    font.setStyleHint(QFont::Monospace);
    font.setFixedPitch(true);
    font.setFamilies(QStringList{
        QStringLiteral("Noto Sans Mono CJK TC"),
        QStringLiteral("Noto Sans Mono CJK SC"),
        QStringLiteral("WenQuanYi Zen Hei Mono"),
        QStringLiteral("Source Han Mono"),
        QStringLiteral("DejaVu Sans Mono"),
        QStringLiteral("monospace"),
    });
    font.setPointSize(baseFontSize_);
    font_ = font;

    // font cache（避免每 cell setBold/setItalic 的成本）
    rebuildFontCache();

    // 預設色彩方案
    colorScheme_ = defaultDarkScheme();

    setFocusPolicy(Qt::StrongFocus);
    setAttribute(Qt::WA_OpaquePaintEvent, true);
    setAttribute(Qt::WA_InputMethodEnabled, true);   // IME 接管
    setAutoFillBackground(false);

    recalcCellMetrics();

    connect(bridge_, &VTermBridge::damageReceived,
            this,    &TerminalSurface::onDamage);
    connect(bridge_, &VTermBridge::cursorChanged,
            this,    &TerminalSurface::onCursorChanged);
    connect(bridge_, &VTermBridge::termResized,
            this,    [this](int, int) { update(); });
    connect(bridge_, &VTermBridge::cursorShapeChanged,
            this,    [this](VTermBridge::CursorShape) {
                updateCursorRect(bridge_->cursorPos());
            });

    // cursor blink
    cursorBlinkTimer_ = new QTimer(this);
    cursorBlinkTimer_->setInterval(kCursorBlinkIntervalMs);
    connect(cursorBlinkTimer_, &QTimer::timeout,
            this,              &TerminalSurface::onCursorBlinkTick);
    cursorBlinkTimer_->start();

    // 渲染節流 timer（damage union → 16ms cap）
    renderThrottleTimer_ = new QTimer(this);
    renderThrottleTimer_->setSingleShot(true);
    renderThrottleTimer_->setInterval(kRenderThrottleMs);
    connect(renderThrottleTimer_, &QTimer::timeout, this, [this]() {
        if (!pendingDirtyRect_.isEmpty()) {
            update(pendingDirtyRect_);
            pendingDirtyRect_ = QRect();
        }
    });
}

void TerminalSurface::rebuildFontCache()
{
    fontBold_       = font_; fontBold_.setBold(true);
    fontItalic_     = font_; fontItalic_.setItalic(true);
    fontBoldItalic_ = font_;
    fontBoldItalic_.setBold(true);
    fontBoldItalic_.setItalic(true);
}

void TerminalSurface::setColorScheme(const ColorScheme& scheme)
{
    colorScheme_ = scheme;
    update();
}

void TerminalSurface::setCurrentLanguage(const QString& languageCode)
{
    // 僅更新 context menu 勾選狀態；不發 languageChangeRequested（避免迴圈）。
    if (!languageCode.isEmpty())
        currentLanguage_ = languageCode;
}

// ── geometry helpers ────────────────────────────────────────────────────────

qreal TerminalSurface::cellWidth() const  { return cellWidth_; }
qreal TerminalSurface::cellHeight() const { return cellHeight_; }

QPoint TerminalSurface::pixelToCell(const QPoint& pos) const
{
    if (cellWidth_ <= 0 || cellHeight_ <= 0) return {0, 0};
    return QPoint(static_cast<int>(pos.x() / cellWidth_),
                  static_cast<int>(pos.y() / cellHeight_));
}

void TerminalSurface::recalcCellMetrics()
{
    QFontMetricsF fm(font_);
    cellWidth_  = fm.horizontalAdvance(QLatin1Char('M'));
    cellHeight_ = fm.height();
}

// ── paint ───────────────────────────────────────────────────────────────────

int TerminalSurface::maxScrollOffset() const
{
    return bridge_->scrollbackLineCount();
}

// 從 logical line 字串中切出 prompt prefix 之後的 user input。
// Heuristic：找最後一個 prompt sigil + 跟著的空白；常見 unix shell prompt 結尾：
//   "> "  tcsh / 自訂 PS1 含 hostname/path 結尾
//   "$ "  bash / sh / zsh 一般 user
//   "% "  csh 預設 / zsh 預設
//   "# "  root
// 沒找到 sigil → 回 trimmed（fallback；對極簡 prompt 或空 prompt 仍可用）
QString TerminalSurface::stripPromptPrefix(const QString& logicalLine)
{
    static const QStringList sigils = {
        QStringLiteral("> "),
        QStringLiteral("$ "),
        QStringLiteral("% "),
        QStringLiteral("# "),
    };

    int bestPos = -1;          // 最後一個 sigil 出現的 *起始* index
    int bestLen = 0;
    for (const auto& s : sigils) {
        const int p = logicalLine.lastIndexOf(s);
        if (p > bestPos) {
            bestPos = p;
            bestLen = s.size();
        }
    }
    if (bestPos < 0) {
        return logicalLine.trimmed();   // 無 sigil → 整行（fallback）
    }
    return logicalLine.mid(bestPos + bestLen).trimmed();
}

void TerminalSurface::paintEvent(QPaintEvent* event)
{
    if (cellWidth_ <= 0 || cellHeight_ <= 0) return;

    QPainter painter(this);
    painter.setFont(font_);

    // 只填 Qt 指定的 dirty rect；WA_OpaquePaintEvent 保證其他區域無需重畫。
    painter.fillRect(event->rect(), colorScheme_.background);

    const int rows = bridge_->rows();
    const int cols = bridge_->cols();
    if (rows <= 0 || cols <= 0) return;

    // dirty rect 涵蓋的 viewRow / col 範圍
    const QRect r = event->rect();
    const int startViewRow = std::max(0,
        static_cast<int>(r.top() / cellHeight_));
    const int endViewRow   = std::min(rows - 1,
        static_cast<int>(r.bottom() / cellHeight_));
    const int startCol = std::max(0,
        static_cast<int>(r.left() / cellWidth_));
    const int endCol   = std::min(cols - 1,
        static_cast<int>(r.right() / cellWidth_));

    // 座標模型：
    //   distFromBottom = (rows-1 - viewRow) + scrollOffset_
    //     0..rows-1  → 在 visible screen 內，screenRow = rows-1 - distFromBottom
    //     >=rows     → 在 scrollback 內，sbIndex = distFromBottom - rows
    //                  （0 = 最近捲出的行）
    const int sbCount = bridge_->scrollbackLineCount();

    for (int viewRow = startViewRow; viewRow <= endViewRow; ++viewRow) {
        const int distFromBottom = (rows - 1 - viewRow) + scrollOffset_;
        const int virtRow = viewRowToVirtual(viewRow);

        if (distFromBottom < rows) {
            // visible screen 行
            const int screenRow = rows - 1 - distFromBottom;
            for (int col = startCol; col <= endCol; ++col) {
                const bool sel = isCellSelected(virtRow, col);
                drawCellAt(painter, viewRow, col,
                           bridge_->cellAt(screenRow, col), sel);
            }
        } else {
            // scrollback 行
            const int sbIndex = distFromBottom - rows;
            if (sbIndex < sbCount) {
                const auto& line = bridge_->scrollbackLineAt(sbIndex);
                const int n = static_cast<int>(line.size());
                for (int col = startCol; col <= endCol; ++col) {
                    const bool sel = isCellSelected(virtRow, col);
                    if (col < n) {
                        drawCellAt(painter, viewRow, col, line[col], sel);
                    } else {
                        drawEmptyCell(painter, viewRow, col, sel);
                    }
                }
            } else {
                // 超出 scrollback（極端：rows 比 widget 高度大）
                for (int col = startCol; col <= endCol; ++col) {
                    const bool sel = isCellSelected(virtRow, col);
                    drawEmptyCell(painter, viewRow, col, sel);
                }
            }
        }
    }

    // cursor / preedit 只在實時視圖（offset 0）繪製
    if (scrollOffset_ == 0) {
        drawCursor(painter);
        if (!preeditString_.isEmpty()) {
            drawPreedit(painter);
        }
    }

    selectionPaintRect_ = selectionPaintRect();
}

void TerminalSurface::drawCellAt(QPainter& painter, int viewRow, int col,
                                 const CellData& cell, bool selected)
{
    // wide char tail 跳過（前一 cell 的 wide rect 已涵蓋）— 但若 selected，
    // 我們仍需把這個 cell 的背景填為反白色，否則右半 cell 會看起來沒被選到。
    if (cell.codepoint == 0
        || cell.codepoint == 0xFFFFFFFFu
        || cell.width <= 0)
    {
        if (selected) {
            const int virtualRow = viewRowToVirtual(viewRow);
            const bool wideHeadSelected = col > 0
                && isCellSelected(virtualRow, col - 1);
            // 寬字前導 cell 已用兩格寬的 rect 畫完背景與 glyph；尾格若再次填色
            // 會在 glyph 畫完後蓋住右半。只有前導格未選取時才保留 fallback。
            if (!wideHeadSelected) {
                const QRectF rEmpty(col * cellWidth_, viewRow * cellHeight_,
                                    cellWidth_, cellHeight_);
                painter.fillRect(rEmpty, colorScheme_.foreground);
            }
        }
        return;
    }

    const int spanCols = std::max(1, static_cast<int>(cell.width));
    const qreal cellW = spanCols * cellWidth_;
    const qreal x     = col * cellWidth_;
    const qreal y     = viewRow * cellHeight_;
    const QRectF r(x, y, cellW, cellHeight_);

    QColor fg = fgForCell(cell);
    QColor bg = bgForCell(cell);
    if (cell.reverse) std::swap(fg, bg);
    if (selected)     std::swap(fg, bg);   // 選取反白（疊加 reverse）

    painter.fillRect(r, bg);

    painter.setFont(fontForAttrs(cell.bold, cell.italic));
    painter.setPen(fg);

    const char32_t cp = static_cast<char32_t>(cell.codepoint);
    const QString ch  = QString::fromUcs4(&cp, 1);
    painter.drawText(r, Qt::AlignLeft | Qt::AlignVCenter, ch);

    // underline / strikethrough
    if (cell.underline) {
        const qreal underY = y + cellHeight_ - 2.0;
        QPen pen(fg);
        pen.setWidthF(1.0);
        painter.setPen(pen);
        painter.drawLine(QPointF(x, underY), QPointF(x + cellW, underY));
    }
    if (cell.strikethrough) {
        const qreal strikeY = y + cellHeight_ * 0.55;
        QPen pen(fg);
        pen.setWidthF(1.0);
        painter.setPen(pen);
        painter.drawLine(QPointF(x, strikeY), QPointF(x + cellW, strikeY));
    }
}

void TerminalSurface::drawEmptyCell(QPainter& painter, int viewRow, int col,
                                    bool selected)
{
    const QRectF r(col * cellWidth_, viewRow * cellHeight_,
                   cellWidth_, cellHeight_);
    painter.fillRect(r,
                     selected ? colorScheme_.foreground : colorScheme_.background);
}

void TerminalSurface::drawCursor(QPainter& painter)
{
    if (!bridge_->cursorVisible()) return;
    if (!cursorBlinkVisible_)      return;   // 閃爍隱藏期

    const QPoint pos = bridge_->cursorPos();
    const int col = std::min(pos.x(), bridge_->cols() - 1);
    const int row = std::min(pos.y(), bridge_->rows() - 1);
    if (col < 0 || row < 0) return;

    const qreal x = col * cellWidth_;
    const qreal y = row * cellHeight_;
    const QRectF cellR(x, y, cellWidth_, cellHeight_);
    const QColor cursorColor = colorScheme_.cursor;

    if (!hasFocus_) {
        // 失焦：永遠用 outline（不論 shape）
        QPen pen(cursorColor);
        pen.setWidthF(1.0);
        painter.setPen(pen);
        painter.setBrush(Qt::NoBrush);
        painter.drawRect(cellR.adjusted(0, 0, -1, -1));
        return;
    }

    switch (bridge_->cursorShape()) {
    case VTermBridge::CursorShape::Block: {
        painter.fillRect(cellR, cursorColor);
        // 用反色繪製字元
        const CellData cell = bridge_->cellAt(row, col);
        if (cell.codepoint != 0 && cell.codepoint != 0xFFFFFFFFu) {
            painter.setPen(colorScheme_.background);
            painter.setFont(fontForAttrs(cell.bold, cell.italic));
            const char32_t cp = static_cast<char32_t>(cell.codepoint);
            const QString ch  = QString::fromUcs4(&cp, 1);
            painter.drawText(cellR, Qt::AlignLeft | Qt::AlignVCenter, ch);
        }
        break;
    }
    case VTermBridge::CursorShape::Underline:
        painter.fillRect(QRectF(x, y + cellHeight_ - 2.0,
                                cellWidth_, 2.0),
                         cursorColor);
        break;
    case VTermBridge::CursorShape::Bar:
        painter.fillRect(QRectF(x, y, 2.0, cellHeight_), cursorColor);
        break;
    }
}

QFont TerminalSurface::fontForAttrs(bool bold, bool italic) const
{
    if (bold && italic) return fontBoldItalic_;
    if (bold)           return fontBold_;
    if (italic)         return fontItalic_;
    return font_;
}

QColor TerminalSurface::fgForCell(const CellData& cell) const
{
    if (cell.fgDefault) return colorScheme_.foreground;
    if (cell.fgIndexed) {
        // bold + ANSI 0..7 → bright (8..15) — terminal 慣例
        uint8_t idx = cell.fgIndex;
        if (cell.bold && idx < 8) idx += 8;
        return colorScheme_.palette[idx];
    }
    return QColor(cell.fgR, cell.fgG, cell.fgB);
}

QColor TerminalSurface::bgForCell(const CellData& cell) const
{
    if (cell.bgDefault) return colorScheme_.background;
    if (cell.bgIndexed) return colorScheme_.palette[cell.bgIndex];
    return QColor(cell.bgR, cell.bgG, cell.bgB);
}

void TerminalSurface::drawPreedit(QPainter& painter)
{
    const QPoint cp = bridge_->cursorPos();
    const qreal x = cp.x() * cellWidth_;
    const qreal y = cp.y() * cellHeight_;

    // 用實際字型 metrics 算寬度（CJK 一個 codepoint = 2 cell 視覺寬，
    // QString::length 為 1，靠 horizontalAdvance 才會接近真實寬）
    QFontMetricsF fm(font_);
    const qreal w = std::max<qreal>(cellWidth_,
                                    fm.horizontalAdvance(preeditString_));
    const QRectF bgRect(x, y, w, cellHeight_);

    // 半透明深色底
    painter.fillRect(bgRect, QColor(60, 60, 60, 220));

    // 黃色文字（與 default fg 區分）
    painter.setPen(QColor(255, 220, 100));
    painter.setFont(font_);
    painter.drawText(bgRect, Qt::AlignLeft | Qt::AlignVCenter, preeditString_);

    // 虛線底線示意 composing 狀態
    QPen dash(QColor(255, 220, 100));
    dash.setStyle(Qt::DashLine);
    dash.setWidth(1);
    painter.setPen(dash);
    painter.drawLine(QPointF(x,         y + cellHeight_ - 1),
                     QPointF(x + w,     y + cellHeight_ - 1));
}

// 前景／背景色由 member function fgForCell / bgForCell 決定
//      （讀 colorScheme_、處理 indexed → palette、bold→bright 規則）

// ── damage / cursor signals ─────────────────────────────────────────────────

void TerminalSurface::onDamage(int startRow, int startCol, int endRow, int endCol)
{
    if (cellWidth_ <= 0 || cellHeight_ <= 0) {
        // 還沒初始化字型 metrics，full repaint 走 timer
        pendingDirtyRect_ = rect();
    } else {
        const QRect r(static_cast<int>(startCol * cellWidth_),
                     static_cast<int>(startRow * cellHeight_),
                     static_cast<int>((endCol - startCol + 1) * cellWidth_) + 1,
                     static_cast<int>((endRow - startRow + 1) * cellHeight_) + 1);
        // 累積 dirty rect，timer 觸發時一次 update（60fps cap）
        pendingDirtyRect_ = pendingDirtyRect_.isEmpty()
            ? r
            : pendingDirtyRect_.united(r);
    }
    if (!renderThrottleTimer_->isActive()) {
        renderThrottleTimer_->start();
    }
}

void TerminalSurface::onCursorChanged()
{
    // 精確區域 update（不重繪整個 widget）
    const QPoint newPos = bridge_->cursorPos();
    if (scrollOffset_ == 0) {
        updateCursorRect(lastCursorPos_);
        if (newPos != lastCursorPos_) {
            updateCursorRect(newPos);
        }
    }
    lastCursorPos_ = newPos;

    // 使用者打字 / 程式移動 cursor 時讓閃爍重置（cursor 立刻可見）
    resetCursorBlink();
}

void TerminalSurface::onCursorBlinkTick()
{
    cursorBlinkVisible_ = !cursorBlinkVisible_;
    if (scrollOffset_ == 0) {
        updateCursorRect(bridge_->cursorPos());
    }
}

void TerminalSurface::resetCursorBlink()
{
    cursorBlinkVisible_ = true;
    if (cursorBlinkTimer_ && cursorBlinkTimer_->isActive()) {
        cursorBlinkTimer_->start();   // restart 計時
    }
}

void TerminalSurface::updateCursorRect(QPoint cellPos)
{
    if (cellWidth_ <= 0 || cellHeight_ <= 0) return;
    // 用 cellWidth*2 寬度 cover wide char（CJK）情況
    const int x = static_cast<int>(cellPos.x() * cellWidth_);
    const int y = static_cast<int>(cellPos.y() * cellHeight_);
    const int w = static_cast<int>(cellWidth_ * 2.0) + 1;
    const int h = static_cast<int>(cellHeight_) + 1;
    update(QRect(x, y, w, h));
}

// ── input ───────────────────────────────────────────────────────────────────

void TerminalSurface::keyPressEvent(QKeyEvent* event)
{
    // Ctrl+Shift+C/V 在 encodeKey 之前攔截，避免與 Ctrl+C (SIGINT) 衝突
    if (event->modifiers() == (Qt::ControlModifier | Qt::ShiftModifier)) {
        if (event->key() == Qt::Key_C) {
            copySelectionToClipboard();
            event->accept();
            return;
        }
        if (event->key() == Qt::Key_V) {
            pasteFromClipboard();
            event->accept();
            return;
        }
    }

    // Ctrl+= / Ctrl+- / Ctrl+0 字型大小調整。允許 Ctrl 或 Ctrl+Shift（一般
    // terminal 慣例，US 鍵盤 + 是 Shift+= 所以 Ctrl++ 自然帶 Shift）。Alt 等其他
    // modifier 在比對前 mask 掉。
    {
        const auto mods = event->modifiers() & (Qt::ControlModifier | Qt::ShiftModifier);
        if ((mods & Qt::ControlModifier) && !(event->modifiers() & Qt::AltModifier)) {
            const int k = event->key();
            if (k == Qt::Key_Equal || k == Qt::Key_Plus) {
                adjustFontSize(1);
                event->accept();
                return;
            }
            if (k == Qt::Key_Minus || k == Qt::Key_Underscore) {
                adjustFontSize(-1);
                event->accept();
                return;
            }
            if (k == Qt::Key_0) {
                resetFontSize();
                event->accept();
                return;
            }
        }
    }

    const QByteArray data = encodeKey(event);
    if (data.isEmpty()) {
        QWidget::keyPressEvent(event);
        return;
    }

    const int key = event->key();

    if (key == Qt::Key_Return || key == Qt::Key_Enter) {
        // 從 PTY cursor 邏輯行（含 wrap）取真實顯示內容，再 strip prompt prefix。
        // 不再用 keystroke buffer — history(↑↓) / Tab / Ctrl-x / arrow + 中插字
        // 都會讓 buffer 與 PTY 該行 desync。
        const QString logicalLine = bridge_->cursorLogicalLineText();
        const QString userInput   = stripPromptPrefix(logicalLine);

        if (userInput.startsWith(QStringLiteral("@@"))) {
            const QString query = userInput.mid(2).trimmed();
            emit chatQueryDetected(query, ChatQueryOrigin::AtCommand);
            // 對齊 terminal_widget.cpp 既有行為：送 Ctrl+U + Enter 清掉 shell 輸入行
            emit keyInput(QByteArrayLiteral("\x15\n"));
            return;
        }
        if (!userInput.isEmpty()) {
            // 通知 IPC 層做 post-notification lineage check（行為對齊舊路徑）
            emit commandSubmitted(userInput);
        }
    }

    emit keyInput(data);

    // 使用者敲鍵 → cursor 立刻可見、閃爍計時重置
    resetCursorBlink();
    if (scrollOffset_ == 0) {
        updateCursorRect(bridge_->cursorPos());
    }
}

QByteArray TerminalSurface::encodeKey(QKeyEvent* event) const
{
    static const QMap<int, QByteArray> kSpecial = {
        {Qt::Key_Up,        QByteArrayLiteral("\x1b[A")},
        {Qt::Key_Down,      QByteArrayLiteral("\x1b[B")},
        {Qt::Key_Right,     QByteArrayLiteral("\x1b[C")},
        {Qt::Key_Left,      QByteArrayLiteral("\x1b[D")},
        {Qt::Key_Home,      QByteArrayLiteral("\x1b[H")},
        {Qt::Key_End,       QByteArrayLiteral("\x1b[F")},
        {Qt::Key_PageUp,    QByteArrayLiteral("\x1b[5~")},
        {Qt::Key_PageDown,  QByteArrayLiteral("\x1b[6~")},
        {Qt::Key_Insert,    QByteArrayLiteral("\x1b[2~")},
        {Qt::Key_Delete,    QByteArrayLiteral("\x1b[3~")},
        {Qt::Key_F1,        QByteArrayLiteral("\x1bOP")},
        {Qt::Key_F2,        QByteArrayLiteral("\x1bOQ")},
        {Qt::Key_F3,        QByteArrayLiteral("\x1bOR")},
        {Qt::Key_F4,        QByteArrayLiteral("\x1bOS")},
        {Qt::Key_F5,        QByteArrayLiteral("\x1b[15~")},
        {Qt::Key_F6,        QByteArrayLiteral("\x1b[17~")},
        {Qt::Key_F7,        QByteArrayLiteral("\x1b[18~")},
        {Qt::Key_F8,        QByteArrayLiteral("\x1b[19~")},
        {Qt::Key_F9,        QByteArrayLiteral("\x1b[20~")},
        {Qt::Key_F10,       QByteArrayLiteral("\x1b[21~")},
        {Qt::Key_F11,       QByteArrayLiteral("\x1b[23~")},
        {Qt::Key_F12,       QByteArrayLiteral("\x1b[24~")},
        {Qt::Key_Backspace, QByteArrayLiteral("\x7f")},
        {Qt::Key_Tab,       QByteArrayLiteral("\t")},
        {Qt::Key_Return,    QByteArrayLiteral("\r")},
        {Qt::Key_Enter,     QByteArrayLiteral("\r")},
        {Qt::Key_Escape,    QByteArrayLiteral("\x1b")},
    };

    const int key = event->key();
    const auto mods = event->modifiers();

    // Ctrl + A..Z → 0x01..0x1A
    if (mods & Qt::ControlModifier) {
        if (key >= Qt::Key_A && key <= Qt::Key_Z) {
            const char c = static_cast<char>(key - Qt::Key_A + 1);
            return QByteArray(1, c);
        }
        if (key == Qt::Key_BracketLeft)  return QByteArray(1, '\x1b');  // Ctrl+[
        if (key == Qt::Key_Backslash)    return QByteArray(1, '\x1c');  // Ctrl+\
        if (key == Qt::Key_BracketRight) return QByteArray(1, '\x1d');  // Ctrl+]
        if (key == Qt::Key_Space)        return QByteArray(1, '\x00');  // Ctrl+Space
    }

    if (auto it = kSpecial.find(key); it != kSpecial.end()) {
        return it.value();
    }

    const QString text = event->text();
    if (!text.isEmpty()) {
        return text.toUtf8();
    }
    return {};
}

// ── IME ─────────────────────────────────────────────────────────────────────

void TerminalSurface::inputMethodEvent(QInputMethodEvent* event)
{
    // commit string：使用者按下 IME 確認（如空白、Enter 候選詞）
    const QString commit = event->commitString();
    if (!commit.isEmpty()) {
        // @@ 偵測改讀 PTY cursor 邏輯行（cursorLogicalLineText），不需要 buffer 累積
        emit keyInput(commit.toUtf8());
    }

    // preedit string：composing 中間狀態（未確認）
    const QString newPreedit = event->preeditString();
    if (newPreedit != preeditString_) {
        preeditString_ = newPreedit;
        update();
    }

    event->accept();
}

QVariant TerminalSurface::inputMethodQuery(Qt::InputMethodQuery query) const
{
    switch (query) {
    case Qt::ImEnabled:
        return true;
    case Qt::ImCursorRectangle: {
        // libvterm cursor (col, row) → pixel rect，IME 候選窗會貼著這個位置
        const QPoint cp = bridge_->cursorPos();
        const int x = static_cast<int>(cp.x() * cellWidth_);
        const int y = static_cast<int>(cp.y() * cellHeight_);
        const int w = static_cast<int>(std::max<qreal>(1.0, cellWidth_));
        const int h = static_cast<int>(std::max<qreal>(1.0, cellHeight_));
        return QRect(x, y, w, h);
    }
    case Qt::ImFont:
        return font_;
    case Qt::ImHints:
        return static_cast<int>(Qt::ImhNone);
    case Qt::ImAnchorPosition:
    case Qt::ImCursorPosition:
        return preeditString_.length();
    default:
        return QWidget::inputMethodQuery(query);
    }
}

// ── scrollback 控制 ──────────────────────────────────────────────────────────

void TerminalSurface::setScrollOffset(int offset)
{
    const int clamped = std::clamp(offset, 0, maxScrollOffset());
    if (clamped != scrollOffset_) {
        scrollOffset_ = clamped;
        selectionPaintRect_ = selectionPaintRect();
        update();
    }
}

void TerminalSurface::wheelEvent(QWheelEvent* event)
{
    // angleDelta.y(): 正 = wheel forward（離身體），慣例 = 看上方/舊內容
    const int delta = event->angleDelta().y();
    if (delta == 0) {
        QWidget::wheelEvent(event);
        return;
    }
    // Ctrl+wheel：字型大小調整（一般 terminal 慣例）
    if (event->modifiers() & Qt::ControlModifier) {
        const int notches = delta / 120;
        if (notches != 0) adjustFontSize(notches > 0 ? 1 : -1);
        event->accept();
        return;
    }
    // Shift+wheel：一行一行精細捲（一般 terminal 慣例）；其餘按 notch step
    int linesUp;
    if (event->modifiers() & Qt::ShiftModifier) {
        linesUp = (delta > 0) ? 1 : ((delta < 0) ? -1 : 0);
    } else {
        linesUp = delta / 40;   // 一個 notch = 120 → 3 行 step
    }
    if (linesUp != 0) {
        emit scrollRequested(linesUp);
    }
    event->accept();
}

// ── focus / resize ──────────────────────────────────────────────────────────

void TerminalSurface::resizeEvent(QResizeEvent* event)
{
    QWidget::resizeEvent(event);
    // 字型未變 → cell metric 不變；保留 hook 作未來 font 切換用
    recalcCellMetrics();
    // 通知 TerminuxTerminal 同步 rows/cols（避開 Qt parent layout 順序問題）
    emit surfaceResized(event->size());
    selectionPaintRect_ = selectionPaintRect();
}

void TerminalSurface::focusInEvent(QFocusEvent* event)
{
    hasFocus_ = true;
    update();
    QWidget::focusInEvent(event);
}

void TerminalSurface::focusOutEvent(QFocusEvent* event)
{
    hasFocus_ = false;
    update();
    QWidget::focusOutEvent(event);
}

// ── selection ───────────────────────────────────────────────────────────────

int TerminalSurface::viewRowToVirtual(int viewRow) const
{
    return bridge_->scrollbackLineCount() + viewRow - scrollOffset_;
}

CellPos TerminalSurface::pixelToVirtualCell(const QPoint& pos) const
{
    if (cellWidth_ <= 0 || cellHeight_ <= 0) return {0, 0};
    const int rows = std::max(1, bridge_->rows());
    const int cols = std::max(1, bridge_->cols());
    int viewRow = static_cast<int>(pos.y() / cellHeight_);
    int col     = static_cast<int>(pos.x() / cellWidth_);
    viewRow = std::clamp(viewRow, 0, rows - 1);
    col     = std::clamp(col,     0, cols - 1);
    return {viewRowToVirtual(viewRow), col};
}

CellData TerminalSurface::cellAtVirtual(int virtualRow, int col) const
{
    const int totalSb = bridge_->scrollbackLineCount();
    const int cols    = bridge_->cols();
    if (virtualRow < 0 || col < 0 || col >= cols) return CellData{};

    if (virtualRow < totalSb) {
        // scrollback：virtualRow 0 = 最舊 → sbIndex = totalSb-1-virtualRow
        const int sbIndex = totalSb - 1 - virtualRow;
        const auto& line = bridge_->scrollbackLineAt(sbIndex);
        if (col < static_cast<int>(line.size())) return line[col];
        return CellData{};
    }
    const int screenRow = virtualRow - totalSb;
    if (screenRow >= bridge_->rows()) return CellData{};
    return bridge_->cellAt(screenRow, col);
}

void TerminalSurface::normalizedSelection(CellPos& start, CellPos& end) const
{
    start = selAnchor_;
    end   = selEnd_;
    if (start.row > end.row
        || (start.row == end.row && start.col > end.col))
    {
        std::swap(start, end);
    }
}

bool TerminalSurface::isCellSelected(int virtualRow, int col) const
{
    if (!hasSelection_) return false;
    CellPos a, b;
    normalizedSelection(a, b);
    if (virtualRow < a.row || virtualRow > b.row) return false;
    if (a.row == b.row)        return col >= a.col && col <= b.col;
    if (virtualRow == a.row)   return col >= a.col;
    if (virtualRow == b.row)   return col <= b.col;
    return true;
}

void TerminalSurface::selectWord(CellPos pos)
{
    auto isBoundary = [&](int col) -> bool {
        const CellData c = cellAtVirtual(pos.row, col);
        const uint32_t cp = c.codepoint;
        return cp == 0 || cp == 0xFFFFFFFFu
            || cp == ' ' || cp == '\t';
    };
    const int cols = bridge_->cols();
    if (pos.col < 0 || pos.col >= cols || isBoundary(pos.col)) {
        selAnchor_ = pos;
        selEnd_    = pos;
        setSelectionState(false);
        return;
    }
    int leftCol = pos.col;
    while (leftCol > 0 && !isBoundary(leftCol - 1)) --leftCol;
    int rightCol = pos.col;
    while (rightCol < cols - 1 && !isBoundary(rightCol + 1)) ++rightCol;
    selAnchor_ = {pos.row, leftCol};
    selEnd_    = {pos.row, rightCol};
    setSelectionState(true);
}

void TerminalSurface::selectLine(CellPos pos)
{
    selAnchor_ = {pos.row, 0};
    selEnd_    = {pos.row, std::max(0, bridge_->cols() - 1)};
    setSelectionState(true);
}

QRect TerminalSurface::selectionPaintRect() const
{
    if (!hasSelection_ || cellWidth_ <= 0 || cellHeight_ <= 0) return {};

    const int rows = bridge_->rows();
    const int cols = bridge_->cols();
    if (rows <= 0 || cols <= 0) return {};

    CellPos start, end;
    normalizedSelection(start, end);

    const int firstVisibleVirtualRow =
        bridge_->scrollbackLineCount() - scrollOffset_;
    const int lastVisibleVirtualRow = firstVisibleVirtualRow + rows - 1;
    const int firstRow = std::max(start.row, firstVisibleVirtualRow);
    const int lastRow = std::min(end.row, lastVisibleVirtualRow);
    if (firstRow > lastRow) return {};

    QRect result;
    for (int virtualRow = firstRow; virtualRow <= lastRow; ++virtualRow) {
        const int viewRow = virtualRow - firstVisibleVirtualRow;
        const int firstCol = (virtualRow == start.row) ? start.col : 0;
        const int lastCol = (virtualRow == end.row) ? end.col : cols - 1;
        const QRect rowRect(
            static_cast<int>(firstCol * cellWidth_),
            static_cast<int>(viewRow * cellHeight_),
            static_cast<int>((lastCol - firstCol + 1) * cellWidth_) + 1,
            static_cast<int>(cellHeight_) + 1);
        result = result.isNull() ? rowRect : result.united(rowRect);
    }
    return result;
}

void TerminalSurface::setSelectionState(bool has)
{
    const bool prev = hasSelection_;
    const QRect oldRect = selectionPaintRect_;
    hasSelection_ = has;
    selectionPaintRect_ = selectionPaintRect();
    const QRect dirtyRect = oldRect.united(selectionPaintRect_);
    if (!dirtyRect.isEmpty()) update(dirtyRect);
    if (prev != has) emit selectionChanged(has);
}

void TerminalSurface::clearSelection()
{
    if (!hasSelection_ && !selecting_) return;
    selecting_ = false;
    setSelectionState(false);
}

QString TerminalSurface::urlAtVirtual(CellPos pos) const
{
    if (cellWidth_ <= 0 || cellHeight_ <= 0) return {};
    const int cols = bridge_->cols();
    if (cols <= 0 || pos.col < 0 || pos.col >= cols) return {};

    // Reconstruct 整個 virtual row 的文字（含 trailing whitespace 才能對到 col）
    QString line;
    line.reserve(static_cast<size_t>(cols));
    for (int c = 0; c < cols; ++c) {
        const CellData cell = cellAtVirtual(pos.row, c);
        if (cell.codepoint == 0xFFFFFFFFu) continue;   // wide char 右半 cell
        if (cell.codepoint == 0) {
            line += QLatin1Char(' ');
        } else {
            const char32_t cp = static_cast<char32_t>(cell.codepoint);
            line += QString::fromUcs4(&cp, 1);
        }
    }

    // URL pattern：http/https + non-whitespace（含 path、query、fragment）。
    // 不貪婪地咬到第一個空白；尾端 punctuation 後處理 strip（避免把 ")" / "."
    // 等斷點吃進去）。
    static const QRegularExpression re(
        QStringLiteral(R"(\bhttps?://[^\s<>"']+)"));
    auto it = re.globalMatch(line);
    while (it.hasNext()) {
        const QRegularExpressionMatch m = it.next();
        const int start = m.capturedStart(0);
        const int end   = m.capturedEnd(0);
        if (pos.col < start || pos.col >= end) continue;
        QString url = m.captured(0);
        // 去掉常見尾端標點（不會是 URL 一部分）
        while (!url.isEmpty()
               && QStringLiteral(".,;:!?)]}>").contains(url.back())) {
            url.chop(1);
        }
        return url;
    }
    return {};
}

QString TerminalSurface::selectedText() const
{
    if (!hasSelection_) return {};
    CellPos a, b;
    normalizedSelection(a, b);

    const int cols   = bridge_->cols();
    const int sbCnt  = bridge_->scrollbackLineCount();
    QString result;
    for (int vRow = a.row; vRow <= b.row; ++vRow) {
        const int colStart = (vRow == a.row) ? a.col : 0;
        const int colEnd   = (vRow == b.row) ? b.col : cols - 1;
        QString line;
        for (int col = colStart; col <= colEnd; ++col) {
            const CellData cell = cellAtVirtual(vRow, col);
            if (cell.codepoint == 0xFFFFFFFFu) {
                continue;   // wide char 右半 cell，跳過
            }
            if (cell.codepoint == 0) {
                line += QLatin1Char(' ');
            } else {
                const char32_t cp = static_cast<char32_t>(cell.codepoint);
                line += QString::fromUcs4(&cp, 1);
            }
        }
        // 行尾 trailing space 去掉
        while (line.endsWith(QLatin1Char(' '))) line.chop(1);
        result += line;
        // 換行：若**下一行**是 line wrap continuation 則不插 \n（unwrap 視覺斷行
        // 為單一邏輯行）。lineinfo 只對 visible row 有效；scrollback 部分 fallback
        // 為一律插 \n（保守，至少不錯接連續邏輯行）。
        if (vRow < b.row) {
            const int nextRow = vRow + 1;
            const int nextVisibleRow = nextRow - sbCnt;   // 0..rows-1 表 visible
            const bool nextIsContinuation =
                (nextVisibleRow >= 0)
                && bridge_->isLineContinuation(nextVisibleRow);
            if (!nextIsContinuation) {
                result += QLatin1Char('\n');
            }
        }
    }
    return result;
}

void TerminalSurface::copySelectionToClipboard()
{
    if (!hasSelection_) return;
    const QString text = selectedText();
    if (text.isEmpty()) return;
    QClipboard* cb = QApplication::clipboard();
    // CLIPBOARD（Ctrl+C / Ctrl+Shift+C / context menu Paste 拿這個）
    cb->setText(text, QClipboard::Clipboard);
    // PRIMARY selection（X11 標配：拖選自動填，滑鼠中鍵 paste 用這個）
    if (cb->supportsSelection()) {
        cb->setText(text, QClipboard::Selection);
    }
}

void TerminalSurface::pasteFromClipboard()
{
    const QString text = QApplication::clipboard()->text(QClipboard::Clipboard);
    if (text.isEmpty()) return;
    pastePayload(text);
}

void TerminalSurface::pasteFromPrimary()
{
    QClipboard* cb = QApplication::clipboard();
    if (!cb->supportsSelection()) {
        // Wayland / 無 X11 PRIMARY → fallback 到 CLIPBOARD
        pasteFromClipboard();
        return;
    }
    const QString text = cb->text(QClipboard::Selection);
    if (text.isEmpty()) return;
    pastePayload(text);
}

void TerminalSurface::pastePayload(const QString& text)
{
    if (text.isEmpty()) return;
    const QByteArray utf8 = text.toUtf8();

    // Paste 內容直接送 PTY；command 由 Enter 時的 cursorLogicalLineText() 從
    // PTY 讀（方案 B），paste 字被 shell echo 回 display 後自然進入該邏輯行。

    // 動態 bracketed paste：shell 啟用 DECSET ?2004h（如 bash readline 預設）才
    // 包 markers；tcsh / csh / 老 sh 沒啟用就送純文字，避免 ESC 序列被當髒字元
    // 混進指令（symptom: 看到 0~paste1~）。
    QByteArray payload;
    if (bridge_ && bridge_->bracketedPasteEnabled()) {
        payload.reserve(utf8.size() + 12);
        payload += QByteArrayLiteral("\x1b[200~");
        payload += utf8;
        payload += QByteArrayLiteral("\x1b[201~");
    } else {
        payload = utf8;
    }
    emit keyInput(payload);
}

// ── mouse events ───────────────────────────────────────────────────────────

void TerminalSurface::mousePressEvent(QMouseEvent* event)
{
    // X11 標配：滑鼠中鍵 paste from PRIMARY selection（拖選自動填）
    if (event->button() == Qt::MiddleButton) {
        setFocus();
        pasteFromPrimary();
        event->accept();
        return;
    }
    if (event->button() != Qt::LeftButton) {
        QWidget::mousePressEvent(event);
        return;
    }
    setFocus();   // 點擊也會搶焦點，方便接 keyPressEvent

    const CellPos pos = pixelToVirtualCell(event->pos());

    // Ctrl+LeftClick：URL 偵測 → 用系統瀏覽器開啟（一般 terminal 慣例）
    if (event->modifiers() & Qt::ControlModifier) {
        const QString url = urlAtVirtual(pos);
        if (!url.isEmpty()) {
            QDesktopServices::openUrl(QUrl(url));
            event->accept();
            return;
        }
        // 沒匹配到 URL → fallthrough 走一般 selection 行為
    }

    // 多擊偵測（位置容差 ±1 cell）
    const int interval = QApplication::doubleClickInterval();
    const bool sameSpot =
        lastClickPos_.row == pos.row
        && std::abs(lastClickPos_.col - pos.col) <= 1;
    if (lastClickTimer_.isValid()
        && lastClickTimer_.elapsed() < interval
        && sameSpot)
    {
        clickCount_ = std::min(clickCount_ + 1, 3);
    } else {
        clickCount_ = 1;
    }
    lastClickTimer_.restart();
    lastClickPos_ = pos;

    if (clickCount_ == 1) {
        selecting_ = true;
        selAnchor_ = pos;
        selEnd_    = pos;
        setSelectionState(false);   // 還沒 drag 不算有效選取
    } else if (clickCount_ == 2) {
        selecting_ = false;
        selectWord(pos);
    } else {
        selecting_ = false;
        selectLine(pos);
    }
    event->accept();
}

void TerminalSurface::mouseMoveEvent(QMouseEvent* event)
{
    if (!selecting_) {
        QWidget::mouseMoveEvent(event);
        return;
    }
    // 節流：把 mouseMove 的 selection 更新合併進 16ms window，避免每個 mouse
    // event 都觸發全 widget 重繪（連拉一秒 60+ 次 paintEvent 會明顯卡）
    pendingSelEnd_ = pixelToVirtualCell(event->pos());
    hasPendingSel_ = true;
    if (selectionUpdateTimer_ == nullptr) {
        selectionUpdateTimer_ = new QTimer(this);
        selectionUpdateTimer_->setSingleShot(true);
        connect(selectionUpdateTimer_, &QTimer::timeout, this, [this]() {
            if (!hasPendingSel_) return;
            hasPendingSel_ = false;
            selEnd_ = pendingSelEnd_;
            const bool same = (selEnd_.row == selAnchor_.row
                               && selEnd_.col == selAnchor_.col);
            setSelectionState(!same);
        });
    }
    if (!selectionUpdateTimer_->isActive()) {
        selectionUpdateTimer_->start(16);
    }
    event->accept();
}

void TerminalSurface::mouseReleaseEvent(QMouseEvent* event)
{
    if (event->button() != Qt::LeftButton) {
        QWidget::mouseReleaseEvent(event);
        return;
    }
    if (selecting_) {
        selecting_ = false;
        // Release 必定 flush — 不能漏最後 mouseMove 位置（即使 timer 還在 pending）
        if (selectionUpdateTimer_ && selectionUpdateTimer_->isActive()) {
            selectionUpdateTimer_->stop();
        }
        hasPendingSel_ = false;
        const CellPos pos = pixelToVirtualCell(event->pos());
        selEnd_ = pos;
        const bool same = (selEnd_.row == selAnchor_.row
                           && selEnd_.col == selAnchor_.col);
        setSelectionState(!same);
    }
    event->accept();
}

// ── context menu / select all / font size ──────────────────────────────────

void TerminalSurface::selectAll()
{
    const int totalSb = bridge_->scrollbackLineCount();
    const int rows    = bridge_->rows();
    const int cols    = bridge_->cols();
    if (rows <= 0 || cols <= 0) return;

    selAnchor_ = {0, 0};                                  // virtualRow 0 = 最舊
    selEnd_    = {totalSb + rows - 1, cols - 1};          // 最後一行最後一欄
    selecting_ = false;
    setSelectionState(true);                              // 內部 update + emit
}

void TerminalSurface::adjustFontSize(int delta)
{
    const int newSize = std::clamp(font_.pointSize() + delta,
                                    kMinFontSize, kMaxFontSize);
    if (newSize == font_.pointSize()) return;
    font_.setPointSize(newSize);
    rebuildFontCache();
    recalcCellMetrics();
    // surfaceResized 觸發 TerminuxTerminal::onSurfaceResized → bridge/pty.resize
    emit surfaceResized(size());
    selectionPaintRect_ = selectionPaintRect();
    update();
}

void TerminalSurface::resetFontSize()
{
    if (font_.pointSize() == baseFontSize_) return;
    font_.setPointSize(baseFontSize_);
    rebuildFontCache();
    recalcCellMetrics();
    emit surfaceResized(size());
    selectionPaintRect_ = selectionPaintRect();
    update();
}

void TerminalSurface::contextMenuEvent(QContextMenuEvent* event)
{
    QMenu menu(this);
    const bool    hasSel  = hasSelection_;
    const QString selText = hasSel ? selectedText() : QString();

    QAction* copyAct = menu.addAction(tr("Copy\tCtrl+Shift+C"));
    copyAct->setEnabled(hasSel);
    connect(copyAct, &QAction::triggered,
            this,    &TerminalSurface::copySelectionToClipboard);

    QAction* pasteAct = menu.addAction(tr("Paste\tCtrl+Shift+V"));
    pasteAct->setEnabled(!QApplication::clipboard()->text().isEmpty());
    connect(pasteAct, &QAction::triggered,
            this,     &TerminalSurface::pasteFromClipboard);

    QAction* selectAllAct = menu.addAction(tr("Select All"));
    connect(selectAllAct, &QAction::triggered,
            this,         &TerminalSurface::selectAll);

    menu.addSeparator();

    QAction* clearAct = menu.addAction(tr("Clear Terminal"));
    connect(clearAct, &QAction::triggered, this, [this]() {
        emit clearTerminalRequested();
    });

    // Notification Bar 群組（子選單）
    QMenu* nbMenu = menu.addMenu(tr("Notification Bar"));

    QAction* nbShowAct = nbMenu->addAction(tr("Show"));
    connect(nbShowAct, &QAction::triggered, this, [this]() {
        emit showNotificationBarRequested();
    });

    QAction* nbTrimAct = nbMenu->addAction(tr("Remove Oldest 10 Messages"));
    connect(nbTrimAct, &QAction::triggered, this, [this]() {
        emit trimOldestNotificationsRequested(10);
    });

    QAction* nbClearAct = nbMenu->addAction(tr("Remove All Messages"));
    connect(nbClearAct, &QAction::triggered, this, [this]() {
        emit clearNotificationsRequested();
    });

    // Language 群組（子選單；checkable exclusive）。語言代碼用 Python canonical
    // 代碼（zh-Hant / en / zh-Hans），選擇後由 TerminuxTerminal 持久化 + RPC 通知。
    QMenu* langMenu = menu.addMenu(tr("Language"));
    QActionGroup* langGroup = new QActionGroup(langMenu);
    langGroup->setExclusive(true);

    struct LangOpt { const char* code; QString label; };
    const LangOpt langOpts[] = {
        {"zh-Hant", QStringLiteral("繁體中文")},
        {"en",      QStringLiteral("English")},
        {"zh-Hans", QStringLiteral("简体中文")},
    };
    for (const auto& opt : langOpts) {
        const QString code = QString::fromLatin1(opt.code);
        QAction* act = langMenu->addAction(opt.label);
        act->setCheckable(true);
        act->setChecked(currentLanguage_ == code);
        act->setActionGroup(langGroup);
        connect(act, &QAction::triggered, this, [this, code]() {
            emit languageChangeRequested(code);
        });
    }

    // Open URL — 只在選取為 http(s) URL 時加入
    if (hasSel) {
        static const QRegularExpression urlRe(
            QStringLiteral(R"(https?://\S+)"));
        const auto m = urlRe.match(selText);
        if (m.hasMatch()) {
            menu.addSeparator();
            const QString url = m.captured(0);
            QAction* urlAct = menu.addAction(tr("Open URL"));
            connect(urlAct, &QAction::triggered, this, [url]() {
                QDesktopServices::openUrl(QUrl(url));
            });
        }
    }

    menu.addSeparator();

    QAction* fontPlus = menu.addAction(tr("Font Size +\tCtrl+="));
    connect(fontPlus, &QAction::triggered, this, [this]() { adjustFontSize(1); });
    QAction* fontMinus = menu.addAction(tr("Font Size -\tCtrl+-"));
    connect(fontMinus, &QAction::triggered, this, [this]() { adjustFontSize(-1); });
    QAction* fontReset = menu.addAction(tr("Reset Font Size\tCtrl+0"));
    connect(fontReset, &QAction::triggered, this, [this]() { resetFontSize(); });

    menu.addSeparator();

    QAction* askAi = menu.addAction(tr("Ask AI about this"));
    askAi->setEnabled(hasSel);
    connect(askAi, &QAction::triggered, this, [this, selText]() {
        // 與 @@ 共用同一條 IPC 路徑，但來源標為 Selection：選取的文字永遠是資料。
        emit chatQueryDetected(selText, ChatQueryOrigin::Selection);
    });

    menu.exec(event->globalPos());
    event->accept();
}

}  // namespace terminux
