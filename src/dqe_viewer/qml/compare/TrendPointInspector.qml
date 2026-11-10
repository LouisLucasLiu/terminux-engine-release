// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Selected trend point inspector。只讀顯示選取點的來源、raw、正規化與 baseline delta，
// 並明確標示「relative change only」；不做任何 score／品質判讀，不暗示 up/down 代表好壞。

import QtQuick
import "../theme"
import "../i18n"

Item {
    id: inspector
    objectName: "trendPointInspector"

    // detail：null 表示未選取；否則為 workspace 預先組好的顯示資料。
    property var detail: null

    readonly property bool hasSelection: detail !== null && detail !== undefined

    implicitWidth: 320
    implicitHeight: 480

    function _text(key) {
        if (!hasSelection || detail[key] === undefined || detail[key] === null) {
            return "—"
        }
        const value = String(detail[key])
        return value.length === 0 ? "—" : value
    }

    function _statusColor(status) {
        switch (status) {
        case "Pass": return DqeTheme.pass
        case "Warn": return DqeTheme.warn
        case "Fail": return DqeTheme.fail
        default: return DqeTheme.text
        }
    }

    component FieldRow: Column {
        property string label: ""
        property string value: "—"
        property color valueColor: DqeTheme.text
        property string valueObjectName: ""
        width: parent ? parent.width : 0
        spacing: DqeTheme.spacingXxs
        Text {
            text: parent.label
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 10
            font.letterSpacing: 0.5
        }
        Text {
            objectName: parent.valueObjectName
            width: parent.width
            text: parent.value
            color: parent.valueColor
            font.family: DqeTheme.fontFamily
            font.pixelSize: 13
            elide: Text.ElideRight
        }
    }

    Text {
        objectName: "inspectorNoTrendSelection"
        anchors.centerIn: parent
        width: parent.width - DqeTheme.spacingXl
        visible: !inspector.hasSelection
        horizontalAlignment: Text.AlignHCenter
        text: DqeI18n.t("Select a chart point or table cell to inspect its detail.", "選取圖表上的點或表格儲存格以檢視明細。", "选取图表上的点或表格单元格以查看明细。")
        color: DqeTheme.mutedText
        font.family: DqeTheme.fontFamily
        font.pixelSize: 13
        wrapMode: Text.Wrap
    }

    Flickable {
        anchors.fill: parent
        anchors.margins: DqeTheme.spacingM
        visible: inspector.hasSelection
        contentWidth: width
        contentHeight: body.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
            id: body
            width: parent.width
            spacing: DqeTheme.spacingM

            Text {
                objectName: "inspectorTrendDqeId"
                width: parent.width
                text: inspector._text("dqeId")
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 18
                elide: Text.ElideRight
            }
            Text {
                width: parent.width
                text: inspector._text("title")
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 13
                elide: Text.ElideRight
            }

            FieldRow {
                label: DqeI18n.t("SOURCE", "來源", "来源")
                valueObjectName: "inspectorTrendSource"
                value: (inspector.hasSelection ? inspector._text("sourceKind") + " · " + inspector._text("sourceLabel") : "—")
            }
            FieldRow { label: DqeI18n.t("JOB / VERSION", "Job / 版本", "Job / 版本"); value: inspector._text("jobOrVersion") }
            FieldRow { label: DqeI18n.t("BLOCK", "Block", "Block"); value: inspector._text("block") }
            FieldRow {
                label: DqeI18n.t("RAW VALUE", "原始數值", "原始数值")
                valueObjectName: "inspectorTrendRaw"
                value: inspector.hasSelection
                       ? inspector._text("rawValue") + (inspector._text("rawUnit") === "—" ? "" : " " + inspector._text("rawUnit"))
                       : "—"
            }
            FieldRow {
                label: DqeI18n.t("NORMALIZED", "正規化", "归一化")
                valueObjectName: "inspectorTrendNormalized"
                value: inspector._text("normalizedText")
            }
            FieldRow {
                label: DqeI18n.t("BASELINE DELTA", "相對基準差", "相对基准差")
                valueObjectName: "inspectorTrendDelta"
                value: inspector._text("deltaText")
            }
            FieldRow {
                label: DqeI18n.t("STATUS", "狀態", "状态")
                valueObjectName: "inspectorTrendStatus"
                value: inspector.hasSelection ? String(inspector._text("status")).toUpperCase() : "—"
                valueColor: inspector.hasSelection ? inspector._statusColor(detail.status) : DqeTheme.text
            }
            FieldRow {
                label: DqeI18n.t("COMPARE ISSUE", "比較問題", "比较问题")
                valueObjectName: "inspectorTrendCompareIssue"
                value: inspector._text("issue")
                valueColor: inspector.hasSelection && inspector._text("issue") !== "—"
                            ? DqeTheme.warn : DqeTheme.text
            }

            Rectangle { width: parent.width; height: DqeTheme.borderWidth; color: DqeTheme.border }

            Text {
                text: DqeI18n.t("Interpretation", "解讀", "解读")
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 10
                font.letterSpacing: 0.5
            }
            Text {
                objectName: "inspectorRelativeWarning"
                width: parent.width
                text: DqeI18n.t("Relative change only. Higher does not imply better.", "僅為相對變化；數值較高不代表較好。", "仅为相对变化；数值较高不代表较好。")
                color: DqeTheme.warn
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }
        }
    }
}
