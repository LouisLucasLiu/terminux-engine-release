// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 右側 selected-item Inspector。
// 僅讀取 item 與 attachment metadata；不讀取 payload、不執行任何 lifecycle／backend 操作。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: inspector

    property var item: null
    property string sourceKind: ""
    property string sourceLabel: ""

    readonly property bool hasSelection: item !== null && item !== undefined
    readonly property string sourceContext: sourceLabel.length > 0
                                            ? (sourceKind.length > 0 ? sourceKind + " · " + sourceLabel : sourceLabel)
                                            : DqeI18n.t("Selected DQE item", "選取的 DQE 項目", "选取的 DQE 项目")

    implicitWidth: 340
    implicitHeight: 520

    function _hasText(value) {
        return value !== undefined && value !== null && String(value).length > 0
    }

    function _optionalText(value) {
        return _hasText(value) ? String(value) : "—"
    }

    function _requiredText(value, fieldName) {
        return _hasText(value) ? String(value) : DqeI18n.t("Data issue: ", "資料問題：", "数据问题：") + fieldName + DqeI18n.t(" missing", " 缺失", " 缺失")
    }

    function _itemValue(name) {
        if (!hasSelection || item === null || item === undefined) {
            return undefined
        }
        return item[name]
    }

    function _attachments() {
        return hasSelection && Array.isArray(item.attachments) ? item.attachments : []
    }

    function _executableCount() {
        const attachments = _attachments()
        let count = 0
        for (let index = 0; index < attachments.length; ++index) {
            const metadata = attachments[index].metadata
            if (metadata !== undefined && metadata !== null && metadata.executable === true) {
                ++count
            }
        }
        return count
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

    component FieldRow: Item {
        id: fieldRow

        property string fieldName: ""
        property string fieldValue: "—"
        property color fieldColor: DqeTheme.text
        property bool wrapValue: false
        property bool elideValue: false
        property bool showMarker: false
        property string tooltipText: ""
        property string fieldObjectName: ""
        readonly property string displayText: fieldValue

        objectName: fieldObjectName
        width: parent ? parent.width : 0
        implicitHeight: label.implicitHeight + DqeTheme.spacingXxs
                        + valueText.implicitHeight + DqeTheme.spacingM
        height: implicitHeight

        Text {
            id: label
            anchors.left: parent.left
            anchors.leftMargin: DqeTheme.spacingM
            anchors.right: parent.right
            anchors.rightMargin: DqeTheme.spacingM
            anchors.top: parent.top
            text: fieldRow.fieldName.toUpperCase()
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 10
            font.letterSpacing: 0.5
            elide: Text.ElideRight
        }

        Rectangle {
            visible: fieldRow.showMarker
            anchors.left: parent.left
            anchors.leftMargin: DqeTheme.spacingM
            anchors.top: label.bottom
            anchors.topMargin: DqeTheme.spacingXs
            width: 8
            height: 8
            radius: width / 2
            color: fieldRow.fieldColor
        }

        Text {
            id: valueText
            objectName: fieldRow.fieldObjectName === "inspectorFieldSourcePath"
                        ? "sourcePathValueText" : ""
            anchors.left: parent.left
            anchors.leftMargin: DqeTheme.spacingM + (fieldRow.showMarker ? DqeTheme.spacingM : 0)
            anchors.right: parent.right
            anchors.rightMargin: DqeTheme.spacingM
            anchors.top: label.bottom
            anchors.topMargin: DqeTheme.spacingXs
            text: fieldRow.fieldValue
            color: fieldRow.fieldColor
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            wrapMode: fieldRow.wrapValue ? Text.Wrap : Text.NoWrap
            maximumLineCount: fieldRow.wrapValue ? 0 : 1
            elide: fieldRow.elideValue ? Text.ElideMiddle : Text.ElideNone
            ToolTip.visible: fieldRow.tooltipText.length > 0 && sourcePathHover.containsMouse
            ToolTip.text: fieldRow.tooltipText
        }

        MouseArea {
            id: sourcePathHover
            objectName: fieldRow.fieldObjectName === "inspectorFieldSourcePath" ? "sourcePathHover" : ""
            anchors.fill: valueText
            hoverEnabled: fieldRow.tooltipText.length > 0
            acceptedButtons: Qt.NoButton
        }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: DqeTheme.borderWidth
            color: DqeTheme.border
        }
    }

    Rectangle {
        anchors.fill: parent
        color: DqeTheme.panel
        border.width: DqeTheme.borderWidth
        border.color: DqeTheme.border
    }

    Text {
        id: sourceContextText
        anchors.left: parent.left
        anchors.leftMargin: DqeTheme.spacingM
        anchors.right: parent.right
        anchors.rightMargin: DqeTheme.spacingM
        anchors.top: parent.top
        anchors.topMargin: DqeTheme.spacingM
        text: inspector.sourceContext
        color: DqeTheme.accent
        font.family: DqeTheme.fontFamily
        font.pixelSize: 12
        elide: Text.ElideRight
    }

    Text {
        id: noSelectionPrompt
        objectName: "inspectorNoSelectionPrompt"
        anchors.left: parent.left
        anchors.leftMargin: DqeTheme.spacingL
        anchors.right: parent.right
        anchors.rightMargin: DqeTheme.spacingL
        anchors.verticalCenter: parent.verticalCenter
        visible: !inspector.hasSelection
        text: DqeI18n.t("Select a DQE item to inspect its details.", "選取一個 DQE 項目以檢視明細。", "选取一个 DQE 项目以查看明细。")
        color: DqeTheme.mutedText
        font.family: DqeTheme.fontFamily
        font.pixelSize: 13
        wrapMode: Text.Wrap
        horizontalAlignment: Text.AlignHCenter
    }

    Column {
        id: identityColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: sourceContextText.bottom
        anchors.topMargin: DqeTheme.spacingS
        visible: inspector.hasSelection

        FieldRow {
            fieldName: "DQE_ID"
            fieldValue: inspector._requiredText(inspector._itemValue("dqeId"), "DQE_ID")
            fieldColor: inspector._hasText(inspector._itemValue("dqeId")) ? DqeTheme.accent : DqeTheme.fail
            fieldObjectName: "inspectorFieldDqeId"
        }
        FieldRow {
            fieldName: DqeI18n.t("Title","標題","标题")
            fieldValue: inspector._requiredText(inspector._itemValue("title"), "Title")
            fieldColor: inspector._hasText(inspector._itemValue("title")) ? DqeTheme.text : DqeTheme.fail
            wrapValue: true
            fieldObjectName: "inspectorFieldTitle"
        }
        FieldRow {
            fieldName: DqeI18n.t("Value","數值","数值")
            fieldValue: inspector._optionalText(inspector._itemValue("value"))
            fieldObjectName: "inspectorFieldValue"
        }
        FieldRow {
            fieldName: DqeI18n.t("Unit","單位","单位")
            fieldValue: inspector._optionalText(inspector._itemValue("unit"))
            fieldObjectName: "inspectorFieldUnit"
        }
        FieldRow {
            fieldName: DqeI18n.t("Status","狀態","状态")
            fieldValue: inspector._requiredText(inspector._itemValue("status"), "Status")
            fieldColor: inspector._statusColor(inspector._itemValue("status"))
            showMarker: true
            fieldObjectName: "inspectorFieldStatus"
        }
    }

    Flickable {
        id: detailsFlickable
        objectName: "inspectorDetailsFlickable"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: identityColumn.bottom
        anchors.topMargin: DqeTheme.spacingS
        anchors.bottom: parent.bottom
        anchors.bottomMargin: DqeTheme.spacingS
        visible: inspector.hasSelection
        clip: true
        contentWidth: width
        contentHeight: detailsColumn.implicitHeight
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.vertical: ScrollBar {}

        Column {
            id: detailsColumn
            width: detailsFlickable.width

            FieldRow {
                fieldName: DqeI18n.t("Description","說明","说明")
                fieldValue: inspector._optionalText(inspector._itemValue("description"))
                wrapValue: true
                fieldObjectName: "inspectorFieldDescription"
            }
            FieldRow {
                fieldName: DqeI18n.t("Reference","參考","参考")
                fieldValue: inspector._optionalText(inspector._itemValue("reference"))
                wrapValue: true
                fieldObjectName: "inspectorFieldReference"
            }
            FieldRow {
                fieldName: DqeI18n.t("Date/time","日期/時間","日期/时间")
                fieldValue: inspector._optionalText(inspector._itemValue("dateTime"))
                fieldObjectName: "inspectorFieldDateTime"
            }
            FieldRow {
                fieldName: DqeI18n.t("Block","Block","Block")
                fieldValue: inspector._optionalText(inspector._itemValue("block"))
                fieldObjectName: "inspectorFieldBlock"
            }
            FieldRow {
                fieldName: DqeI18n.t("Flow","Flow","Flow")
                fieldValue: inspector._optionalText(inspector._itemValue("flow"))
                fieldObjectName: "inspectorFieldFlow"
            }
            FieldRow {
                fieldName: DqeI18n.t("Source path","來源路徑","来源路径")
                fieldValue: inspector._optionalText(inspector._itemValue("sourcePath"))
                elideValue: true
                tooltipText: inspector._hasText(inspector._itemValue("sourcePath"))
                             ? String(inspector._itemValue("sourcePath")) : ""
                fieldObjectName: "inspectorFieldSourcePath"
            }
            FieldRow {
                fieldName: DqeI18n.t("Attachment count","附件數","附件数")
                fieldValue: String(inspector._attachments().length)
                fieldObjectName: "inspectorFieldAttachmentCount"
            }
            FieldRow {
                fieldName: DqeI18n.t("Executable count","可執行檔數","可执行文件数")
                fieldValue: String(inspector._executableCount())
                fieldObjectName: "inspectorFieldExecutableCount"
            }
        }
    }
}
