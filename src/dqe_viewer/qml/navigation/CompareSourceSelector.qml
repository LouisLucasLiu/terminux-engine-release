// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Compare 來源選擇器。Temporary／Archived 分區、使用者選取順序、block filter、
// 多選 DQE_ID 系列與「First selected point = 100」規則；只發 signal，不含資料邏輯。
// 不得出現 corner 控制項。
//
// 版面：來源清單與 DQE 清單各自有界、獨立捲動（ColumnLayout 兩個 fillHeight 區），
// 避免 job 一多就把 DQE checkbox group 擠到看不到的位置。

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../theme"
import "../i18n"

Item {
    id: selector
    objectName: "compareSourceSelector"

    property var temporarySources: []   // [{ sourceId, displayName, subtitle }]
    property var archivedSources: []
    property var selectedSourceIds: []  // 已選取（有序）
    property var dqeSeries: []          // [{ dqeId, title, color, selected }]
    property int selectedCount: 0
    property alias blockFilterText: blockField.text

    signal sourceToggled(string sourceId)
    signal dqeToggled(string dqeId)
    signal blockFilterEdited(string text)
    signal exportRequested()

    implicitWidth: 260
    implicitHeight: 640

    function _isSelected(sourceId) {
        const ids = Array.isArray(selectedSourceIds) ? selectedSourceIds : []
        return ids.indexOf(sourceId) !== -1
    }

    function _orderLabel(sourceId) {
        const ids = Array.isArray(selectedSourceIds) ? selectedSourceIds : []
        const pos = ids.indexOf(sourceId)
        return pos === -1 ? "" : String(pos + 1)
    }

    // DQE SERIES 表格欄寬（index：0=勾選格 1=DQE_ID 2=Title 3=Desc）。
    // 1/2/3 欄可拖曳 resize（寫入 _dqeUserColW）；Title 為彈性欄，吸收剩餘。
    property var _dqeUserColW: ({})   // { colIndex: width } 使用者覆寫

    function _dqeOverride(index) {
        const w = _dqeUserColW[index]
        return (w !== undefined && Number(w) > 0) ? Number(w) : 0
    }

    function _dqeColW(index, totalW) {
        const cb = 26
        if (index === 0) {
            return cb
        }
        const idAuto = Math.max(60, Math.round((totalW - cb) * 0.38))
        const deAuto = Math.max(48, Math.round((totalW - cb) * 0.28))
        if (index === 1) {
            return _dqeOverride(1) > 0 ? _dqeOverride(1) : idAuto   // DQE_ID
        }
        if (index === 3) {
            return _dqeOverride(3) > 0 ? _dqeOverride(3) : deAuto   // Desc
        }
        // Title（index 2）：彈性欄，吸收剩餘寬（或使用者直接覆寫）。
        if (_dqeOverride(2) > 0) {
            return _dqeOverride(2)
        }
        const idW = _dqeOverride(1) > 0 ? _dqeOverride(1) : idAuto
        const deW = _dqeOverride(3) > 0 ? _dqeOverride(3) : deAuto
        return Math.max(48, totalW - cb - idW - deW)
    }

    // 拖曳 handle 寫入欄寬覆寫（複製整個 map 觸發 binding 更新）。
    function _setDqeColW(index, newWidth) {
        var next = {}
        for (var k in _dqeUserColW) {
            next[k] = _dqeUserColW[k]
        }
        next[index] = Math.max(40, Math.round(newWidth))
        _dqeUserColW = next
    }

    function _displayText(value) {
        const s = String(value === undefined || value === null ? "" : value).trim()
        return s.length === 0 ? "—" : s
    }

    // DQE SERIES 表頭欄寬拖曳 handle（放在欄右緣）。只在按住拖曳時調整。
    component DqeResizeHandle: Rectangle {
        id: handle
        property int colIndex: 0
        width: 6
        height: parent ? parent.height : 0
        anchors.right: parent ? parent.right : undefined
        z: 2
        color: handleArea.containsMouse || handleArea.pressed ? DqeTheme.accent : "transparent"
        opacity: handleArea.pressed ? 0.9 : 0.4
        MouseArea {
            id: handleArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.SizeHorCursor
            acceptedButtons: Qt.LeftButton
            preventStealing: true
            property real _startX: 0
            property real _startW: 0
            onPressed: (mouse) => {
                mouse.accepted = true
                _startX = mapToItem(selector, mouse.x, mouse.y).x
                _startW = handle.parent ? handle.parent.width : 0
            }
            onPositionChanged: (mouse) => {
                if (!pressed) {
                    return
                }
                const cur = mapToItem(selector, mouse.x, mouse.y).x
                selector._setDqeColW(handle.colIndex, _startW + (cur - _startX))
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: DqeTheme.panel
        border.width: DqeTheme.borderWidth
        border.color: DqeTheme.border
    }

    component SectionLabel: Text {
        color: DqeTheme.accent
        font.family: DqeTheme.fontFamily
        font.pixelSize: 11
        font.letterSpacing: 0.5
    }

    component SourceRow: AbstractButton {
        id: sourceRow
        required property var modelData
        readonly property string sourceId: modelData.sourceId === undefined ? "" : String(modelData.sourceId)
        readonly property bool isChosen: selector._isSelected(sourceId)
        width: parent ? parent.width : 0
        height: 44
        focusPolicy: Qt.StrongFocus
        objectName: "compareSource_" + sourceId
        onClicked: selector.sourceToggled(sourceId)

        background: Rectangle {
            radius: DqeTheme.radiusSmall
            color: sourceRow.hovered ? DqeTheme.panelRaised : "transparent"
            border.width: sourceRow.activeFocus ? DqeTheme.borderWidthStrong : 0
            border.color: DqeTheme.accent
        }
        contentItem: Row {
            spacing: DqeTheme.spacingS
            leftPadding: DqeTheme.spacingXs
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: sourceRow.isChosen ? "☑" : "☐"
                color: sourceRow.isChosen ? DqeTheme.accent : DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 16
            }
            Column {
                anchors.verticalCenter: parent.verticalCenter
                spacing: DqeTheme.spacingXxs
                Text {
                    text: (sourceRow.isChosen ? sourceRow._orderPrefix() : "")
                          + (sourceRow.modelData.displayName === undefined ? "—"
                                                                           : String(sourceRow.modelData.displayName))
                    color: sourceRow.isChosen ? DqeTheme.text : DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
                Text {
                    text: sourceRow.modelData.subtitle === undefined ? "" : String(sourceRow.modelData.subtitle)
                    color: DqeTheme.faintText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 10
                    elide: Text.ElideRight
                }
            }
        }
        function _orderPrefix() {
            const label = selector._orderLabel(sourceId)
            return label === "" ? "" : (label + ". ")
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: DqeTheme.spacingS
        spacing: DqeTheme.spacingS

        // ── header ──
        RowLayout {
            Layout.fillWidth: true
            SectionLabel {
                Layout.fillWidth: true
                text: DqeI18n.t("COMPARE SOURCES", "比較來源", "比较来源")
            }
            Text {
                objectName: "compareSelectedCount"
                text: selector.selectedCount + " " + DqeI18n.t("selected", "已選", "已选")
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
            }
        }

        SectionLabel { text: DqeI18n.t("TEMPORARY JOBS / ARCHIVED VERSIONS", "暫存 Job / 已歸檔版本", "暂存 Job / 已归档版本") }

        // ── 來源清單（有界、獨立捲動）──
        Flickable {
            objectName: "compareSourcesScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredHeight: 1
            contentWidth: width
            contentHeight: sourcesColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}

            Column {
                id: sourcesColumn
                width: parent.width
                spacing: DqeTheme.spacingXs

                Repeater {
                    model: selector.temporarySources
                    delegate: SourceRow {}
                }
                SectionLabel {
                    text: DqeI18n.t("ARCHIVED VERSIONS", "已歸檔版本", "已归档版本")
                    topPadding: DqeTheme.spacingS
                    visible: selector.archivedSources.length > 0
                }
                Repeater {
                    model: selector.archivedSources
                    delegate: SourceRow {}
                }
            }
        }

        // ── block filter（固定）──
        SectionLabel { text: DqeI18n.t("BLOCK FILTER", "Block 篩選", "Block 筛选") }
        TextField {
            id: blockField
            objectName: "compareBlockFilter"
            Layout.fillWidth: true
            height: 32
            placeholderText: DqeI18n.t("Filter series by block", "以 block 篩選 series", "以 block 筛选 series")
            color: DqeTheme.text
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            selectByMouse: true
            onTextEdited: selector.blockFilterEdited(text)
            background: Rectangle {
                radius: DqeTheme.radiusSmall
                color: DqeTheme.headerBackground
                border.width: blockField.activeFocus ? DqeTheme.borderWidthStrong : DqeTheme.borderWidth
                border.color: blockField.activeFocus ? DqeTheme.accent : DqeTheme.border
            }
        }

        // ── DQE checkbox group（已選 jobs 的交集；有界、獨立捲動）──
        SectionLabel {
            text: DqeI18n.t("DQE SERIES (intersection of selected jobs)", "DQE 系列（已選 job 之交集）", "DQE 系列（已选 job 之交集）")
        }
        Text {
            objectName: "compareDqeEmptyHint"
            Layout.fillWidth: true
            visible: selector.dqeSeries.length === 0
            text: selector.selectedCount < 1
                  ? DqeI18n.t("Select at least one job first", "先勾選至少一個 job", "先勾选至少一个 job")
                  : DqeI18n.t("Selected jobs share no DQE", "所選 job 無共同 DQE", "所选 job 无共同 DQE")
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            wrapMode: Text.Wrap
        }
        // 表格欄位：DQE_ID | Title | Description（前綴勾選格）。表頭可拖曳 resize。
        Row {
            id: dqeHeader
            objectName: "compareDqeHeader"
            Layout.fillWidth: true
            visible: selector.dqeSeries.length > 0
            height: 18
            Item { width: selector._dqeColW(0, dqeHeader.width); height: 1 }
            Repeater {
                model: [DqeI18n.t("DQE_ID","DQE_ID","DQE_ID"),
                        DqeI18n.t("Title","標題","标题"),
                        DqeI18n.t("Desc","說明","说明")]
                delegate: Item {
                    required property int index
                    required property string modelData
                    width: selector._dqeColW(index + 1, dqeHeader.width)
                    height: dqeHeader.height
                    Text {
                        anchors.fill: parent
                        text: parent.modelData
                        color: DqeTheme.mutedText
                        font.family: DqeTheme.fontFamily
                        font.pixelSize: 9
                        font.letterSpacing: 0.3
                        elide: Text.ElideRight
                        verticalAlignment: Text.AlignVCenter
                    }
                    DqeResizeHandle { colIndex: parent.index + 1 }
                }
            }
        }
        Flickable {
            objectName: "compareDqeScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredHeight: 1
            visible: selector.dqeSeries.length > 0
            contentWidth: width
            contentHeight: dqeColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}

            Column {
                id: dqeColumn
                width: parent.width
                spacing: DqeTheme.spacingXxs

                Repeater {
                    model: selector.dqeSeries
                    delegate: AbstractButton {
                        id: dqeRow
                        required property var modelData
                        readonly property string dqeId: modelData.dqeId === undefined ? "" : String(modelData.dqeId)
                        readonly property bool chosen: modelData.selected === true
                        width: dqeColumn.width
                        height: 28
                        focusPolicy: Qt.StrongFocus
                        objectName: "compareDqe_" + dqeId
                        onClicked: selector.dqeToggled(dqeId)
                        background: Rectangle {
                            radius: DqeTheme.radiusSmall
                            color: dqeRow.hovered ? DqeTheme.panelRaised : "transparent"
                            border.width: dqeRow.activeFocus ? DqeTheme.borderWidthStrong : 0
                            border.color: DqeTheme.accent
                        }
                        contentItem: Row {
                            // 勾選格（☑/☐ + 線色點）
                            Item {
                                width: selector._dqeColW(0, dqeRow.width)
                                height: dqeRow.height
                                Row {
                                    anchors.verticalCenter: parent.verticalCenter
                                    leftPadding: DqeTheme.spacingXs
                                    spacing: DqeTheme.spacingXxs
                                    Text {
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: dqeRow.chosen ? "☑" : "☐"
                                        color: dqeRow.chosen ? DqeTheme.accent : DqeTheme.mutedText
                                        font.family: DqeTheme.fontFamily
                                        font.pixelSize: 14
                                    }
                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 8; height: 8; radius: 4
                                        color: dqeRow.modelData.color === undefined ? DqeTheme.mutedText
                                                                                    : String(dqeRow.modelData.color)
                                        opacity: dqeRow.chosen ? 1.0 : 0.35
                                    }
                                }
                            }
                            // DQE_ID
                            Text {
                                width: selector._dqeColW(1, dqeRow.width)
                                height: dqeRow.height
                                text: dqeRow.dqeId
                                color: dqeRow.chosen ? DqeTheme.text : DqeTheme.mutedText
                                font.family: DqeTheme.fontFamily
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }
                            // Title
                            Text {
                                width: selector._dqeColW(2, dqeRow.width)
                                height: dqeRow.height
                                text: selector._displayText(dqeRow.modelData.title)
                                color: dqeRow.chosen ? DqeTheme.text : DqeTheme.mutedText
                                font.family: DqeTheme.fontFamily
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }
                            // Description
                            Text {
                                width: selector._dqeColW(3, dqeRow.width)
                                height: dqeRow.height
                                text: selector._displayText(dqeRow.modelData.description)
                                color: DqeTheme.mutedText
                                font.family: DqeTheme.fontFamily
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                    }
                }
            }
        }

        // ── normalization 說明 + export（固定於底部）──
        Text {
            objectName: "compareNormalizationRule"
            Layout.fillWidth: true
            text: DqeI18n.t("Normalization: first selected point = 100", "正規化：第一個選取點 = 100", "归一化：第一个选取点 = 100")
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            wrapMode: Text.Wrap
        }
        AbstractButton {
            id: exportButton
            objectName: "compareExportButton"
            Layout.fillWidth: true
            height: 32
            focusPolicy: Qt.StrongFocus
            onClicked: selector.exportRequested()
            background: Rectangle {
                radius: DqeTheme.radiusSmall
                color: exportButton.hovered ? DqeTheme.panelRaised : DqeTheme.headerBackground
                border.width: exportButton.activeFocus ? DqeTheme.borderWidthStrong : DqeTheme.borderWidth
                border.color: exportButton.activeFocus ? DqeTheme.accent : DqeTheme.border
            }
            contentItem: Text {
                text: DqeI18n.t("Export HTML", "匯出 HTML", "导出 HTML")
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }
    }
}
