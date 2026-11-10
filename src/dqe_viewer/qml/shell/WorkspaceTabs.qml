// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

// Archived / Temporary / Compare 三個 workspace tab。
// 本元件持有 workspace enum，作為整個 shell 的單一真實來源（其餘元件一律
// 以 WorkspaceTabs.Archived/Temporary/Compare 整數值比較，不比對顯示字串）。
Item {
    id: tabs

    enum Workspace {
        Archived,
        Temporary,
        Compare
    }

    // 目前選中的 workspace（由外部 owner 綁定；預設 Archived）。
    property int activeWorkspace: WorkspaceTabs.Archived
    // 使用者要求切換到某 workspace（fire-and-forget，實際切換由 owner 決定）。
    signal workspaceRequested(int workspace)

    // 內部識別用（index 對齊 enum）；顯示文字經 _tabLabel 翻譯，不比對顯示字串。
    readonly property var _labels: ["Archived", "Temporary", "Compare"]

    function _tabLabel(index) {
        if (index === WorkspaceTabs.Temporary) {
            return DqeI18n.t("Temporary", "暫存", "暂存")
        }
        if (index === WorkspaceTabs.Compare) {
            return DqeI18n.t("Compare", "比較", "比较")
        }
        return DqeI18n.t("Archived", "已歸檔", "已归档")
    }

    implicitWidth: row.implicitWidth
    implicitHeight: row.implicitHeight

    Row {
        id: row
        anchors.centerIn: parent
        spacing: DqeTheme.spacingS

        Repeater {
            model: tabs._labels
            delegate: AbstractButton {
                id: pill
                required property int index
                required property string modelData

                objectName: "workspaceTab" + index
                text: modelData
                height: 30
                implicitWidth: Math.max(96, label.implicitWidth + DqeTheme.spacingXl * 2)
                focusPolicy: Qt.StrongFocus

                readonly property bool selected: tabs.activeWorkspace === index

                onClicked: tabs.workspaceRequested(index)

                background: Rectangle {
                    radius: DqeTheme.radiusSmall
                    color: pill.selected ? DqeTheme.accent
                                         : (pill.hovered ? DqeTheme.panelRaised : "transparent")
                    border.width: pill.selected ? 0 : DqeTheme.borderWidth
                    border.color: DqeTheme.border
                }

                contentItem: Text {
                    id: label
                    text: tabs._tabLabel(pill.index)
                    color: pill.selected ? DqeTheme.headerBackground : DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 14
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }

                // 鍵盤焦點可見：activeFocus 時顯示 accent 外框。
                Rectangle {
                    objectName: "tabFocusRing" + pill.index
                    anchors.fill: parent
                    anchors.margins: -DqeTheme.borderWidthStrong
                    radius: DqeTheme.radiusSmall + DqeTheme.borderWidthStrong
                    color: "transparent"
                    visible: pill.activeFocus
                    border.width: DqeTheme.borderWidthStrong
                    border.color: DqeTheme.accent
                }
            }
        }
    }
}
