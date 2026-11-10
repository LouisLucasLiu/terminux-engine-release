// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/vterm_bridge.h"

#include <cstring>
#include <algorithm>

namespace terminux {

// ── ctor / dtor ─────────────────────────────────────────────────────────────

VTermBridge::VTermBridge(int rows, int cols, QObject* parent)
    : QObject(parent), rows_(rows), cols_(cols)
{
    vt_ = vterm_new(rows, cols);
    vterm_set_utf8(vt_, /*is_utf8=*/1);

    screen_ = vterm_obtain_screen(vt_);
    vterm_screen_enable_altscreen(screen_, /*altscreen=*/1);

    // callback 表必須是 static / 長壽生命期物件，這裡用 namespace-scope 變數
    // （function-local static 也行，但要確定不會被多個 instance 衝突；libvterm
    // 把它複製進內部，所以 stack-local 也 OK，這裡保險用 static const）
    static const VTermScreenCallbacks kCallbacks = {
        /* damage      */ &VTermBridge::onDamage,
        /* moverect    */ nullptr,
        /* movecursor  */ &VTermBridge::onMoveCursor,
        /* settermprop */ &VTermBridge::onSetTermProp,
        /* bell        */ &VTermBridge::onBell,
        /* resize      */ &VTermBridge::onResize,
        /* sb_pushline */ &VTermBridge::onSbPushLine,
        /* sb_popline  */ &VTermBridge::onSbPopLine,
        /* sb_clear    */ nullptr,
        /* sb_pushline4*/ nullptr,
    };
    vterm_screen_set_callbacks(screen_, &kCallbacks, this);
    vterm_screen_reset(screen_, /*hard=*/1);

    // 註冊 unrecognised OSC fallback（接 OSC 133 prompt markers）。
    // fallbacks_ 必須 member（libvterm 只存 pointer 不 copy struct）
    fallbacks_      = VTermStateFallbacks{};
    fallbacks_.osc  = &VTermBridge::onUnrecognisedOsc;
    VTermState* state = vterm_obtain_state(vt_);
    vterm_state_set_unrecognised_fallbacks(state, &fallbacks_, this);
}

VTermBridge::~VTermBridge()
{
    if (vt_) {
        vterm_free(vt_);
        vt_ = nullptr;
        screen_ = nullptr;
    }
}

// ── public API ──────────────────────────────────────────────────────────────

void VTermBridge::feedData(const QByteArray& data)
{
    if (data.isEmpty()) return;
    vterm_input_write(vt_, data.constData(), static_cast<size_t>(data.size()));
    // libvterm 會同步觸發 damage/movecursor/sb_* callbacks
}

void VTermBridge::resize(int rows, int cols)
{
    if (rows == rows_ && cols == cols_) return;
    vterm_set_size(vt_, rows, cols);
    // onResize callback 會更新 rows_/cols_ + emit termResized
}

CellData VTermBridge::cellAt(int row, int col) const
{
    if (row < 0 || col < 0 || row >= rows_ || col >= cols_) {
        return CellData{};   // out-of-range 回預設空 cell
    }
    VTermPos pos{row, col};
    VTermScreenCell cell;
    if (vterm_screen_get_cell(screen_, pos, &cell) == 0) {
        return CellData{};
    }
    return cellFromVTerm(cell);
}

QPoint VTermBridge::cursorPos() const     { return cursorPos_; }
bool   VTermBridge::cursorVisible() const { return cursorVisible_; }
int    VTermBridge::rows() const          { return rows_; }
int    VTermBridge::cols() const          { return cols_; }

int VTermBridge::scrollbackLineCount() const
{
    return static_cast<int>(scrollback_.size());
}

const std::vector<CellData>& VTermBridge::scrollbackLineAt(int index) const
{
    static const std::vector<CellData> kEmptyLine;
    if (index < 0 || index >= static_cast<int>(scrollback_.size())) {
        return kEmptyLine;
    }
    return scrollback_[index].cells;
}

int VTermBridge::sbPushCount() const { return sbPushCount_; }
int VTermBridge::sbPopCount()  const { return sbPopCount_; }

bool VTermBridge::bracketedPasteEnabled() const
{
    if (!vt_) return false;
    VTermState* state = vterm_obtain_state(vt_);
    return vterm_state_get_bracketpaste(state) != 0;
}

bool VTermBridge::isLineContinuation(int row) const
{
    if (!vt_ || row < 0 || row >= rows_) return false;
    VTermState* state = vterm_obtain_state(vt_);
    const VTermLineInfo* info = vterm_state_get_lineinfo(state, row);
    return info != nullptr && info->continuation != 0;
}

QString VTermBridge::cursorLogicalLineText() const
{
    if (rows_ <= 0 || cols_ <= 0) return {};

    const int curRow = cursorPos_.y();
    if (curRow < 0 || curRow >= rows_) return {};

    // 邏輯行起點：當前 row 若為 soft-wrap continuation，往上回溯到非 continuation 首列
    int startRow = curRow;
    while (startRow > 0 && isLineContinuation(startRow)) {
        --startRow;
    }
    // 邏輯行終點：下一 visible row 仍是 continuation → 屬同一邏輯行，往下延伸
    int endRow = curRow;
    while (endRow + 1 < rows_ && isLineContinuation(endRow + 1)) {
        ++endRow;
    }

    // 逐 row 逐 col 重建（慣例對齊 selectedText：0xFFFFFFFF=wide 右半跳過、
    // 0=空白、其餘=codepoint）。wrap continuation row 直接相接，不插分隔。
    QString result;
    result.reserve((endRow - startRow + 1) * cols_);
    for (int row = startRow; row <= endRow; ++row) {
        for (int col = 0; col < cols_; ++col) {
            const CellData cell = cellAt(row, col);
            if (cell.codepoint == 0xFFFFFFFFu) {
                continue;   // wide char 右半 cell
            }
            if (cell.codepoint == 0) {
                result += QLatin1Char(' ');
            } else {
                const char32_t cp = static_cast<char32_t>(cell.codepoint);
                result += QString::fromUcs4(&cp, 1);
            }
        }
    }
    // 邏輯行尾端 trailing space 去除（行尾留白不算內容；wrap 中間列為滿列無尾白）
    while (result.endsWith(QLatin1Char(' '))) result.chop(1);
    return result;
}

// ── cell 轉換 ────────────────────────────────────────────────────────────────

CellData VTermBridge::cellFromVTerm(const VTermScreenCell& cell) const
{
    CellData out;
    out.codepoint = cell.chars[0];
    out.width     = cell.width;

    // fg：保留 indexed 來源（給 ColorScheme 覆蓋用），同時也填 RGB（給 truecolor 路徑）
    {
        VTermColor fg = cell.fg;  // 複本（convert_color_to_rgb 會就地修改）
        if (VTERM_COLOR_IS_DEFAULT_FG(&fg)) {
            out.fgDefault = true;
        } else {
            if (VTERM_COLOR_IS_INDEXED(&fg)) {
                out.fgIndexed = true;
                out.fgIndex   = fg.indexed.idx;
                vterm_screen_convert_color_to_rgb(screen_, &fg);
            }
            out.fgDefault = false;
            out.fgR = fg.rgb.red;
            out.fgG = fg.rgb.green;
            out.fgB = fg.rgb.blue;
        }
    }
    // bg
    {
        VTermColor bg = cell.bg;
        if (VTERM_COLOR_IS_DEFAULT_BG(&bg)) {
            out.bgDefault = true;
        } else {
            if (VTERM_COLOR_IS_INDEXED(&bg)) {
                out.bgIndexed = true;
                out.bgIndex   = bg.indexed.idx;
                vterm_screen_convert_color_to_rgb(screen_, &bg);
            }
            out.bgDefault = false;
            out.bgR = bg.rgb.red;
            out.bgG = bg.rgb.green;
            out.bgB = bg.rgb.blue;
        }
    }

    out.bold          = cell.attrs.bold;
    out.italic        = cell.attrs.italic;
    out.underline     = (cell.attrs.underline != VTERM_UNDERLINE_OFF);
    out.strikethrough = cell.attrs.strike;
    out.reverse       = cell.attrs.reverse;
    return out;
}

void VTermBridge::cellToVTerm(const CellData& src, VTermScreenCell* dst)
{
    std::memset(dst, 0, sizeof *dst);
    dst->chars[0] = src.codepoint;
    dst->width    = static_cast<char>(src.width > 0 ? src.width : 1);

    if (src.fgDefault) {
        dst->fg.type = VTERM_COLOR_RGB | VTERM_COLOR_DEFAULT_FG;
    } else {
        dst->fg.type = VTERM_COLOR_RGB;
        dst->fg.rgb.red   = src.fgR;
        dst->fg.rgb.green = src.fgG;
        dst->fg.rgb.blue  = src.fgB;
    }
    if (src.bgDefault) {
        dst->bg.type = VTERM_COLOR_RGB | VTERM_COLOR_DEFAULT_BG;
    } else {
        dst->bg.type = VTERM_COLOR_RGB;
        dst->bg.rgb.red   = src.bgR;
        dst->bg.rgb.green = src.bgG;
        dst->bg.rgb.blue  = src.bgB;
    }

    dst->attrs.bold      = src.bold ? 1 : 0;
    dst->attrs.italic    = src.italic ? 1 : 0;
    dst->attrs.underline =
        src.underline ? VTERM_UNDERLINE_SINGLE : VTERM_UNDERLINE_OFF;
    dst->attrs.strike    = src.strikethrough ? 1 : 0;
    dst->attrs.reverse   = src.reverse ? 1 : 0;
}

// ── callbacks ───────────────────────────────────────────────────────────────

int VTermBridge::onDamage(VTermRect rect, void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    // libvterm rect 是 half-open（end exclusive）；signal 用 inclusive
    if (rect.end_row <= rect.start_row || rect.end_col <= rect.start_col) {
        return 1;  // 空 rect 不發
    }
    emit self->damageReceived(rect.start_row, rect.start_col,
                              rect.end_row - 1, rect.end_col - 1);
    return 1;
}

int VTermBridge::onMoveCursor(VTermPos pos, VTermPos /*oldpos*/,
                              int visible, void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    self->cursorPos_     = QPoint(pos.col, pos.row);
    self->cursorVisible_ = (visible != 0);
    emit self->cursorChanged();
    return 1;
}

int VTermBridge::onSetTermProp(VTermProp prop, VTermValue* val, void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    switch (prop) {
    case VTERM_PROP_TITLE:
        // VTermStringFragment 可能跨多次 callback；用 initial/final flag 累積
        if (val->string.initial) {
            self->titleBuffer_.clear();
        }
        self->titleBuffer_.append(val->string.str,
                                  static_cast<int>(val->string.len));
        if (val->string.final) {
            emit self->titleChanged(QString::fromUtf8(self->titleBuffer_));
            self->titleBuffer_.clear();
        }
        return 1;

    case VTERM_PROP_CURSORVISIBLE:
        self->cursorVisible_ = (val->boolean != 0);
        emit self->cursorChanged();
        return 1;

    case VTERM_PROP_CURSORSHAPE: {
        // libvterm: 1=BLOCK, 2=UNDERLINE, 3=BAR_LEFT
        CursorShape shape = CursorShape::Block;
        switch (val->number) {
        case VTERM_PROP_CURSORSHAPE_UNDERLINE: shape = CursorShape::Underline; break;
        case VTERM_PROP_CURSORSHAPE_BAR_LEFT:  shape = CursorShape::Bar;       break;
        case VTERM_PROP_CURSORSHAPE_BLOCK:
        default:                               shape = CursorShape::Block;    break;
        }
        if (shape != self->cursorShape_) {
            self->cursorShape_ = shape;
            emit self->cursorShapeChanged(shape);
        }
        return 1;
    }

    default:
        // ALTSCREEN / MOUSE / 等：libvterm 內部已處理，回 0 表示「沒興趣」
        return 0;
    }
}

int VTermBridge::onBell(void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    emit self->bellRing();
    return 1;
}

int VTermBridge::onResize(int rows, int cols, void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    self->rows_ = rows;
    self->cols_ = cols;
    emit self->termResized(rows, cols);
    return 1;
}

int VTermBridge::onSbPushLine(int cols, const VTermScreenCell* cells, void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    ScrollbackLine line;
    line.cells.reserve(static_cast<size_t>(cols));
    for (int i = 0; i < cols; ++i) {
        line.cells.push_back(self->cellFromVTerm(cells[i]));
    }
    self->scrollback_.push_front(std::move(line));
    if (self->scrollback_.size() > static_cast<size_t>(kMaxScrollbackLines)) {
        self->scrollback_.pop_back();
    }
    ++self->sbPushCount_;
    return 1;
}

int VTermBridge::onUnrecognisedOsc(int command, VTermStringFragment frag, void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    if (command != 133) return 0;          // 非 133 不處理（讓 libvterm 預設行為）
    if (!frag.initial || !frag.final) return 0;   // 跨 fragment 不處理（OSC 133 不應發生）

    const QByteArray payload(frag.str, static_cast<int>(frag.len));
    if (payload.isEmpty()) return 0;

    // payload 開頭判型：A / B / C / D[;exit_code]
    const char marker = payload.at(0);
    switch (marker) {
    case 'A':
        emit self->promptMarker(PromptMarkerType::PrePrompt, 0);
        return 1;
    case 'C':
        emit self->promptMarker(PromptMarkerType::CommandBegin, 0);
        return 1;
    case 'D': {
        int exitCode = 0;
        const int semi = payload.indexOf(';');
        if (semi > 0) {
            bool ok = false;
            const int parsed = payload.mid(semi + 1).toInt(&ok);
            if (ok) exitCode = parsed;
        }
        emit self->promptMarker(PromptMarkerType::CommandEnd, exitCode);
        return 1;
    }
    default:
        // 'B'（PostPrompt）等其他 marker 暫不處理
        return 0;
    }
}

int VTermBridge::onSbPopLine(int cols, VTermScreenCell* cells, void* user)
{
    auto* self = static_cast<VTermBridge*>(user);
    if (self->scrollback_.empty()) {
        return 0;   // 沒得 pop
    }
    const ScrollbackLine line = std::move(self->scrollback_.front());
    self->scrollback_.pop_front();

    const int n = std::min(cols, static_cast<int>(line.cells.size()));
    for (int i = 0; i < n; ++i) {
        cellToVTerm(line.cells[i], &cells[i]);
    }
    // 剩餘 col 補空白
    for (int i = n; i < cols; ++i) {
        std::memset(&cells[i], 0, sizeof cells[i]);
        cells[i].chars[0] = ' ';
        cells[i].width    = 1;
        cells[i].fg.type  = VTERM_COLOR_RGB | VTERM_COLOR_DEFAULT_FG;
        cells[i].bg.type  = VTERM_COLOR_RGB | VTERM_COLOR_DEFAULT_BG;
    }
    ++self->sbPopCount_;
    return 1;
}

}  // namespace terminux
