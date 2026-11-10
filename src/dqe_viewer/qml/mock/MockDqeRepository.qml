// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 純記憶體 mock repository。
// fixture 資料固定且深度凍結；所有對外資料皆回傳獨立複本。

import QtQml

QtObject {
    id: repository
    objectName: "mockDqeRepository"

    readonly property var _archivedData: MockArchivedData {}
    readonly property var _temporaryData: MockTemporaryData {}
    readonly property var _compareData: MockCompareData {}

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
        if (value === null || value === undefined) {
            return value
        }
        return JSON.parse(JSON.stringify(value))
    }

    readonly property var scenarioNames: _freeze([
        "archived_normal",
        "archived_fail_selected",
        "temporary_normal",
        "temporary_empty",
        "temporary_loading",
        "database_unavailable",
        "unsupported_attachment",
        "missing_attachment",
        "integrity_incomplete",
        "compare_normal",
        "compare_zero_baseline",
        "compare_non_numeric"
    ])

    readonly property var _scenarioSetup: _freeze({
        archived_normal: { dataState: "ready", sourceId: "archived-v3", dqeId: "STA_001" },
        archived_fail_selected: { dataState: "ready", sourceId: "archived-v3", dqeId: "STA_002" },
        temporary_normal: { dataState: "ready", sourceId: "temporary-mock-job-001", dqeId: "MOCK_TEMP_PASS_001" },
        temporary_empty: { dataState: "empty", sourceId: "temporary-mock-job-empty", dqeId: "" },
        temporary_loading: { dataState: "loading", sourceId: "", dqeId: "" },
        database_unavailable: { dataState: "database_unavailable", sourceId: "", dqeId: "" },
        unsupported_attachment: { dataState: "ready", sourceId: "archived-v3", dqeId: "MOCK_ATTACHMENT_UNSUPPORTED" },
        missing_attachment: { dataState: "ready", sourceId: "archived-v3", dqeId: "MOCK_ATTACHMENT_MISSING" },
        integrity_incomplete: { dataState: "ready", sourceId: "archived-v3", dqeId: "MOCK_ATTACHMENT_INTEGRITY" },
        compare_normal: { dataState: "ready", sourceId: "", dqeId: "" },
        compare_zero_baseline: { dataState: "ready", sourceId: "", dqeId: "" },
        compare_non_numeric: { dataState: "ready", sourceId: "", dqeId: "" }
    })

    property string _scenario: "archived_normal"
    property string _selectedSourceId: "archived-v3"
    property string _selectedDqeId: "STA_001"

    readonly property string scenario: _scenario
    readonly property string dataState: _scenarioSetup[_scenario].dataState
    readonly property string selectedSourceId: _selectedSourceId
    readonly property string selectedDqeId: _selectedDqeId

    function _isCompareScenario() {
        return _scenario.indexOf("compare_") === 0
    }

    function _activeGroups() {
        switch (_scenario) {
        case "archived_normal":
        case "archived_fail_selected":
        case "unsupported_attachment":
        case "missing_attachment":
        case "integrity_incomplete":
            return _archivedData.groups
        case "temporary_normal":
            return _temporaryData.normalGroups
        case "temporary_empty":
            return _temporaryData.emptyGroups
        default:
            return []
        }
    }

    function _selectedGroup() {
        const groups = _activeGroups()
        for (let index = 0; index < groups.length; ++index) {
            if (groups[index].source.sourceId === _selectedSourceId) {
                return groups[index]
            }
        }
        return null
    }

    function _sourceList(groups) {
        const sources = []
        for (let index = 0; index < groups.length; ++index) {
            sources.push(groups[index].source)
        }
        return sources
    }

    function listArchivedSources() {
        if (_scenario === "database_unavailable") {
            return []
        }
        return _clone(_sourceList(_archivedData.groups))
    }

    function listTemporaryJobs() {
        if (_scenario === "database_unavailable" || _scenario === "temporary_loading") {
            return []
        }
        return _clone(_sourceList(_temporaryData.normalGroups).concat(_sourceList(_temporaryData.emptyGroups)))
    }

    function selectGroup(sourceId) {
        const groups = _activeGroups()
        for (let index = 0; index < groups.length; ++index) {
            if (groups[index].source.sourceId === sourceId) {
                _selectedSourceId = sourceId
                _selectedDqeId = groups[index].items.length > 0 ? groups[index].items[0].dqeId : ""
                return true
            }
        }
        return false
    }

    function selectSource(sourceId) {
        return selectGroup(sourceId)
    }

    function getSummaryCounts() {
        const group = _selectedGroup()
        if (group === null) {
            return { total: 0, pass: 0, warn: 0, fail: 0, unknown: 0 }
        }
        return _clone({
                          total: group.total,
                          pass: group.pass,
                          warn: group.warn,
                          fail: group.fail,
                          unknown: group.unknown
                      })
    }

    function getItemList() {
        const group = _selectedGroup()
        return group === null ? [] : _clone(group.items)
    }

    function selectItem(dqeId) {
        const group = _selectedGroup()
        if (group === null) {
            return false
        }
        for (let index = 0; index < group.items.length; ++index) {
            if (group.items[index].dqeId === dqeId) {
                _selectedDqeId = dqeId
                return true
            }
        }
        return false
    }

    function getSelectedItem() {
        const group = _selectedGroup()
        if (group === null) {
            return null
        }
        for (let index = 0; index < group.items.length; ++index) {
            if (group.items[index].dqeId === _selectedDqeId) {
                return _clone(group.items[index])
            }
        }
        return null
    }

    function getSelectedAttachments() {
        const item = getSelectedItem()
        return item === null ? [] : _clone(item.attachments)
    }

    function getCompareSources() {
        if (!_isCompareScenario()) {
            return []
        }
        return _clone(_compareData.scenarios[_scenario].sources)
    }

    function getSelectedDqeSeries() {
        if (!_isCompareScenario()) {
            return []
        }
        return _clone(_compareData.scenarios[_scenario].selectedDqeSeries)
    }

    function switchScenario(name) {
        if (scenarioNames.indexOf(name) === -1) {
            return false
        }
        const setup = _scenarioSetup[name]
        _scenario = name
        _selectedSourceId = setup.sourceId
        _selectedDqeId = setup.dqeId
        return true
    }
}
