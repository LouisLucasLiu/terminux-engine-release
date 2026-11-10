// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 正規化趨勢圖的 renderer 邊界；線段與互動標記共用同一繪圖區及座標映射。
// 缺值會切斷折線，不可比較的指標僅顯示 N/A 圖例。

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes
import "../theme"
import "../i18n"

Item {
    id: chart
    objectName: "normalizedTrendChart"

    // ── 輸入 ──
    property var orderedSources: []   // [{ sourceId, shortLabel, sourceKind, ... }]（使用者順序）
    property var series: []           // [{ dqeId, title, color, issue, points:[{ sourceId, normalizedValue|null, status, issue, rawValue, rawUnit }] }]
    property real yMin: 80
    property real yMax: 130
    property string selectedDqeId: ""
    property string selectedSourceId: ""
    // production staging 時去除空狀態的 "(mock)" 標註。
    property bool productionSource: false

    signal pointClicked(string dqeId, string sourceId)

    readonly property int sourceCount: Array.isArray(orderedSources) ? orderedSources.length : 0
    // 至少兩個來源才繪圖。
    readonly property bool ready: sourceCount >= 2
    readonly property var _markerList: _buildMarkers()
    readonly property int drawableSeriesCount: _drawableCount()
    // 線段建立後至少跨過一次排版，才允許擷取目前資料的畫面。
    readonly property bool captureReady: _captureComplete && ready && width > 0 && height > 0
                                         && _plotRect.width > 0 && _plotRect.height > 0
    readonly property var _segments: _buildSegments()
    readonly property var _ticks: _yTicks()
    readonly property real _yLabelWidth: {
        let widest = 0
        for (let index = 0; index < _ticks.length; ++index) {
            widest = Math.max(widest, axisMetrics.advanceWidth(String(Math.round(_ticks[index]))))
        }
        return Math.ceil(widest)
    }
    readonly property rect _plotRect: Qt.rect(
        _yLabelWidth + DqeTheme.spacingS, DqeTheme.spacingM,
        Math.max(0, plotArea.width - _yLabelWidth - DqeTheme.spacingS - DqeTheme.spacingM),
        Math.max(0, plotArea.height - axisMetrics.height - DqeTheme.spacingM * 2))

    FontMetrics {
        id: axisMetrics
        font.family: DqeTheme.fontFamily
        font.pixelSize: 10
    }

    function _statusColor(status) {
        switch (status) {
        case "Pass": return DqeTheme.pass
        case "Warn": return DqeTheme.warn
        case "Fail": return DqeTheme.fail
        default: return DqeTheme.unknown
        }
    }

    function _pointForSource(oneSeries, sourceId) {
        const points = Array.isArray(oneSeries.points) ? oneSeries.points : []
        for (let index = 0; index < points.length; ++index) {
            if (String(points[index].sourceId) === sourceId) {
                return points[index]
            }
        }
        return null
    }

    function _isDrawable(oneSeries) {
        // issue 非 null（zero baseline／unit mismatch／baseline 缺失或非數字）→ N/A，不繪線。
        return oneSeries.issue === null || oneSeries.issue === undefined
    }

    function _drawableCount() {
        let count = 0
        const all = Array.isArray(series) ? series : []
        for (let index = 0; index < all.length; ++index) {
            if (_isDrawable(all[index])) {
                ++count
            }
        }
        return count
    }

    // 攤平為 overlay 用的可繪點清單（跳過 missing/null → 形成缺口）。
    function _buildMarkers() {
        const markers = []
        if (!ready) {
            return markers
        }
        const allSeries = Array.isArray(series) ? series : []
        const sources = orderedSources
        for (let s = 0; s < allSeries.length; ++s) {
            const oneSeries = allSeries[s]
            if (!_isDrawable(oneSeries)) {
                continue
            }
            for (let i = 0; i < sources.length; ++i) {
                const point = _pointForSource(oneSeries, String(sources[i].sourceId))
                if (point === null || point.normalizedValue === null || point.normalizedValue === undefined) {
                    continue
                }
                markers.push({
                    dqeId: String(oneSeries.dqeId),
                    sourceId: String(sources[i].sourceId),
                    xIndex: i,
                    yValue: Number(point.normalizedValue),
                    color: String(oneSeries.color),
                    status: point.status === undefined || point.status === null ? "" : String(point.status)
                })
            }
        }
        return markers
    }

    // 線段、格線、來源標籤與選取標記一律使用 plotArea 內的同一座標。
    function _xpix(xIndex) {
        return _plotRect.x + (xIndex + 0.5) / Math.max(1, sourceCount) * _plotRect.width
    }

    function _ypix(value) {
        return _plotRect.y + (yMax - value) / Math.max(0.0001, yMax - yMin) * _plotRect.height
    }

    function _niceStep(range) {
        if (range <= 40) { return 10 }
        if (range <= 100) { return 20 }
        return 50
    }

    function _yTicks() {
        const ticks = []
        const step = _niceStep(yMax - yMin)
        let value = Math.ceil(yMin / step) * step
        while (value <= yMax + 0.0001) {
            ticks.push(value)
            value += step
        }
        return ticks
    }

    function _buildSegments() {
        if (!ready) {
            return []
        }
        const segments = [{
            dqeId: "", color: String(DqeTheme.borderStrong), strokeWidth: 1,
            points: [{ x: 0, y: 100 }, { x: sourceCount - 1, y: 100 }]
        }]
        const allSeries = Array.isArray(series) ? series : []
        for (let seriesIndex = 0; seriesIndex < allSeries.length; ++seriesIndex) {
            const oneSeries = allSeries[seriesIndex]
            if (!_isDrawable(oneSeries)) {
                continue
            }
            let points = []
            for (let sourceIndex = 0; sourceIndex < sourceCount; ++sourceIndex) {
                const point = _pointForSource(oneSeries, String(orderedSources[sourceIndex].sourceId))
                if (point !== null && point.normalizedValue !== null
                        && point.normalizedValue !== undefined) {
                    points.push({ x: sourceIndex, y: Number(point.normalizedValue) })
                } else {
                    if (points.length >= 2) {
                        segments.push({ dqeId: String(oneSeries.dqeId), color: String(oneSeries.color),
                                          strokeWidth: 3, points: points })
                    }
                    points = []
                }
            }
            // 單獨一點由 overlay 標記呈現，不產生無長度的折線。
            if (points.length >= 2) {
                segments.push({ dqeId: String(oneSeries.dqeId), color: String(oneSeries.color),
                                  strokeWidth: 3, points: points })
            }
        }
        return segments
    }

    function _segmentPath(points) {
        const path = []
        for (let index = 0; index < points.length; ++index) {
            path.push(Qt.point(_xpix(points[index].x), _ypix(points[index].y)))
        }
        return path
    }

    property bool _componentReady: false
    property bool _captureComplete: false
    property int _revision: 0
    property int _layoutRevision: -1

    function _invalidateCapture() {
        _captureComplete = false
        ++_revision
        _layoutRevision = -1
    }

    function _segmentsEstablished() {
        if (segmentRepeater.count !== _segments.length) {
            return false
        }
        for (let index = 0; index < _segments.length; ++index) {
            const segment = segmentRepeater.itemAt(index)
            if (segment === null) {
                return false
            }
            // Repeater 將 modelData 轉成值物件；比較內容以確認目前資料，而非 JS 身分。
            const actual = segment.modelData
            const expected = _segments[index]
            if (actual.dqeId !== expected.dqeId || actual.color !== expected.color
                    || actual.strokeWidth !== expected.strokeWidth
                    || actual.points.length !== expected.points.length) {
                return false
            }
            for (let pointIndex = 0; pointIndex < expected.points.length; ++pointIndex) {
                if (actual.points[pointIndex].x !== expected.points[pointIndex].x
                        || actual.points[pointIndex].y !== expected.points[pointIndex].y) {
                    return false
                }
            }
            segment.ensurePolished()
            if (segment.status !== Shape.Ready) {
                return false
            }
        }
        return true
    }

    FrameAnimation {
        running: chart._componentReady && chart.ready && chart.visible
                 && chart.width > 0 && chart.height > 0
                 && chart._plotRect.width > 0 && chart._plotRect.height > 0
                 && !chart.captureReady
        onTriggered: {
            legendRow.ensurePolished()
            plotArea.ensurePolished()
            if (!chart._segmentsEstablished()) {
                chart._layoutRevision = -1
                return
            }
            // 連續兩幀須屬於同一份資料與尺寸；中間經過場景排版才完成握手。
            if (chart._layoutRevision === chart._revision) {
                chart._captureComplete = true
            } else {
                chart._layoutRevision = chart._revision
            }
        }
    }

    onOrderedSourcesChanged: _invalidateCapture()
    onSeriesChanged: _invalidateCapture()
    onYMinChanged: _invalidateCapture()
    onYMaxChanged: _invalidateCapture()
    onSelectedDqeIdChanged: _invalidateCapture()
    onSelectedSourceIdChanged: _invalidateCapture()
    onWidthChanged: _invalidateCapture()
    onHeightChanged: _invalidateCapture()
    on_PlotRectChanged: _invalidateCapture()
    on_SegmentsChanged: _invalidateCapture()
    Component.onCompleted: {
        _componentReady = true
        _invalidateCapture()
    }

    // ── 未達繪圖條件時的教學狀態 ──
    Item {
        objectName: "trendInstructionalState"
        anchors.fill: parent
        visible: !chart.ready
        Text {
            anchors.centerIn: parent
            width: parent.width - DqeTheme.spacingXl * 2
            horizontalAlignment: Text.AlignHCenter
            text: chart.productionSource
                  ? DqeI18n.t("Select at least two sources to plot a normalized trend.", "請選取至少兩個來源以繪製正規化趨勢。", "请选取至少两个来源以绘制归一化趋势。")
                  : DqeI18n.t("Select at least two sources to plot a normalized trend (mock).", "請選取至少兩個來源以繪製正規化趨勢（mock）。", "请选取至少两个来源以绘制归一化趋势（mock）。")
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 14
            wrapMode: Text.Wrap
        }
    }

    // ── legend（頂端）──
    Row {
        id: legendRow
        objectName: "trendLegend"
        visible: chart.ready
        anchors.top: parent.top
        anchors.topMargin: DqeTheme.spacingS
        x: plotArea.x + chart._plotRect.x + chart._plotRect.width / 2 - width / 2
        spacing: DqeTheme.spacingL
        Repeater {
            model: chart.ready ? chart.series : []
            delegate: Row {
                id: legendEntry
                required property var modelData
                spacing: DqeTheme.spacingXs
                Rectangle {
                    width: 10; height: 10; radius: 5
                    anchors.verticalCenter: parent.verticalCenter
                    color: legendEntry.modelData.color === undefined ? DqeTheme.mutedText : String(legendEntry.modelData.color)
                    opacity: (legendEntry.modelData.issue === null || legendEntry.modelData.issue === undefined) ? 1.0 : 0.4
                }
                Text {
                    text: String(legendEntry.modelData.dqeId)
                          + ((legendEntry.modelData.issue === null || legendEntry.modelData.issue === undefined) ? "" : " · N/A")
                    color: (legendEntry.modelData.issue === null || legendEntry.modelData.issue === undefined)
                           ? DqeTheme.text : DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                }
            }
        }
    }

    // ── 繪圖區（Shapes 與 overlay 同尺寸疊放）──
    Item {
        id: plotArea
        objectName: "trendPlotArea"
        visible: chart.ready
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: legendRow.bottom
        anchors.bottom: parent.bottom
        anchors.topMargin: DqeTheme.spacingS
        anchors.leftMargin: DqeTheme.spacingS
        anchors.rightMargin: DqeTheme.spacingM
        anchors.bottomMargin: DqeTheme.spacingS

        Item {
            id: rendererHost
            objectName: "trendRendererHost"
            anchors.fill: parent
            clip: true

            Repeater {
                id: segmentRepeater
                model: chart._segments
                onItemAdded: chart._invalidateCapture()
                onItemRemoved: chart._invalidateCapture()
                delegate: Shape {
                    id: segmentShape
                    required property var modelData
                    required property int index
                    objectName: index === 0 ? "trendBaseline" : "trendSegment_" + modelData.dqeId + "_" + index
                    anchors.fill: parent
                    asynchronous: false

                    ShapePath {
                        strokeColor: segmentShape.modelData.color
                        strokeWidth: segmentShape.modelData.strokeWidth
                        fillColor: "transparent"
                        capStyle: ShapePath.RoundCap
                        joinStyle: ShapePath.RoundJoin
                        startX: chart._xpix(segmentShape.modelData.points[0].x)
                        startY: chart._ypix(segmentShape.modelData.points[0].y)
                        PathPolyline {
                            path: chart._segmentPath(segmentShape.modelData.points)
                            onPathChanged: chart._invalidateCapture()
                        }
                    }
                }
            }
        }

        // ── overlay：y 格線/標籤、x 來源標籤、baseline 標籤、狀態標記、選取強調、可點擊 ──
        Item {
            id: overlay
            anchors.fill: parent

            // y 格線與標籤
            Repeater {
                model: chart.ready ? chart._ticks : []
                delegate: Item {
                    id: yTick
                    required property var modelData
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 1
                    y: chart._ypix(Number(yTick.modelData))
                    Rectangle {
                        anchors.left: parent.left
                        anchors.leftMargin: chart._plotRect.x
                        anchors.right: parent.right
                        anchors.rightMargin: plotArea.width - chart._plotRect.x - chart._plotRect.width
                        height: 1
                        color: Number(yTick.modelData) === 100 ? DqeTheme.borderStrong : DqeTheme.border
                        opacity: Number(yTick.modelData) === 100 ? 0.9 : 0.4
                    }
                    Text {
                        anchors.right: parent.left
                        anchors.rightMargin: -chart._plotRect.x + DqeTheme.spacingXs
                        anchors.verticalCenter: parent.verticalCenter
                        text: String(Math.round(Number(yTick.modelData)))
                        color: DqeTheme.mutedText
                        font.family: DqeTheme.fontFamily
                        font.pixelSize: 10
                    }
                }
            }

            // x 來源標籤（底部）
            Repeater {
                model: chart.ready ? chart.orderedSources : []
                delegate: Text {
                    required property var modelData
                    required property int index
                    x: chart._xpix(index) - width / 2
                    y: chart._plotRect.y + chart._plotRect.height + DqeTheme.spacingXs
                    text: modelData.shortLabel === undefined ? String(modelData.sourceId)
                                                             : String(modelData.shortLabel)
                    color: DqeTheme.mutedText
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 10
                }
            }

            // 狀態標記（線色填充 + 狀態色外框）；可點擊觸發選取。
            Repeater {
                model: chart._markerList
                delegate: Rectangle {
                    id: marker
                    required property var modelData
                    readonly property bool isSelected: modelData.dqeId === chart.selectedDqeId
                                                       && modelData.sourceId === chart.selectedSourceId
                    objectName: "trendMarker_" + modelData.dqeId + "_" + modelData.sourceId
                    width: isSelected ? 16 : 12
                    height: width
                    radius: width / 2
                    x: chart._xpix(modelData.xIndex) - width / 2
                    y: chart._ypix(modelData.yValue) - width / 2
                    color: String(modelData.color)
                    border.width: 2
                    border.color: chart._statusColor(modelData.status)
                    z: isSelected ? 2 : 1

                    MouseArea {
                        anchors.fill: parent
                        anchors.margins: -3
                        onClicked: (mouse) => {
                            mouse.accepted = true
                            chart.pointClicked(marker.modelData.dqeId, marker.modelData.sourceId)
                        }
                    }
                }
            }

            // 選取點的強調環（在標記外圈）
            Repeater {
                model: chart._markerList
                delegate: Rectangle {
                    required property var modelData
                    visible: modelData.dqeId === chart.selectedDqeId
                             && modelData.sourceId === chart.selectedSourceId
                    objectName: "trendEmphasis"
                    width: 24; height: 24; radius: 12
                    x: chart._xpix(modelData.xIndex) - width / 2
                    y: chart._ypix(modelData.yValue) - width / 2
                    color: "transparent"
                    border.width: 2
                    border.color: DqeTheme.text
                    z: 3
                }
            }
        }

        // ready（≥2 sources）但無可繪 series 的狀態：區分「尚未勾選 DQE」與「有選但全 N/A」。
        Text {
            objectName: "trendNoSeriesState"
            anchors.centerIn: parent
            visible: chart.ready && chart.drawableSeriesCount === 0
            width: parent.width - DqeTheme.spacingXl * 2
            horizontalAlignment: Text.AlignHCenter
            readonly property bool _noSeriesSelected: !Array.isArray(chart.series) || chart.series.length === 0
            text: _noSeriesSelected
                  ? DqeI18n.t("Select one or more DQE metrics to plot a normalized trend.", "請勾選一個以上的 DQE 指標以繪製正規化趨勢。", "请勾选一个以上的 DQE 指标以绘制归一化趋势。")
                  : (chart.productionSource
                     ? DqeI18n.t("No comparable trend to plot — check baseline and unit issues.", "無可比較的趨勢 — 請檢查 baseline 與單位問題。", "无可比较的趋势 — 请检查 baseline 与单位问题。")
                     : DqeI18n.t("No comparable trend to plot — check baseline and unit issues (mock).", "無可比較的趨勢 — 請檢查 baseline 與單位問題（mock）。", "无可比较的趋势 — 请检查 baseline 与单位问题（mock）。"))
            color: _noSeriesSelected ? DqeTheme.mutedText : DqeTheme.warn
            font.family: DqeTheme.fontFamily
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
    }
}
