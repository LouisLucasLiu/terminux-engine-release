// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 純資料的 Compare view model（無 chart／table styling、無後端、無 socket 通訊）。
// 支援 Temporary＋Archived 混合來源、使用者指定順序、block filter、多個 DQE_ID series，
// 以 first-point 正規化產生 backend-neutral 的輸出結構供 消費。
// 所有計算委派給無狀態的 Normalization.js；本元件只負責 wire QML 屬性與衍生輸出。

import QtQml
import "Normalization.js" as Norm

QtObject {
    id: model
    objectName: "compareViewModel"

    // ── 輸入（由 owner／harness 設定）──
    // ordered source descriptors（使用者指定順序，第一個即唯一 baseline）。
    property var sources: []
    // 多個 DQE_ID series：{ dqeId, title, unit, block?, points:[{sourceId, value, unit, status}] }。
    property var inputSeries: []
    // block filter：非空時只保留 series.block 精確相符者（大小寫敏感）。
    property string blockFilter: ""
    // selected-point detail 的選取狀態。
    property string selectedDqeId: ""
    property string selectedSourceId: ""

    // ── 衍生輸出（output shape）──
    readonly property var result: Norm.buildModel(sources, inputSeries, blockFilter)
    readonly property var orderedSources: result.orderedSources
    readonly property string baselineSourceId: result.baselineSourceId
    readonly property var series: result.series
    readonly property var rawRows: result.rawRows
    readonly property var issues: result.issues
    readonly property var selectedPointDetail: Norm.selectedDetail(result, selectedDqeId, selectedSourceId)
    readonly property bool hasIssues: Array.isArray(issues) && issues.length > 0

    function setSources(nextSources) {
        sources = Array.isArray(nextSources) ? nextSources : []
    }

    function setSeries(nextSeries) {
        inputSeries = Array.isArray(nextSeries) ? nextSeries : []
    }

    function setBlockFilter(nextFilter) {
        blockFilter = nextFilter === undefined || nextFilter === null ? "" : String(nextFilter)
    }

    function selectPoint(dqeId, sourceId) {
        selectedDqeId = dqeId === undefined || dqeId === null ? "" : String(dqeId)
        selectedSourceId = sourceId === undefined || sourceId === null ? "" : String(sourceId)
    }

    function clearSelection() {
        selectedDqeId = ""
        selectedSourceId = ""
    }

    // 診斷輸出：把整個模型與目前選取序列化為 JSON（供 harness／測試 dump）。
    function toJson() {
        return JSON.stringify({
            orderedSources: orderedSources,
            baselineSourceId: baselineSourceId,
            series: series,
            rawRows: rawRows,
            issues: issues,
            selectedPointDetail: selectedPointDetail
        }, null, 2)
    }
}
