// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Temporary mock job navigator。
// 只顯示 owner 提供的 mock job 目錄；不掃描檔案系統、不查資料庫、不呼叫外部工具。
// 排序：依 mock execution time 新到舊（決定性，jobKey 反向作為 tie-break）。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: navigator
    objectName: "temporaryJobNavigator"

    property string currentWorkspace: "Temporary"
    property var jobs: []
    property string selectedJobKey: ""
    property int retentionDays: 14
    // 預設保留 mock visual；DQE Viewer production binding injected provider 才使用中性 production label。
    property bool productionSource: false
    property alias filterText: filterInput.text

    signal jobSelected(string jobKey)

    readonly property var visibleJobs: _visibleJobs()

    implicitWidth: 260
    implicitHeight: 560

    function _jobKey(job) {
        return job !== null && job !== undefined && job.jobKey !== undefined
               ? String(job.jobKey) : ""
    }

    function _jobSearchText(job) {
        if (job === null || job === undefined) {
            return ""
        }
        const fields = [job.jobKey, job.block, job.flow, job.executionTime]
        return fields.join(" ").toLowerCase()
    }

    // 依 execution time 新到舊；相同時間以 jobKey 反向排序，確保完全決定性。
    function _sorted(list) {
        const copy = list.slice()
        copy.sort(function (a, b) {
            const ta = a.executionTime === undefined ? "" : String(a.executionTime)
            const tb = b.executionTime === undefined ? "" : String(b.executionTime)
            if (ta < tb) { return 1 }
            if (ta > tb) { return -1 }
            const ka = navigator._jobKey(a)
            const kb = navigator._jobKey(b)
            if (ka < kb) { return 1 }
            if (ka > kb) { return -1 }
            return 0
        })
        return copy
    }

    function _visibleJobs() {
        const source = Array.isArray(jobs) ? jobs : []
        const query = filterText.trim().toLowerCase()
        const matched = []
        for (let index = 0; index < source.length; ++index) {
            if (query.length === 0 || _jobSearchText(source[index]).indexOf(query) !== -1) {
                matched.push(source[index])
            }
        }
        return _sorted(matched)
    }

    function _worstStatus(job) {
        if (job === null || job === undefined) {
            return { label: "—", color: DqeTheme.unknown }
        }
        const fail = Number(job.fail) || 0
        const warn = Number(job.warn) || 0
        const pass = Number(job.pass) || 0
        if (fail > 0) {
            return { label: fail + " FAIL", color: DqeTheme.fail }
        }
        if (warn > 0) {
            return { label: warn + " WARN", color: DqeTheme.warn }
        }
        if (pass > 0) {
            return { label: "PASS", color: DqeTheme.pass }
        }
        return { label: "—", color: DqeTheme.unknown }
    }

    // expired 是 lifecycle 狀態，不是 error，因此用 muted 色而非 fail 色。
    function _productionRemainingDays(job) {
        if (job === null || job === undefined || job.expiresAt === undefined) {
            return -1
        }
        // Temporary adapter 的 authoritative expiry 格式是 yyyyMMdd_HHmmss。
        // 解析失敗時不可捏造剩餘天數，改由呼叫端顯示 policy 天數。
        const match = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(String(job.expiresAt))
        if (match === null) {
            return -1
        }
        const expiry = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]),
                                Number(match[4]), Number(match[5]), Number(match[6]))
        const expiryMs = expiry.getTime()
        if (isNaN(expiryMs)) {
            return -1
        }
        return Math.max(0, Math.ceil((expiryMs - Date.now()) / 86400000))
    }

    function _retentionText(job) {
        if (job !== null && job !== undefined && job.expired === true) {
            return "expired"
        }
        const days = job === null || job === undefined ? 0 : (Number(job.retentionDays) || 0)
        if (navigator.productionSource) {
            const remaining = _productionRemainingDays(job)
            return remaining >= 0 ? "expires in " + remaining + "d" : "retention " + days + "d"
        }
        return "expires in " + days + "d"
    }

    function _isVisible(jobKey) {
        for (let index = 0; index < visibleJobs.length; ++index) {
            if (_jobKey(visibleJobs[index]) === jobKey) {
                return true
            }
        }
        return false
    }

    // 選取的 job 只要仍出現在清單就保留；被 filter 排除時清一次選取。
    function _clearSelectionIfFilteredOut() {
        if (selectedJobKey !== "" && !_isVisible(selectedJobKey)) {
            jobSelected("")
        }
    }

    onFilterTextChanged: Qt.callLater(_clearSelectionIfFilteredOut)
    onJobsChanged: Qt.callLater(_clearSelectionIfFilteredOut)

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

        Row {
            width: parent.width
            Text {
                objectName: "temporaryJobNavigatorContext"
                width: parent.width - retentionBadge.implicitWidth - DqeTheme.spacingS
                text: DqeI18n.t("TEMPORARY", "暫存", "暂存")
                      + (navigator.productionSource ? DqeI18n.t(" / JOBS", " / JOB", " / JOB") : DqeI18n.t(" / MOCK JOBS", " / MOCK JOB", " / MOCK JOB"))
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
                font.letterSpacing: 0.5
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            Text {
                id: retentionBadge
                objectName: "temporaryRetentionBadge"
                text: DqeI18n.t("Retention ", "保留 ", "保留 ") + navigator.retentionDays + "d"
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
                verticalAlignment: Text.AlignVCenter
            }
        }

        TextField {
            id: filterInput
            objectName: "temporaryJobNavigatorFilter"
            width: parent.width
            height: 32
            placeholderText: DqeI18n.t("Search job_key / block / flow", "搜尋 job_key / block / flow", "搜索 job_key / block / flow")
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
            id: jobFlickable
            objectName: "temporaryJobNavigatorList"
            width: parent.width
            height: parent.height - filterInput.height - navigatorContextHeight
                    - DqeTheme.spacingS * 2
            clip: true
            contentWidth: width
            contentHeight: jobColumn.implicitHeight
            boundsBehavior: Flickable.StopAtBounds

            readonly property int navigatorContextHeight: 20

            ScrollBar.vertical: ScrollBar {}

            Column {
                id: jobColumn
                width: jobFlickable.width
                spacing: DqeTheme.spacingXs

                Repeater {
                    model: navigator.visibleJobs

                    delegate: AbstractButton {
                        id: jobButton
                        required property var modelData

                        readonly property string jobKey: navigator._jobKey(modelData)
                        readonly property bool selected: jobKey !== ""
                                                       && navigator.selectedJobKey === jobKey
                        readonly property var worst: navigator._worstStatus(modelData)
                        objectName: "temporaryJob" + jobKey
                        width: jobColumn.width
                        height: 62
                        focusPolicy: Qt.StrongFocus
                        onClicked: {
                            if (jobKey !== "" && jobKey !== navigator.selectedJobKey) {
                                navigator.jobSelected(jobKey)
                            }
                        }

                        background: Rectangle {
                            radius: DqeTheme.radiusSmall
                            color: jobButton.selected ? DqeTheme.selection
                                                      : (jobButton.hovered ? DqeTheme.panelRaised
                                                                           : "transparent")
                            border.width: jobButton.selected ? DqeTheme.borderWidthStrong
                                                             : DqeTheme.borderWidth
                            border.color: jobButton.selected ? DqeTheme.accent : DqeTheme.border
                        }

                        contentItem: Item {
                            anchors.fill: parent

                            Column {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.leftMargin: DqeTheme.spacingM
                                anchors.rightMargin: DqeTheme.spacingS
                                spacing: DqeTheme.spacingXxs

                                Item {
                                    width: parent.width
                                    height: jobKeyText.implicitHeight
                                    Text {
                                        id: jobKeyText
                                        anchors.left: parent.left
                                        anchors.right: worstBadge.left
                                        anchors.rightMargin: DqeTheme.spacingS
                                        text: jobButton.jobKey === "" ? "—" : jobButton.jobKey
                                        color: jobButton.selected ? DqeTheme.accent : DqeTheme.text
                                        font.family: DqeTheme.fontFamily
                                        font.pixelSize: 12
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        id: worstBadge
                                        objectName: "temporaryJobWorst" + jobButton.jobKey
                                        anchors.right: parent.right
                                        text: jobButton.worst.label
                                        color: jobButton.worst.color
                                        font.family: DqeTheme.fontFamily
                                        font.pixelSize: 10
                                        font.letterSpacing: 0.5
                                    }
                                }
                                Text {
                                    width: parent.width
                                    text: (modelData.block === undefined ? "—" : String(modelData.block))
                                          + " · " + (modelData.flow === undefined ? "—" : String(modelData.flow))
                                    color: DqeTheme.mutedText
                                    font.family: DqeTheme.fontFamily
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                                Text {
                                    objectName: "temporaryJobRetention" + jobButton.jobKey
                                    width: parent.width
                                    text: navigator._retentionText(modelData)
                                    color: DqeTheme.faintText
                                    font.family: DqeTheme.fontFamily
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }

                Text {
                    objectName: "temporaryJobNavigatorNoMatches"
                    width: parent.width
                    topPadding: DqeTheme.spacingL
                    visible: navigator.visibleJobs.length === 0
                    text: filterInput.text.length > 0 ? DqeI18n.t("No temporary jobs match this filter.", "沒有符合此篩選的暫存 job。", "没有符合此筛选的暂存 job。")
                                                     : DqeI18n.t("No temporary jobs", "沒有暫存 job", "没有暂存 job")
                    color: DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }

                Text {
                    objectName: "temporaryJobNavigatorEmptyHint"
                    width: parent.width
                    visible: navigator.visibleJobs.length === 0 && filterInput.text.length === 0
                    text: DqeI18n.t("Adjust filters or wait for a new run", "調整篩選或等待新的執行", "调整筛选或等待新的执行")
                    color: DqeTheme.faintText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }
        }
    }
}
