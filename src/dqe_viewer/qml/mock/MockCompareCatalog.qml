// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Compare workspace 專屬的固定 mock 目錄（Temporary＋Archived 混合來源）。
// 只提供決定性、深度凍結的展示資料；raw value 一律字串、原樣保留。
// 與 的 MockCompareData 獨立：本檔提供較豐富的多來源／多 DQE_ID 資料集，
// 供 CompareViewModel 正規化後由 呈現。

import QtQml

QtObject {
    id: catalog
    objectName: "mockCompareCatalog"

    function _freeze(value) {
        if (value === null || typeof value !== "object" || Object.isFrozen(value)) {
            return value
        }
        const keys = Object.keys(value)
        for (let index = 0; index < keys.length; ++index) {
            _freeze(value[keys[index]])
        }
        return Object.freeze(value)
    }

    function _clone(value) {
        return JSON.parse(JSON.stringify(value))
    }

    // 全部可選來源（Temporary 在前、Archived 在後）；shortLabel 供 x 軸標示。
    readonly property var sources: _freeze([
        { sourceKind: "temporary", sourceId: "temp-154102", displayName: "job 154102_001",
          jobKey: "job 154102_001", versionPath: "", block: "top", flow: "openroad",
          shortLabel: "T-154102", subtitle: "top · openroad" },
        { sourceKind: "temporary", sourceId: "temp-151255", displayName: "job 151255_001",
          jobKey: "job 151255_001", versionPath: "", block: "top", flow: "openroad",
          shortLabel: "T-151255", subtitle: "top · openroad" },
        { sourceKind: "temporary", sourceId: "temp-223518", displayName: "job 223518_002",
          jobKey: "job 223518_002", versionPath: "", block: "cpu_subsys", flow: "yosys",
          shortLabel: "T-223518", subtitle: "cpu_subsys · yosys" },
        { sourceKind: "archived", sourceId: "arch-v2", displayName: "2009_0603-LEC (V2)",
          jobKey: "2009_0603-LEC", versionPath: "trial / top / INITDATA", block: "top", flow: "openroad",
          shortLabel: "A-V2", subtitle: "trial / top / INITDATA" },
        { sourceKind: "archived", sourceId: "arch-v3", displayName: "2009_0604-LEC (V3)",
          jobKey: "2009_0604-LEC", versionPath: "trial / top / INITDATA", block: "top", flow: "openroad",
          shortLabel: "A-V3", subtitle: "trial / top / INITDATA" },
        { sourceKind: "archived", sourceId: "arch-v1", displayName: "2011_0214 (V1)",
          jobKey: "2011_0214", versionPath: "trial / top / POSTCTS", block: "top", flow: "openroad",
          shortLabel: "A-V1", subtitle: "trial / top / POSTCTS" }
    ])

    // 預設選取來源（依使用者順序）：兩個 Temporary + 兩個 Archived。
    readonly property var defaultSelectedSourceIds: _freeze([
        "temp-154102", "temp-151255", "arch-v2", "arch-v3"
    ])

    // DQE_ID 系列目錄；每系列在各來源的 raw value（字串）與 status。
    readonly property var seriesCatalog: _freeze([
        {
            dqeId: "STA_001", title: "Number of checks", unit: "count", block: "top",
            points: [
                { sourceId: "temp-154102", value: "19250", unit: "count", status: "Pass" },
                { sourceId: "temp-151255", value: "19830", unit: "count", status: "Pass" },
                { sourceId: "temp-223518", value: "18900", unit: "count", status: "Pass" },
                { sourceId: "arch-v2", value: "19443", unit: "count", status: "Pass" },
                { sourceId: "arch-v3", value: "20405", unit: "count", status: "Pass" },
                { sourceId: "arch-v1", value: "19000", unit: "count", status: "Pass" }
            ],
            attachments: []
        },
        {
            dqeId: "STA_002", title: "Number of violation", unit: "count", block: "top",
            points: [
                { sourceId: "temp-154102", value: "64", unit: "count", status: "Fail" },
                { sourceId: "temp-151255", value: "59", unit: "count", status: "Warn" },
                { sourceId: "temp-223518", value: "70", unit: "count", status: "Fail" },
                { sourceId: "arch-v2", value: "76", unit: "count", status: "Fail" },
                { sourceId: "arch-v3", value: "79", unit: "count", status: "Fail" },
                { sourceId: "arch-v1", value: "60", unit: "count", status: "Warn" }
            ],
            attachments: [
                { attachmentId: "sta002-image", displayName: "STA_002_attachment.png",
                  previewKind: "image", sizeLabel: "Image · 284 KB",
                  mockPayload: { label: "Mock evidence image · violation distribution" } },
                { attachmentId: "sta002-text", displayName: "STA_002_executable.sh",
                  previewKind: "text", sizeLabel: "Text · 1.8 KB",
                  mockPayload: { text: "#!/bin/sh\n# Mock evidence script (non-executable mock)\necho compare" } },
                { attachmentId: "sta002-source", displayName: "source_report.rpt",
                  previewKind: "unsupported", sizeLabel: "Source path",
                  mockPayload: { label: "Mock source path payload" } }
            ]
        },
        {
            dqeId: "STA_004", title: "Total negative slack", unit: "ns", block: "top",
            points: [
                { sourceId: "temp-154102", value: "-12.47", unit: "ns", status: "Warn" },
                { sourceId: "temp-151255", value: "-13.48", unit: "ns", status: "Warn" },
                { sourceId: "temp-223518", value: "-12.00", unit: "ns", status: "Warn" },
                { sourceId: "arch-v2", value: "-12.10", unit: "ns", status: "Warn" },
                { sourceId: "arch-v3", value: "-11.10", unit: "ns", status: "Pass" },
                { sourceId: "arch-v1", value: "-13.00", unit: "ns", status: "Warn" }
            ],
            attachments: []
        }
    ])

    // 穩定的每 DQE_ID 線條色（依 catalog 次序，來自 theme tokens）。
    readonly property var seriesColorOrder: _freeze([
        { dqeId: "STA_001", color: "#12C8E8" },
        { dqeId: "STA_002", color: "#FF5C67" },
        { dqeId: "STA_004", color: "#F6B94B" }
    ])

    function sourceList() { return _clone(sources) }
    function seriesList() { return _clone(seriesCatalog) }
    function defaultSelection() { return _clone(defaultSelectedSourceIds) }

    function colorFor(dqeId) {
        for (let index = 0; index < seriesColorOrder.length; ++index) {
            if (seriesColorOrder[index].dqeId === dqeId) {
                return seriesColorOrder[index].color
            }
        }
        // 後備 palette（決定性）。
        const palette = ["#32D17D", "#B98CFF", "#6EE7B7", "#F59E0B"]
        let hash = 0
        for (let c = 0; c < dqeId.length; ++c) {
            hash = (hash + dqeId.charCodeAt(c)) % palette.length
        }
        return palette[hash]
    }
}
