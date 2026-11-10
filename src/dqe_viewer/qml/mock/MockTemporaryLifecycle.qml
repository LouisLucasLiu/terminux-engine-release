// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Temporary workspace 專屬的固定 mock 導覽／lifecycle 資料。
// 本檔只提供決定性、深度凍結的展示資料；不觸及任何資料庫、檔案系統或後端服務。
// 與 的 MockTemporaryData 完全獨立：的 fixture 供 review surface（items／
// attachments）使用，本檔僅供 job navigator 與 mock lifecycle 顯示使用。

import QtQml

QtObject {
    id: lifecycle
    objectName: "mockTemporaryLifecycle"

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

    // 預設 retention N（天）。retention editor 只會改變 harness 記憶體顯示，永不持久化。
    readonly property int defaultRetentionDays: 14

    // Check-in 對話框（mock）顯示的既有版本；純展示，不做任何寫入。
    readonly property var checkinVersions: _freeze([
        { version: "v3", dateTime: "2026-07-09 15:12:55", note: "mock existing version" },
        { version: "v2", dateTime: "2026-07-05 09:41:20", note: "mock existing version" },
        { version: "v1", dateTime: "2026-07-01 20:03:07", note: "mock existing version" }
    ])

    // Mock temporary job 目錄；刻意以非排序順序提供，讓 navigator 的
    // 「依 execution time 新到舊」決定性排序被實際執行到。
    // 每筆帶 counts，navigator 由此推導 worst/summary status 徽章。
    readonly property var jobs: _freeze([
        {
            jobKey: "20260710_223518_002",
            block: "cpu_subsys",
            flow: "yosys",
            executionTime: "2026-07-10 22:35:18",
            retentionDays: 12,
            expired: false,
            total: 30, pass: 30, warn: 0, fail: 0, unknown: 0
        },
        {
            jobKey: "20260711_154102_001",
            block: "top",
            flow: "openroad",
            executionTime: "2026-07-11 15:41:02",
            retentionDays: 13,
            expired: false,
            total: 48, pass: 40, warn: 2, fail: 6, unknown: 0
        },
        {
            jobKey: "20260628_101010_001",
            block: "block2",
            flow: "openroad",
            executionTime: "2026-06-28 10:10:10",
            retentionDays: 0,
            expired: true,
            total: 12, pass: 12, warn: 0, fail: 0, unknown: 0
        },
        {
            jobKey: "20260711_151255_001",
            block: "top",
            flow: "openroad",
            executionTime: "2026-07-11 15:12:55",
            retentionDays: 13,
            expired: false,
            total: 52, pass: 42, warn: 10, fail: 0, unknown: 0
        },
        {
            jobKey: "20260709_151255_001",
            block: "block1",
            flow: "openroad",
            executionTime: "2026-07-09 15:12:55",
            retentionDays: 11,
            expired: false,
            total: 18, pass: 17, warn: 0, fail: 1, unknown: 0
        },
        {
            jobKey: "20260710_184411_001",
            block: "gpu",
            flow: "openroad",
            executionTime: "2026-07-10 18:44:11",
            retentionDays: 12,
            expired: false,
            total: 24, pass: 22, warn: 0, fail: 2, unknown: 0
        }
    ])

    // 回傳獨立複本，避免呼叫端意外改動凍結資料。
    function jobList() {
        return JSON.parse(JSON.stringify(jobs))
    }

    function versionList() {
        return JSON.parse(JSON.stringify(checkinVersions))
    }
}
