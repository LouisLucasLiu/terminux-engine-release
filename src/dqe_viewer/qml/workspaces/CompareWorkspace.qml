// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Compare workspace 的 composition owner。
// 使用 CompareViewModel 產生正規化輸出，組合 source selector、trend chart、
// raw table、point inspector 與 Evidence，全部在單一 shell（layout key compare）。
// 圖表繪製器型別只存在於 NormalizedTrendChart（renderer boundary），本檔不 import 任何 chart 型別。

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import "../i18n"
import "../compare" as Compare
import "../mock" as Mock
import "../navigation" as Navigation
import "../review" as Review
import "../shell" as Shell
import "../theme"

Item {
    id: workspace
    objectName: "compareWorkspace"

    // ── 選取狀態 ──
    property var selectedSourceIds: []   // 使用者選取順序
    property var dqeSelectedIds: []      // 已選取的 DQE_ID
    property string blockFilter: "top"
    property string selectedDqeId: ""
    property string selectedSourceId: ""
    property string selectedAttachmentId: ""
    property bool chartReadyForCapture: false
    // test/staging 才注入 production snapshot aggregator；一般啟動維持 mock catalog。
    property var compareProvider: null
    property bool forceProductionMode: false
    // Compare 結果 HTML 匯出的 C++ helper（由 app 注入；QML 無法自行寫檔）。
    property var htmlExporter: null
    // production 預設「不」自動選取任何 job：使用者自行挑選要比較的 jobs，再從 DQE 交集
    // checkbox group 勾選要作圖的 DQE。test/staging harness 可設為 true 保留舊的「全選」行為。
    property bool autoSelectAllOnLoad: false
    // production：source snapshots 由 IPC 非同步抵達，_resetForTest 首次執行時 catalog 可能為空。
    // 這個旗標記錄「已用實際資料 seed 過預設選取」（僅 autoSelectAllOnLoad 時有意義），
    // 避免資料抵達後不補選或覆寫使用者後續的手動取消選取。
    property bool _productionSeeded: false

    property alias layout: workspaceShell.layout

    signal pointSelectionChanged(string dqeId, string sourceId)
    // HTML 匯出結果：ok=true 時 info=檔案路徑；ok=false 時 info=錯誤訊息。
    signal exportCompleted(bool ok, string info)

    // ── 衍生資料 ──
    readonly property var orderedSelectedSources: _orderedSelectedSources()
    readonly property var selectedInputSeries: _selectedInputSeries()
    readonly property var chartSeries: _chartSeries()
    readonly property var yRange: _yRange()
    readonly property int selectedCount: Array.isArray(selectedSourceIds) ? selectedSourceIds.length : 0
    readonly property var selectedDetail: _selectedDetail()
    readonly property var selectedAttachments: _selectedAttachments()
    readonly property var selectedAttachment: _attachmentById(selectedAttachmentId)
    readonly property bool productionSource: compareProvider !== null || forceProductionMode
    readonly property var _sourceCatalog: productionSource
                                       ? (compareProvider === null ? [] : compareProvider.sources)
                                                          : (catalog === null ? [] : catalog.sources)
    readonly property var _seriesCatalog: productionSource
                                       ? (compareProvider === null ? [] : compareProvider.series)
                                                          : (catalog === null ? [] : catalog.seriesCatalog)
    // 比較來源一律是 Temporary job 與 Archived version；正式資料與 sample data 用同一個標籤。
    readonly property string contextText: "Temporary + Archived / block: " + (blockFilter.length > 0 ? blockFilter : "—")

    implicitWidth: 1280
    implicitHeight: 720

    Loader {
        id: catalogLoader
        active: !workspace.productionSource
        sourceComponent: Component { Mock.MockCompareCatalog { objectName: "compareCatalog" } }
        onLoaded: workspace._resetForTest()
    }
    readonly property var catalog: catalogLoader.item

    Compare.CompareViewModel {
        id: compareModel
        objectName: "compareViewModel"
        sources: workspace.orderedSelectedSources
        inputSeries: workspace.selectedInputSeries
        blockFilter: workspace.blockFilter
        selectedDqeId: workspace.selectedDqeId
        selectedSourceId: workspace.selectedSourceId
    }

    function _sourceById(sourceId) {
        const all = workspace._sourceCatalog
        for (let i = 0; i < all.length; ++i) {
            if (String(all[i].sourceId) === sourceId) {
                return all[i]
            }
        }
        return null
    }

    function _sourcesByKind(kind) {
        const all = workspace._sourceCatalog
        const list = []
        for (let i = 0; i < all.length; ++i) {
            if (String(all[i].sourceKind) === kind) {
                list.push(all[i])
            }
        }
        return list
    }

    function _seriesById(dqeId) {
        const all = workspace._seriesCatalog
        for (let i = 0; i < all.length; ++i) {
            if (String(all[i].dqeId) === dqeId) {
                return all[i]
            }
        }
        return null
    }

    function _orderedSelectedSources() {
        const ids = Array.isArray(selectedSourceIds) ? selectedSourceIds : []
        const list = []
        for (let i = 0; i < ids.length; ++i) {
            const source = _sourceById(String(ids[i]))
            if (source !== null) {
                list.push(source)
            }
        }
        return list
    }

    // C++ QVariantList 在 QML 可能是 sequence wrapper 而非 JavaScript Array。
    // Normalization 的契約明確要求 Array；此處只做 shape bridge，絕不變更 raw 值／unit／status。
    function _arrayValues(value) {
        const list = []
        if (value === null || value === undefined || value.length === undefined) {
            return list
        }
        for (let index = 0; index < Number(value.length); ++index) {
            list.push(value[index])
        }
        return list
    }

    function _selectedInputSeries() {
        const selected = Array.isArray(dqeSelectedIds) ? dqeSelectedIds : []
        const all = workspace._seriesCatalog
        const list = []
        for (let i = 0; i < all.length; ++i) {
            if (selected.indexOf(String(all[i].dqeId)) !== -1) {
                if (!productionSource) {
                    list.push(all[i])
                    continue
                }
                const upstream = all[i]
                const points = []
                const upstreamPoints = _arrayValues(upstream.points)
                for (let pointIndex = 0; pointIndex < upstreamPoints.length; ++pointIndex) {
                    const point = upstreamPoints[pointIndex]
                    points.push({
                        sourceId: point.sourceId,
                        value: point.value,
                        unit: point.unit,
                        status: point.status,
                        statusRaw: point.statusRaw,
                        comparisonIssue: point.comparisonIssue,
                        duplicateRows: _arrayValues(point.duplicateRows)
                    })
                }
                list.push({
                    dqeId: upstream.dqeId,
                    title: upstream.title,
                    unit: upstream.unit,
                    block: upstream.block,
                    points: points
                })
            }
        }
        return list
    }

    // 把 model 的 series 加上穩定線色供 chart／legend 使用。
    function _chartSeries() {
        const modelSeries = Array.isArray(compareModel.series) ? compareModel.series : []
        const list = []
        for (let i = 0; i < modelSeries.length; ++i) {
            const entry = modelSeries[i]
            list.push({
                dqeId: entry.dqeId,
                title: entry.title,
                unit: entry.unit,
                issue: entry.issue,
                points: entry.points,
                color: workspace._colorFor(String(entry.dqeId))
            })
        }
        return list
    }

    function _yRange() {
        let minValue = 100
        let maxValue = 100
        const all = chartSeries
        for (let i = 0; i < all.length; ++i) {
            if (all[i].issue !== null && all[i].issue !== undefined) {
                continue
            }
            const points = Array.isArray(all[i].points) ? all[i].points : []
            for (let j = 0; j < points.length; ++j) {
                const nv = points[j].normalizedValue
                if (nv !== null && nv !== undefined) {
                    minValue = Math.min(minValue, Number(nv))
                    maxValue = Math.max(maxValue, Number(nv))
                }
            }
        }
        const lo = Math.floor((minValue - 5) / 10) * 10
        const hi = Math.ceil((maxValue + 5) / 10) * 10
        return { lo: lo, hi: hi }
    }

    function _selectedDetail() {
        const pd = compareModel.selectedPointDetail
        if (pd === null || pd === undefined) {
            return null
        }
        const source = _sourceById(String(pd.sourceId))
        const kind = source === null ? "" : String(source.sourceKind)
        const normalized = pd.normalizedValue
        const normalizedText = normalized === null || normalized === undefined
                               ? "N/A" : String(Math.round(Number(normalized)))
        let deltaText = "N/A"
        if (normalized !== null && normalized !== undefined) {
            const delta = Math.round(Number(normalized) - 100)
            deltaText = (delta >= 0 ? "+" : "") + delta + "%"
        }
        return {
            dqeId: pd.dqeId,
            title: pd.title,
            sourceKind: kind.length > 0 ? kind.charAt(0).toUpperCase() + kind.slice(1) : "—",
            sourceLabel: source === null ? String(pd.sourceId) : String(source.shortLabel),
            jobOrVersion: source === null ? "—" : String(source.jobKey),
            block: source === null ? "—" : String(source.block),
            rawValue: pd.rawValue,
            rawUnit: pd.rawUnit,
            status: pd.status,
            statusRaw: pd.statusRaw,
            issue: pd.issue,
            duplicateRows: pd.duplicateRows,
            normalizedText: normalizedText,
            deltaText: deltaText
        }
    }

    function _selectedAttachments() {
        if (productionSource) {
            if (compareProvider === null || compareProvider === undefined) {
                return []
            }
            // 與 snapshot series 相同，轉成 JS array 才能交給既有 AttachmentList。
            return _arrayValues(compareProvider.attachmentsForPoint(selectedSourceId, selectedDqeId))
        }
        const series = _seriesById(selectedDqeId)
        if (series === null || !Array.isArray(series.attachments)) {
            return []
        }
        return series.attachments
    }

    function _attachmentById(attachmentId) {
        const attachments = selectedAttachments
        for (let i = 0; i < attachments.length; ++i) {
            if (String(attachments[i].attachmentId) === attachmentId) {
                return attachments[i]
            }
        }
        return null
    }

    // 已選 jobs 的 DQE_ID 交集：只有在「每一個」已選 source 都出現的 DQE 才算可比較。
    // series.points 只含該 DQE 實際出現過的 source（aggregator 不補缺點），故 point 存在即代表
    // 該 source 有此 DQE。mock 無此限制（展示全部）。
    function _intersectionDqeIds() {
        const selSources = Array.isArray(selectedSourceIds)
                           ? selectedSourceIds.map(function(id) { return String(id) }) : []
        if (selSources.length === 0) {
            return []
        }
        const all = workspace._seriesCatalog
        const ids = []
        for (let i = 0; i < all.length; ++i) {
            const points = _arrayValues(all[i].points)
            const present = {}
            for (let p = 0; p < points.length; ++p) {
                present[String(points[p].sourceId)] = true
            }
            let inAll = true
            for (let k = 0; k < selSources.length; ++k) {
                if (present[selSources[k]] !== true) {
                    inAll = false
                    break
                }
            }
            if (inAll) {
                ids.push(String(all[i].dqeId))
            }
        }
        return ids
    }

    // selector 用的 DQE 清單（含 selected 旗標、線色，以及表格欄位 title/description）。
    // production：只列出「已選 jobs 的 DQE 交集」，讓使用者從共同 DQE 中多選比較對象。
    // mock：維持列出全部。description 取 series metadata。
    function _dqeSeriesForSelector() {
        const all = workspace._seriesCatalog
        const selected = Array.isArray(dqeSelectedIds) ? dqeSelectedIds : []
        const intersection = productionSource ? _intersectionDqeIds() : null
        const list = []
        for (let i = 0; i < all.length; ++i) {
            const dqeId = String(all[i].dqeId)
            if (intersection !== null && intersection.indexOf(dqeId) === -1) {
                continue
            }
            list.push({
                dqeId: all[i].dqeId,
                title: all[i].title === undefined || all[i].title === null ? "" : String(all[i].title),
                description: all[i].description === undefined || all[i].description === null
                             ? "" : String(all[i].description),
                color: workspace._colorFor(dqeId),
                selected: selected.indexOf(dqeId) !== -1
            })
        }
        return list
    }

    // ── 互動 ──
    function toggleSource(sourceId) {
        const id = String(sourceId)
        const ids = Array.isArray(selectedSourceIds) ? selectedSourceIds.slice() : []
        const pos = ids.indexOf(id)
        if (pos === -1) {
            ids.push(id)
        } else {
            ids.splice(pos, 1)
            if (id === selectedSourceId) {
                _clearPoint()
            }
        }
        selectedSourceIds = ids
    }

    // staging／integration 可傳入 QVariantList；轉為 JS array 以維持使用者指定的順序語意。
    function setSourceOrder(sourceIds) {
        selectedSourceIds = _arrayValues(sourceIds)
    }

    function toggleDqe(dqeId) {
        const id = String(dqeId)
        const ids = Array.isArray(dqeSelectedIds) ? dqeSelectedIds.slice() : []
        const pos = ids.indexOf(id)
        if (pos === -1) {
            ids.push(id)
        } else {
            ids.splice(pos, 1)
            if (id === selectedDqeId) {
                _clearPoint()
            }
        }
        dqeSelectedIds = ids
    }

    function setBlockFilter(text) {
        blockFilter = text === undefined || text === null ? "" : String(text)
    }

    function _clearPoint() {
        selectedDqeId = ""
        selectedSourceId = ""
        selectedAttachmentId = ""
        pointSelectionChanged("", "")
    }

    function selectPoint(dqeId, sourceId) {
        const dqe = dqeId === undefined || dqeId === null ? "" : String(dqeId)
        const source = sourceId === undefined || sourceId === null ? "" : String(sourceId)
        if (dqe === "" || source === "") {
            _clearPoint()
            return
        }
        selectedDqeId = dqe
        selectedSourceId = source
        // attachment 隨選取的 DQE 重置為第一個。
        const attachments = _selectedAttachments()
        selectedAttachmentId = attachments.length > 0 ? String(attachments[0].attachmentId) : ""
        pointSelectionChanged(dqe, source)
    }

    function selectAttachment(attachmentId) {
        const id = attachmentId === undefined || attachmentId === null ? "" : String(attachmentId)
        if (_attachmentById(id) !== null) {
            selectedAttachmentId = id
        }
    }

    // ── HTML 匯出 ────────────────────────────────────────────────────────────
    FileDialog {
        id: exportDialog
        objectName: "compareExportDialog"
        title: DqeI18n.t("Export Compare result as HTML", "匯出 Compare 結果為 HTML", "导出 Compare 结果为 HTML")
        fileMode: FileDialog.SaveFile
        defaultSuffix: "html"
        nameFilters: ["HTML (*.html)"]
        onAccepted: workspace._writeExport(selectedFile)
    }

    // 由 selector 的「Export HTML」按鈕呼叫：開啟儲存對話框。
    // 以 exporter 提供的完整 URL（含 home/documents 目錄）預填，避免相對路徑無法預填。
    function requestExport() {
        if (htmlExporter !== null && htmlExporter !== undefined
                && typeof htmlExporter.suggestedFileUrl === "function") {
            // 完整 file:// URL（含目錄）；FileDialog 會據此預填資料夾與檔名。
            exportDialog.selectedFile = htmlExporter.suggestedFileUrl()
        }
        exportDialog.open()
    }

    function _writeExport(fileUrl) {
        if (htmlExporter === null || htmlExporter === undefined) {
            exportCompleted(false, DqeI18n.t("Export failed: exporter not available",
                                             "匯出失敗：exporter 未注入", "导出失败：exporter 未注入"))
            return
        }
        const err = htmlExporter.writeHtml(fileUrl, _buildExportHtml())
        exportCompleted(err.length === 0, err.length === 0 ? String(fileUrl) : err)
    }

    function _escHtml(value) {
        return String(value === undefined || value === null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    }

    function _pointForExport(oneSeries, sourceId) {
        const points = Array.isArray(oneSeries.points) ? oneSeries.points : []
        for (let i = 0; i < points.length; ++i) {
            if (String(points[i].sourceId) === sourceId) {
                return points[i]
            }
        }
        return null
    }

    // 依目前選取的 sources 與已勾選 DQE（chartSeries）產生自足式 HTML 表格。
    function _buildExportHtml() {
        const sources = orderedSelectedSources
        const chart = chartSeries
        const langAttr = DqeI18n.language   // en / zh-Hant / zh-Hans → 合法 HTML lang code
        let html = "<!doctype html><html lang=\"" + langAttr + "\"><head><meta charset=\"utf-8\">"
                 + "<title>DQE Compare Export</title><style>"
                 + "body{font-family:system-ui,sans-serif;margin:24px;color:#1a1a1a}"
                 + "h1{font-size:18px;margin:0 0 4px}p{font-size:13px;color:#444;margin:4px 0}"
                 + "table{border-collapse:collapse;margin-top:14px}"
                 + "caption{text-align:left;color:#666;font-size:12px;margin-bottom:6px}"
                 + "th,td{border:1px solid #ccc;padding:5px 9px;font-size:13px;text-align:left;white-space:nowrap}"
                 + "th{background:#f2f4f7}.na{color:#999}.baseline{font-weight:700}"
                 + "ol{font-size:13px;margin:4px 0}</style></head><body>"
        html += "<h1>" + _escHtml(DqeI18n.t("DQE Normalized Comparison", "DQE 正規化比較", "DQE 归一化比较")) + "</h1>"
        html += "<p>" + _escHtml(DqeI18n.t("Generated: ", "產生時間：", "生成时间："))
              + _escHtml(new Date().toLocaleString())
              + "　" + _escHtml(DqeI18n.t("Mode: ", "模式：", "模式："))
              + (productionSource ? "Production" : "Mock")
              + "　" + _escHtml(DqeI18n.t("Block filter: ", "Block 篩選：", "Block 筛选："))
              + _escHtml(blockFilter.length > 0 ? blockFilter : DqeI18n.t("(all)", "（全部）", "（全部）")) + "</p>"

        html += "<p><b>" + _escHtml(DqeI18n.t("Sources", "來源", "来源")) + "</b>"
              + _escHtml(DqeI18n.t(" (first = baseline = 100%):", "（第一個為 baseline = 100%）：", "（第一个为 baseline = 100%）：")) + "</p><ol>"
        for (let s = 0; s < sources.length; ++s) {
            const label = sources[s].shortLabel === undefined
                          ? sources[s].sourceId : sources[s].shortLabel
            const jobKey = sources[s].jobKey === undefined ? "" : sources[s].jobKey
            html += "<li" + (s === 0 ? " class=\"baseline\"" : "") + ">"
                  + _escHtml(label) + (String(jobKey).length > 0 ? " — " + _escHtml(jobKey) : "")
                  + "</li>"
        }
        html += "</ol>"

        html += "<table><caption>"
              + _escHtml(DqeI18n.t("Each cell is normalized % (raw value). baseline = 100%; N/A = not comparable.",
                                   "每格為 normalized %（原始值）。baseline = 100%；N/A 表示無法比較。",
                                   "每格为 normalized %（原始值）。baseline = 100%；N/A 表示无法比较。"))
              + "</caption><tr>"
              + "<th>DQE_ID</th><th>" + _escHtml(DqeI18n.t("Title", "標題", "标题"))
              + "</th><th>" + _escHtml(DqeI18n.t("Unit", "單位", "单位")) + "</th>"
        for (let s = 0; s < sources.length; ++s) {
            const label = sources[s].shortLabel === undefined
                          ? sources[s].sourceId : sources[s].shortLabel
            html += "<th>" + _escHtml(label) + "</th>"
        }
        html += "</tr>"

        for (let i = 0; i < chart.length; ++i) {
            const series = chart[i]
            const naSeries = series.issue !== null && series.issue !== undefined
            html += "<tr><td>" + _escHtml(series.dqeId) + "</td><td>" + _escHtml(series.title)
                  + "</td><td>" + _escHtml(series.unit) + "</td>"
            for (let s = 0; s < sources.length; ++s) {
                const point = _pointForExport(series, String(sources[s].sourceId))
                let cell = "<span class=\"na\">—</span>"
                if (point !== null) {
                    const raw = point.rawValue === undefined || point.rawValue === null
                                ? "" : String(point.rawValue)
                    const unit = point.rawUnit === undefined || point.rawUnit === null
                                 ? "" : String(point.rawUnit)
                    const rawText = raw.length > 0
                                    ? _escHtml(raw + (unit.length > 0 && unit !== "count" ? " " + unit : ""))
                                    : ""
                    const nv = point.normalizedValue
                    if (!naSeries && nv !== null && nv !== undefined) {
                        cell = Math.round(Number(nv)) + "%"
                             + (rawText.length > 0 ? " (" + rawText + ")" : "")
                    } else if (rawText.length > 0) {
                        cell = "<span class=\"na\">" + rawText + "</span>"
                    }
                }
                html += "<td>" + cell + "</td>"
            }
            html += "</tr>"
        }
        html += "</table></body></html>"
        return html
    }

    // 還原至預設狀態（供測試在各案例間重置）。
    function _resetForTest() {
        // mock catalog 的 block 皆為 "top"，維持原預設。production 真實資料的 block 通常是
        // chip_top 或空字串，硬編 "top" 會被 精確比對（Normalization seriesBlock !== filter）
        // 濾光所有 series → NORMALIZED TREND 顯示 "No comparable trend to plot"。production 預設
        // 改為不過濾（""），完整顯示可比較的 series；使用者仍可於 selector 輸入 block 縮小範圍。
        blockFilter = productionSource ? "" : "top"
        if (productionSource) {
            // 預設不選任何 job（3-1）；使用者手動挑 jobs 後再從 DQE 交集勾選。
            // autoSelectAllOnLoad（test/staging harness）時保留舊的「全選」行為。
            if (autoSelectAllOnLoad) {
                _seedProductionSelectionAll()
            } else {
                selectedSourceIds = []
                dqeSelectedIds = []
                _productionSeeded = false
                _clearPoint()
            }
            return
        }
        if (catalog !== null) {
            selectedSourceIds = catalog.defaultSelection()
        } else {
            selectedSourceIds = []
        }
        const dqeIds = []
        const all = workspace._seriesCatalog
        for (let i = 0; i < all.length; ++i) {
            dqeIds.push(String(all[i].dqeId))
        }
        dqeSelectedIds = dqeIds
        // mock reference selection is frozen。
        selectPoint("STA_002", "arch-v3")
    }

    // 全選所有 source 與 DQE 並選第一個點（僅 autoSelectAllOnLoad 的 test/staging 用）。
    function _seedProductionSelectionAll() {
        const productionIds = []
        for (let sourceIndex = 0; sourceIndex < _sourceCatalog.length; ++sourceIndex) {
            productionIds.push(String(_sourceCatalog[sourceIndex].sourceId))
        }
        selectedSourceIds = productionIds
        const dqeIds = []
        for (let seriesIndex = 0; seriesIndex < _seriesCatalog.length; ++seriesIndex) {
            dqeIds.push(String(_seriesCatalog[seriesIndex].dqeId))
        }
        dqeSelectedIds = dqeIds
        if (dqeIds.length > 0 && productionIds.length > 0) {
            selectPoint(dqeIds[0], productionIds[0])
        } else {
            _clearPoint()
        }
        _productionSeeded = productionIds.length > 0
    }

    // production + autoSelectAllOnLoad：snapshots 非同步抵達後，若 _resetForTest 首跑時 catalog
    // 為空而未 seed，於資料抵達時補上全選。手動模式（預設）不在此自動選取。
    function _seedProductionSelectionIfNeeded() {
        if (!productionSource || !autoSelectAllOnLoad || _productionSeeded) {
            return
        }
        if (_sourceCatalog.length === 0) {
            return
        }
        const productionIds = []
        for (let sourceIndex = 0; sourceIndex < _sourceCatalog.length; ++sourceIndex) {
            productionIds.push(String(_sourceCatalog[sourceIndex].sourceId))
        }
        selectedSourceIds = productionIds
        const dqeIds = []
        for (let seriesIndex = 0; seriesIndex < _seriesCatalog.length; ++seriesIndex) {
            dqeIds.push(String(_seriesCatalog[seriesIndex].dqeId))
        }
        dqeSelectedIds = dqeIds
        if (dqeIds.length > 0 && productionIds.length > 0) {
            selectPoint(dqeIds[0], productionIds[0])
        }
        _productionSeeded = true
    }

    function _colorFor(dqeId) {
        if (!productionSource && catalog !== null) {
            return catalog.colorFor(dqeId)
        }
        const palette = ["#12C8E8", "#FF5C67", "#F6B94B", "#32D17D", "#B98CFF"]
        let hash = 0
        for (let index = 0; index < dqeId.length; ++index) {
            hash = (hash + dqeId.charCodeAt(index)) % palette.length
        }
        return palette[hash]
    }

    function _validateProductionSelections() {
        if (!productionSource) {
            return
        }
        const validSources = []
        for (let index = 0; index < _sourceCatalog.length; ++index) {
            validSources.push(String(_sourceCatalog[index].sourceId))
        }
        const nextSources = []
        for (let index = 0; index < selectedSourceIds.length; ++index) {
            if (validSources.indexOf(String(selectedSourceIds[index])) !== -1) {
                nextSources.push(String(selectedSourceIds[index]))
            }
        }
        selectedSourceIds = nextSources
        const validDqes = []
        for (let seriesIndex = 0; seriesIndex < _seriesCatalog.length; ++seriesIndex) {
            validDqes.push(String(_seriesCatalog[seriesIndex].dqeId))
        }
        dqeSelectedIds = dqeSelectedIds.filter((dqeId) => validDqes.indexOf(String(dqeId)) !== -1)
        if (validSources.indexOf(selectedSourceId) === -1 || validDqes.indexOf(selectedDqeId) === -1) {
            _clearPoint()
        }
    }

    Connections {
        target: workspace.compareProvider
        function onSnapshotsChanged() {
            workspace._validateProductionSelections()
            workspace._seedProductionSelectionIfNeeded()
        }
    }

    onCompareProviderChanged: Qt.callLater(_resetForTest)
    Component.onCompleted: Qt.callLater(_resetForTest)

    Shell.DqeWorkspaceShell {
        id: workspaceShell
        objectName: "workspaceShell"
        anchors.fill: parent
        workspaceKey: "compare"
        navigationTitle: DqeI18n.t("COMPARE SOURCES", "比較來源", "比较来源")
        centerTitle: DqeI18n.t("NORMALIZED TREND", "正規化趨勢", "归一化趋势")
        inspectorTitle: DqeI18n.t("SELECTED TREND POINT", "選取的趨勢點", "选取的趋势点")
        evidenceTitle: DqeI18n.t("EVIDENCE / ATTACHMENT PREVIEW", "證據 / 附件預覽", "证据 / 附件预览")

        navigationContent: Component {
            Navigation.CompareSourceSelector {
                temporarySources: workspace._sourcesByKind("temporary")
                archivedSources: workspace._sourcesByKind("archived")
                selectedSourceIds: workspace.selectedSourceIds
                dqeSeries: workspace._dqeSeriesForSelector()
                selectedCount: workspace.selectedCount
                blockFilterText: workspace.blockFilter
                onSourceToggled: (sourceId) => { workspace.toggleSource(sourceId) }
                onDqeToggled: (dqeId) => { workspace.toggleDqe(dqeId) }
                onBlockFilterEdited: (text) => { workspace.setBlockFilter(text) }
                onExportRequested: workspace.requestExport()
            }
        }

        centerContent: Component {
            SplitView {
                objectName: "compareCenterSplit"
                orientation: Qt.Vertical

                handle: Rectangle {
                    objectName: "compareCenterHandle"
                    implicitHeight: 6
                    readonly property bool pressed: SplitHandle.pressed
                    color: pressed ? DqeTheme.accent
                         : (SplitHandle.hovered ? DqeTheme.borderStrong : DqeTheme.border)
                }

                Compare.NormalizedTrendChart {
                    objectName: "normalizedTrendChart"
                    SplitView.fillHeight: true
                    SplitView.minimumHeight: 220
                    productionSource: workspace.productionSource
                    orderedSources: workspace.orderedSelectedSources
                    series: workspace.chartSeries
                    yMin: workspace.yRange.lo
                    yMax: workspace.yRange.hi
                selectedDqeId: workspace.selectedDqeId
                selectedSourceId: workspace.selectedSourceId
                onCaptureReadyChanged: workspace.chartReadyForCapture = captureReady
                onPointClicked: (dqeId, sourceId) => { workspace.selectPoint(dqeId, sourceId) }
                }

                Compare.RawComparisonTable {
                    objectName: "rawComparisonTable"
                    SplitView.preferredHeight: 210
                    SplitView.minimumHeight: 140
                    orderedSources: workspace.orderedSelectedSources
                    series: workspace.chartSeries
                    selectedDqeId: workspace.selectedDqeId
                    selectedSourceId: workspace.selectedSourceId
                    onCellSelected: (dqeId, sourceId) => { workspace.selectPoint(dqeId, sourceId) }
                }
            }
        }

        inspectorContent: Component {
            Compare.TrendPointInspector {
                objectName: "trendPointInspector"
                detail: workspace.selectedDetail
            }
        }

        evidenceContent: Component {
            Review.EvidencePanel {
                objectName: "compareEvidencePanel"
                attachments: workspace.selectedAttachments
                selectedAttachmentId: workspace.selectedAttachmentId
                previewKind: workspace.selectedAttachment === null
                             ? "" : String(workspace.selectedAttachment.previewKind)
                previewPayload: workspace.selectedAttachment === null
                                ? null : workspace.selectedAttachment.mockPayload
                productionSource: workspace.productionSource
                onAttachmentSelected: (attachmentId) => { workspace.selectAttachment(attachmentId) }
            }
        }
    }
}
