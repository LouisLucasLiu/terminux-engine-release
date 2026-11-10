// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

// 最簡渲染 widget。
// 從 VTermBridge 讀 cell grid，QPainter 畫到自身。鍵盤事件 → keyInput signal。
// 不接 PTY、不接 MainWindow。@@ 偵測：Enter 時讀 PTY cursor 邏輯行（含 wrap
// unwrap + prompt prefix strip heuristic），不再依賴 keystroke 累積 buffer。

#include "terminal/chat_query_origin.h"
#include "terminal/color_scheme.h"

#include <QElapsedTimer>
#include <QFont>
#include <QPoint>
#include <QRect>
#include <QSize>
#include <QString>
#include <QWidget>

class QTimer;
class QContextMenuEvent;

namespace terminux {

class VTermBridge;
struct CellData;

/// 選取座標。row 是 virtualRow（統一 scrollback + visible 的 0-based 編號，
/// 0=最舊 scrollback、scrollbackLineCount-1=最新 scrollback、
/// scrollbackLineCount..scrollbackLineCount+rows-1=visible screen 0..rows-1）。
struct CellPos {
    int row = 0;
    int col = 0;
};

class TerminalSurface : public QWidget {
    Q_OBJECT
public:
    explicit TerminalSurface(VTermBridge* bridge, QWidget* parent = nullptr);

    qreal cellWidth() const;
    qreal cellHeight() const;

    /// 從 widget pixel 座標換到 cell 座標（給選取等功能使用）
    QPoint pixelToCell(const QPoint& pos) const;

    /// IME / 候選窗定位（Qt 會 query 這個 widget）
    QVariant inputMethodQuery(Qt::InputMethodQuery query) const override;

    /// scrollback：0=底部、>0=往上看舊內容（最大 = scrollbackLineCount）
    int  scrollOffset() const { return scrollOffset_; }
    void setScrollOffset(int offset);

    /// 選取相關 API
    bool    hasSelection() const { return hasSelection_; }
    QString selectedText() const;
    void    clearSelection();
    void    copySelectionToClipboard();
    void    pasteFromClipboard();
    void    pasteFromPrimary();   // X11 PRIMARY selection（滑鼠中鍵 paste）
    void    selectAll();          // 右鍵選單 / Ctrl+Shift+A

    /// Ctrl+click URL：cellPos 落在 URL pattern 內回那個 URL，沒匹配回空字串
    QString urlAtVirtual(CellPos pos) const;

    /// font size 調整（context menu / Ctrl+= / Ctrl+- / Ctrl+0）
    void adjustFontSize(int delta);   // delta=+1 放大、-1 縮小，clamp 到 [6, 36]
    void resetFontSize();             // 回 baseFontSize_
    int  fontPointSize() const { return font_.pointSize(); }

    /// 色彩方案（可在 runtime 切換）
    const ColorScheme& colorScheme() const { return colorScheme_; }
    void               setColorScheme(const ColorScheme& scheme);

    /// preferred language（context menu 勾選狀態）。code 為 canonical 代碼
    /// （zh-Hant / en / zh-Hans）。setter 僅更新勾選狀態，不發 signal。
    const QString& currentLanguage() const { return currentLanguage_; }
    void           setCurrentLanguage(const QString& languageCode);

signals:
    /// 使用者鍵入：bytes 已經 encode 為 PTY input（特殊鍵 → escape sequence）
    void keyInput(const QByteArray& data);

    /// `@@` 開頭 + Enter → query 為 `@@` 後內容（已 trim）
    /// `@@` 輸入（AtCommand）或右鍵選字（Selection）；文字本身不做任何改寫。
    void chatQueryDetected(const QString& query, ChatQueryOrigin origin);

    /// 一般命令 + Enter（非 @@）→ 送出已 trim 的命令字串供 IPC 層做 lineage check
    void commandSubmitted(const QString& command);

    /// 滑鼠滾輪請求往上 N 行（正值=往舊看；負值=往新看）
    void scrollRequested(int linesUp);

    /// 給 TerminuxTerminal 觸發 rows/cols sync（避開 Qt layout 順序問題）
    void surfaceResized(QSize newSize);

    /// 選取狀態變化（給未來 context menu 灰掉/啟用 Copy 用）
    void selectionChanged(bool hasSelection);

    /// context menu「Clear Terminal」→ TerminuxTerminal 處理
    void clearTerminalRequested();

    /// context menu「Notification Bar → Show」→ TerminuxTerminal 重新顯示 NotificationBar
    void showNotificationBarRequested();

    /// context menu「Notification Bar → 移除最舊 N 筆」→ 移除最舊 count 筆通知
    void trimOldestNotificationsRequested(int count);

    /// context menu「Notification Bar → 移除全部」→ 清空所有通知
    void clearNotificationsRequested();

    /// context menu「Language → 選擇語言」→ TerminuxTerminal 持久化 + RPC 通知 daemon。
    /// code 為 Python 端 canonical 語言代碼（zh-Hant / en / zh-Hans）。
    void languageChangeRequested(const QString& languageCode);

protected:
    void paintEvent(QPaintEvent* event) override;
    void keyPressEvent(QKeyEvent* event) override;
    void resizeEvent(QResizeEvent* event) override;
    void focusInEvent(QFocusEvent* event) override;
    void focusOutEvent(QFocusEvent* event) override;
    void inputMethodEvent(class QInputMethodEvent* event) override;
    void wheelEvent(class QWheelEvent* event) override;
    void mousePressEvent  (class QMouseEvent* event) override;
    void mouseMoveEvent   (class QMouseEvent* event) override;
    void mouseReleaseEvent(class QMouseEvent* event) override;
    void contextMenuEvent (QContextMenuEvent* event) override;

private slots:
    void onDamage(int startRow, int startCol, int endRow, int endCol);
    void onCursorChanged();
    void onCursorBlinkTick();

private:
    void       drawCellAt(QPainter& painter, int viewRow, int col,
                          const CellData& cell, bool selected = false);
    void       drawEmptyCell(QPainter& painter, int viewRow, int col,
                             bool selected = false);
    void       drawCursor(QPainter& painter);
    void       drawPreedit(QPainter& painter);
    QByteArray encodeKey(class QKeyEvent* event) const;
    /// 共用：把字串作為 paste 負載送進 PTY（bracketed paste markers / nl 處理），
    /// pasteFromClipboard / pasteFromPrimary 共用
    void       pastePayload(const QString& text);
    void       recalcCellMetrics();
    int        maxScrollOffset() const;   // = bridge_->scrollbackLineCount()

    /// 從 cursor 邏輯行字串中 strip prompt prefix，回 user input 部分。
    /// Heuristic：從尾巴往前找最後一個 prompt sigil（"> " / "$ " / "% " / "# "），
    /// sigil + 後續空白之後的 substring 即為 user input。沒找到 sigil 時整行回
    /// trimmed（fallback）。
    /// 已知限制：user input 含 sigil（例如 "@@compare > 5"）會被誤切；待 OSC 133;B
    /// 標準化後改成精確判定（已知限制：shell hook 不發 ;B）。
    static QString stripPromptPrefix(const QString& logicalLine);

    // cursor 與字型 helpers
    void   resetCursorBlink();                       // 鍵盤輸入時呼叫
    void   updateCursorRect(QPoint cellPos);         // 精確區域 update
    QFont  fontForAttrs(bool bold, bool italic) const;
    QColor fgForCell(const CellData& cell) const;    // 取代 static 版本（讀 colorScheme_）
    QColor bgForCell(const CellData& cell) const;

    // font cache 重建（adjust/reset font size 後呼叫）
    void rebuildFontCache();
    static constexpr int kMinFontSize = 6;
    static constexpr int kMaxFontSize = 36;

    // selection helpers
    int      viewRowToVirtual(int viewRow) const;
    CellPos  pixelToVirtualCell(const QPoint& pos) const;
    CellData cellAtVirtual(int virtualRow, int col) const;
    bool     isCellSelected(int virtualRow, int col) const;
    void     selectWord(CellPos pos);
    void     selectLine(CellPos pos);
    void     normalizedSelection(CellPos& start, CellPos& end) const;
    QRect    selectionPaintRect() const;
    void     setSelectionState(bool has);   // emit selectionChanged + dirty update

    // 色彩一律由 member 版本 fgForCell/bgForCell 決定（讀 colorScheme_）。

    VTermBridge* bridge_;
    QFont        font_;
    QFont        fontBold_;
    QFont        fontItalic_;
    QFont        fontBoldItalic_;
    qreal        cellWidth_  = 0.0;
    qreal        cellHeight_ = 0.0;

    ColorScheme  colorScheme_;
    int          baseFontSize_ = 11;     // reset 用的初始值

    // preferred language（context menu 勾選狀態；canonical 代碼）。預設 zh-Hant，
    // 與 Python preferences.DEFAULT_LANGUAGE 一致。
    QString      currentLanguage_ = QStringLiteral("zh-Hant");

    // cursor blink
    QTimer*      cursorBlinkTimer_   = nullptr;
    bool         cursorBlinkVisible_ = true;
    QPoint       lastCursorPos_{0, 0};

    // 渲染節流（damage union → 16ms timer flush）
    QTimer*      renderThrottleTimer_ = nullptr;
    QRect        pendingDirtyRect_;
    static constexpr int kRenderThrottleMs = 16;   // ~60fps cap

    // Phase T 方案 B：command input 改由 bridge_->cursorLogicalLineText() 從
    // PTY 讀取 cursor 當前邏輯行（含 wrap），不再維護 keystroke buffer。
    // 這樣 history(↑↓) / Tab completion / Ctrl-x / arrow + 中插字 都自然反映
    // 在 PTY 顯示行上，不會與 buffer drift。
    QString preeditString_;       // IME composing 中間狀態（畫在 cursor 上）
    bool    hasFocus_ = false;

    int     scrollOffset_ = 0;    // scrollback 視圖

    // selection state
    bool          selecting_    = false;   // 拖曳中
    bool          hasSelection_ = false;
    QRect         selectionPaintRect_;
    CellPos       selAnchor_{};            // 拖曳起點（virtualRow）

    // mouseMove 節流：每秒可能 fire 50+ 次，全 widget update 太貴；
    // 用單發 timer 合併 16ms 內多次 move
    QTimer*       selectionUpdateTimer_ = nullptr;
    CellPos       pendingSelEnd_{};
    bool          hasPendingSel_ = false;
    CellPos       selEnd_{};               // 當前/結束位置
    int           clickCount_   = 0;       // 1=normal, 2=word, 3=line
    QElapsedTimer lastClickTimer_;
    CellPos       lastClickPos_{};
};

}  // namespace terminux
