// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import QtQuick
import "../theme"

// mock placeholder：填入 DqeWorkspaceShell 的 content slot。
// 只做 labeled placeholder，不含 Summary Cards / table / Inspector 欄位 /
// attachment / chart 等任何 workspace 內容（那些屬於各自的元件）。
Item {
    id: region
    property string regionLabel: ""
    property string workspaceKey: ""

    Column {
        anchors.centerIn: parent
        spacing: DqeTheme.spacingS
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: region.regionLabel
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 15
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            visible: region.workspaceKey.length > 0
            text: region.workspaceKey + " · mock placeholder"
            color: DqeTheme.faintText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
        }
    }
}
