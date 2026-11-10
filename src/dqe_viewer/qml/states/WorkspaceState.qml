// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// workspace 共用 canonical state。normal 不顯示 overlay；其餘狀態皆有文字與 marker。

import QtQuick
import "../theme"

Item {
    id: workspaceState
    objectName: "workspaceState"

    // normal / loading / empty / error / database_unavailable。
    property string state: "normal"
    property string message: ""
    property string detail: ""

    readonly property bool isNormal: state === "normal"
    readonly property string marker: {
        switch (state) {
        case "loading": return "⌛"
        case "empty": return "○"
        case "database_unavailable": return "×"
        case "error": return "!"
        default: return "•"
        }
    }
    readonly property color markerColor: {
        switch (state) {
        case "loading": return DqeTheme.accent
        case "empty": return DqeTheme.mutedText
        case "database_unavailable": return DqeTheme.fail
        case "error": return DqeTheme.fail
        default: return DqeTheme.mutedText
        }
    }

    visible: !isNormal
    implicitWidth: content.implicitWidth
    implicitHeight: content.implicitHeight

    Column {
        id: content
        anchors.centerIn: parent
        width: Math.max(0, parent.width - DqeTheme.spacingXl * 2)
        spacing: DqeTheme.spacingS

        StateMessage {
            width: parent.width
            marker: workspaceState.marker
            markerColor: workspaceState.markerColor
            message: workspaceState.message
            messageColor: DqeTheme.text
            messageSize: 16
        }
        Text {
            width: parent.width
            visible: workspaceState.detail.length > 0
            text: workspaceState.detail
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }
    }
}
