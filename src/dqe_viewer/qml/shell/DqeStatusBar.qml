// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import QtQuick
import QtQuick.Layouts
import "../theme"
import "../i18n"

// 26px 底部狀態列：資料來源、目前 context、操作/狀態訊息，
// 以及 mock 模式明確的 MOCK DATA 指示。
Rectangle {
    id: statusBar

    // ── 公開 API（契約）──
    property string sourceText: ""
    property string contextText: ""
    property string messageText: ""
    property bool mockMode: true

    implicitHeight: 26
    color: DqeTheme.headerBackground

    // 頂部 1px 分隔線
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: DqeTheme.borderWidth
        color: DqeTheme.border
    }

    // 小型「標籤 + 值」樣式
    component Slot: RowLayout {
        id: slot
        property string labelText: ""
        property string valueText: ""
        spacing: DqeTheme.spacingS
        Text {
            text: slot.labelText
            color: DqeTheme.accent
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            font.letterSpacing: 0.5
        }
        Text {
            text: slot.valueText
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            elide: Text.ElideRight
            Layout.maximumWidth: 420
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: DqeTheme.spacingL
        anchors.rightMargin: DqeTheme.spacingL
        spacing: DqeTheme.spacingXl

        Slot {
            labelText: DqeI18n.t("DATA SOURCE", "資料來源", "数据来源")
            valueText: statusBar.sourceText
        }
        Slot {
            labelText: DqeI18n.t("CONTEXT", "情境", "情境")
            valueText: statusBar.contextText
        }

        Item { Layout.fillWidth: true }

        Text {
            text: statusBar.messageText
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
        }
        Text {
            text: DqeI18n.t("MOCK DATA", "MOCK 資料", "MOCK 数据")
            visible: statusBar.mockMode
            color: DqeTheme.warn
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            font.bold: true
            font.letterSpacing: 0.5
        }
    }
}
