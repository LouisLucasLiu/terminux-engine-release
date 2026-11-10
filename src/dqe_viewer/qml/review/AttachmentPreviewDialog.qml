// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 安全的最大化 mock preview Dialog。

import QtQuick
import QtQuick.Controls
import "../theme"

Dialog {
    id: previewDialog
    objectName: "attachmentPreviewDialog"

    property string previewKind: ""
    property var previewPayload: null
    property string previewText: ""
    property url previewImageUrl: ""
    property string attachmentName: ""
    property bool productionSource: false
    signal previewClosed()

    parent: Overlay.overlay
    modal: true
    focus: true
    padding: 0
    closePolicy: Popup.CloseOnEscape
    width: parent ? Math.round(parent.width * 0.76) : 1000
    height: parent ? Math.round(parent.height * 0.76) : 700
    anchors.centerIn: parent

    onClosed: previewClosed()

    background: Rectangle {
        color: DqeTheme.panel
        border.width: DqeTheme.borderWidthStrong
        border.color: DqeTheme.accent
        radius: DqeTheme.radiusPanel
    }

    contentItem: Item {
        Text {
            id: dialogTitle
            anchors.left: parent.left
            anchors.leftMargin: DqeTheme.spacingL
            anchors.right: parent.right
            anchors.rightMargin: DqeTheme.spacingL
            anchors.top: parent.top
            anchors.topMargin: DqeTheme.spacingL
            text: previewDialog.attachmentName
            color: DqeTheme.accent
            font.family: DqeTheme.fontFamily
            font.pixelSize: 14
            elide: Text.ElideRight
        }

        AttachmentPreview {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: dialogTitle.bottom
            anchors.topMargin: DqeTheme.spacingM
            anchors.bottom: parent.bottom
            anchors.margins: DqeTheme.spacingL
            previewKind: previewDialog.previewKind
            previewPayload: previewDialog.previewPayload
            previewText: previewDialog.previewText
            previewImageUrl: previewDialog.previewImageUrl
            attachmentName: previewDialog.attachmentName
            maximized: true
            productionSource: previewDialog.productionSource
        }
    }
}
