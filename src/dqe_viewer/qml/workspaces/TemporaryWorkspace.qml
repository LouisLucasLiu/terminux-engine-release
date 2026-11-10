// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Temporary workspace 的 composition owner。
// 一般啟動延續 deterministic mock；DQE Viewer production binding injected production provider 會停用所有 mock
// repository/lifecycle instance，僅顯示 strict read-only 的 safe DTO 與 metadata。

import QtQuick
import QtQuick.Controls
import "../mock" as Mock
import "../navigation" as Navigation
import "../review" as Review
import "../shell" as Shell
import "../states" as States
import "../theme"
import "../i18n"

Item {
    id: workspace
    objectName: "temporaryWorkspace"

    property string scenarioName: "temporary_normal"
    property string workspaceState: "loading"
    property string activeFilter: ""
    property string selectedJobKey: ""
    property string selectedDqeId: ""
    property string selectedAttachmentId: ""
    // C++ 啟動器注入的 mock job-key；有值時嘗試選取對應 job（launch context）。
    property string jobKeyToOpen: ""
    property bool jobNotFound: false
    property string notFoundJobKey: ""
    property int retentionDays: 14
    property string lastLifecycleAction: ""
    property var jobs: []
    property var summaryCounts: _emptyCounts()
    property var items: []
    property var selectedItem: null
    property var _evidencePanel: null
    // 只有 isolated harness/staging context 注入 production provider；一般啟動維持 mock。
    property var repositoryProvider: null
    // staging 只有 Archived source 時，Temporary tab 仍必須顯示安全 unavailable，而非 mock data。
    property bool forceProductionMode: false
    // isolated/staging injection only；ordinary launch 仍沒有 production operation context。
    property var operationProvider: null
    property string _loadedProductionSourceId: ""
    // 僅在新 production context 首次可用時選取預設 job；refresh 不得改選其他 job。
    property bool _productionInitialized: false
    readonly property var mockRepository: repositoryLoader.item
    readonly property var lifecycleData: lifecycleLoader.item
    property alias layout: workspaceShell.layout

    // purge 只在 mock 記憶體中移除 job；_loadScenario／重啟後復原。
    property var _purgedJobs: []

    readonly property bool productionMode: repositoryProvider !== null || forceProductionMode
    readonly property bool operationRunning: productionMode && operationProvider !== null
                                           && operationProvider.running
    readonly property bool isReady: workspaceState === "ready" || workspaceState === "partial"
    readonly property bool jobsAvailable: productionMode ? isReady
                                                         : (_config().jobsAvailable && workspaceState !== "empty"
                                                            && workspaceState !== "loading")
    readonly property var selectedAttachments: _attachmentsFor(selectedItem)
    readonly property var selectedAttachment: _attachmentById(selectedAttachmentId)
    readonly property var selectedJob: _jobByKey(selectedJobKey)
    readonly property string stateMessage: _stateMessage()
    readonly property string jobContext: selectedJobKey.length > 0
                                         ? DqeI18n.t("Temporary / ", "暫存 / ", "暂存 / ") + selectedJobKey
                                         : (productionMode ? DqeI18n.t("Temporary workspace", "暫存工作區", "暂存工作区")
                                                           : DqeI18n.t("Temporary mock workspace", "暫存 mock 工作區", "暂存 mock 工作区"))
    readonly property bool previewDialogOpen: _evidencePanel !== null && _evidencePanel.previewDialogOpen

    signal jobSelectionChanged(string jobKey)
    signal itemSelectionChanged(string dqeId)
    signal attachmentSelectionChanged(string attachmentId)
    signal jobKeyOpenResolved(string jobKey, bool found)
    signal lifecycleActionRequested(string action, string jobKey)

    implicitWidth: 1280
    implicitHeight: 720

    // production mode 不建立 mock review/lifecycle data，避免任何意外 fallback。
    Loader {
        id: repositoryLoader
        active: !workspace.productionMode
        sourceComponent: Component {
            Mock.MockDqeRepository { objectName: "temporaryMockRepository" }
        }
    }

    Loader {
        id: lifecycleLoader
        active: !workspace.productionMode
        sourceComponent: Component {
            Mock.MockTemporaryLifecycle { objectName: "temporaryLifecycleData" }
        }
    }

    // terminal success/partial 才 refresh affected opaque source；不改目前 selection。
    Connections {
        target: workspace.operationProvider
        function onRefreshRequested(sourceId) {
            if (workspace.productionMode && workspace.repositoryProvider !== null) {
                workspace.repositoryProvider.loadTemporaryJob(String(sourceId))
            }
        }
    }

    function _activeRepository() {
        return productionMode ? repositoryProvider : mockRepository
    }

    // scenario → macro state／review 來源設定的單一對照表。
    readonly property var _scenarioConfig: ({
        "temporary_normal": { macro: "ready", repo: "temporary_normal",
            reviewSource: "temporary-mock-job-001", autoItem: "", jobsAvailable: true },
        "temporary_empty": { macro: "empty", repo: "temporary_empty",
            reviewSource: "temporary-mock-job-empty", autoItem: "", jobsAvailable: false },
        "temporary_loading": { macro: "loading", repo: "temporary_loading",
            reviewSource: "", autoItem: "", jobsAvailable: false },
        "database_unavailable": { macro: "database_unavailable", repo: "database_unavailable",
            reviewSource: "", autoItem: "", jobsAvailable: true },
        "unsupported_attachment": { macro: "ready", repo: "unsupported_attachment",
            reviewSource: "archived-v3", autoItem: "MOCK_ATTACHMENT_UNSUPPORTED", jobsAvailable: true }
    })

    function _config() {
        const config = _scenarioConfig[scenarioName]
        return config !== undefined ? config
               : { macro: "loading", repo: "temporary_loading", reviewSource: "", autoItem: "", jobsAvailable: false }
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

    function _jobByKey(jobKey) {
        for (let index = 0; index < jobs.length; ++index) {
            if (String(jobs[index].jobKey) === jobKey) {
                return jobs[index]
            }
        }
        return null
    }

    function _isPurged(jobKey) {
        return _purgedJobs.indexOf(jobKey) !== -1
    }

    // 依 execution time 新到舊；tie-break 用 jobKey 反向，確保決定性（與 navigator 一致）。
    function _sortJobs(list) {
        const copy = list.slice()
        copy.sort(function (a, b) {
            const ta = a.executionTime === undefined ? "" : String(a.executionTime)
            const tb = b.executionTime === undefined ? "" : String(b.executionTime)
            if (ta < tb) { return 1 }
            if (ta > tb) { return -1 }
            const ka = String(a.jobKey)
            const kb = String(b.jobKey)
            if (ka < kb) { return 1 }
            if (ka > kb) { return -1 }
            return 0
        })
        return copy
    }

    function _rebuildJobs() {
        const config = _config()
        if (!config.jobsAvailable) {
            jobs = []
            return
        }
        if (lifecycleData === null) {
            jobs = []
            return
        }
        const catalog = lifecycleData.jobList()
        const kept = []
        for (let index = 0; index < catalog.length; ++index) {
            if (!_isPurged(String(catalog[index].jobKey))) {
                kept.push(catalog[index])
            }
        }
        jobs = _sortJobs(kept)
    }

    function _stateMessage() {
        switch (workspaceState) {
        case "empty":
            return DqeI18n.t("No temporary DQE results", "沒有暫存 DQE 結果", "没有暂存 DQE 结果")
        case "loading":
            return productionMode ? DqeI18n.t("Loading temporary data…", "載入暫存資料中…", "载入暂存数据中…") : DqeI18n.t("Loading temporary mock data…", "載入暫存 mock 資料中…", "载入暂存 mock 数据中…")
        case "database_unavailable":
            return DqeI18n.t("DQE database unavailable", "DQE 資料庫無法使用", "DQE 数据库无法使用")
        case "unavailable":
            return DqeI18n.t("DQE database unavailable", "DQE 資料庫無法使用", "DQE 数据库无法使用")
        case "error":
            return DqeI18n.t("Temporary repository request failed.", "暫存 repository 請求失敗。", "暂存 repository 请求失败。")
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

    function _loadProduction() {
        if (repositoryProvider === null) {
            workspaceState = "unavailable"
            jobs = []
            _loadedProductionSourceId = ""
            _clearItemSelection()
            return
        }
        const active = _activeRepository()
        const nextState = active.dataState
        workspaceState = nextState
        if (nextState === "loading") {
            // refresh 中保留既有選取與 retention context，等待 current token 完成。
            return
        }
        if (!isReady) {
            if (nextState === "empty") {
                jobs = []
                selectedJobKey = ""
                _loadedProductionSourceId = ""
                _clearItemSelection()
                _productionInitialized = true
            }
            return
        }

        const previousKey = selectedJobKey
        jobs = _sortJobs(active.listTemporaryJobs())
        if (jobs.length === 0) {
            workspaceState = "empty"
            selectedJobKey = ""
            _loadedProductionSourceId = ""
            _clearItemSelection()
            _productionInitialized = true
            return
        }
        // 同一 production context 的 retention 不可因 job reload 改回 mock default。
        retentionDays = Number(jobs[0].retentionDays) || retentionDays
        if (jobKeyToOpen.length > 0) {
            _productionInitialized = true
            openJobKey(jobKeyToOpen)
            return
        }
        if (_jobByKey(previousKey) !== null) {
            selectJob(previousKey, false, true)
            _productionInitialized = true
        } else if (previousKey !== "") {
            // refresh 移除原 job 時清 selection；絕不悄悄選取最新另一筆。
            selectedJobKey = ""
            _loadedProductionSourceId = ""
            _clearItemSelection()
            _productionInitialized = true
        } else if (!_productionInitialized) {
            _productionInitialized = true
            selectJob(String(jobs[0].jobKey), false)
        }
    }

    // 載入目前 scenario 的 review fixture；非 ready（loading／error）時清空 review。
    function _loadReview() {
        const config = _config()
        const mock = mockRepository
        summaryCounts = _emptyCounts()
        items = []
        selectedDqeId = ""
        selectedItem = null
        selectedAttachmentId = ""
        if (!isReady || config.reviewSource === "") {
            itemSelectionChanged("")
            attachmentSelectionChanged("")
            return
        }
        if (mock === null || !mock.selectGroup(config.reviewSource)) {
            itemSelectionChanged("")
            attachmentSelectionChanged("")
            return
        }
        summaryCounts = mock.getSummaryCounts()
        items = mock.getItemList()
        activeFilter = ""
        let targetId = ""
        if (config.autoItem !== "") {
            targetId = config.autoItem
        } else if (items.length > 0 && items[0].dqeId !== undefined) {
            targetId = String(items[0].dqeId)
        }
        if (targetId !== "") {
            _setSelectedItem(targetId)
        } else {
            itemSelectionChanged("")
            attachmentSelectionChanged("")
        }
    }

    function selectJob(jobKey, notify, preserveItemSelection) {
        const requestedKey = jobKey === undefined || jobKey === null ? "" : String(jobKey)
        // dry-run/actual 尚未 terminal 前不得變更 target job。
        if (operationRunning) {
            return
        }
        if (requestedKey === "") {
            if (selectedJobKey === "") {
                return
            }
            selectedJobKey = ""
            _clearItemSelection()
            if (notify === true) {
                jobSelectionChanged("")
            }
            return
        }
        if (requestedKey === selectedJobKey && !productionMode) {
            return
        }
        if (_jobByKey(requestedKey) === null) {
            return
        }
        selectedJobKey = requestedKey
        if (productionMode) {
            const active = _activeRepository()
            const selectedJob = _jobByKey(requestedKey)
            if (!active.selectTemporaryJob(String(selectedJob.sourceId))) {
                selectedJobKey = ""
                _clearItemSelection()
                return
            }
            summaryCounts = active.getSummaryCounts()
            items = active.getItemList()
            if (preserveItemSelection === true) {
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
            } else {
                activeFilter = ""
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
            }
            if (_loadedProductionSourceId !== String(selectedJob.sourceId)) {
                _loadedProductionSourceId = String(selectedJob.sourceId)
                active.loadTemporaryJob(_loadedProductionSourceId)
            }
        } else {
            // 選取 job 即載入該 job 的 review fixture（單一 mock fixture，idempotent）。
            _loadReview()
        }
        if (notify === true) {
            jobSelectionChanged(requestedKey)
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

    function setStatusFilter(status) {
        activeFilter = status === undefined || status === null ? "" : String(status)
    }

    function openSelectedAttachmentPreview() {
        if (_evidencePanel !== null) {
            _evidencePanel.openSelectedAttachmentPreview()
        }
    }

    // launch context。找到→切至該 job；找不到→停在 Temporary、清選取、
    // 顯示非阻擋的 Job not found 通知（絕不視為 database error）。
    function openJobKey(jobKey) {
        const requestedKey = jobKey === undefined || jobKey === null ? "" : String(jobKey)
        if (requestedKey === "") {
            jobNotFound = false
            notFoundJobKey = ""
            return
        }
        if (_jobByKey(requestedKey) !== null) {
            jobNotFound = false
            notFoundJobKey = ""
            selectJob(requestedKey, true)
            jobKeyOpenResolved(requestedKey, true)
            return
        }
        // 找不到：清選取、標記非阻擋通知，狀態不變（不是 error state）。
        selectJob("", true)
        jobNotFound = true
        notFoundJobKey = requestedKey
        jobKeyOpenResolved(requestedKey, false)
    }

    // ── mock lifecycle handlers：只更新記憶體顯示與發 signal，無任何後端呼叫 ──
    function onCheckinRequested(jobKey) {
        lastLifecycleAction = DqeI18n.t("Check-in requested (mock): ", "已請求簽入（mock）：", "已请求签入（mock）：") + jobKey
        lifecycleActionRequested("checkin", jobKey)
    }

    function onRetentionEditRequested(days) {
        // 只改「顯示用」的 mock retention；不持久化、重啟後復原。
        retentionDays = Number(days) || retentionDays
        lastLifecycleAction = DqeI18n.t("Retention set (mock): ", "已設定保留（mock）：", "已设定保留（mock）：") + retentionDays + " " + DqeI18n.t("days", "天", "天")
        lifecycleActionRequested("retention", selectedJobKey)
    }

    function onPurgeRequested(jobKey) {
        const key = String(jobKey)
        if (key !== "" && _purgedJobs.indexOf(key) === -1) {
            const next = _purgedJobs.slice()
            next.push(key)
            _purgedJobs = next
        }
        _rebuildJobs()
        // 若選取中的 job 被 purge，改選最新剩餘 job（沒有則清空）。
        if (selectedJobKey === key) {
            selectedJobKey = ""
            if (isReady && jobs.length > 0) {
                selectJob(String(jobs[0].jobKey), true)
            } else {
                _clearItemSelection()
                jobSelectionChanged("")
            }
        }
        lastLifecycleAction = DqeI18n.t("Purged (mock): ", "已清除（mock）：", "已清除（mock）：") + key
        lifecycleActionRequested("purge", key)
    }

    function _loadScenario() {
        if (productionMode) {
            _loadProduction()
            return
        }
        const config = _config()
        const mock = mockRepository
        if (mock === null || !mock.switchScenario(config.repo)) {
            return
        }
        // 重置所有 mock lifecycle 顯示狀態（等同「重啟後不保留」）。
        _purgedJobs = []
        retentionDays = lifecycleData === null ? retentionDays : lifecycleData.defaultRetentionDays
        lastLifecycleAction = ""
        jobNotFound = false
        notFoundJobKey = ""
        activeFilter = ""
        workspaceState = config.macro

        _rebuildJobs()

        selectedJobKey = ""
        _clearItemSelection()

        // 有 job 時預設選取最新可用 job（execution time 最新者）。
        if (jobsAvailable && jobs.length > 0) {
            selectJob(String(jobs[0].jobKey), false)
        } else {
            _loadReview()
        }

        // launch context 優先於預設選取。
        if (jobKeyToOpen.length > 0) {
            openJobKey(jobKeyToOpen)
        }
    }

    onScenarioNameChanged: _loadScenario()
    onRepositoryProviderChanged: {
        if (productionMode && repositoryProvider !== null) {
            _productionInitialized = false
            _loadProduction()
        }
    }
    onJobKeyToOpenChanged: {
        if (jobKeyToOpen.length > 0) {
            openJobKey(jobKeyToOpen)
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
        workspaceKey: "temporary"
        navigationTitle: DqeI18n.t("TEMPORARY JOBS", "暫存 Job", "暂存 Job")
        centerTitle: DqeI18n.t("TEMPORARY DQE", "暫存 DQE", "暂存 DQE")
        inspectorTitle: DqeI18n.t("SELECTED ITEM INSPECTOR", "選取項目檢視", "选取项目查看")
        evidenceTitle: DqeI18n.t("EVIDENCE / ATTACHMENT PREVIEW", "證據 / 附件預覽", "证据 / 附件预览")

        navigationContent: Component {
            Item {
                id: navigationRoot
                objectName: "temporaryNavigationContent"

                Navigation.TemporaryJobNavigator {
                    id: jobNavigator
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.bottom: lifecycleBar.visible ? lifecycleBar.top : parent.bottom
                    anchors.bottomMargin: lifecycleBar.visible ? DqeTheme.spacingS : 0
                    jobs: workspace.jobs
                    selectedJobKey: workspace.selectedJobKey
                    retentionDays: workspace.retentionDays
                    currentWorkspace: "Temporary"
                    productionSource: workspace.productionMode
                    onJobSelected: (jobKey) => { workspace.selectJob(jobKey, true) }
                }

                Review.LifecycleActionBar {
                    id: lifecycleBar
                    objectName: "temporaryLifecycleActionBar"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.leftMargin: DqeTheme.spacingS
                    anchors.rightMargin: DqeTheme.spacingS
                    anchors.bottomMargin: DqeTheme.spacingS
                    // retention 是全域 policy；production 即使無 job 也必須能讀取／修改它。
                    visible: workspace.productionMode ? workspace.operationProvider !== null
                                                      : (workspace.jobsAvailable && workspace.jobs.length > 0)
                    jobKey: workspace.selectedJobKey
                    retentionDays: workspace.retentionDays
                    productionSource: workspace.productionMode
                    operationProvider: workspace.productionMode ? workspace.operationProvider : null
                    checkinEligible: workspace.productionMode && workspace.selectedJob !== null
                                      && workspace.selectedJob.canCheckin === true
                    checkinVersions: !workspace.productionMode && workspace.lifecycleData !== null
                                     ? workspace.lifecycleData.versionList() : []
                    onCheckinRequested: (jobKey) => { workspace.onCheckinRequested(jobKey) }
                    onPurgeRequested: (jobKey) => { workspace.onPurgeRequested(jobKey) }
                    onRetentionEditRequested: { workspace.onRetentionEditRequested(requestedRetentionDays) }
                }
            }
        }

        centerContent: Component {
            Item {
                id: centerContent
                objectName: "temporaryCenterContent"

                Text {
                    objectName: "temporaryJobCount"
                    anchors.right: parent.right
                    anchors.rightMargin: DqeTheme.spacingM
                    anchors.top: parent.top
                    anchors.topMargin: DqeTheme.spacingS
                    text: workspace.workspaceState === "database_unavailable"
                          || workspace.workspaceState === "unavailable" ? DqeI18n.t("Connection failed", "連線失敗", "连接失败")
                          : workspace.jobs.length + " " + DqeI18n.t("jobs", "個 job", "个 job")
                    color: workspace.workspaceState === "database_unavailable"
                           || workspace.workspaceState === "unavailable" ? DqeTheme.fail
                                                                              : DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                }

                Text {
                    objectName: "temporaryJobNotFound"
                    anchors.left: parent.left
                    anchors.leftMargin: DqeTheme.spacingM
                    anchors.top: parent.top
                    anchors.topMargin: DqeTheme.spacingS
                    visible: workspace.jobNotFound
                    text: DqeI18n.t("Job not found: ", "找不到 Job：", "找不到 Job：") + workspace.notFoundJobKey
                          + (workspace.productionMode ? "" : " (mock)")
                    color: DqeTheme.warn
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                }

                Review.DqeSummaryCards {
                    id: summaryCards
                    objectName: "temporarySummaryCards"
                    visible: workspace.isReady
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: DqeTheme.spacingM
                    anchors.rightMargin: DqeTheme.spacingM
                    anchors.top: parent.top
                    anchors.topMargin: DqeTheme.spacingL + DqeTheme.spacingS
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
                    objectName: "temporaryDqeItemTable"
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
                    objectName: "temporaryWorkspaceState"
                    anchors.fill: parent
                    visible: !workspace.isReady

                    Column {
                        anchors.centerIn: parent
                        spacing: DqeTheme.spacingS
                        width: parent.width

                        States.WorkspaceState {
                            objectName: "temporaryCanonicalWorkspaceState"
                            width: parent.width
                            state: workspace.workspaceState === "unavailable" ? "error"
                                  : workspace.workspaceState
                            message: workspace.stateMessage
                            detail: workspace.workspaceState === "empty"
                                    ? DqeI18n.t("New jobs will appear here after package extraction", "package 抽取後，新 job 會顯示在此", "package 抽取后，新 job 会显示在此")
                                    : ((workspace.workspaceState === "database_unavailable"
                                        || workspace.workspaceState === "unavailable")
                                       ? (workspace.productionMode
                                          ? "The temporary database could not be opened read-only"
                                          : "The mock review database could not be opened") : "")
                        }
                        // 保留 已核准的可尋址 state probes；視覺由 canonical component 統一。
                        Item { objectName: "temporaryEmptyState"; visible: workspace.workspaceState === "empty" }
                        Item { objectName: "temporaryLoadingState"; visible: workspace.workspaceState === "loading" }
                        Item { objectName: "temporaryDatabaseUnavailableState"; visible: workspace.workspaceState === "database_unavailable" }
                        Item {
                            width: parent.width
                            height: retryButton.height
                            visible: !workspace.productionMode
                                     && workspace.workspaceState === "database_unavailable"
                            AbstractButton {
                                id: retryButton
                                objectName: "temporaryRetryButton"
                                anchors.horizontalCenter: parent.horizontalCenter
                                width: 120
                                height: 32
                                focusPolicy: Qt.StrongFocus
                                onClicked: workspace._loadScenario()
                                background: Rectangle {
                                    radius: DqeTheme.radiusSmall
                                    color: retryButton.hovered ? DqeTheme.panelRaised : "transparent"
                                    border.width: retryButton.activeFocus ? DqeTheme.borderWidthStrong
                                                                          : DqeTheme.borderWidth
                                    border.color: DqeTheme.accent
                                }
                                contentItem: Text {
                                    text: DqeI18n.t("Retry (mock)", "重試（mock）", "重试（mock）")
                                    color: DqeTheme.accent
                                    font.family: DqeTheme.fontFamily
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                    }
                }
            }
        }

        inspectorContent: Component {
            Review.DqeItemInspector {
                objectName: "temporaryDqeItemInspector"
                item: workspace.selectedItem
                sourceKind: workspace.selectedItem === null ? "" : "temporary"
                sourceLabel: workspace.jobContext
            }
        }

        evidenceContent: Component {
            Review.EvidencePanel {
                id: evidencePanel
                objectName: "temporaryEvidencePanel"
                attachments: workspace.selectedAttachments
                selectedAttachmentId: workspace.selectedAttachmentId
                previewKind: workspace.selectedAttachment === null
                             ? "" : String(workspace.selectedAttachment.previewKind)
                previewPayload: workspace.selectedAttachment === null
                                ? null : workspace.selectedAttachment.mockPayload
                productionSource: workspace.productionMode
                previewProvider: workspace.productionMode ? workspace.repositoryProvider : null
                Component.onCompleted: workspace._evidencePanel = evidencePanel
                onAttachmentSelected: (attachmentId) => { workspace.selectAttachment(attachmentId) }
            }
        }
    }
}
