// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../theme"
import "../i18n"

// 48px 應用 chrome 頂列：身分標識、compact context、workspace tabs、
// Refresh（僅 signal）、mock 連線指示。禁止出現 DB 路徑 / Retention / Check-in /
// Purge / 版本或 lineage 控制 / 任何真實 IPC。
Rectangle {
    id: header

    // ── 公開 API（契約）──
    property int activeWorkspace: WorkspaceTabs.Archived
    property string contextText: ""
    property string connectionState: "Connected"
    signal workspaceRequested(int workspace)
    signal refreshRequested()

    implicitHeight: 48
    color: DqeTheme.headerBackground

    // 底部 1px 分隔線
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: DqeTheme.borderWidth
        color: DqeTheme.border
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: DqeTheme.spacingL
        anchors.rightMargin: DqeTheme.spacingL
        spacing: DqeTheme.spacingL

        // ── 身分標識：DQE Viewer ──
        RowLayout {
            spacing: DqeTheme.spacingS
            Text {
                text: "DQE Viewer"
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 16
                font.bold: true
            }
        }

        // ── compact context ──
        Text {
            text: header.contextText
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 13
            elide: Text.ElideRight
            Layout.maximumWidth: 260
        }

        Item { Layout.fillWidth: true }

        // ── workspace tabs（置中）──
        WorkspaceTabs {
            id: workspaceTabs
            objectName: "workspaceTabs"
            activeWorkspace: header.activeWorkspace
            onWorkspaceRequested: (workspace) => header.workspaceRequested(workspace)
        }

        Item { Layout.fillWidth: true }

        // ── 語言選單（en / 繁 / 簡；即時切換 DqeI18n.language）──
        AbstractButton {
            id: langButton
            objectName: "languageButton"
            focusPolicy: Qt.StrongFocus
            implicitHeight: 28
            implicitWidth: langLabel.implicitWidth + DqeTheme.spacingM * 2
            onClicked: langMenu.open()
            background: Rectangle {
                radius: DqeTheme.radiusSmall
                color: langButton.hovered ? DqeTheme.panelRaised : "transparent"
                border.width: DqeTheme.borderWidth
                border.color: DqeTheme.border
            }
            contentItem: Text {
                id: langLabel
                text: "🌐 " + DqeI18n.displayName(DqeI18n.language)
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 13
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            Menu {
                id: langMenu
                objectName: "languageMenu"
                MenuItem {
                    objectName: "langEn"
                    text: "English"
                    onTriggered: DqeI18n.setLanguage("en")
                }
                MenuItem {
                    objectName: "langZhHant"
                    text: "繁體中文"
                    onTriggered: DqeI18n.setLanguage("zh-Hant")
                }
                MenuItem {
                    objectName: "langZhHans"
                    text: "简体中文"
                    onTriggered: DqeI18n.setLanguage("zh-Hans")
                }
            }
        }

        // ── Refresh（僅 signal，不做任何 IPC）──
        AbstractButton {
            id: refreshButton
            objectName: "refreshButton"
            text: DqeI18n.t("Refresh", "重新整理", "刷新")
            focusPolicy: Qt.StrongFocus
            implicitHeight: 28
            implicitWidth: refreshLabel.implicitWidth + DqeTheme.spacingM * 2
            onClicked: header.refreshRequested()

            background: Rectangle {
                radius: DqeTheme.radiusSmall
                color: refreshButton.hovered ? DqeTheme.panelRaised : "transparent"
                border.width: DqeTheme.borderWidth
                border.color: DqeTheme.border
            }
            contentItem: Text {
                id: refreshLabel
                text: refreshButton.text
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 13
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            Rectangle {
                anchors.fill: parent
                anchors.margins: -DqeTheme.borderWidthStrong
                radius: DqeTheme.radiusSmall + DqeTheme.borderWidthStrong
                color: "transparent"
                visible: refreshButton.activeFocus
                border.width: DqeTheme.borderWidthStrong
                border.color: DqeTheme.accent
            }
        }

        // ── mock 連線指示 ──
        RowLayout {
            spacing: DqeTheme.spacingS
            Rectangle {
                width: 8
                height: 8
                radius: 4
                color: header.connectionState === "Connected" ? DqeTheme.pass : DqeTheme.unknown
            }
            Text {
                text: header.connectionState
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 13
            }
        }
    }
}
