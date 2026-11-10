// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 共用 DQE 狀態摘要卡。
// 此元件只發出篩選要求；不改寫外部 activeFilter 或任何資料模型。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: summaryCards

    property int totalCount: 0
    property int passCount: 0
    property int warnCount: 0
    property int failCount: 0
    property int unknownCount: 0
    property string activeFilter: ""

    signal statusFilterRequested(string status)

    readonly property var _cards: [
        { label: DqeI18n.t("Total","總計","总计"), status: "" },
        { label: DqeI18n.t("Pass","通過","通过"), status: "Pass" },
        { label: DqeI18n.t("Warn","警告","警告"), status: "Warn" },
        { label: DqeI18n.t("Fail","失敗","失败"), status: "Fail" },
        { label: DqeI18n.t("Unknown","未知","未知"), status: "Unknown" }
    ]

    implicitWidth: 620
    implicitHeight: 80

    function _countFor(status) {
        switch (status) {
        case "Pass":
            return passCount
        case "Warn":
            return warnCount
        case "Fail":
            return failCount
        case "Unknown":
            return unknownCount
        default:
            return totalCount
        }
    }

    function _colorFor(status) {
        switch (status) {
        case "Pass":
            return DqeTheme.pass
        case "Warn":
            return DqeTheme.warn
        case "Fail":
            return DqeTheme.fail
        case "Unknown":
            return DqeTheme.unknown
        default:
            return DqeTheme.accent
        }
    }

    Row {
        id: cardRow
        anchors.fill: parent
        spacing: DqeTheme.spacingS

        Repeater {
            model: summaryCards._cards

            delegate: AbstractButton {
                id: card
                required property int index

                objectName: "summaryCard" + summaryCards._cards[index].label
                width: (cardRow.width - cardRow.spacing * (summaryCards._cards.length - 1))
                       / summaryCards._cards.length
                height: cardRow.height
                focusPolicy: Qt.StrongFocus

                readonly property string labelText: summaryCards._cards[index].label
                readonly property string filterStatus: summaryCards._cards[index].status
                readonly property int countValue: summaryCards._countFor(filterStatus)
                readonly property string displayValue: String(countValue)
                readonly property color statusColor: summaryCards._colorFor(filterStatus)
                readonly property bool priority: filterStatus === "Warn" || filterStatus === "Fail"
                readonly property bool selected: filterStatus === ""
                                              ? summaryCards.activeFilter === ""
                                              : summaryCards.activeFilter === filterStatus

                onClicked: summaryCards.statusFilterRequested(filterStatus)

                background: Rectangle {
                    radius: DqeTheme.radiusPanel
                    color: card.selected ? DqeTheme.selection
                                         : (card.priority ? DqeTheme.panelRaised : DqeTheme.panel)
                    border.width: card.selected ? DqeTheme.borderWidthStrong : DqeTheme.borderWidth
                    border.color: card.selected || card.priority ? card.statusColor : DqeTheme.border
                }

                contentItem: Item {
                    Row {
                        anchors.left: parent.left
                        anchors.leftMargin: DqeTheme.spacingM
                        anchors.top: parent.top
                        anchors.topMargin: DqeTheme.spacingM
                        spacing: DqeTheme.spacingXs

                        Rectangle {
                            width: 7
                            height: 7
                            radius: width / 2
                            anchors.verticalCenter: parent.verticalCenter
                            color: card.statusColor
                        }

                        Text {
                            text: card.labelText.toUpperCase()
                            color: card.statusColor
                            font.family: DqeTheme.fontFamily
                            font.pixelSize: 11
                            font.letterSpacing: 0.6
                        }
                    }

                    Text {
                        anchors.left: parent.left
                        anchors.leftMargin: DqeTheme.spacingM
                        anchors.bottom: parent.bottom
                        anchors.bottomMargin: DqeTheme.spacingM - DqeTheme.spacingXs
                        text: card.displayValue
                        color: card.statusColor
                        font.family: DqeTheme.fontFamily
                        font.pixelSize: 28
                        font.weight: Font.Medium
                    }
                }
            }
        }
    }
}
