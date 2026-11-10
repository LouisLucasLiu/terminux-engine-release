// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 暫存工作來源的固定 mock fixtures。

import QtQml

QtObject {
    id: temporaryData

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

    function _group(source, items) {
        const counts = {
            total: items.length,
            pass: 0,
            warn: 0,
            fail: 0,
            unknown: 0
        }
        for (let index = 0; index < items.length; ++index) {
            switch (items[index].status) {
            case "Pass":
                ++counts.pass
                break
            case "Warn":
                ++counts.warn
                break
            case "Fail":
                ++counts.fail
                break
            default:
                ++counts.unknown
                break
            }
        }
        return {
            source: source,
            total: counts.total,
            pass: counts.pass,
            warn: counts.warn,
            fail: counts.fail,
            unknown: counts.unknown,
            items: items
        }
    }

    readonly property var normalGroups: _freeze([
        _group({
                   sourceKind: "temporary",
                   sourceId: "temporary-mock-job-001",
                   displayName: "Mock temporary job 001",
                   jobKey: "mock-job-20260711-001",
                   versionPath: "",
                   block: "top",
                   flow: "openroad",
                   dateTime: "2026-07-11 16:05:00"
               }, [
                   {
                       dqeId: "MOCK_TEMP_PASS_001",
                       title: "Mock temporary checks",
                       value: "48",
                       unit: "count",
                       status: "Pass",
                       description: "Clearly labeled mock temporary pass row.",
                       reference: "Mock temporary reference",
                       sourcePath: "mock://temporary/mock-job-001/mock_pass.rpt",
                       dateTime: "2026-07-11 16:05:00",
                       block: "top",
                       flow: "openroad",
                       attachments: []
                   },
                   {
                       dqeId: "MOCK_TEMP_WARN_001",
                       title: "Mock temporary warning",
                       value: "3",
                       unit: "count",
                       status: "Warn",
                       description: "Clearly labeled mock temporary warning row.",
                       reference: "Mock temporary reference",
                       sourcePath: "mock://temporary/mock-job-001/mock_warn.rpt",
                       dateTime: "2026-07-11 16:05:00",
                       block: "top",
                       flow: "openroad",
                       attachments: []
                   },
                   {
                       dqeId: "MOCK_TEMP_FAIL_001",
                       title: "Mock temporary failure",
                       value: "2",
                       unit: "count",
                       status: "Fail",
                       description: "Clearly labeled mock temporary fail row.",
                       reference: "Mock temporary reference",
                       sourcePath: "mock://temporary/mock-job-001/mock_fail.rpt",
                       dateTime: "2026-07-11 16:05:00",
                       block: "top",
                       flow: "openroad",
                       attachments: []
                   },
                   {
                       dqeId: "MOCK_TEMP_UNKNOWN_001",
                       title: "Mock temporary unknown",
                       value: "",
                       unit: "",
                       status: "Unknown",
                       description: "Clearly labeled mock temporary unknown row.",
                       reference: "",
                       sourcePath: "mock://temporary/mock-job-001/mock_unknown.rpt",
                       dateTime: "2026-07-11 16:05:00",
                       block: "top",
                       flow: "openroad",
                       attachments: []
                   }
               ])
    ])

    readonly property var emptyGroups: _freeze([
        _group({
                   sourceKind: "temporary",
                   sourceId: "temporary-mock-job-empty",
                   displayName: "Mock temporary job (empty)",
                   jobKey: "mock-job-empty",
                   versionPath: "",
                   block: "top",
                   flow: "openroad",
                   dateTime: "2026-07-11 16:10:00"
               }, [])
    ])
}
