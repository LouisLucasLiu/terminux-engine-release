// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import QtQuick
import QtQuick.Controls
import "theme"
import "i18n"
// DqeHeader / WorkspaceTabs / DqeStatusBar 為同模組（DqeViewer）型別，隱式可見，
// 無需額外 import；theme 為目錄 import（手寫 qmldir singleton）。

// Archived 是一般啟動的預設 workspace。
// Temporary workspace 已組成（單一 shell、重用共用 review 元件、mock lifecycle）。
// Compare workspace 已組成（Qt Quick Shapes renderer 位於 NormalizedTrendChart）。
// 七個 canonical visual scenario 只供 test/offscreen 注入；無 production binding。
ApplicationWindow {
    id: root
    visible: true
    width: 1440
    height: 900
    minimumWidth: 1280
    minimumHeight: 720
    title: "DQE Viewer"
    color: DqeTheme.background

    // 啟動語言（C++ 依 daemon preferences.get_language 注入 → 跟隨 terminal）。
    // header 語言選單可本地即時覆寫（DqeI18n.setLanguage）。
    property string initialLanguage: ""
    onInitialLanguageChanged: {
        if (initialLanguage.length > 0) {
            DqeI18n.setLanguage(initialLanguage)
        }
    }
    Component.onCompleted: {
        if (initialLanguage.length > 0) {
            DqeI18n.setLanguage(initialLanguage)
        }
    }

    // C++ 啟動器注入的 job-key；mode 已在 C++ CLI 完整驗證，QML 不以它推斷 mode。
    property string jobKey: ""
    // production controller 僅由已驗證的 staging CLI 注入。QML 不可讀 endpoint、
    // DB/path 或其他 transport context；沒有任何 context 時保留既有 mock mode。
    property bool productionMode: false
    property var archivedRepositoryController: null
    property var temporaryRepositoryController: null
    property var temporaryOperationController: null
    property var compareSnapshotProvider: null
    property var compareHtmlExporter: null
    // 目前 active workspace（預設 Archived）。
    property int activeWorkspace: WorkspaceTabs.Archived
    // Status bar 的操作訊息（mock）。
    property string statusMessage: "Ready"
    // harness／證據可切換 的既有 mock scenario；不連 production source。
    property string archivedScenario: "archived_normal"
    // harness／證據可切換 Temporary 的 mock scenario；不連 production source。
    property string temporaryScenario: "temporary_normal"
    // 只供 test/offscreen `--ui-scenario` 注入。空值維持一般原型預設。
    property string uiScenario: ""
    property bool _scenarioApplying: false
    property string scenarioHandshakeState: "idle"

    signal uiScenarioReady(string scenarioName)

    readonly property bool _hasUiScenario: uiScenario.length > 0
    onJobKeyChanged: {
        if (jobKey.length > 0) {
            activeWorkspace = WorkspaceTabs.Temporary;
        }
    }

    // 標題列的 compact context：所選 job／version 有 block 時顯示它，否則留空。
    function _blockContext(selection) {
        const block = selection === null || selection === undefined || selection.block === undefined
                      ? "" : String(selection.block)
        return block.length > 0 ? "block: " + block : ""
    }

    // mock refresh 保持原有行為；staging 只刷新已注入的 read controller。
    function refreshCurrentWorkspace() {
        if (!productionMode) {
            statusMessage = "Refresh requested (mock)"
            return
        }
        if (activeWorkspace === WorkspaceTabs.Archived && archivedRepositoryFacadeLoader.item !== null) {
            archivedRepositoryFacadeLoader.item.refreshSources()
        } else if (activeWorkspace === WorkspaceTabs.Temporary
                   && temporaryRepositoryFacadeLoader.item !== null) {
            temporaryRepositoryFacadeLoader.item.refreshSources()
        }
        statusMessage = "Refresh requested"
    }

    function _restoreScenarioLayouts() {
        archivedWorkspace.layout.leftWidth = 260
        archivedWorkspace.layout.rightWidth = 340
        archivedWorkspace.layout.evidenceHeight = 255
        archivedWorkspace.layout.leftCollapsed = false
        archivedWorkspace.layout.rightCollapsed = false
        temporaryWorkspace.layout.leftWidth = 260
        temporaryWorkspace.layout.rightWidth = 340
        temporaryWorkspace.layout.evidenceHeight = 255
        temporaryWorkspace.layout.leftCollapsed = false
        temporaryWorkspace.layout.rightCollapsed = false
        compareWorkspace.layout.leftWidth = 260
        compareWorkspace.layout.rightWidth = 340
        compareWorkspace.layout.evidenceHeight = 255
        compareWorkspace.layout.leftCollapsed = false
        compareWorkspace.layout.rightCollapsed = false
    }

    function _applyUiScenario() {
        if (!_hasUiScenario) {
            return
        }
        _scenarioApplying = true
        scenarioHandshakeState = "configuring"
        _restoreScenarioLayouts()
        archivedScenario = "archived_normal"
        temporaryScenario = "temporary_normal"
        compareWorkspace._resetForTest()
        statusMessage = "UI scenario: " + uiScenario + " (mock)"

        switch (uiScenario) {
        case "archived-main":
            activeWorkspace = WorkspaceTabs.Archived
            break
        case "fail-selected":
            activeWorkspace = WorkspaceTabs.Archived
            break
        case "attachment-maximized":
            activeWorkspace = WorkspaceTabs.Archived
            break
        case "panels-resized":
            activeWorkspace = WorkspaceTabs.Archived
            archivedWorkspace.layout.leftWidth = 330
            archivedWorkspace.layout.rightWidth = 290
            archivedWorkspace.layout.evidenceHeight = 350
            break
        case "temporary-empty":
            activeWorkspace = WorkspaceTabs.Temporary
            temporaryScenario = "temporary_empty"
            break
        case "database-unavailable":
            activeWorkspace = WorkspaceTabs.Temporary
            temporaryScenario = "database_unavailable"
            break
        case "compare-trend":
            activeWorkspace = WorkspaceTabs.Compare
            break
        default:
            // C++ 入口會先拒絕未知 scenario；這裡保留防禦，避免靜默落到 production 預設。
            statusMessage = "Unknown UI scenario (mock)"
            return
        }
        Qt.callLater(_applyUiScenarioSelection)
    }

    function _applyUiScenarioSelection() {
        switch (uiScenario) {
        case "fail-selected":
            archivedWorkspace.setStatusFilter("Fail")
            archivedWorkspace.selectItem("STA_002")
            break
        case "attachment-maximized":
            archivedWorkspace.selectItem("STA_001")
            Qt.callLater(archivedWorkspace.openSelectedAttachmentPreview)
            break
        default:
            break
        }
        _scenarioApplying = false
        scenarioHandshakeState = "polling"
        scenarioReadyPoll.restart()
    }

    function _isUiScenarioReady() {
        switch (uiScenario) {
        case "archived-main":
            return activeWorkspace === WorkspaceTabs.Archived && archivedWorkspace.isReady
                   && archivedWorkspace.selectedDqeId === "STA_001"
        case "fail-selected":
            return activeWorkspace === WorkspaceTabs.Archived && archivedWorkspace.isReady
                   && archivedWorkspace.activeFilter === "Fail"
                   && archivedWorkspace.selectedDqeId === "STA_002"
        case "attachment-maximized":
            return activeWorkspace === WorkspaceTabs.Archived && archivedWorkspace.isReady
                   && archivedWorkspace.selectedDqeId === "STA_001"
                   && archivedWorkspace.previewDialogOpen
        case "panels-resized":
            return activeWorkspace === WorkspaceTabs.Archived && archivedWorkspace.isReady
                   && archivedWorkspace.layout.leftWidth === 330
                   && archivedWorkspace.layout.rightWidth === 290
                   && archivedWorkspace.layout.evidenceHeight === 350
        case "temporary-empty":
            return activeWorkspace === WorkspaceTabs.Temporary
                   && temporaryWorkspace.workspaceState === "empty"
        case "database-unavailable":
            return activeWorkspace === WorkspaceTabs.Temporary
                   && temporaryWorkspace.workspaceState === "database_unavailable"
        case "compare-trend":
            return activeWorkspace === WorkspaceTabs.Compare
                   && compareWorkspace.selectedCount >= 2
                   && compareWorkspace.chartReadyForCapture
        default:
            return false
        }
    }

    function _pollUiScenarioReady() {
        if (!_hasUiScenario || _scenarioApplying) {
            return
        }
        if (_isUiScenarioReady()) {
            scenarioHandshakeState = "ready"
            uiScenarioReady(uiScenario)
        } else {
            scenarioHandshakeState = "waiting"
            scenarioReadyPoll.restart()
        }
    }

    onUiScenarioChanged: {
        // property change handler 觸發時，衍生 readonly binding 尚未必完成更新，直接讀原值。
        if (uiScenario.length > 0) {
            Qt.callLater(_applyUiScenario)
        }
    }

    Timer {
        id: scenarioReadyPoll
        interval: 16
        repeat: false
        onTriggered: root._pollUiScenarioReady()
    }

    header: DqeHeader {
        objectName: "dqeHeader"
        activeWorkspace: root.activeWorkspace
        // 專案／版次標籤屬於內建 sample data；正式資料沒有這兩個欄位，只顯示所選項目的 block。
        contextText: root.activeWorkspace === WorkspaceTabs.Compare
                     ? compareWorkspace.contextText
                     : (root.productionMode
                        ? root._blockContext(root.activeWorkspace === WorkspaceTabs.Temporary
                                             ? temporaryWorkspace.selectedJob
                                             : archivedWorkspace.selectedSource)
                        : (root.activeWorkspace === WorkspaceTabs.Temporary
                           ? "Project-to-V0 / trial / "
                             + (temporaryWorkspace.selectedJob === null
                                ? "top" : String(temporaryWorkspace.selectedJob.block))
                           : (archivedWorkspace.sourceContext.length > 0
                              ? "Project-to-V0 / trial / "
                                + (archivedWorkspace.selectedSource === null
                                   ? "—" : String(archivedWorkspace.selectedSource.block))
                              : "Project-to-V0 / trial / top")))
        connectionState: root.productionMode
                         && ((root.activeWorkspace === WorkspaceTabs.Archived
                              && (archivedWorkspace.workspaceState === "unavailable"
                                  || archivedWorkspace.workspaceState === "error"))
                             || (root.activeWorkspace === WorkspaceTabs.Temporary
                                 && (temporaryWorkspace.workspaceState === "database_unavailable"
                                     || temporaryWorkspace.workspaceState === "unavailable"
                                     || temporaryWorkspace.workspaceState === "error")))
                         ? "Disconnected" : "Connected"
        onWorkspaceRequested: (workspace) => { root.activeWorkspace = workspace; }
        onRefreshRequested: root.refreshCurrentWorkspace()
    }

    footer: DqeStatusBar {
        objectName: "dqeStatusBar"
        sourceText: root.productionMode
                    ? "Terminux daemon"
                    : (root.activeWorkspace === WorkspaceTabs.Compare
                       ? DqeI18n.t("Mock compare source", "Mock 比較來源", "Mock 比较来源")
                       : (root.activeWorkspace === WorkspaceTabs.Temporary
                          ? DqeI18n.t("Mock temporary source", "Mock 暫存來源", "Mock 暂存来源") : DqeI18n.t("Mock archived source", "Mock 已歸檔來源", "Mock 已归档来源")))
        contextText: root.activeWorkspace === WorkspaceTabs.Compare
                     ? compareWorkspace.contextText
                     : (root.activeWorkspace === WorkspaceTabs.Temporary
                        ? temporaryWorkspace.jobContext
                        : (archivedWorkspace.sourceContext.length > 0
                           ? archivedWorkspace.sourceContext : "Archived mock workspace"))
        messageText: root.statusMessage
        mockMode: !root.productionMode
    }

    // Archived composition 擁有唯一 shell；layout key 固定 archived，避免
    // 與後續 Temporary／Compare workspace 的設定混用。
    ArchivedWorkspace {
        id: archivedWorkspace
        objectName: "archivedWorkspace"
        anchors.fill: parent
        visible: root.activeWorkspace === WorkspaceTabs.Archived
        scenarioName: root.archivedScenario
        forceProductionMode: root.productionMode
        repositoryProvider: archivedRepositoryFacadeLoader.item
        onVersionSelected: (sourceId) => {
            root.statusMessage = sourceId === "" ? "Archived version selection cleared"
                                                : (root.productionMode ? "Archived version selected"
                                                                       : "Archived version selected (mock)")
        }
        onItemSelectionChanged: (dqeId) => {
            if (dqeId === "") {
                root.statusMessage = archivedWorkspace.activeFilter === "" ? "No DQE item selected"
                                                                           : "Active filter hides the selected item"
            }
        }
    }

    // Temporary composition 擁有自己的 shell；layout key 固定 temporary，
    // 與 archived 版面設定完全隔離。啟動器注入的 mock job-key 透過 jobKeyToOpen 傳入。
    TemporaryWorkspace {
        id: temporaryWorkspace
        objectName: "temporaryWorkspace"
        anchors.fill: parent
        visible: root.activeWorkspace === WorkspaceTabs.Temporary
        scenarioName: root.temporaryScenario
        jobKeyToOpen: root.jobKey
        forceProductionMode: root.productionMode
        repositoryProvider: temporaryRepositoryFacadeLoader.item
        operationProvider: root.temporaryOperationController
        onJobSelectionChanged: (jobKey) => {
            root.statusMessage = jobKey === "" ? "No temporary job selected"
                                               : (root.productionMode ? "Temporary job selected"
                                                                      : "Temporary job selected (mock)")
        }
        onJobKeyOpenResolved: (jobKey, found) => {
            root.statusMessage = found ? (root.productionMode ? "Opened temporary job"
                                                               : "Opened temporary job (mock)")
                                       : "Job not found"
        }
        onLifecycleActionRequested: (action, jobKey) => {
            root.statusMessage = "Lifecycle " + action + " requested (mock)"
        }
    }

    // Compare composition 擁有自己的 shell；layout key 固定 compare，
    // 與 archived／temporary 版面設定完全隔離。趨勢線段由 NormalizedTrendChart 的 Qt Quick Shapes 繪製。
    CompareWorkspace {
        id: compareWorkspace
        objectName: "compareWorkspace"
        anchors.fill: parent
        visible: root.activeWorkspace === WorkspaceTabs.Compare
        forceProductionMode: root.productionMode
        compareProvider: root.compareSnapshotProvider
        htmlExporter: root.compareHtmlExporter
        onPointSelectionChanged: (dqeId, sourceId) => {
            root.statusMessage = dqeId === "" ? "No trend point selected"
                                              : (root.productionMode ? "Trend point selected"
                                                                     : "Trend point selected (mock)")
        }
        onExportCompleted: (ok, info) => {
            root.statusMessage = ok
                ? (DqeI18n.t("Exported HTML: ", "已匯出 HTML：", "已导出 HTML：") + info)
                : (DqeI18n.t("Export failed: ", "匯出失敗：", "导出失败：") + info)
        }
    }

    // facade 是 QML 可見的唯一 repository surface。它們只包裝 allow-list DTO；
    // transport、endpoint、DB/path 與 private refs 仍停留在 C++ controller/worker。
    Loader {
        id: archivedRepositoryFacadeLoader
        active: root.archivedRepositoryController !== null
        sourceComponent: Component {
            DqeRepositoryFacade { provider: root.archivedRepositoryController }
        }
    }

    Loader {
        id: temporaryRepositoryFacadeLoader
        active: root.temporaryRepositoryController !== null
        sourceComponent: Component {
            DqeRepositoryFacade { provider: root.temporaryRepositoryController }
        }
    }
}
