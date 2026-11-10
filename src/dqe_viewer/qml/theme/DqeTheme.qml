// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
pragma Singleton
import QtQuick

// DQE Viewer 設計 tokens 的唯一來源。
// 任何 QML 都必須從此 singleton 取值，禁止散落硬編碼字面量。
QtObject {
    // ── 顏色 tokens ──
    readonly property color background: "#06131F"
    readonly property color headerBackground: "#050E17"
    readonly property color panel: "#0A1C2A"
    readonly property color panelAlternate: "#0D2434"
    readonly property color panelRaised: "#102B3D"
    readonly property color border: "#20465A"
    readonly property color borderStrong: "#2E6177"
    readonly property color text: "#D8E9F2"
    readonly property color mutedText: "#7FA2B5"
    readonly property color faintText: "#45697C"
    readonly property color accent: "#12C8E8"
    readonly property color pass: "#32D17D"
    readonly property color warn: "#F6B94B"
    readonly property color fail: "#FF5C67"
    readonly property color unknown: "#9AAEB9"
    readonly property color selection: "#123D52"

    // ── 間距 tokens（2/4/8/12/16/24 px）──
    readonly property int spacingXxs: 2
    readonly property int spacingXs: 4
    readonly property int spacingS: 8
    readonly property int spacingM: 12
    readonly property int spacingL: 16
    readonly property int spacingXl: 24

    // ── 邊框寬度與圓角 ──
    readonly property int borderWidth: 1
    readonly property int borderWidthStrong: 2
    readonly property int radiusSmall: 4
    readonly property int radiusPanel: 5

    // ── 字型：使用平台 UI 字型（決定性 fallback）；不 bundle 新字型 ──
    readonly property string fontFamily: Qt.application.font.family
}
