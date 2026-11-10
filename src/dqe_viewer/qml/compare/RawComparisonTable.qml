// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Raw value comparison table（secondary surface）。
// 永遠顯示原始 Value／Unit／Status；正規化不改變任何 raw 字串。
// cell 以 {sourceId, dqeId} 與 chart 雙向同步：點擊 cell 觸發 cellSelected。
// 欄位（SOURCE / 各 DQE / STATUS）皆可拖曳調整寬度；總寬超過表格時提供水平捲軸。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: table
    objectName: "rawComparisonTable"

    property var orderedSources: []  // [{ sourceId, shortLabel }]
    property var series: []          // [{ dqeId, title, points:[{ sourceId, rawValue, rawUnit, status }] }]
    property string selectedDqeId: ""
    property string selectedSourceId: ""

    signal cellSelected(string dqeId, string sourceId)

    readonly property int seriesCount: Array.isArray(series) ? series.length : 0
    readonly property string activeDqeId: _activeDqeId()

    // 使用者拖曳後的欄寬覆寫，key = "__source" / "__status" / dqeId；未覆寫則用預設。
    property var _userColW: ({})

    implicitWidth: 600
    implicitHeight: 260

    function _activeDqeId() {
        const all = Array.isArray(series) ? series : []
        for (let i = 0; i < all.length; ++i) {
            if (String(all[i].dqeId) === selectedDqeId) {
                return selectedDqeId
            }
        }
        return all.length > 0 ? String(all[0].dqeId) : ""
    }

    function _pointOf(dqeId, sourceId) {
        const all = Array.isArray(series) ? series : []
        for (let i = 0; i < all.length; ++i) {
            if (String(all[i].dqeId) !== dqeId) {
                continue
            }
            const points = Array.isArray(all[i].points) ? all[i].points : []
            for (let j = 0; j < points.length; ++j) {
                if (String(points[j].sourceId) === sourceId) {
                    return points[j]
                }
            }
        }
        return null
    }

    function _rawText(point) {
        if (point !== null && point.issue === "duplicate_dqe_id") {
            return "duplicate_dqe_id"
        }
        if (point === null || point.rawValue === null || point.rawValue === undefined) {
            return "—"
        }
        const unit = point.rawUnit === undefined || point.rawUnit === null ? "" : String(point.rawUnit)
        // 原樣顯示 raw value；單位（非 count）附加於後。
        return String(point.rawValue) + (unit.length > 0 && unit !== "count" ? " " + unit : "")
    }

    function _statusColor(status) {
        switch (status) {
        case "Pass": return DqeTheme.pass
        case "Warn": return DqeTheme.warn
        case "Fail": return DqeTheme.fail
        default: return DqeTheme.unknown
        }
    }

    function _shortLabel(sourceId) {
        const sources = Array.isArray(orderedSources) ? orderedSources : []
        for (let i = 0; i < sources.length; ++i) {
            if (String(sources[i].sourceId) === sourceId) {
                return sources[i].shortLabel === undefined ? sourceId : String(sources[i].shortLabel)
            }
        }
        return sourceId
    }

    // ── 欄寬 ────────────────────────────────────────────────────────────────
    function _override(key) {
        const w = _userColW[key]
        return (w !== undefined && Number(w) > 0) ? Number(w) : 0
    }
    function _sourceWidth() {
        const ov = _override("__source")
        return ov > 0 ? ov : 96
    }
    function _statusWidthEff() {
        const ov = _override("__status")
        return ov > 0 ? ov : 88
    }
    // DQE 欄未拉寬時平均分配剩餘寬（吸收 SOURCE/STATUS 覆寫後的剩餘）。
    function _cellDefaultWidth() {
        const remain = width - _sourceWidth() - _statusWidthEff()
        return seriesCount > 0 ? Math.max(60, remain / seriesCount) : 0
    }
    function _dqeWidth(dqeId) {
        const ov = _override(String(dqeId))
        return ov > 0 ? ov : _cellDefaultWidth()
    }
    function _setColWidth(key, newWidth) {
        var next = {}
        for (var k in _userColW) { next[k] = _userColW[k] }
        next[key] = Math.max(48, Math.round(newWidth))
        _userColW = next
    }
    // 全部欄總寬（= 橫向捲動 content 寬）。未拉寬時等於表格寬（不出現捲軸）。
    function _totalWidth() {
        var total = _sourceWidth() + _statusWidthEff()
        const all = Array.isArray(series) ? series : []
        for (var i = 0; i < all.length; ++i) {
            total += _dqeWidth(String(all[i].dqeId))
        }
        return total
    }

    // 可重用的欄寬拖曳 handle（放在 header cell 右緣）。
    component ResizeHandle: Rectangle {
        id: handle
        property string colKey: ""
        property real currentWidth: 0
        objectName: "rawColResize_" + colKey
        width: 6
        height: parent ? parent.height : 0
        anchors.right: parent ? parent.right : undefined
        z: 2
        color: handleArea.containsMouse || handleArea.pressed ? DqeTheme.accent : "transparent"
        opacity: handleArea.pressed ? 0.9 : 0.5
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
                _startX = mapToItem(table, mouse.x, mouse.y).x
                _startW = handle.currentWidth
            }
            onPositionChanged: (mouse) => {
                if (!pressed) {
                    return
                }
                const cur = mapToItem(table, mouse.x, mouse.y).x
                table._setColWidth(handle.colKey, _startW + (cur - _startX))
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: DqeTheme.panel
        border.width: DqeTheme.borderWidth
        border.color: DqeTheme.border
    }

    Column {
        anchors.fill: parent
        anchors.margins: DqeTheme.spacingM
        spacing: DqeTheme.spacingXs

        // 標題列固定（不隨水平捲動）。
        Row {
            width: parent.width
            Text {
                width: parent.width - selectedInfo.implicitWidth
                text: DqeI18n.t("RAW VALUE COMPARISON", "原始數值比較", "原始数值比较")
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                font.letterSpacing: 0.5
            }
            Text {
                id: selectedInfo
                objectName: "rawTableSelectedInfo"
                text: table.selectedDqeId.length > 0 && table.selectedSourceId.length > 0
                      ? DqeI18n.t("Selected point: ", "選取點：", "选取点：") + table.selectedDqeId + " · " + table._shortLabel(table.selectedSourceId)
                      : DqeI18n.t("No point selected", "未選取點", "未选取点")
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
            }
        }

        // 水平捲動容器：header + 資料列一起橫向捲動。
        Flickable {
            id: hScroll
            objectName: "rawTableHScroll"
            width: parent.width
            height: parent.height - y
            contentWidth: table._totalWidth()
            contentHeight: height
            clip: true
            flickableDirection: Flickable.HorizontalFlick
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.horizontal: ScrollBar {
                objectName: "rawTableHScrollBar"
                policy: hScroll.contentWidth > hScroll.width + 1
                        ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
            }

            Column {
                width: hScroll.contentWidth
                spacing: DqeTheme.spacingXs

                // header row（含欄寬 handle）
                Row {
                    id: headerRow
                    width: hScroll.contentWidth
                    height: 24

                    Item {
                        width: table._sourceWidth()
                        height: parent.height
                        Text {
                            anchors.fill: parent
                            text: DqeI18n.t("SOURCE", "來源", "来源")
                            color: DqeTheme.mutedText
                            font.family: DqeTheme.fontFamily
                            font.pixelSize: 10
                            elide: Text.ElideRight
                            verticalAlignment: Text.AlignVCenter
                        }
                        ResizeHandle { colKey: "__source"; currentWidth: table._sourceWidth() }
                    }
                    Repeater {
                        model: table.series
                        delegate: Item {
                            required property var modelData
                            readonly property string dqeId: String(modelData.dqeId)
                            width: table._dqeWidth(dqeId)
                            height: headerRow.height
                            Text {
                                anchors.fill: parent
                                text: parent.dqeId
                                color: DqeTheme.mutedText
                                font.family: DqeTheme.fontFamily
                                font.pixelSize: 10
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                            }
                            ResizeHandle { colKey: parent.dqeId; currentWidth: table._dqeWidth(parent.dqeId) }
                        }
                    }
                    Item {
                        width: table._statusWidthEff()
                        height: parent.height
                        Text {
                            anchors.fill: parent
                            text: DqeI18n.t("STATUS", "狀態", "状态")
                            color: DqeTheme.mutedText
                            font.family: DqeTheme.fontFamily
                            font.pixelSize: 10
                            elide: Text.ElideRight
                            verticalAlignment: Text.AlignVCenter
                        }
                        ResizeHandle { colKey: "__status"; currentWidth: table._statusWidthEff() }
                    }
                }

                Rectangle { width: hScroll.contentWidth; height: DqeTheme.borderWidth; color: DqeTheme.border }

                // data rows
                Repeater {
                    model: table.orderedSources
                    delegate: Rectangle {
                        id: dataRow
                        required property var modelData
                        readonly property string sourceId: modelData.sourceId === undefined ? "" : String(modelData.sourceId)
                        readonly property bool rowSelected: sourceId === table.selectedSourceId
                        objectName: "compareRow_" + sourceId
                        width: hScroll.contentWidth
                        height: 30
                        color: rowSelected ? DqeTheme.selection : "transparent"
                        border.width: rowSelected ? DqeTheme.borderWidthStrong : 0
                        border.color: DqeTheme.accent
                        radius: DqeTheme.radiusSmall

                        Row {
                            anchors.fill: parent
                            Text {
                                width: table._sourceWidth()
                                leftPadding: DqeTheme.spacingXs
                                text: modelData.shortLabel === undefined ? dataRow.sourceId : String(modelData.shortLabel)
                                color: DqeTheme.text
                                font.family: DqeTheme.fontFamily
                                font.pixelSize: 12
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                                height: parent.height
                            }
                            Repeater {
                                model: table.series
                                delegate: AbstractButton {
                                    id: valueCell
                                    required property var modelData
                                    readonly property string dqeId: modelData.dqeId === undefined ? "" : String(modelData.dqeId)
                                    readonly property var point: table._pointOf(dqeId, dataRow.sourceId)
                                    readonly property bool cellSelected: dqeId === table.selectedDqeId
                                                                         && dataRow.sourceId === table.selectedSourceId
                                    width: table._dqeWidth(dqeId)
                                    height: parent.height
                                    objectName: "compareCell_" + dqeId + "_" + dataRow.sourceId
                                    focusPolicy: Qt.StrongFocus
                                    onClicked: table.cellSelected(dqeId, dataRow.sourceId)
                                    background: Rectangle {
                                        color: valueCell.cellSelected ? DqeTheme.panelRaised
                                                                      : (valueCell.hovered ? DqeTheme.panelAlternate : "transparent")
                                        border.width: valueCell.cellSelected ? DqeTheme.borderWidthStrong : 0
                                        border.color: DqeTheme.accent
                                        radius: DqeTheme.radiusSmall
                                    }
                                    contentItem: Text {
                                        leftPadding: DqeTheme.spacingXs
                                        text: table._rawText(valueCell.point)
                                        color: DqeTheme.text
                                        font.family: DqeTheme.fontFamily
                                        font.pixelSize: 12
                                        elide: Text.ElideRight
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                }
                            }
                            Item {
                                width: table._statusWidthEff()
                                height: parent.height
                                Text {
                                    objectName: "compareStatus_" + dataRow.sourceId
                                    anchors.left: parent.left
                                    anchors.leftMargin: DqeTheme.spacingXs
                                    anchors.verticalCenter: parent.verticalCenter
                                    readonly property var _statusPoint: table._pointOf(table.activeDqeId, dataRow.sourceId)
                                    text: _statusPoint === null || _statusPoint.status === undefined || _statusPoint.status === null
                                          ? "—" : String(_statusPoint.status).toUpperCase()
                                    color: _statusPoint === null ? DqeTheme.unknown
                                                                 : table._statusColor(_statusPoint.status)
                                    font.family: DqeTheme.fontFamily
                                    font.pixelSize: 11
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
