// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 比較工作區的固定 raw series fixtures。
// 值保持字串，由後續元件決定如何正規化與呈現。

import QtQml

QtObject {
    id: compareData

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

    readonly property var scenarios: _freeze({
        compare_normal: {
            sources: [
                {
                    sourceKind: "archived",
                    sourceId: "archived-v3",
                    displayName: "2009_0604-LEC (V3)",
                    jobKey: "2009_0604-LEC",
                    versionPath: "mock://archived/trial/top/000-INITDATA/2009_0604-LEC/V3",
                    block: "top",
                    flow: "openroad",
                    dateTime: "2026-07-11 15:41:02"
                },
                {
                    sourceKind: "temporary",
                    sourceId: "temporary-mock-job-001",
                    displayName: "Mock temporary job 001",
                    jobKey: "mock-job-20260711-001",
                    versionPath: "",
                    block: "top",
                    flow: "openroad",
                    dateTime: "2026-07-11 16:05:00"
                }
            ],
            selectedDqeSeries: [
                {
                    dqeId: "MOCK_COMPARE_RATIO",
                    title: "Mock comparison ratio",
                    unit: "%",
                    points: [
                        { sourceId: "archived-v3", value: "100", unit: "%", status: "Pass" },
                        { sourceId: "temporary-mock-job-001", value: "125", unit: "%", status: "Warn" }
                    ]
                }
            ]
        },
        compare_zero_baseline: {
            sources: [
                {
                    sourceKind: "archived",
                    sourceId: "archived-v3",
                    displayName: "2009_0604-LEC (V3)",
                    jobKey: "2009_0604-LEC",
                    versionPath: "mock://archived/trial/top/000-INITDATA/2009_0604-LEC/V3",
                    block: "top",
                    flow: "openroad",
                    dateTime: "2026-07-11 15:41:02"
                },
                {
                    sourceKind: "temporary",
                    sourceId: "temporary-mock-job-001",
                    displayName: "Mock temporary job 001",
                    jobKey: "mock-job-20260711-001",
                    versionPath: "",
                    block: "top",
                    flow: "openroad",
                    dateTime: "2026-07-11 16:05:00"
                }
            ],
            selectedDqeSeries: [
                {
                    dqeId: "MOCK_COMPARE_ZERO",
                    title: "Mock zero baseline",
                    unit: "count",
                    points: [
                        { sourceId: "archived-v3", value: "0", unit: "count", status: "Unknown" },
                        { sourceId: "temporary-mock-job-001", value: "25", unit: "count", status: "Pass" }
                    ]
                }
            ]
        },
        compare_non_numeric: {
            sources: [
                {
                    sourceKind: "archived",
                    sourceId: "archived-v3",
                    displayName: "2009_0604-LEC (V3)",
                    jobKey: "2009_0604-LEC",
                    versionPath: "mock://archived/trial/top/000-INITDATA/2009_0604-LEC/V3",
                    block: "top",
                    flow: "openroad",
                    dateTime: "2026-07-11 15:41:02"
                },
                {
                    sourceKind: "temporary",
                    sourceId: "temporary-mock-job-001",
                    displayName: "Mock temporary job 001",
                    jobKey: "mock-job-20260711-001",
                    versionPath: "",
                    block: "top",
                    flow: "openroad",
                    dateTime: "2026-07-11 16:05:00"
                }
            ],
            selectedDqeSeries: [
                {
                    dqeId: "MOCK_COMPARE_TEXT",
                    title: "Mock non-numeric baseline",
                    unit: "count",
                    points: [
                        { sourceId: "archived-v3", value: "not available", unit: "count", status: "Unknown" },
                        { sourceId: "temporary-mock-job-001", value: "25", unit: "count", status: "Pass" }
                    ]
                }
            ]
        }
    })
}
