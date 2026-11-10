// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// canonical state 訊息。文字與 marker 固定成對呈現，狀態意義不依賴顏色。

import QtQuick
import "../theme"

Item {
    id: stateMessage

    property string marker: "•"
    property string message: ""
    property color markerColor: DqeTheme.mutedText
    property color messageColor: DqeTheme.text
    property int messageSize: 14

    implicitWidth: content.implicitWidth
    implicitHeight: content.implicitHeight

    Row {
        id: content
        anchors.centerIn: parent
        spacing: DqeTheme.spacingS

        Text {
            objectName: "stateMarker"
            text: stateMessage.marker
            color: stateMessage.markerColor
            font.family: DqeTheme.fontFamily
            font.pixelSize: stateMessage.messageSize + 2
            font.bold: true
            verticalAlignment: Text.AlignVCenter
        }
        Text {
            objectName: "stateMessageText"
            width: Math.max(0, stateMessage.width - stateMessage.messageSize * 2)
            text: stateMessage.message
            color: stateMessage.messageColor
            font.family: DqeTheme.fontFamily
            font.pixelSize: stateMessage.messageSize
            wrapMode: Text.Wrap
            verticalAlignment: Text.AlignVCenter
        }
    }
}
