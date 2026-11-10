// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// attachment 清單。只傳遞明確 attachment ID，絕不根據檔名推論 preview kind。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: attachmentList

    property var attachments: []
    property string selectedAttachmentId: ""

    signal attachmentSelected(string attachmentId)
    signal attachmentActivated(string attachmentId)

    readonly property var _items: Array.isArray(attachments) ? attachments : []

    Text {
        id: listLabel
        anchors.left: parent.left
        anchors.leftMargin: DqeTheme.spacingM
        anchors.top: parent.top
        anchors.topMargin: DqeTheme.spacingM
        text: DqeI18n.t("ATTACHMENTS","附件","附件")
        color: DqeTheme.accent
        font.family: DqeTheme.fontFamily
        font.pixelSize: 11
        font.letterSpacing: 0.5
    }

    ListView {
        id: listView
        objectName: "attachmentListView"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: listLabel.bottom
        anchors.topMargin: DqeTheme.spacingS
        anchors.bottom: parent.bottom
        clip: true
        model: attachmentList._items
        spacing: DqeTheme.spacingXs
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.vertical: ScrollBar {}

        delegate: Item {
            id: row
            required property var modelData

            readonly property string attachmentId: modelData.attachmentId === undefined
                                                 ? "" : String(modelData.attachmentId)
            readonly property bool selected: attachmentId !== ""
                                           && attachmentList.selectedAttachmentId === attachmentId
            objectName: "attachmentRow" + attachmentId
            width: listView.width
            height: 48
            activeFocusOnTab: true

            Keys.onReturnPressed: attachmentList.attachmentActivated(attachmentId)
            Keys.onEnterPressed: attachmentList.attachmentActivated(attachmentId)

            Rectangle {
                anchors.fill: parent
                color: row.selected ? DqeTheme.selection : DqeTheme.panel
                border.width: row.selected ? DqeTheme.borderWidthStrong : DqeTheme.borderWidth
                border.color: row.selected ? DqeTheme.accent : DqeTheme.border
                radius: DqeTheme.radiusSmall
            }

            Text {
                anchors.left: parent.left
                anchors.leftMargin: DqeTheme.spacingM
                anchors.right: parent.right
                anchors.rightMargin: DqeTheme.spacingM
                anchors.top: parent.top
                anchors.topMargin: DqeTheme.spacingS
                text: modelData.displayName === undefined ? "—" : String(modelData.displayName)
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                elide: Text.ElideRight
            }
            Text {
                anchors.left: parent.left
                anchors.leftMargin: DqeTheme.spacingM
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.bottomMargin: DqeTheme.spacingS
                text: modelData.previewKind === undefined ? "—" : String(modelData.previewKind)
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 10
                elide: Text.ElideRight
            }

            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                onClicked: {
                    row.forceActiveFocus()
                    if (!row.selected && row.attachmentId !== "") {
                        attachmentList.attachmentSelected(row.attachmentId)
                    }
                }
                onDoubleClicked: attachmentList.attachmentActivated(row.attachmentId)
            }
        }
    }
}
