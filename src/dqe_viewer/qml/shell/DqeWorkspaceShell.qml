// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import QtQuick
import QtQuick.Controls
import "../theme"

// 共用的巢狀 SplitView workspace shell。
// 幾何：Left Navigation（全高）｜ 右側垂直 SplitView〔上方水平 SplitView(Center｜Inspector) ｜ Evidence〕。
// Evidence 橫跨 Center+Right；Left 維持全高。Center 不得小於 620px。
// 只提供 content slots（Component）與 labeled placeholders；不含任何 workspace 內容邏輯。
Item {
    id: shell

    // ── 公開 content slots（workspace 頁面填入，達成單一 shell 重用）──
    property Component navigationContent: null
    property Component centerContent: null
    property Component inspectorContent: null
    property Component evidenceContent: null
    property string workspaceKey: "Archived"

    // 面板標題（可由 workspace 覆寫）
    property string navigationTitle: "NAVIGATION"
    property string centerTitle: "WORKSPACE"
    property string inspectorTitle: "INSPECTOR"
    property string evidenceTitle: "EVIDENCE / ATTACHMENT PREVIEW"

    // per-workspace 持久化狀態
    property alias layout: layout
    readonly property int railWidth: 20

    // 只有「使用者正在拖曳對應 handle」時才把尺寸寫回持久化，避免初始/程式化
    // 版面的 transient 寬高覆蓋 restore 出來的值。
    property bool _leftDragActive: false
    property bool _rightDragActive: false
    property bool _evidenceDragActive: false

    // 供外部（App/測試）讀寫 collapse
    property alias leftCollapsed: layout.leftCollapsed
    property alias rightCollapsed: layout.rightCollapsed

    LayoutSettings {
        id: layout
        workspaceKey: shell.workspaceKey
    }

    // 把持久化的尺寸 imperative 套到 SplitView 子項（避免 preferredWidth 綁定造成 binding loop）。
    function _applyLeft() { leftNav.SplitView.preferredWidth = layout.leftWidth; }
    function _applyRight() { inspector.SplitView.preferredWidth = layout.rightWidth; }
    function _applyEvidence() { evidence.SplitView.preferredHeight = layout.evidenceHeight; }
    function _applyAll() { _applyLeft(); _applyRight(); _applyEvidence(); }

    Component.onCompleted: _applyAll()

    Connections {
        target: layout
        function onWorkspaceKeyChanged() { Qt.callLater(shell._applyAll); }
        function onLeftWidthChanged() { Qt.callLater(shell._applyLeft); }
        function onRightWidthChanged() { Qt.callLater(shell._applyRight); }
        function onEvidenceHeightChanged() { Qt.callLater(shell._applyEvidence); }
        function onLeftCollapsedChanged() { Qt.callLater(shell._applyLeft); }
        function onRightCollapsedChanged() { Qt.callLater(shell._applyRight); }
    }

    // ── 面板本體（header + slot Loader）；非折疊區塊直接使用 ──
    component PanelBody: Rectangle {
        id: pbody
        property string title: ""
        property string subtitle: ""
        property Component slot: null
        property bool collapsible: false
        property string collapseButtonName: ""
        signal collapseClicked()
        color: DqeTheme.panel

        Rectangle { // header bar
            id: hdr
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 34
            color: DqeTheme.panelAlternate
            Rectangle {
                anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                height: DqeTheme.borderWidth; color: DqeTheme.border
            }
            Row {
                anchors.left: parent.left
                anchors.leftMargin: DqeTheme.spacingM
                anchors.verticalCenter: parent.verticalCenter
                spacing: DqeTheme.spacingS
                Text {
                    text: pbody.title
                    color: DqeTheme.accent
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    font.letterSpacing: 0.5
                    anchors.verticalCenter: parent.verticalCenter
                }
                Text {
                    visible: pbody.subtitle.length > 0
                    text: pbody.subtitle
                    color: DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
            AbstractButton {
                visible: pbody.collapsible
                objectName: pbody.collapseButtonName
                anchors.right: parent.right
                anchors.rightMargin: DqeTheme.spacingS
                anchors.verticalCenter: parent.verticalCenter
                width: 22; height: 22
                focusPolicy: Qt.StrongFocus
                onClicked: pbody.collapseClicked()
                contentItem: Text {
                    text: "‹" // ‹ 收合
                    color: DqeTheme.mutedText
                    font.pixelSize: 16
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: DqeTheme.radiusSmall
                    color: parent.hovered ? DqeTheme.panelRaised : "transparent"
                    border.width: parent.activeFocus ? DqeTheme.borderWidthStrong : 0
                    border.color: DqeTheme.accent
                }
            }
        }

        Loader {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: hdr.bottom
            anchors.bottom: parent.bottom
            sourceComponent: pbody.slot
            active: pbody.slot !== null
        }
        // slot 為空時的預設 labeled placeholder
        Text {
            visible: pbody.slot === null
            anchors.centerIn: parent
            text: pbody.title + " placeholder"
            color: DqeTheme.faintText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 14
        }
    }

    // ── 折疊後的 rail（含展開 chevron）──
    component Rail: Rectangle {
        id: rail
        property string expandButtonName: ""
        signal expandClicked()
        color: DqeTheme.panelAlternate
        Rectangle {
            anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom
            width: DqeTheme.borderWidth; color: DqeTheme.border
        }
        AbstractButton {
            objectName: rail.expandButtonName
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: DqeTheme.spacingS
            width: 18; height: 22
            focusPolicy: Qt.StrongFocus
            onClicked: rail.expandClicked()
            contentItem: Text {
                text: "›" // › 展開
                color: DqeTheme.mutedText
                font.pixelSize: 16
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: DqeTheme.radiusSmall
                color: parent.hovered ? DqeTheme.panelRaised : "transparent"
                border.width: parent.activeFocus ? DqeTheme.borderWidthStrong : 0
                border.color: DqeTheme.accent
            }
        }
    }

    SplitView {
        id: outer
        objectName: "outerSplit"
        anchors.fill: parent
        orientation: Qt.Horizontal

        handle: Rectangle {
            objectName: "handleLeft"
            implicitWidth: 6
            readonly property bool pressed: SplitHandle.pressed
            onPressedChanged: shell._leftDragActive = pressed
            color: pressed ? DqeTheme.accent
                 : (SplitHandle.hovered ? DqeTheme.borderStrong : DqeTheme.border)
        }

        // ── Left Navigation（全高；折疊為 rail）──
        Item {
            id: leftNav
            objectName: "leftNav"
            SplitView.minimumWidth: layout.leftCollapsed ? shell.railWidth : layout.minLeft
            SplitView.maximumWidth: layout.leftCollapsed ? shell.railWidth : layout.maxLeft
            onWidthChanged: {
                if (shell._leftDragActive && !layout.leftCollapsed && width > shell.railWidth) {
                    layout.leftWidth = Math.round(width);
                }
            }
            PanelBody {
                anchors.fill: parent
                visible: !layout.leftCollapsed
                title: shell.navigationTitle
                subtitle: shell.workspaceKey
                slot: shell.navigationContent
                collapsible: true
                collapseButtonName: "leftCollapseBtn"
                onCollapseClicked: layout.leftCollapsed = true
            }
            Rail {
                anchors.fill: parent
                visible: layout.leftCollapsed
                expandButtonName: "leftExpandBtn"
                onExpandClicked: layout.leftCollapsed = false
            }
        }

        // ── 右側垂直 SplitView：上方(Center|Inspector) ｜ Evidence ──
        SplitView {
            id: rightSide
            objectName: "rightSplit"
            SplitView.fillWidth: true
            orientation: Qt.Vertical

            handle: Rectangle {
                objectName: "handleEvidence"
                implicitHeight: 6
                readonly property bool pressed: SplitHandle.pressed
                onPressedChanged: shell._evidenceDragActive = pressed
                color: pressed ? DqeTheme.accent
                     : (SplitHandle.hovered ? DqeTheme.borderStrong : DqeTheme.border)
            }

            SplitView {
                id: top
                objectName: "topSplit"
                SplitView.fillHeight: true
                orientation: Qt.Horizontal

                handle: Rectangle {
                    objectName: "handleInspector"
                    implicitWidth: 6
                    readonly property bool pressed: SplitHandle.pressed
                    onPressedChanged: shell._rightDragActive = pressed
                    color: pressed ? DqeTheme.accent
                         : (SplitHandle.hovered ? DqeTheme.borderStrong : DqeTheme.border)
                }

                // ── Center Workspace（不得 < 620px）──
                PanelBody {
                    id: center
                    objectName: "center"
                    SplitView.fillWidth: true
                    SplitView.minimumWidth: 620
                    title: shell.centerTitle
                    slot: shell.centerContent
                }

                // ── Right Inspector（折疊為 rail）──
                Item {
                    id: inspector
                    objectName: "inspector"
                    SplitView.minimumWidth: layout.rightCollapsed ? shell.railWidth : layout.minRight
                    SplitView.maximumWidth: layout.rightCollapsed ? shell.railWidth : layout.maxRight
                    onWidthChanged: {
                        if (shell._rightDragActive && !layout.rightCollapsed && width > shell.railWidth) {
                            layout.rightWidth = Math.round(width);
                        }
                    }
                    PanelBody {
                        anchors.fill: parent
                        visible: !layout.rightCollapsed
                        title: shell.inspectorTitle
                        slot: shell.inspectorContent
                        collapsible: true
                        collapseButtonName: "rightCollapseBtn"
                        onCollapseClicked: layout.rightCollapsed = true
                    }
                    Rail {
                        anchors.fill: parent
                        visible: layout.rightCollapsed
                        expandButtonName: "rightExpandBtn"
                        onExpandClicked: layout.rightCollapsed = false
                    }
                }
            }

            // ── Evidence（橫跨 Center+Right；可垂直調整）──
            PanelBody {
                id: evidence
                objectName: "evidence"
                SplitView.minimumHeight: layout.minEvidence
                SplitView.maximumHeight: layout.maxEvidence
                title: shell.evidenceTitle
                slot: shell.evidenceContent
                onHeightChanged: {
                    if (shell._evidenceDragActive && height > 0) {
                        layout.evidenceHeight = Math.round(height);
                    }
                }
            }
        }
    }
}
