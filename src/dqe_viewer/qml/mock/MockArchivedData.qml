// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 封存來源的固定 mock fixtures。
// 此檔只保存靜態資料；所有對外輸出均由 repository 複製後提供。

import QtQml

QtObject {
    id: archivedData

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

    // STA_001 與 STA_002 的欄位值對齊已提供的 dqe_rpt 參考資料。
    // 其餘 DQE ID 一律以 MOCK_ 前綴標示為 fixture 資料。
    readonly property var groups: _freeze([
        _group({
                   sourceKind: "archived",
                   sourceId: "archived-v3",
                   displayName: "2009_0604-LEC (V3)",
                   jobKey: "2009_0604-LEC",
                   versionPath: "mock://archived/trial/top/000-INITDATA/2009_0604-LEC/V3",
                   block: "top",
                   flow: "openroad",
                   dateTime: "2026-07-11 15:41:02"
               }, [
                   {
                       dqeId: "STA_001",
                       title: "Number of checks",
                       value: "19250",
                       unit: "",
                       status: "Pass",
                       description: "Extracted quality evidence from the selected DQE report.",
                       reference: "STA signoff rule set",
                       sourcePath: "mock://reports/sta_Func_best/pt.rpt",
                       dateTime: "2026-07-11 15:41:02",
                       block: "top",
                       flow: "openroad",
                       attachments: [
                           {
                               attachmentId: "sta001-image",
                               displayName: "STA_001_mock_evidence.png",
                               previewKind: "image",
                               metadata: { label: "Mock evidence image", size: "284 kB" },
                               mockPayload: { caption: "Mock violation distribution", series: ["12", "18", "27"] }
                           },
                           {
                               attachmentId: "sta001-text",
                               displayName: "STA_001_mock_report.txt",
                               previewKind: "text",
                               metadata: { label: "Mock text evidence", size: "1.8 kB" },
                               mockPayload: { text: "Mock report excerpt for STA_001." }
                           }
                       ]
                   },
                   {
                       dqeId: "STA_002",
                       title: "Number of violation",
                       value: "64",
                       unit: "",
                       status: "Fail",
                       description: "Mock fixture preserving the provided STA_002 result.",
                       reference: "STA signoff rule set",
                       sourcePath: "mock://reports/sta_Func_best/pt.rpt",
                       dateTime: "2026-07-11 15:41:02",
                       block: "top",
                       flow: "openroad",
                       attachments: [
                           {
                               attachmentId: "sta002-text",
                               displayName: "STA_002_mock_report.txt",
                               previewKind: "text",
                               metadata: { label: "Mock failure detail", size: "2.1 kB" },
                               mockPayload: {
                                   text: "Mock report: Number of violation\nObserved value: 64\nStatus: FAIL"
                               }
                           }
                       ]
                   },
                   {
                       dqeId: "MOCK_WARN_001",
                       title: "Mock total negative slack",
                       value: "-12.47",
                       unit: "ns",
                       status: "Warn",
                       description: "Clearly labeled mock warning row.",
                       reference: "Mock timing threshold",
                       sourcePath: "mock://reports/mock_warn.rpt",
                       dateTime: "2026-07-11 15:41:02",
                       block: "top",
                       flow: "openroad",
                       attachments: []
                   },
                   {
                       dqeId: "MOCK_ATTACHMENT_UNSUPPORTED",
                       title: "Mock unsupported attachment",
                       value: "1",
                       unit: "count",
                       status: "Unknown",
                       description: "Fixture for an explicitly unsupported preview kind.",
                       reference: "Mock attachment policy",
                       sourcePath: "mock://reports/mock_unsupported.rpt",
                       dateTime: "2026-07-11 15:41:02",
                       block: "top",
                       flow: "openroad",
                       attachments: [
                           {
                               attachmentId: "mock-unsupported-attachment",
                               displayName: "mock_unsupported_payload.bin",
                               previewKind: "unsupported",
                               metadata: { label: "Mock unsupported payload" },
                               mockPayload: null
                           }
                       ]
                   },
                   {
                       dqeId: "MOCK_ATTACHMENT_MISSING",
                       title: "Mock missing attachment",
                       value: "1",
                       unit: "count",
                       status: "Unknown",
                       description: "Fixture for an explicitly missing attachment.",
                       reference: "Mock attachment policy",
                       sourcePath: "mock://reports/mock_missing.rpt",
                       dateTime: "2026-07-11 15:41:02",
                       block: "top",
                       flow: "openroad",
                       attachments: [
                           {
                               attachmentId: "mock-missing-attachment",
                               displayName: "mock_missing_payload.txt",
                               previewKind: "missing",
                               metadata: { label: "Mock missing payload" },
                               mockPayload: null
                           }
                       ]
                   },
                   {
                       dqeId: "MOCK_ATTACHMENT_INTEGRITY",
                       title: "Mock incomplete integrity attachment",
                       value: "1",
                       unit: "count",
                       status: "Unknown",
                       description: "Fixture for incomplete attachment integrity data.",
                       reference: "Mock attachment policy",
                       sourcePath: "mock://reports/mock_integrity.rpt",
                       dateTime: "2026-07-11 15:41:02",
                       block: "top",
                       flow: "openroad",
                       attachments: [
                           {
                               attachmentId: "mock-integrity-attachment",
                               displayName: "mock_integrity_payload.txt",
                               previewKind: "integrity_incomplete",
                               metadata: { label: "Mock incomplete integrity payload" },
                               mockPayload: null
                           }
                       ]
                   },
                   {
                       dqeId: "MOCK_OPTIONAL_FIELDS",
                       title: "Mock optional fields omitted",
                       value: "0",
                       unit: "",
                       status: "Pass",
                       dateTime: "2026-07-11 15:41:02",
                       block: "top",
                       flow: "openroad",
                       attachments: []
                   }
               ]),
        _group({
                   sourceKind: "archived",
                   sourceId: "archived-v2",
                   displayName: "2009_0603-LEC (V2)",
                   jobKey: "2009_0603-LEC",
                   versionPath: "mock://archived/trial/top/000-INITDATA/2009_0603-LEC/V2",
                   block: "top",
                   flow: "openroad",
                   dateTime: "2026-07-10 09:30:00"
               }, [
                   {
                       dqeId: "MOCK_ARCHIVED_V2_001",
                       title: "Mock archived prior-version check",
                       value: "17",
                       unit: "count",
                       status: "Pass",
                       description: "Clearly labeled mock prior-version row.",
                       reference: "Mock archive reference",
                       sourcePath: "mock://reports/mock_archived_v2.rpt",
                       dateTime: "2026-07-10 09:30:00",
                       block: "top",
                       flow: "openroad",
                       attachments: []
                   }
               ])
    ])
}
