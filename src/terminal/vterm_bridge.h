// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

// libvterm adapter。
// 把 libvterm C API 包成 Qt-friendly 的 C++ 類別：cell grid 查詢、scrollback、
// alternate screen、damage / cursor / title / bell signal。
// 不做渲染、不接 PTY（由 TerminalSurface 與 TerminuxTerminal 負責）。

#include <QByteArray>
#include <QObject>
#include <QPoint>
#include <QString>

#include <cstdint>
#include <deque>
#include <vector>

#include <vterm.h>  // libvterm types 在 callback 簽名中出現，必須完整型別

namespace terminux {

/// 給渲染層用的 cell 資料結構。RGB 已經把 indexed color 轉好；
/// 起也保留 indexed flag/index 供色彩方案覆蓋（bold→bright 等）。
struct CellData {
    uint32_t codepoint = ' ';   // 0 表示 wide char 後半 cell
    int      width     = 1;     // 1 / 2（CJK）/ 0（wide char 後半）

    bool fgDefault = true;
    bool bgDefault = true;
    uint8_t fgR = 0, fgG = 0, fgB = 0;
    uint8_t bgR = 0, bgG = 0, bgB = 0;

    // 色彩來源旗標（給 ColorScheme 覆蓋用）
    bool    fgIndexed = false;   // true → 來自 256-color palette
    uint8_t fgIndex   = 0;       // fgIndexed=true 時有效（0..255）
    bool    bgIndexed = false;
    uint8_t bgIndex   = 0;

    bool bold          = false;
    bool italic        = false;
    bool underline     = false;
    bool strikethrough = false;
    bool reverse       = false;
};

/// scrollback 一行（保留完整 attrs，記憶體預算約 13–19 MB / 10K 行 / 80 cols）
struct ScrollbackLine {
    std::vector<CellData> cells;
};

class VTermBridge : public QObject {
    Q_OBJECT
public:
    /// cursor 形狀（DECSCUSR / VTERM_PROP_CURSORSHAPE）
    enum class CursorShape { Block, Underline, Bar };

    static constexpr int kMaxScrollbackLines = 10000;

    explicit VTermBridge(int rows, int cols, QObject* parent = nullptr);
    ~VTermBridge() override;

    /// 餵 PTY 輸出進來。libvterm 同步呼叫 callbacks → emit signals。
    void feedData(const QByteArray& data);

    /// 改變 terminal 尺寸。會觸發 resize callback → emit termResized。
    void resize(int rows, int cols);

    /// 查詢 visible screen 的 cell（row 0=頂端，col 0=最左）
    CellData cellAt(int row, int col) const;

    QPoint cursorPos() const;       // x=col, y=row
    bool   cursorVisible() const;

    int rows() const;
    int cols() const;

    int                          scrollbackLineCount() const;
    const std::vector<CellData>& scrollbackLineAt(int index) const;  // 0 = 最近捲出的行

    /// 測試用：累積 sb_pushline / sb_popline 觸發次數
    int sbPushCount() const;
    int sbPopCount() const;

    /// cursor 形狀（DECSCUSR）
    CursorShape cursorShape() const { return cursorShape_; }

    /// shell 是否啟用 bracketed paste mode（DECSET ?2004h）。
    /// paste 路徑根據此值決定是否包 \x1b[200~ ... \x1b[201~（不啟用就送純文字，
    /// 否則 tcsh/csh 等不認 markers 的 shell 會把 ESC 序列當髒字元混入指令）。
    bool bracketedPasteEnabled() const;

    /// 此 visible row 是否為前一行的 line wrap continuation（true → 邏輯上是
    /// 同一行被軟換行）。row 越界 / 非 visible 區回 false。
    /// 用途：selectedText 跨 row unwrap，paste 時可選擇移除 wrap \n。
    bool isLineContinuation(int row) const;

    /// cursor 所在「邏輯行」的完整文字（含游標之後；跨 soft-wrap continuation
    /// row 合併為單一邏輯行）。row 0=visible 頂端；只看 visible screen（cursor
    /// 必在 visible 區）。trailing space 去除。空 / 越界 → 空字串。
    /// 用途（Phase T 方案 B）：Enter 時取真實顯示內容供 @@ 偵測 / command submit，
    /// 取代易與 PTY desync 的 keystroke buffer。重建慣例對齊 selectedText：
    /// codepoint 0xFFFFFFFF=wide char 右半（跳過）、0=空白、其餘=該字元。
    QString cursorLogicalLineText() const;

    /// OSC 133 prompt semantic markers
    enum class PromptMarkerType {
        PrePrompt,    // OSC 133;A — 命令結束 / prompt 即將顯示
        CommandBegin, // OSC 133;C — user 確定執行命令
        CommandEnd    // OSC 133;D[;exit_code] — 命令結束（帶 exit code）
    };

signals:
    /// rect 為 [startRow..endRow, startCol..endCol] inclusive
    void damageReceived(int startRow, int startCol, int endRow, int endCol);
    void termResized(int rows, int cols);
    void cursorChanged();
    void bellRing();
    void titleChanged(const QString& title);

    /// cursor 形狀變化（block / underline / bar）
    void cursorShapeChanged(CursorShape shape);

    /// OSC 133 prompt marker 收到（exitCode 只在 CommandEnd 有意義）
    void promptMarker(PromptMarkerType type, int exitCode);

private:
    // member helper（非 static，因需要 screen_ 做 indexed→RGB 轉換）
    CellData cellFromVTerm(const VTermScreenCell& cell) const;
    static void cellToVTerm(const CellData& src, VTermScreenCell* dst);

    // libvterm callbacks（C function pointer，static + user→this）
    static int onDamage(VTermRect rect, void* user);
    static int onMoveCursor(VTermPos pos, VTermPos oldpos, int visible, void* user);
    static int onSetTermProp(VTermProp prop, VTermValue* val, void* user);
    static int onBell(void* user);
    static int onResize(int rows, int cols, void* user);
    static int onSbPushLine(int cols, const VTermScreenCell* cells, void* user);
    static int onSbPopLine(int cols, VTermScreenCell* cells, void* user);

    /// unrecognised OSC fallback（OSC 133 prompt markers 走這邊）
    static int onUnrecognisedOsc(int command, VTermStringFragment frag, void* user);

    VTerm*       vt_     = nullptr;
    VTermScreen* screen_ = nullptr;
    int          rows_;
    int          cols_;
    QPoint       cursorPos_{0, 0};
    bool         cursorVisible_ = true;

    std::deque<ScrollbackLine> scrollback_;
    QByteArray                 titleBuffer_;

    // 觀測計數（測試用，不影響功能）
    int sbPushCount_ = 0;
    int sbPopCount_  = 0;

    // cursor shape state
    CursorShape cursorShape_ = CursorShape::Block;

    // fallbacks struct 必須是 member！libvterm 只存 pointer 不 copy
    // (state.c: `state->fallbacks = fallbacks;`)，stack/local 變數會 dangle
    VTermStateFallbacks fallbacks_{};
};

}  // namespace terminux
