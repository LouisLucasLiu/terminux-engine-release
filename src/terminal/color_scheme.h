// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

// 色彩方案。header-only inline 函式（避免在現有 lib 加 .cpp）。
//
// ColorScheme 內 palette[256]：
//   0..7    = 16 色 ANSI normal（black/red/green/yellow/blue/magenta/cyan/white）
//   8..15   = 16 色 ANSI bright
//   16..231 = 6×6×6 RGB cube（標準 xterm 演算法）
//   232..255 = 24-step grayscale ramp

#include <QColor>
#include <QString>

#include <array>

namespace terminux {

struct ColorScheme {
    QString                  name;
    QColor                   foreground;
    QColor                   background;
    QColor                   cursor;
    QColor                   selection;            // 選取目前以 fg/bg swap 呈現，此欄位預留
    std::array<QColor, 256>  palette{};
};

/// 把 16-231 的 6×6×6 cube 與 232-255 的 grayscale 填入 palette。
/// 0..15 由 caller 自行填好。
inline void fillExtendedPalette(std::array<QColor, 256>& palette)
{
    // 16..231：6×6×6 cube（每個 channel 取值 0, 95, 135, 175, 215, 255）
    static constexpr int cubeSteps[6] = {0, 95, 135, 175, 215, 255};
    for (int r = 0; r < 6; ++r) {
        for (int g = 0; g < 6; ++g) {
            for (int b = 0; b < 6; ++b) {
                const int idx = 16 + r * 36 + g * 6 + b;
                palette[idx] = QColor(cubeSteps[r], cubeSteps[g], cubeSteps[b]);
            }
        }
    }
    // 232..255：24-step grayscale（從 8 到 238，每步 +10）
    for (int i = 0; i < 24; ++i) {
        const int v = 8 + i * 10;
        palette[232 + i] = QColor(v, v, v);
    }
}

/// One Dark 風格的深色預設方案（前 16 色取 One Dark / Atom 慣用色值）
inline ColorScheme defaultDarkScheme()
{
    ColorScheme s;
    s.name       = QStringLiteral("Terminux Dark (One-Dark-ish)");
    s.foreground = QColor(0xAB, 0xB2, 0xBF);
    s.background = QColor(0x28, 0x2C, 0x34);
    s.cursor     = QColor(0x52, 0x8B, 0xFF);
    s.selection  = QColor(0x3E, 0x44, 0x51);

    // 0..7  normal
    s.palette[0] = QColor(0x28, 0x2C, 0x34);   // black（≈bg）
    s.palette[1] = QColor(0xE0, 0x6C, 0x75);   // red
    s.palette[2] = QColor(0x98, 0xC3, 0x79);   // green
    s.palette[3] = QColor(0xE5, 0xC0, 0x7B);   // yellow
    s.palette[4] = QColor(0x61, 0xAF, 0xEF);   // blue
    s.palette[5] = QColor(0xC6, 0x78, 0xDD);   // magenta
    s.palette[6] = QColor(0x56, 0xB6, 0xC2);   // cyan
    s.palette[7] = QColor(0xAB, 0xB2, 0xBF);   // white（≈fg）
    // 8..15 bright（基本上是 normal 提亮版；One Dark 取相近色）
    s.palette[8]  = QColor(0x5C, 0x63, 0x70);  // bright black（grey）
    s.palette[9]  = QColor(0xE0, 0x6C, 0x75);  // bright red（同 normal red）
    s.palette[10] = QColor(0x98, 0xC3, 0x79);  // bright green
    s.palette[11] = QColor(0xE5, 0xC0, 0x7B);  // bright yellow
    s.palette[12] = QColor(0x61, 0xAF, 0xEF);  // bright blue
    s.palette[13] = QColor(0xC6, 0x78, 0xDD);  // bright magenta
    s.palette[14] = QColor(0x56, 0xB6, 0xC2);  // bright cyan
    s.palette[15] = QColor(0xFF, 0xFF, 0xFF);  // bright white

    fillExtendedPalette(s.palette);
    return s;
}

}  // namespace terminux
