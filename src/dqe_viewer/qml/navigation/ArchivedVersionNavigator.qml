// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Archived mock source navigator。
// 只顯示 owner 提供的 immutable mock SourceRef；不掃描檔案系統或呼叫外部工具。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: navigator
    objectName: "archivedVersionNavigator"

    property string currentWorkspace: "Archived"
    property var sources: []
    property string selectedSourceId: ""
    // 預設維持 mock 視覺；production facade 注入時才改用中性標籤。
    property bool productionSource: false
    property alias filterText: filterInput.text

    signal sourceSelected(string sourceId)

    readonly property var visibleSources: _filteredSources()
    // 專案／版次是內建 sample data 的標籤；正式資料沒有這兩個欄位，樹狀結構從 block 開始。
    readonly property string _projectReleaseLabel: "Project-to-V0 / trial"
    readonly property string _treeIndent: productionSource ? "" : "  "
    readonly property string _blockLabel: visibleSources.length > 0
                                          && visibleSources[0].block !== undefined
                                          ? String(visibleSources[0].block) : "—"
    readonly property string _stageLabel: visibleSources.length > 0
                                          && visibleSources[0].flow !== undefined
                                          ? String(visibleSources[0].flow) : "—"

    implicitWidth: 260
    implicitHeight: 560

    function _sourceId(source) {
        return source !== null && source !== undefined && source.sourceId !== undefined
               ? String(source.sourceId) : ""
    }

    function _sourceSearchText(source) {
        if (source === null || source === undefined) {
            return ""
        }
        const fields = [source.displayName, source.jobKey, source.block, source.flow,
                        source.dateTime, source.versionPath]
        return fields.join(" ").toLowerCase()
    }

    function _filteredSources() {
        const sourceItems = Array.isArray(sources) ? sources : []
        const query = filterText.trim().toLowerCase()
        const matched = []
        for (let index = 0; index < sourceItems.length; ++index) {
            if (query.length === 0 || _sourceSearchText(sourceItems[index]).indexOf(query) !== -1) {
                matched.push(sourceItems[index])
            }
        }
        return matched
    }

    function _isVisible(sourceId) {
        for (let index = 0; index < visibleSources.length; ++index) {
            if (_sourceId(visibleSources[index]) === sourceId) {
                return true
            }
        }
        return false
    }

    function _clearSelectionIfFilteredOut() {
        if (selectedSourceId !== "" && !_isVisible(selectedSourceId)) {
            sourceSelected("")
        }
    }

    onFilterTextChanged: Qt.callLater(_clearSelectionIfFilteredOut)
    onSourcesChanged: Qt.callLater(_clearSelectionIfFilteredOut)

    Rectangle {
        anchors.fill: parent
        color: DqeTheme.panel
        border.width: DqeTheme.borderWidth
        border.color: DqeTheme.border
    }

    Column {
        anchors.fill: parent
        anchors.margins: DqeTheme.spacingS
        spacing: DqeTheme.spacingS

        Text {
            objectName: "archivedNavigatorContext"
            width: parent.width
            text: navigator.currentWorkspace.toUpperCase()
                  + (navigator.productionSource ? " / SOURCES" : " / MOCK SOURCES")
            color: DqeTheme.accent
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            font.letterSpacing: 0.5
            elide: Text.ElideRight
        }

        TextField {
            id: filterInput
            objectName: "archivedNavigatorFilter"
            width: parent.width
            height: 32
            placeholderText: DqeI18n.t("Filter phase / block / version", "篩選 phase / block / version", "筛选 phase / block / version")
            color: DqeTheme.text
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            selectByMouse: true
            background: Rectangle {
                radius: DqeTheme.radiusSmall
                color: DqeTheme.headerBackground
                border.width: filterInput.activeFocus ? DqeTheme.borderWidthStrong : DqeTheme.borderWidth
                border.color: filterInput.activeFocus ? DqeTheme.accent : DqeTheme.border
            }
        }

        Flickable {
            id: sourceFlickable
            objectName: "archivedNavigatorSourceList"
            width: parent.width
            height: parent.height - filterInput.height - navigatorContextHeight
                    - DqeTheme.spacingS * 2
            clip: true
            contentWidth: width
            contentHeight: sourceColumn.implicitHeight
            boundsBehavior: Flickable.StopAtBounds

            readonly property int navigatorContextHeight: 20

            ScrollBar.vertical: ScrollBar {}

            Column {
                id: sourceColumn
                width: sourceFlickable.width
                spacing: DqeTheme.spacingXs

                Text {
                    objectName: "archivedNavigatorProjectHeading"
                    width: parent.width
                    visible: !navigator.productionSource
                    text: DqeI18n.t("PROJECT / RELEASE", "專案 / 版次", "项目 / 版次")
                    color: DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 10
                    font.letterSpacing: 0.5
                }
                Text {
                    objectName: "archivedNavigatorProjectRelease"
                    width: parent.width
                    visible: !navigator.productionSource
                    text: "▾ " + navigator._projectReleaseLabel
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
                Text {
                    objectName: "archivedNavigatorBlock"
                    width: parent.width
                    text: navigator._treeIndent + "▾ block / " + navigator._blockLabel
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
                Text {
                    width: parent.width
                    text: navigator._treeIndent + "  ▾ stage / " + navigator._stageLabel
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
                Text {
                    width: parent.width
                    topPadding: DqeTheme.spacingS
                    text: navigator._treeIndent + "    VERSIONS"
                    color: DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 10
                    font.letterSpacing: 0.5
                }

                Repeater {
                    model: navigator.visibleSources

                    delegate: AbstractButton {
                        id: versionButton
                        required property var modelData

                        readonly property string sourceId: navigator._sourceId(modelData)
                        readonly property bool selected: sourceId !== ""
                                                       && navigator.selectedSourceId === sourceId
                        objectName: "archivedVersion" + sourceId
                        width: sourceColumn.width
                        height: 46
                        focusPolicy: Qt.StrongFocus
                        onClicked: {
                            if (sourceId !== "" && sourceId !== navigator.selectedSourceId) {
                                navigator.sourceSelected(sourceId)
                            }
                        }

                        background: Rectangle {
                            radius: DqeTheme.radiusSmall
                            color: versionButton.selected ? DqeTheme.selection
                                                         : (versionButton.hovered ? DqeTheme.panelRaised
                                                                                  : "transparent")
                            border.width: versionButton.selected ? DqeTheme.borderWidthStrong
                                                                  : DqeTheme.borderWidth
                            border.color: versionButton.selected ? DqeTheme.accent : DqeTheme.border
                        }

                        contentItem: Column {
                            leftPadding: DqeTheme.spacingM
                            rightPadding: DqeTheme.spacingS
                            spacing: DqeTheme.spacingXxs
                            Text {
                                width: parent.width
                                text: modelData.displayName === undefined ? "—" : String(modelData.displayName)
                                color: versionButton.selected ? DqeTheme.accent : DqeTheme.text
                                font.family: DqeTheme.fontFamily
                                font.pixelSize: 12
                                elide: Text.ElideRight
                            }
                            Text {
                                width: parent.width
                                text: modelData.jobKey === undefined ? "—" : String(modelData.jobKey)
                                color: DqeTheme.mutedText
                                font.family: DqeTheme.fontFamily
                                font.pixelSize: 10
                                elide: Text.ElideRight
                            }
                        }
                    }
                }

                Text {
                    objectName: "archivedNavigatorNoMatches"
                    width: parent.width
                    topPadding: DqeTheme.spacingL
                    visible: navigator.visibleSources.length === 0
                    text: filterInput.text.length > 0 ? DqeI18n.t("No archived versions match this filter.", "沒有符合此篩選的已歸檔版本。", "没有符合此筛选的已归档版本。")
                                                     : DqeI18n.t("No archived versions are available.", "沒有可用的已歸檔版本。", "没有可用的已归档版本。")
                    color: DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
            }
        }
    }
}
