// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Archived workspace 的 composition owner。
// 所有資料來自 的 deterministic mock repository；共用 review 元件只接收資料與 signal。

import QtQuick
import "../mock" as Mock
import "../navigation" as Navigation
import "../review" as Review
import "../shell" as Shell
import "../states" as States
import "../theme"
import "../i18n"

Item {
    id: workspace
    objectName: "archivedWorkspace"

    property string scenarioName: "archived_normal"
    property string workspaceState: "loading"
    property string activeFilter: ""
    property string selectedSourceId: ""
    property string selectedDqeId: ""
    property string selectedAttachmentId: ""
    property var archivedSources: []
    property var selectedSource: null
    property var summaryCounts: _emptyCounts()
    property var items: []
    property var selectedItem: null
    property var _evidencePanel: null
    // production facade 只由 isolated harness 或已驗證 staging CLI 注入。
    property var repositoryProvider: null
    // 僅有 Temporary/Archived 其中一種 source 的 staging context 仍不可退回 mock。
    property bool forceProductionMode: false
    // 僅在新 production context 首次可用時採用初始 source；refresh 不得悄悄改選其他資料。
    property bool _productionInitialized: false
    readonly property var mockRepository: repositoryLoader.item
    property alias layout: workspaceShell.layout

    readonly property bool isReady: workspaceState === "ready" || workspaceState === "partial"
    readonly property bool productionMode: repositoryProvider !== null || forceProductionMode
    readonly property var selectedAttachments: _attachmentsFor(selectedItem)
    readonly property var selectedAttachment: _attachmentById(selectedAttachmentId)
    readonly property string stateMessage: _stateMessage()
    readonly property string sourceContext: selectedSource === null || selectedSource === undefined
                                            ? ""
                                            : String(selectedSource.displayName)
    readonly property bool previewDialogOpen: _evidencePanel !== null && _evidencePanel.previewDialogOpen

    signal versionSelected(string sourceId)
    signal itemSelectionChanged(string dqeId)
    signal attachmentSelectionChanged(string attachmentId)

    implicitWidth: 1280
    implicitHeight: 720

    // staging mode 不建立 mock repository，避免 production UI 有任何 mock review data fallback。
    Loader {
        id: repositoryLoader
        active: !workspace.productionMode
        sourceComponent: Component {
            Mock.MockDqeRepository { objectName: "archivedMockRepository" }
        }
        onLoaded: workspace._loadScenario()
    }

    function _activeRepository() {
        return repositoryProvider === null ? mockRepository : repositoryProvider
    }

    function _emptyCounts() {
        return { total: 0, pass: 0, warn: 0, fail: 0, unknown: 0 }
    }

    function _attachmentsFor(item) {
        return item !== null && item !== undefined && Array.isArray(item.attachments)
               ? item.attachments : []
    }

    function _attachmentById(attachmentId) {
        const attachments = selectedAttachments
        for (let index = 0; index < attachments.length; ++index) {
            if (attachments[index].attachmentId !== undefined
                    && String(attachments[index].attachmentId) === attachmentId) {
                return attachments[index]
            }
        }
        return null
    }

    function _sourceById(sourceId) {
        for (let index = 0; index < archivedSources.length; ++index) {
            if (String(archivedSources[index].sourceId) === sourceId) {
                return archivedSources[index]
            }
        }
        return null
    }

    function _stateMessage() {
        switch (workspaceState) {
        case "empty":
            return productionMode ? DqeI18n.t("No archived data is available.", "沒有可用的已歸檔資料。", "没有可用的已归档数据。")
                                  : DqeI18n.t("No archived mock data is available.", "沒有可用的已歸檔 mock 資料。", "没有可用的已归档 mock 数据。")
        case "loading":
            return productionMode ? DqeI18n.t("Loading archived data…", "載入已歸檔資料中…", "载入已归档数据中…")
                                  : DqeI18n.t("Loading archived mock data…", "載入已歸檔 mock 資料中…", "载入已归档 mock 数据中…")
        case "source_error":
            return "Archived source error (mock)."
        case "unavailable":
            return DqeI18n.t("Archived source is unavailable.", "已歸檔來源無法使用。", "已归档来源无法使用。")
        case "error":
            return DqeI18n.t("Archived request failed.", "已歸檔請求失敗。", "已归档请求失败。")
        default:
            return ""
        }
    }

    function _clearItemSelection() {
        if (selectedDqeId === "" && selectedItem === null && selectedAttachmentId === "") {
            return
        }
        selectedDqeId = ""
        selectedItem = null
        selectedAttachmentId = ""
        itemSelectionChanged("")
        attachmentSelectionChanged("")
    }

    function _clearSourceSelection(notify) {
        const changed = selectedSourceId !== "" || selectedDqeId !== "" || items.length !== 0
        selectedSourceId = ""
        selectedSource = null
        summaryCounts = _emptyCounts()
        items = []
        activeFilter = ""
        _clearItemSelection()
        if (notify === true && changed) {
            versionSelected("")
        }
    }

    function _selectFirstAttachment() {
        const attachments = selectedAttachments
        selectedAttachmentId = attachments.length > 0 && attachments[0].attachmentId !== undefined
                               ? String(attachments[0].attachmentId) : ""
        attachmentSelectionChanged(selectedAttachmentId)
    }

    function _setSelectedItem(dqeId) {
        const requestedId = dqeId === undefined || dqeId === null ? "" : String(dqeId)
        if (requestedId === selectedDqeId) {
            return
        }
        if (requestedId === "") {
            _clearItemSelection()
            return
        }
        if (!_activeRepository().selectItem(requestedId)) {
            return
        }
        selectedDqeId = requestedId
        selectedItem = _activeRepository().getSelectedItem()
        _selectFirstAttachment()
        itemSelectionChanged(requestedId)
    }

    function selectVersion(sourceId, notify) {
        const requestedId = sourceId === undefined || sourceId === null ? "" : String(sourceId)
        if (requestedId === "") {
            _clearSourceSelection(notify)
            return
        }
        if (requestedId === selectedSourceId) {
            return
        }
        if (!_activeRepository().selectGroup(requestedId)) {
            return
        }
        selectedSourceId = requestedId
        selectedSource = _sourceById(requestedId)
        summaryCounts = _activeRepository().getSummaryCounts()
        items = _activeRepository().getItemList()
        activeFilter = ""

        // version 切換後依 決策選取第一個 mock item；空 group 保持無選取。
        const firstItemId = items.length > 0 && items[0].dqeId !== undefined
                            ? String(items[0].dqeId) : ""
        selectedDqeId = ""
        selectedItem = null
        selectedAttachmentId = ""
        if (firstItemId !== "") {
            _setSelectedItem(firstItemId)
        } else {
            itemSelectionChanged("")
            attachmentSelectionChanged("")
        }
        if (notify === true) {
            versionSelected(requestedId)
        }
    }

    function selectItem(dqeId) {
        _setSelectedItem(dqeId)
    }

    function selectAttachment(attachmentId) {
        const requestedId = attachmentId === undefined || attachmentId === null ? "" : String(attachmentId)
        if (requestedId === selectedAttachmentId || _attachmentById(requestedId) === null) {
            return
        }
        selectedAttachmentId = requestedId
        attachmentSelectionChanged(requestedId)
    }

    function _loadProduction() {
        const active = _activeRepository()
        workspaceState = active.dataState
        // 此處剛更新 workspaceState，直接讀取值以避免 binding 尚未重算時誤判。
        if (workspaceState === "loading") {
            // refresh 期間保持既有 safe rows 與選取，等待目前 generation 結果。
            return
        }
        if (workspaceState !== "ready" && workspaceState !== "partial") {
            // 失敗 read 不可藉由舊資料改寫 selection；也不應清掉可供使用者 retry 的 context。
            return
        }
        archivedSources = active.listArchivedSources()
        if (archivedSources.length === 0) {
            workspaceState = "empty"
            _clearSourceSelection(false)
            _productionInitialized = true
            return
        }

        const retainedSource = _sourceById(selectedSourceId)
        if (retainedSource !== null) {
            selectedSource = retainedSource
            summaryCounts = active.getSummaryCounts()
            items = active.getItemList()
            const activeItem = active.getSelectedItem()
            if (activeItem === null || activeItem === undefined) {
                _clearItemSelection()
            } else {
                selectedItem = activeItem
                selectedDqeId = String(activeItem.dqeId)
                if (_attachmentById(selectedAttachmentId) === null) {
                    selectedAttachmentId = ""
                    attachmentSelectionChanged("")
                }
            }
            _productionInitialized = true
            return
        }
        if (selectedSourceId !== "") {
            // source 被 refresh 移除時，只清除失效 identity，絕不改選第一筆。
            _clearSourceSelection(false)
            _productionInitialized = true
            return
        }
        if (!_productionInitialized) {
            _productionInitialized = true
            selectVersion(String(archivedSources[0].sourceId), false)
        }
    }

    function setStatusFilter(status) {
        activeFilter = status === undefined || status === null ? "" : String(status)
    }

    function openSelectedAttachmentPreview() {
        if (_evidencePanel !== null) {
            _evidencePanel.openSelectedAttachmentPreview()
        }
    }

    function _loadScenario() {
        if (productionMode) {
            if (repositoryProvider === null) {
                workspaceState = "unavailable"
                archivedSources = []
                _clearSourceSelection(false)
                return
            }
            _loadProduction()
            return
        }
        const mock = mockRepository
        if (mock === null || !mock.switchScenario(scenarioName)) {
            return
        }
        activeFilter = ""
        if (mock.dataState === "database_unavailable") {
            workspaceState = "source_error"
        } else {
            workspaceState = mock.dataState
        }

        if (!isReady) {
            archivedSources = []
            _clearSourceSelection(false)
            return
        }

        archivedSources = mock.listArchivedSources()
        if (archivedSources.length === 0) {
            workspaceState = "empty"
            _clearSourceSelection(false)
            return
        }
        selectedSourceId = ""
        selectVersion(String(archivedSources[0].sourceId), false)
    }

    onScenarioNameChanged: _loadScenario()
    onRepositoryProviderChanged: {
        if (productionMode && repositoryProvider !== null) {
            _productionInitialized = false
            _loadProduction()
        }
    }
    Component.onCompleted: _loadScenario()

    Connections {
        target: workspace.repositoryProvider
        function onRepositoryStateChanged() { workspace._loadProduction() }
        function onSourcesChanged() { workspace._loadProduction() }
    }

    Shell.DqeWorkspaceShell {
        id: workspaceShell
        objectName: "workspaceShell"
        anchors.fill: parent
        workspaceKey: "archived"
        navigationTitle: DqeI18n.t("ARCHIVED VERSIONS", "已歸檔版本", "已归档版本")
        centerTitle: DqeI18n.t("QUALITY REVIEW", "品質審查", "质量审查")
        inspectorTitle: DqeI18n.t("SELECTED ITEM INSPECTOR", "選取項目檢視", "选取项目查看")
        evidenceTitle: DqeI18n.t("EVIDENCE / ATTACHMENT PREVIEW", "證據 / 附件預覽", "证据 / 附件预览")

        navigationContent: Component {
            Navigation.ArchivedVersionNavigator {
                sources: workspace.isReady ? workspace.archivedSources : []
                selectedSourceId: workspace.selectedSourceId
                currentWorkspace: "Archived"
                productionSource: workspace.productionMode
                onSourceSelected: (sourceId) => { workspace.selectVersion(sourceId, true) }
            }
        }

        centerContent: Component {
            Item {
                id: centerContent
                objectName: "archivedCenterContent"

                Review.DqeSummaryCards {
                    id: summaryCards
                    objectName: "archivedSummaryCards"
                    visible: workspace.isReady
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: DqeTheme.spacingM
                    anchors.rightMargin: DqeTheme.spacingM
                    anchors.top: parent.top
                    anchors.topMargin: DqeTheme.spacingM
                    height: 80
                    totalCount: workspace.summaryCounts.total
                    passCount: workspace.summaryCounts.pass
                    warnCount: workspace.summaryCounts.warn
                    failCount: workspace.summaryCounts.fail
                    unknownCount: workspace.summaryCounts.unknown
                    activeFilter: workspace.activeFilter
                    onStatusFilterRequested: (status) => { workspace.setStatusFilter(status) }
                }

                Review.DqeItemTable {
                    objectName: "archivedDqeItemTable"
                    visible: workspace.isReady
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: DqeTheme.spacingM
                    anchors.rightMargin: DqeTheme.spacingM
                    anchors.top: summaryCards.bottom
                    anchors.topMargin: DqeTheme.spacingM
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: DqeTheme.spacingM
                    model: workspace.items
                    statusFilter: workspace.activeFilter
                    selectedDqeId: workspace.selectedDqeId
                    onItemSelected: (dqeId) => { workspace.selectItem(dqeId) }
                }

                Item {
                    objectName: "archivedWorkspaceState"
                    anchors.fill: parent
                    visible: !workspace.isReady

                    States.WorkspaceState {
                        objectName: "archivedCanonicalWorkspaceState"
                        anchors.fill: parent
                        state: workspace.workspaceState === "source_error"
                               || workspace.workspaceState === "unavailable"
                               || workspace.workspaceState === "error" ? "error"
                                                                   : workspace.workspaceState
                        message: workspace.stateMessage
                    }
                    // 保留 已核准的可尋址 state probes；視覺由 canonical component 統一。
                    Item { objectName: "archivedEmptyState"; visible: workspace.workspaceState === "empty" }
                    Item { objectName: "archivedLoadingState"; visible: workspace.workspaceState === "loading" }
                    Item { objectName: "archivedSourceErrorState"; visible: workspace.workspaceState === "source_error" }
                }
            }
        }

        inspectorContent: Component {
            Review.DqeItemInspector {
                objectName: "archivedDqeItemInspector"
                item: workspace.selectedItem
                sourceKind: workspace.selectedSource === null ? "" : "archived"
                sourceLabel: workspace.sourceContext
            }
        }

        evidenceContent: Component {
            Review.EvidencePanel {
                id: evidencePanel
                objectName: "archivedEvidencePanel"
                attachments: workspace.selectedAttachments
                selectedAttachmentId: workspace.selectedAttachmentId
                previewKind: workspace.selectedAttachment === null
                             ? "" : String(workspace.selectedAttachment.previewKind)
                previewPayload: workspace.selectedAttachment === null
                                ? null : workspace.selectedAttachment.mockPayload
                productionSource: workspace.repositoryProvider !== null
                previewProvider: workspace.repositoryProvider
                Component.onCompleted: workspace._evidencePanel = evidencePanel
                onAttachmentSelected: (attachmentId) => { workspace.selectAttachment(attachmentId) }
            }
        }
    }
}
