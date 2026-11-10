// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 可重用的 DQE item 結果表。
// model 是唯讀輸入；本元件只建立顯示用副本、發出 selection／activation signal。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: table

    property var model: []
    property string statusFilter: ""
    property string selectedDqeId: ""

    signal itemSelected(string dqeId)
    signal itemActivated(string dqeId)

    readonly property var _columns: [
        { label: DqeI18n.t("Status","狀態","状态"), key: "status" },
        { label: DqeI18n.t("DQE_ID","DQE_ID","DQE_ID"), key: "dqeId" },
        { label: DqeI18n.t("Title","標題","标题"), key: "title" },
        { label: DqeI18n.t("Value","數值","数值"), key: "value" },
        { label: DqeI18n.t("Unit","單位","单位"), key: "unit" }
    ]
    property string _sortColumn: ""
    property bool _sortAscending: true
    // 使用者拖曳後的欄寬覆寫（index 對齊 _columns）；0 = 沿用比例自動寬度。
    // Title（index 2）為彈性欄，不提供直接拖曳，吸收其餘欄調整後的剩餘寬度。
    property var _userColW: [0, 0, 0, 0, 0]
    readonly property var _visibleItems: _buildVisibleItems()

    implicitWidth: 620
    implicitHeight: 360

    onStatusFilterChanged: Qt.callLater(_clearSelectionIfHidden)

    function _itemId(item) {
        if (item === null || item === undefined || item.dqeId === undefined || item.dqeId === null) {
            return ""
        }
        return String(item.dqeId)
    }

    function _displayString(value) {
        if (value === undefined || value === null || value === "") {
            return "—"
        }
        return String(value)
    }

    function _sortString(value) {
        if (value === undefined || value === null) {
            return ""
        }
        return String(value)
    }

    function _statusColor(status) {
        switch (status) {
        case "Pass":
            return DqeTheme.pass
        case "Warn":
            return DqeTheme.warn
        case "Fail":
            return DqeTheme.fail
        default:
            return DqeTheme.unknown
        }
    }

    // 各欄最小寬度（拖曳下限；與比例自動寬度的下限一致）。
    function _columnMinWidth(index) {
        if (index === 0) {
            return 82
        }
        if (index === 1) {
            return 96
        }
        if (index === 3) {
            return 72
        }
        if (index === 4) {
            return 52
        }
        return 140
    }

    // 依表格寬度計算的比例自動寬度（未被使用者覆寫時採用）。
    function _autoColumnWidth(index) {
        const statusWidth = Math.max(82, Math.round(width * 0.14))
        const idWidth = Math.max(96, Math.round(width * 0.17))
        const valueWidth = Math.max(72, Math.round(width * 0.13))
        const unitWidth = Math.max(52, Math.round(width * 0.09))
        if (index === 0) {
            return statusWidth
        }
        if (index === 1) {
            return idWidth
        }
        if (index === 3) {
            return valueWidth
        }
        if (index === 4) {
            return unitWidth
        }
        return Math.max(140, width - statusWidth - idWidth - valueWidth - unitWidth)
    }

    // 使用者覆寫寬度（>0 才有效）；讀取 _userColW 使呼叫此函式的 binding 依賴其變更。
    function _overrideWidth(index) {
        const overrides = _userColW
        if (Array.isArray(overrides) && overrides.length > index && Number(overrides[index]) > 0) {
            return Number(overrides[index])
        }
        return 0
    }

    // 非彈性欄（Status/DQE_ID/Value/Unit）的有效寬度：優先使用者覆寫，否則比例自動寬度。
    function _fixedColumnWidth(index) {
        const override = _overrideWidth(index)
        return override > 0 ? override : _autoColumnWidth(index)
    }

    function _columnWidth(index) {
        if (index === 2) {
            // Title 為彈性欄：使用者可直接覆寫；否則吸收其餘欄後的剩餘寬度。
            const titleOverride = _overrideWidth(2)
            if (titleOverride > 0) {
                return titleOverride
            }
            const used = _fixedColumnWidth(0) + _fixedColumnWidth(1)
                       + _fixedColumnWidth(3) + _fixedColumnWidth(4)
            return Math.max(140, width - used)
        }
        const override = _overrideWidth(index)
        return override > 0 ? override : _autoColumnWidth(index)
    }

    // 拖曳 handle 寫入使用者覆寫寬度（複製整個陣列以觸發 binding 更新）。
    function _setColumnWidth(index, newWidth) {
        const source = (Array.isArray(_userColW) && _userColW.length === _columns.length)
                       ? _userColW.slice() : [0, 0, 0, 0, 0]
        source[index] = Math.max(_columnMinWidth(index), Math.round(newWidth))
        _userColW = source
    }

    // 全部欄的總寬（= 橫向捲動的 content 寬）。未拖曳時 Title 吸收剩餘寬 → 總寬等於
    // 表格寬（不出現橫捲軸）；欄被拉寬後總寬 > 表格寬 → 出現水平 scroll bar。
    function _totalColumnsWidth() {
        return _columnWidth(0) + _columnWidth(1) + _columnWidth(2)
             + _columnWidth(3) + _columnWidth(4)
    }

    function _buildVisibleItems() {
        const source = Array.isArray(model) ? model : []
        const entries = []
        for (let index = 0; index < source.length; ++index) {
            const item = source[index]
            if (statusFilter === "" || item.status === statusFilter) {
                entries.push({ item: item, sourceIndex: index })
            }
        }
        if (_sortColumn !== "") {
            entries.sort(function(left, right) {
                const leftValue = _sortString(left.item[_sortColumn])
                const rightValue = _sortString(right.item[_sortColumn])
                if (leftValue < rightValue) {
                    return _sortAscending ? -1 : 1
                }
                if (leftValue > rightValue) {
                    return _sortAscending ? 1 : -1
                }

                const leftId = _itemId(left.item)
                const rightId = _itemId(right.item)
                if (leftId < rightId) {
                    return -1
                }
                if (leftId > rightId) {
                    return 1
                }
                return left.sourceIndex - right.sourceIndex
            })
        }
        return entries.map(function(entry) { return entry.item })
    }

    function _toggleSort(column) {
        if (_sortColumn === column) {
            _sortAscending = !_sortAscending
        } else {
            _sortColumn = column
            _sortAscending = true
        }
    }

    function _clearSelectionIfHidden() {
        if (selectedDqeId === "") {
            return
        }
        for (let index = 0; index < _visibleItems.length; ++index) {
            if (_itemId(_visibleItems[index]) === selectedDqeId) {
                return
            }
        }
        itemSelected("")
    }

    Rectangle {
        anchors.fill: parent
        color: DqeTheme.panel
        border.width: DqeTheme.borderWidth
        border.color: DqeTheme.border
    }

    // 水平捲動容器：欄被拉寬到超過表格寬時提供橫向 scroll bar；未拉寬時 content 寬等於
    // 表格寬、捲軸自動隱藏。垂直捲動仍由內層 ListView 自己處理（兩軸互補、不衝突）。
    Flickable {
        id: horizontalScroll
        objectName: "dqeTableHScroll"
        anchors.fill: parent
        contentWidth: table._totalColumnsWidth()
        contentHeight: height
        clip: true
        flickableDirection: Flickable.HorizontalFlick
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.horizontal: ScrollBar {
            objectName: "dqeTableHScrollBar"
            // 只有欄總寬確實超過表格寬才顯示，避免剛好塞滿時出現常駐空捲軸。
            policy: horizontalScroll.contentWidth > horizontalScroll.width + 1
                    ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
        }

    Row {
        id: headerRow
        width: horizontalScroll.contentWidth
        height: 36

        Repeater {
            model: table._columns

            delegate: AbstractButton {
                id: headerCell
                required property int index

                objectName: "tableHeader" + table._columns[index].label.replace("_", "")
                width: table._columnWidth(index)
                height: headerRow.height
                focusPolicy: Qt.StrongFocus
                readonly property string columnKey: table._columns[index].key
                readonly property bool sorted: table._sortColumn === columnKey

                onClicked: table._toggleSort(columnKey)

                background: Rectangle {
                    color: headerCell.sorted ? DqeTheme.panelRaised : DqeTheme.panelAlternate
                    border.width: DqeTheme.borderWidth
                    border.color: headerCell.sorted ? DqeTheme.borderStrong : DqeTheme.border
                }

                contentItem: Text {
                    leftPadding: DqeTheme.spacingS
                    rightPadding: DqeTheme.spacingS
                    text: table._columns[index].label
                          + (headerCell.sorted ? (table._sortAscending ? " ▲" : " ▼") : "")
                    color: headerCell.sorted ? DqeTheme.accent : DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                    font.letterSpacing: 0.4
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                }

                // 欄寬拖曳 handle：位於欄右緣。置於 contentItem 之上並自行吸收滑鼠事件，
                // 避免觸發 header 的排序 click。只有實際「按住拖曳」才調整寬度：hoverEnabled
                // 會讓 onPositionChanged 在單純滑過時也觸發，若不以 pressed 守衛，游標一碰到
                // 邊界欄位就會依 _startX（未按下時為 0）暴衝變寬。
                Rectangle {
                    id: resizeHandle
                    objectName: "columnResizeHandle" + table._columns[index].label.replace("_", "")
                    width: 6
                    height: headerCell.height
                    anchors.right: parent.right
                    z: 2
                    color: resizeHandleArea.containsMouse || resizeHandleArea.pressed
                           ? DqeTheme.accent : "transparent"
                    opacity: resizeHandleArea.pressed ? 0.9 : 0.5

                    MouseArea {
                        id: resizeHandleArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.SizeHorCursor
                        acceptedButtons: Qt.LeftButton
                        preventStealing: true
                        property real _startX: 0
                        property real _startWidth: 0
                        onPressed: (mouse) => {
                            mouse.accepted = true
                            _startX = mapToItem(table, mouse.x, mouse.y).x
                            _startWidth = table._columnWidth(index)
                        }
                        onPositionChanged: (mouse) => {
                            if (!pressed) {
                                return
                            }
                            const currentX = mapToItem(table, mouse.x, mouse.y).x
                            table._setColumnWidth(index, _startWidth + (currentX - _startX))
                        }
                    }
                }
            }
        }
    }

    ListView {
        id: itemList
        objectName: "dqeItemList"
        y: headerRow.height
        width: horizontalScroll.contentWidth
        height: horizontalScroll.height - headerRow.height
        clip: true
        model: table._visibleItems
        spacing: 0
        reuseItems: true
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.vertical: ScrollBar {}

        delegate: Item {
            id: row
            required property var modelData

            objectName: "dqeRow" + table._itemId(modelData)
            width: itemList.width
            height: 40
            activeFocusOnTab: true
            readonly property string selectionKey: table._itemId(modelData)
            readonly property string statusText: table._displayString(modelData.status)
            readonly property string dqeIdText: table._displayString(modelData.dqeId)
            readonly property string titleText: table._displayString(modelData.title)
            readonly property string valueText: table._displayString(modelData.value)
            readonly property string unitText: table._displayString(modelData.unit)
            readonly property bool selected: selectionKey !== "" && table.selectedDqeId === selectionKey
            readonly property color statusColor: table._statusColor(modelData.status)

            Keys.onReturnPressed: table.itemActivated(selectionKey)
            Keys.onEnterPressed: table.itemActivated(selectionKey)

            Rectangle {
                anchors.fill: parent
                color: row.selected ? DqeTheme.selection : DqeTheme.panel
                border.width: row.selected ? DqeTheme.borderWidthStrong : DqeTheme.borderWidth
                border.color: row.selected ? DqeTheme.accent : DqeTheme.border
            }

            Item {
                x: 0
                width: table._columnWidth(0)
                height: parent.height
                Row {
                    anchors.left: parent.left
                    anchors.leftMargin: DqeTheme.spacingM
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: DqeTheme.spacingXs
                    Rectangle {
                        width: 9
                        height: 9
                        radius: width / 2
                        anchors.verticalCenter: parent.verticalCenter
                        color: row.statusColor
                    }
                    Text {
                        text: row.statusText
                        color: row.statusColor
                        font.family: DqeTheme.fontFamily
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                }
            }

            Text {
                x: table._columnWidth(0) + DqeTheme.spacingS
                width: table._columnWidth(1) - DqeTheme.spacingM
                height: parent.height
                text: row.dqeIdText
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }

            Text {
                x: table._columnWidth(0) + table._columnWidth(1) + DqeTheme.spacingS
                width: table._columnWidth(2) - DqeTheme.spacingM
                height: parent.height
                text: row.titleText
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }

            Text {
                x: table._columnWidth(0) + table._columnWidth(1) + table._columnWidth(2) + DqeTheme.spacingS
                width: table._columnWidth(3) - DqeTheme.spacingM
                height: parent.height
                text: row.valueText
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }

            Text {
                x: table._columnWidth(0) + table._columnWidth(1) + table._columnWidth(2)
                   + table._columnWidth(3) + DqeTheme.spacingS
                width: table._columnWidth(4) - DqeTheme.spacingM
                height: parent.height
                text: row.unitText
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }

            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                onClicked: {
                    row.forceActiveFocus()
                    if (row.selectionKey !== "" && table.selectedDqeId !== row.selectionKey) {
                        table.itemSelected(row.selectionKey)
                    }
                }
                onDoubleClicked: table.itemActivated(row.selectionKey)
            }
        }
    }
    }

    // 固定在表格 viewport 置中（不隨橫向捲動移動）。
    Text {
        objectName: "emptyResultMessage"
        anchors.horizontalCenter: parent.horizontalCenter
        y: headerRow.height + (parent.height - headerRow.height - height) / 2
        visible: table._visibleItems.length === 0
        text: DqeI18n.t("No DQE items match the active filter", "沒有符合目前篩選的 DQE 項目", "没有符合当前筛选的 DQE 项目")
        color: DqeTheme.mutedText
        font.family: DqeTheme.fontFamily
        font.pixelSize: 13
    }
}
