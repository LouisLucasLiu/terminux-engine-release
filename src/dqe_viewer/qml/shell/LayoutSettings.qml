// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import QtQuick
import QtCore

// 每個 workspace（Archived / Temporary / Compare）獨立持久化版面尺寸與 collapse 狀態。
// 以 workspaceKey 作為 key 前綴達成隔離；載入時 clamp 至合法界限，
// 遇到損毀/超界值安全 fallback 到預設。儲存後端為 QtCore Settings（QSettings）。
Item {
    id: ls

    property string workspaceKey: "Archived"

    // ── 界限與預設（單一來源）──
    readonly property int minLeft: 180
    readonly property int maxLeft: 480
    readonly property int defLeft: 260
    readonly property int minRight: 240
    readonly property int maxRight: 520
    readonly property int defRight: 340
    readonly property int minEvidence: 140
    readonly property int maxEvidence: 520
    readonly property int defEvidence: 255

    // ── 目前值（持久化目標）──
    property int leftWidth: defLeft
    property int rightWidth: defRight
    property int evidenceHeight: defEvidence
    property bool leftCollapsed: false
    property bool rightCollapsed: false

    // 載入期間抑制寫回，避免 clamp 後又立即覆寫。
    // 預設 true：在首次 load() 完成前（含建構期初始屬性值），一律不得寫回，
    // 否則初始預設值會在 load 讀取前就 clobber 磁碟上已持久化的值。
    property bool _loading: true

    Settings {
        id: store
        category: "dqe_workspace_layout"
    }

    function _clampInt(v, lo, hi, def) {
        var n = Number(v);
        if (!isFinite(n)) {
            return def;
        }
        n = Math.round(n);
        if (n < lo || n > hi) {
            return Math.max(lo, Math.min(hi, n));
        }
        return n;
    }

    function _toBool(v, def) {
        if (typeof v === "boolean") {
            return v;
        }
        if (v === "true" || v === 1 || v === "1") {
            return true;
        }
        if (v === "false" || v === 0 || v === "0") {
            return false;
        }
        return def;
    }

    function _k(name) {
        return workspaceKey + "/" + name;
    }

    function load() {
        _loading = true;
        leftWidth = _clampInt(store.value(_k("leftWidth"), defLeft), minLeft, maxLeft, defLeft);
        rightWidth = _clampInt(store.value(_k("rightWidth"), defRight), minRight, maxRight, defRight);
        evidenceHeight = _clampInt(store.value(_k("evidenceHeight"), defEvidence), minEvidence, maxEvidence, defEvidence);
        leftCollapsed = _toBool(store.value(_k("leftCollapsed"), false), false);
        rightCollapsed = _toBool(store.value(_k("rightCollapsed"), false), false);
        _loading = false;
    }

    function _save(name, v) {
        if (!_loading) {
            store.setValue(_k(name), v);
        }
    }

    onLeftWidthChanged: _save("leftWidth", leftWidth)
    onRightWidthChanged: _save("rightWidth", rightWidth)
    onEvidenceHeightChanged: _save("evidenceHeight", evidenceHeight)
    onLeftCollapsedChanged: _save("leftCollapsed", leftCollapsed)
    onRightCollapsedChanged: _save("rightCollapsed", rightCollapsed)
    onWorkspaceKeyChanged: load()

    Component.onCompleted: load()
}
