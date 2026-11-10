// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 純函式的 Compare 正規化 helper（無狀態、無後端、無 UI）。
// 對齊規則、嚴格數值解析與 first-point 正規化全部集中於此，方便獨立驗證。
// 契約細節見對應 spec；任何 raw 字串一律原樣保留，缺失點以 null 表示、絕不補 0。
.pragma library

// 嚴格數值解析：接受正負整數、小數、科學記號與前後空白；
// 拒絕逗號、單位字尾、空字串、NaN、Infinity、hex。
function parseNumeric(raw) {
    if (raw === undefined || raw === null) {
        return { ok: false, value: 0 }
    }
    var trimmed = String(raw).trim()
    if (trimmed.length === 0) {
        return { ok: false, value: 0 }
    }
    // 尾隨小數點（如 "1."）與缺指數位（如 "1e"）皆不接受。
    var strict = /^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$/
    if (!strict.test(trimmed)) {
        return { ok: false, value: 0 }
    }
    var num = parseFloat(trimmed)
    // 溢位成 Infinity（如超大指數）在此攔下。
    if (!isFinite(num)) {
        return { ok: false, value: 0 }
    }
    return { ok: true, value: num }
}

// Unit 比較用：先 trim，之後維持大小寫敏感（不做 lowercasing）。
function unitTrim(unit) {
    return (unit === undefined || unit === null) ? "" : String(unit).trim()
}

function _rawString(value) {
    return value === undefined || value === null ? null : String(value)
}

function _cloneDescriptor(source) {
    return JSON.parse(JSON.stringify(source))
}

// 依 severity 決定 series 層級單一 issue 標籤；unit 不相容最優先（根本無法比較）。
function _seriesIssueLabel(labels) {
    var order = ["unit_mismatch", "duplicate_dqe_id", "baseline_missing", "baseline_non_numeric", "baseline_zero"]
    for (var i = 0; i < order.length; ++i) {
        if (labels.indexOf(order[i]) !== -1) {
            return order[i]
        }
    }
    return null
}

// 建構整個 backend-neutral 的比較模型輸出。
// sources：使用者指定順序的 source descriptors；seriesInput：多個 DQE_ID series；
// blockFilter：非空時，只保留 series.block 精確相符者（大小寫敏感）。
function buildModel(sources, seriesInput, blockFilter) {
    var srcs = Array.isArray(sources) ? sources : []
    var orderedSources = []
    var orderedIds = []
    for (var s = 0; s < srcs.length; ++s) {
        var descriptor = _cloneDescriptor(srcs[s])
        orderedSources.push(descriptor)
        orderedIds.push(String(srcs[s].sourceId))
    }
    var baselineId = orderedIds.length > 0 ? orderedIds[0] : ""

    var filter = (blockFilter === undefined || blockFilter === null) ? "" : String(blockFilter)
    var inputSeries = Array.isArray(seriesInput) ? seriesInput : []

    var series = []
    var rawRows = []
    var issues = []

    for (var i = 0; i < inputSeries.length; ++i) {
        var rawSeries = inputSeries[i]
        var seriesBlock = rawSeries.block === undefined ? "" : String(rawSeries.block)
        // block filter：精確、大小寫敏感；空 filter 不過濾。
        if (filter.length > 0 && seriesBlock !== filter) {
            continue
        }

        var dqeId = String(rawSeries.dqeId)
        var title = rawSeries.title === undefined ? "" : String(rawSeries.title)
        var seriesUnit = rawSeries.unit === undefined ? "" : String(rawSeries.unit)

        var pointsBySource = {}
        var inPoints = Array.isArray(rawSeries.points) ? rawSeries.points : []
        for (var p = 0; p < inPoints.length; ++p) {
            pointsBySource[String(inPoints[p].sourceId)] = inPoints[p]
        }

        // baseline = 第一個選取的 source；缺失／非數字／0 一律不改用後續 source。
        var baselinePoint = pointsBySource.hasOwnProperty(baselineId) ? pointsBySource[baselineId] : undefined
        var baselineDuplicate = baselinePoint !== undefined
                                && String(baselinePoint.comparisonIssue || "") === "duplicate_dqe_id"
        var baselineRaw = baselinePoint === undefined ? null : _rawString(baselinePoint.value)
        var baselineParsed = baselineRaw === null ? { ok: false, value: 0 } : parseNumeric(baselineRaw)
        var baselineUnitKey = unitTrim(baselinePoint === undefined ? "" : baselinePoint.unit)

        // 單位一致性：只在 baseline 存在時，比對「存在」的點對 baseline 單位。
        // baseline 缺失時由 baseline_missing 主導，不另報單位問題。
        var unitMismatch = false
        if (baselinePoint !== undefined && !baselineDuplicate) {
            for (var u = 0; u < orderedIds.length; ++u) {
                var up = pointsBySource[orderedIds[u]]
                if (up === undefined) {
                    continue
                }
                if (unitTrim(up.unit) !== baselineUnitKey) {
                    unitMismatch = true
                    break
                }
            }
        }

        var seriesLabels = []
        if (baselineDuplicate) {
            // duplicate baseline 不能被 first/last row 偷換，整條 series 依 規則 N/A。
            seriesLabels.push("duplicate_dqe_id")
        } else if (baselinePoint === undefined) {
            seriesLabels.push("baseline_missing")
        } else if (!baselineParsed.ok) {
            seriesLabels.push("baseline_non_numeric")
        } else if (baselineParsed.value === 0) {
            seriesLabels.push("baseline_zero")
        }
        if (unitMismatch) {
            seriesLabels.push("unit_mismatch")
        }
        var canNormalize = seriesLabels.length === 0
        var seriesIssue = _seriesIssueLabel(seriesLabels)
        for (var li = 0; li < seriesLabels.length; ++li) {
            issues.push({ dqeId: dqeId, sourceId: null, kind: seriesLabels[li] })
        }

        var points = []
        var cells = []
        for (var o = 0; o < orderedIds.length; ++o) {
            var sid = orderedIds[o]
            var point = pointsBySource[sid]
            var present = point !== undefined
            var rawValue = present ? _rawString(point.value) : null
            var rawUnit = present ? _rawString(point.unit) : null
            var status = present ? _rawString(point.status) : null
            var normalized = null
            var pointIssue = null
            var upstreamIssue = present && point.comparisonIssue !== undefined
                                ? String(point.comparisonIssue) : ""

            if (!present) {
                pointIssue = "missing_dqe_id"
            } else if (upstreamIssue === "duplicate_dqe_id") {
                // duplicate 的 safe raw rows 只供表格/說明；此 point 永遠不是可比較 numeric point。
                pointIssue = "duplicate_dqe_id"
            } else {
                var parsed = parseNumeric(rawValue)
                if (!parsed.ok) {
                    pointIssue = "non_numeric"
                } else if (canNormalize) {
                    normalized = parsed.value / baselineParsed.value * 100
                }
            }

            if (pointIssue !== null) {
                issues.push({ dqeId: dqeId, sourceId: sid, kind: pointIssue })
            }
            points.push({
                sourceId: sid,
                rawValue: rawValue,
                rawUnit: rawUnit,
                normalizedValue: normalized,
                status: status,
                statusRaw: present && point.statusRaw !== undefined ? _rawString(point.statusRaw) : null,
                duplicateRows: present && point.duplicateRows !== undefined ? point.duplicateRows : [],
                issue: pointIssue
            })
            cells.push({ sourceId: sid, rawValue: rawValue, rawUnit: rawUnit, status: status,
                         statusRaw: present && point.statusRaw !== undefined ? _rawString(point.statusRaw) : null,
                         duplicateRows: present && point.duplicateRows !== undefined ? point.duplicateRows : [],
                         issue: pointIssue })
        }

        series.push({
            dqeId: dqeId,
            title: title,
            unit: seriesUnit,
            baselineSourceId: baselineId,
            points: points,
            issue: seriesIssue
        })
        rawRows.push({ dqeId: dqeId, title: title, unit: seriesUnit, cells: cells })
    }

    return {
        orderedSources: orderedSources,
        baselineSourceId: baselineId,
        series: series,
        rawRows: rawRows,
        issues: issues
    }
}

// 由已建構模型取出指定 (dqeId, sourceId) 的 selected-point detail；找不到回傳 null。
function selectedDetail(model, dqeId, sourceId) {
    if (model === null || model === undefined) {
        return null
    }
    var wantDqe = dqeId === undefined || dqeId === null ? "" : String(dqeId)
    var wantSource = sourceId === undefined || sourceId === null ? "" : String(sourceId)
    if (wantDqe.length === 0 || wantSource.length === 0) {
        return null
    }
    var allSeries = Array.isArray(model.series) ? model.series : []
    for (var i = 0; i < allSeries.length; ++i) {
        var s = allSeries[i]
        if (s.dqeId !== wantDqe) {
            continue
        }
        for (var j = 0; j < s.points.length; ++j) {
            var point = s.points[j]
            if (point.sourceId === wantSource) {
                return {
                    dqeId: s.dqeId,
                    title: s.title,
                    unit: s.unit,
                    baselineSourceId: s.baselineSourceId,
                    sourceId: point.sourceId,
                    rawValue: point.rawValue,
                    rawUnit: point.rawUnit,
                    normalizedValue: point.normalizedValue,
                    status: point.status,
                    statusRaw: point.statusRaw,
                    duplicateRows: point.duplicateRows,
                    issue: point.issue
                }
            }
        }
    }
    return null
}
