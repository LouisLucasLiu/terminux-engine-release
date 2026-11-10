// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Temporary workspace 的 lifecycle 動作列。
// mock 模式只發出 signal；production 模式僅透過注入 operation controller 執行核准的 lifecycle operation。

import QtQuick
import QtQuick.Controls
import "../theme"
import "../i18n"

Item {
    id: bar
    objectName: "lifecycleActionBar"

    property string jobKey: ""
    property int retentionDays: 14
    property var checkinVersions: []
    // production 注入共同 operation controller；Retention 是全域 Temporary policy，Purge 仍維持 mock-only。
    property bool productionSource: false
    property var operationProvider: null
    property bool checkinEligible: false
    // retention editor 在 Apply 時把使用者挑選的 mock 值寫進此 property，
    // 供 owner 讀取後更新「僅顯示用」的 retention；本元件自身不改任何持久化狀態。
    property int requestedRetentionDays: retentionDays

    signal checkinRequested(string jobKey)
    signal retentionEditRequested()
    signal purgeRequested(string jobKey)

    readonly property bool hasJob: jobKey.length > 0
    readonly property bool operationRunning: productionSource && operationProvider !== null
                                         && operationProvider.running

    implicitWidth: 240
    implicitHeight: Math.max(contentColumn.height, productionColumn.height)

    component ActionButton: AbstractButton {
        id: actionButton
        property color accentColor: DqeTheme.accent
        property bool danger: false
        height: 30
        focusPolicy: Qt.StrongFocus
        opacity: enabled ? 1.0 : 0.45
        background: Rectangle {
            radius: DqeTheme.radiusSmall
            color: actionButton.hovered && actionButton.enabled ? DqeTheme.panelRaised : "transparent"
            border.width: actionButton.activeFocus ? DqeTheme.borderWidthStrong : DqeTheme.borderWidth
            border.color: actionButton.danger ? DqeTheme.fail
                                              : (actionButton.activeFocus ? DqeTheme.accent : DqeTheme.border)
        }
        contentItem: Text {
            text: actionButton.text
            color: actionButton.danger ? DqeTheme.fail : actionButton.accentColor
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
    }

    Column {
        id: contentColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        spacing: DqeTheme.spacingXs
        visible: !bar.productionSource
        height: visible ? implicitHeight : 0

        Text {
            width: parent.width
            text: DqeI18n.t("LIFECYCLE", "生命週期", "生命周期")
            color: DqeTheme.accent
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            font.letterSpacing: 0.5
        }

        Text {
            objectName: "lifecycleMockMarker"
            width: parent.width
            text: DqeI18n.t("MOCK · non-production · resets on restart", "MOCK · 非正式 · 重啟即重置", "MOCK · 非正式 · 重启即重置")
            color: DqeTheme.warn
            font.family: DqeTheme.fontFamily
            font.pixelSize: 10
            wrapMode: Text.Wrap
        }

        ActionButton {
            objectName: "lifecycleCheckinButton"
            width: parent.width
            text: DqeI18n.t("Check-in whole job (mock)", "簽入整個 job（mock）", "签入整个 job（mock）")
            enabled: bar.hasJob
            onClicked: checkinDialog.open()
        }

        ActionButton {
            objectName: "lifecyclePurgeButton"
            width: parent.width
            text: DqeI18n.t("Purge… (mock)", "清除…（mock）", "清除…（mock）")
            danger: true
            enabled: bar.hasJob
            onClicked: purgeDialog.open()
        }

        Item {
            width: parent.width
            height: retentionRow.implicitHeight
            Row {
                id: retentionRow
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: DqeTheme.spacingS
                Text {
                    objectName: "lifecycleRetentionValue"
                    width: parent.width - retentionEditButton.width - DqeTheme.spacingS
                    text: DqeI18n.t("Retention N: ", "保留 N：", "保留 N：") + bar.retentionDays + " " + DqeI18n.t("days", "天", "天")
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                    height: retentionEditButton.height
                }
                ActionButton {
                    id: retentionEditButton
                    objectName: "lifecycleRetentionEditButton"
                    width: 64
                    text: DqeI18n.t("Edit", "編輯", "编辑")
                    onClicked: {
                        retentionDialog.pendingDays = bar.retentionDays
                        retentionDialog.open()
                    }
                }
            }
        }
    }

    // production lifecycle 只透過 injected controller 執行；Purge 不復用 mock dialog。
    Column {
        id: productionColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        spacing: DqeTheme.spacingXs
        visible: bar.productionSource && bar.operationProvider !== null
        height: visible ? implicitHeight : 0

        Text {
            width: parent.width
            text: DqeI18n.t("CHECK-IN", "簽入", "签入")
            color: DqeTheme.accent
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            font.letterSpacing: 0.5
        }

        Text {
            objectName: "productionCheckinStatus"
            width: parent.width
            visible: bar.operationProvider !== null
                     && (bar.operationProvider.operationMessage !== ""
                         || bar.operationProvider.operationState !== "idle")
            text: {
                if (bar.operationProvider === null)
                    return ""
                let summary = bar.operationProvider.operationMessage
                if (bar.operationProvider.lastResult !== "not_run")
                    summary += " Result: " + bar.operationProvider.lastResult + "."
                if (bar.operationProvider.failureCode !== "")
                    summary += " Code: " + bar.operationProvider.failureCode + "."
                return summary
            }
            color: bar.operationProvider !== null && bar.operationProvider.operationState === "partial"
                   ? DqeTheme.warn
                   : (bar.operationProvider !== null && (bar.operationProvider.operationState === "failed"
                                                         || bar.operationProvider.operationState === "refused")
                      ? DqeTheme.fail : DqeTheme.mutedText)
            font.family: DqeTheme.fontFamily
            font.pixelSize: 10
            wrapMode: Text.Wrap
        }

        ActionButton {
            objectName: "productionCheckinButton"
            width: parent.width
            text: bar.operationRunning ? DqeI18n.t("Check-in running…", "簽入執行中…", "签入执行中…") : DqeI18n.t("Check in whole job", "簽入整個 job", "签入整个 job")
            enabled: bar.checkinEligible && !bar.operationRunning
            onClicked: productionCheckinDialog.open()
        }

        Rectangle {
            width: parent.width
            height: DqeTheme.borderWidth
            color: DqeTheme.border
        }

        Text {
            width: parent.width
            text: DqeI18n.t("TEMPORARY RETENTION POLICY", "暫存保留政策", "暂存保留策略")
            color: DqeTheme.accent
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            font.letterSpacing: 0.5
        }

        Text {
            objectName: "productionRetentionPolicyLabel"
            width: parent.width
            text: DqeI18n.t("Global policy for all Temporary jobs", "適用所有暫存 job 的全域政策", "适用所有暂存 job 的全局策略")
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 10
            wrapMode: Text.Wrap
        }

        Text {
            objectName: "productionRetentionPolicyValue"
            width: parent.width
            text: bar.operationProvider !== null && bar.operationProvider.authoritativeRetentionDays > 0
                  ? bar.operationProvider.authoritativeRetentionDays + " days"
                  : DqeI18n.t("Load authoritative policy before editing", "編輯前先載入權威政策值", "编辑前先载入权威策略值")
            color: DqeTheme.text
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            wrapMode: Text.Wrap
        }

        ActionButton {
            objectName: "productionRetentionEditButton"
            width: parent.width
            text: bar.operationRunning ? DqeI18n.t("Loading policy…", "載入政策中…", "载入策略中…") : DqeI18n.t("Edit policy", "編輯政策", "编辑策略")
            enabled: bar.operationProvider !== null && bar.operationProvider.retentionEnabled
            onClicked: bar.operationProvider.beginRetentionEditor()
        }

        Rectangle {
            width: parent.width
            height: DqeTheme.borderWidth
            color: DqeTheme.border
        }

        Text {
            width: parent.width
            text: DqeI18n.t("TEMPORARY PURGE", "暫存清除", "暂存清除")
            color: DqeTheme.fail
            font.family: DqeTheme.fontFamily
            font.pixelSize: 11
            font.letterSpacing: 0.5
        }

        Text {
            objectName: "productionPurgeScopeLabel"
            width: parent.width
            text: DqeI18n.t("Preview first. Only the selected job or jobs expired now can be purged.", "先預覽。只有選取的 job 或已過期的 job 可被清除。", "先预览。只有选取的 job 或已过期的 job 可被清除。")
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 10
            wrapMode: Text.Wrap
        }

        ActionButton {
            objectName: "productionPurgeSelectedPreviewButton"
            width: parent.width
            danger: true
            text: bar.operationRunning ? DqeI18n.t("Purge preview running…", "清除預覽執行中…", "清除预览执行中…") : DqeI18n.t("Preview selected job purge", "預覽清除選取 job", "预览清除选取 job")
            enabled: bar.operationProvider !== null && bar.operationProvider.purgeSelectedEnabled
            onClicked: bar.operationProvider.beginPurgePreview("selected_jobs")
        }

        ActionButton {
            objectName: "productionPurgeExpiredPreviewButton"
            width: parent.width
            danger: true
            text: bar.operationRunning ? DqeI18n.t("Purge preview running…", "清除預覽執行中…", "清除预览执行中…") : DqeI18n.t("Preview expired jobs purge", "預覽清除已過期 job", "预览清除已过期 job")
            enabled: bar.operationProvider !== null && bar.operationProvider.purgeExpiredEnabled
            onClicked: bar.operationProvider.beginPurgePreview("expired_now")
        }
    }

    // 使用者明確提供 context 後才能 mandatory dry-run；service 會驗證兩個目錄存在。
    Dialog {
        id: productionCheckinDialog
        objectName: "productionCheckinDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: bar.operationRunning ? Popup.NoAutoClose : Popup.CloseOnEscape
        anchors.centerIn: parent
        width: parent ? Math.min(520, Math.round(parent.width * 0.52)) : 520
        padding: DqeTheme.spacingL
        height: implicitContentHeight + topPadding + bottomPadding

        background: Rectangle {
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidthStrong
            border.color: DqeTheme.accent
            radius: DqeTheme.radiusPanel
        }

        contentItem: Column {
            width: productionCheckinDialog.availableWidth
            spacing: DqeTheme.spacingS
            Text {
                text: DqeI18n.t("Check-in dry-run", "簽入試跑", "签入试跑")
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 14
            }
            Text {
                width: parent.width
                text: DqeI18n.t("Job: ", "Job：", "Job：") + (bar.hasJob ? bar.jobKey : "—")
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
            }
            Text {
                width: parent.width
                text: DqeI18n.t("The target version must already exist. This preview does not create a version.", "目標版本必須已存在。此預覽不會建立版本。", "目标版本必须已存在。此预览不会创建版本。")
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            TextField {
                id: productionWorkspaceField
                objectName: "productionCheckinWorkspaceField"
                width: parent.width
                placeholderText: DqeI18n.t("Workspace root", "Workspace 根目錄", "Workspace 根目录")
                text: bar.operationProvider !== null ? bar.operationProvider.workspaceRoot : ""
                enabled: !bar.operationRunning
            }
            TextField {
                id: productionVersionField
                objectName: "productionCheckinVersionField"
                width: parent.width
                placeholderText: DqeI18n.t("Existing target version (relative path)", "既有目標版本（相對路徑）", "既有目标版本（相对路径）")
                text: bar.operationProvider !== null ? bar.operationProvider.targetVersionPath : ""
                enabled: !bar.operationRunning
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    width: 120
                    text: DqeI18n.t("Cancel", "取消", "取消")
                    enabled: !bar.operationRunning
                    onClicked: productionCheckinDialog.close()
                }
                ActionButton {
                    objectName: "productionCheckinDryRunButton"
                    width: 170
                    text: DqeI18n.t("Run dry-run", "執行試跑", "执行试跑")
                    accentColor: DqeTheme.pass
                    enabled: bar.checkinEligible && !bar.operationRunning
                    onClicked: {
                        if (bar.operationProvider.beginCheckinDryRun(productionWorkspaceField.text,
                                                                     productionVersionField.text)) {
                            productionCheckinDialog.close()
                        }
                    }
                }
            }
        }
    }

    // editor 只顯示 backend authoritative 天數；policy path 與格式不會進入 QML。
    Dialog {
        id: productionRetentionDialog
        objectName: "productionRetentionDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: bar.operationRunning ? Popup.NoAutoClose : Popup.CloseOnEscape
        anchors.centerIn: parent
        width: parent ? Math.min(480, Math.round(parent.width * 0.5)) : 480
        padding: DqeTheme.spacingL
        height: implicitContentHeight + topPadding + bottomPadding
        onClosed: {
            if (bar.operationProvider !== null && bar.operationProvider.retentionEditorReady
                    && !bar.operationRunning) {
                bar.operationProvider.cancelRetentionEditor()
            }
        }

        background: Rectangle {
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidthStrong
            border.color: DqeTheme.accent
            radius: DqeTheme.radiusPanel
        }

        contentItem: Column {
            width: productionRetentionDialog.availableWidth
            spacing: DqeTheme.spacingS
            Text {
                text: DqeI18n.t("Temporary retention policy", "暫存保留政策", "暂存保留策略")
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 14
            }
            Text {
                width: parent.width
                text: DqeI18n.t("This global policy applies to every Temporary job. Saving changes lifecycle display only; it never deletes data.", "此全域政策適用每個暫存 job。儲存只改變生命週期顯示，絕不刪除資料。", "此全局策略适用每个暂存 job。保存只改变生命周期显示，绝不删除数据。")
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            Text {
                width: parent.width
                text: DqeI18n.t("Authoritative current value: ", "權威目前值：", "权威当前值：")
                      + (bar.operationProvider === null ? "—"
                                                         : bar.operationProvider.authoritativeRetentionDays + " days")
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
            }
            TextField {
                id: productionRetentionDaysField
                objectName: "productionRetentionDaysField"
                width: parent.width
                placeholderText: "1–3650 days"
                inputMethodHints: Qt.ImhDigitsOnly
                validator: IntValidator { bottom: 1; top: 3650 }
                enabled: !bar.operationRunning
            }
            Text {
                width: parent.width
                text: DqeI18n.t("Allowed range: 1–3650 days. No Temporary records will be deleted.", "允許範圍：1–3650 天。不會刪除任何暫存記錄。", "允许范围：1–3650 天。不会删除任何暂存记录。")
                color: DqeTheme.faintText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    objectName: "productionRetentionCancelButton"
                    width: 120
                    text: DqeI18n.t("Cancel", "取消", "取消")
                    enabled: !bar.operationRunning
                    onClicked: {
                        bar.operationProvider.cancelRetentionEditor()
                        productionRetentionDialog.close()
                    }
                }
                ActionButton {
                    objectName: "productionRetentionSaveButton"
                    width: 180
                    text: bar.operationRunning ? DqeI18n.t("Saving…", "儲存中…", "保存中…") : DqeI18n.t("Save policy", "儲存政策", "保存策略")
                    accentColor: DqeTheme.pass
                    enabled: bar.operationProvider !== null && bar.operationProvider.retentionEditorReady
                             && !bar.operationRunning && productionRetentionDaysField.acceptableInput
                    onClicked: bar.operationProvider.saveRetentionPolicy(productionRetentionDaysField.text)
                }
            }
        }
    }

    // 只有相符且成功的 dry-run 加上明確確認後，才能執行 actual。
    Dialog {
        id: productionCheckinConfirmDialog
        objectName: "productionCheckinConfirmDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: bar.operationRunning ? Popup.NoAutoClose : Popup.CloseOnEscape
        anchors.centerIn: parent
        width: parent ? Math.min(560, Math.round(parent.width * 0.56)) : 560
        padding: DqeTheme.spacingL
        height: implicitContentHeight + topPadding + bottomPadding

        background: Rectangle {
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidthStrong
            border.color: DqeTheme.warn
            radius: DqeTheme.radiusPanel
        }

        contentItem: Column {
            width: productionCheckinConfirmDialog.availableWidth
            spacing: DqeTheme.spacingS
            Text {
                text: DqeI18n.t("Confirm check-in", "確認簽入", "确认签入")
                color: DqeTheme.warn
                font.family: DqeTheme.fontFamily
                font.pixelSize: 14
            }
            Text {
                width: parent.width
                text: bar.operationProvider === null ? "" : "Job: " + bar.operationProvider.jobKey
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
            }
            Text {
                width: parent.width
                text: bar.operationProvider === null ? "" : "Workspace: " + bar.operationProvider.workspaceRoot
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
                elide: Text.ElideMiddle
            }
            Text {
                width: parent.width
                text: bar.operationProvider === null ? "" : "Existing target: " + bar.operationProvider.targetVersionPath
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
                elide: Text.ElideMiddle
            }
            Text {
                text: "Planned files"
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
            }
            Repeater {
                model: bar.operationProvider === null ? [] : bar.operationProvider.files
                delegate: Text {
                    required property var modelData
                    width: parent.width
                    text: "• " + String(modelData.relpath)
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                    elide: Text.ElideMiddle
                }
            }
            Text {
                width: parent.width
                text: bar.operationProvider !== null && bar.operationProvider.conflicts.length > 0
                      ? "Existing target files"
                      : "Existing target files: none"
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
            }
            Repeater {
                model: bar.operationProvider === null ? [] : bar.operationProvider.conflicts
                delegate: Text {
                    required property var modelData
                    width: parent.width
                    text: "• " + String(modelData.relpath) + " — " + String(modelData.summary)
                    color: DqeTheme.warn
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                    elide: Text.ElideMiddle
                }
            }
            Text {
                width: parent.width
                text: bar.operationProvider !== null && bar.operationProvider.warnings.length > 0
                      ? "Warnings"
                      : "Warnings: none"
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
            }
            Repeater {
                model: bar.operationProvider === null ? [] : bar.operationProvider.warnings
                delegate: Text {
                    required property string modelData
                    width: parent.width
                    text: "• " + modelData
                    color: DqeTheme.warn
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }
            Text {
                width: parent.width
                text: "Actual check-in is irreversible here. Partial results require an explicit manual retry."
                color: DqeTheme.warn
                font.family: DqeTheme.fontFamily
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    objectName: "productionCheckinCancelConfirmationButton"
                    width: 120
                    text: DqeI18n.t("Cancel", "取消", "取消")
                    enabled: !bar.operationRunning
                    onClicked: {
                        bar.operationProvider.cancelConfirmation()
                        productionCheckinConfirmDialog.close()
                    }
                }
                ActionButton {
                    objectName: "productionCheckinConfirmButton"
                    width: 160
                    text: bar.operationRunning ? "Running…" : "Confirm check-in"
                    accentColor: DqeTheme.pass
                    enabled: bar.operationProvider !== null && bar.operationProvider.confirmationReady
                             && !bar.operationRunning
                    onClicked: {
                        if (bar.operationProvider.confirmCheckin()) {
                            productionCheckinConfirmDialog.close()
                        }
                    }
                }
            }
        }
    }

    Connections {
        target: bar.operationProvider
        function onOperationChanged() {
            if (bar.productionSource && bar.operationProvider !== null
                    && bar.operationProvider.confirmationReady) {
                productionCheckinConfirmDialog.open()
            }
            if (bar.productionSource && bar.operationProvider !== null
                    && bar.operationProvider.retentionEditorReady
                    && bar.operationProvider.operationPhase === "retention_editing"
                    && !productionRetentionDialog.opened) {
                productionRetentionDaysField.text = String(bar.operationProvider.authoritativeRetentionDays)
                productionRetentionDialog.open()
            }
            if (bar.productionSource && bar.operationProvider !== null
                    && bar.operationProvider.purgePreviewReady
                    && bar.operationProvider.operationPhase === "awaiting_purge_confirmation"
                    && !productionPurgeConfirmDialog.opened) {
                productionPurgeConfirmDialog.open()
            }
        }
        function onRetentionPolicyRefreshed(retentionDays) {
            productionRetentionDialog.close()
        }
        function onPurgePreviewInvalidated() {
            productionPurgeConfirmDialog.close()
        }
    }

    // 只顯示 allow-list summary；token、DB path 與 private ref 永不進 QML。
    Dialog {
        id: productionPurgeConfirmDialog
        objectName: "productionPurgeConfirmDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: bar.operationRunning ? Popup.NoAutoClose : Popup.CloseOnEscape
        anchors.centerIn: parent
        width: parent ? Math.min(520, Math.round(parent.width * 0.52)) : 520
        padding: DqeTheme.spacingL
        height: implicitContentHeight + topPadding + bottomPadding
        onClosed: {
            if (bar.operationProvider !== null && bar.operationProvider.purgePreviewReady
                    && !bar.operationProvider.running) {
                bar.operationProvider.cancelPurgePreview()
            }
        }

        background: Rectangle {
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidthStrong
            border.color: DqeTheme.fail
            radius: DqeTheme.radiusPanel
        }

        contentItem: Column {
            width: productionPurgeConfirmDialog.availableWidth
            spacing: DqeTheme.spacingS
            Text {
                text: "Confirm Temporary purge"
                color: DqeTheme.fail
                font.family: DqeTheme.fontFamily
                font.pixelSize: 14
            }
            Text {
                width: parent.width
                text: bar.operationProvider !== null
                      && bar.operationProvider.purgePreviewMode === "selected_jobs"
                      ? "The currently selected Temporary job will be rechecked before deletion."
                      : "All jobs expired at preview time will be rechecked before deletion."
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }
            Repeater {
                model: bar.operationProvider === null ? [] : bar.operationProvider.purgeJobLabels
                delegate: Text {
                    required property string modelData
                    width: parent.width
                    text: "• " + modelData
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
            }
            Text {
                width: parent.width
                text: {
                    if (bar.operationProvider === null) return ""
                    return bar.operationProvider.purgeCandidateJobCount + " " + DqeI18n.t("jobs · ", "個 job · ", "个 job · ")
                         + bar.operationProvider.purgeCandidateRowCount + " DQE rows · "
                         + bar.operationProvider.purgeCandidateAttachmentBlobCount + " attachment blobs"
                }
                color: DqeTheme.warn
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            Repeater {
                model: bar.operationProvider === null ? [] : bar.operationProvider.purgeWarnings
                delegate: Text {
                    required property string modelData
                    width: parent.width
                    text: "Warning: " + modelData
                    color: DqeTheme.warn
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 10
                    wrapMode: Text.Wrap
                }
            }
            Text {
                width: parent.width
                text: "This destructive operation cannot be retried automatically. VACUUM and date-range purge are not available here."
                color: DqeTheme.fail
                font.family: DqeTheme.fontFamily
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    objectName: "productionPurgeCancelButton"
                    width: 120
                    text: DqeI18n.t("Cancel", "取消", "取消")
                    enabled: !bar.operationRunning
                    onClicked: productionPurgeConfirmDialog.close()
                }
                ActionButton {
                    objectName: "productionPurgeConfirmButton"
                    width: 190
                    danger: true
                    text: bar.operationRunning ? "Purging…" : "Confirm destructive purge"
                    enabled: bar.operationProvider !== null && bar.operationProvider.purgePreviewReady
                             && !bar.operationRunning
                    onClicked: {
                        if (bar.operationProvider.confirmPurge()) {
                            productionPurgeConfirmDialog.close()
                        }
                    }
                }
            }
        }
    }

    // ── Check-in（mock）：顯示既有 mock 版本，Confirm 只發 signal ──
    Dialog {
        id: checkinDialog
        objectName: "lifecycleCheckinDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        anchors.centerIn: parent
        width: parent ? Math.min(460, Math.round(parent.width * 0.5)) : 460
        padding: DqeTheme.spacingL

        background: Rectangle {
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidthStrong
            border.color: DqeTheme.accent
            radius: DqeTheme.radiusPanel
        }

        contentItem: Column {
            spacing: DqeTheme.spacingS
            Text {
                text: "Check-in whole job — MOCK"
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 14
            }
            Text {
                width: parent.width
                text: DqeI18n.t("Job: ", "Job：", "Job：") + (bar.hasJob ? bar.jobKey : "—")
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                elide: Text.ElideRight
            }
            Text {
                text: "Existing mock versions:"
                color: DqeTheme.mutedText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 11
            }
            Repeater {
                model: Array.isArray(bar.checkinVersions) ? bar.checkinVersions : []
                delegate: Text {
                    required property var modelData
                    text: "• " + (modelData.version === undefined ? "—" : String(modelData.version))
                          + "  ·  " + (modelData.dateTime === undefined ? "—" : String(modelData.dateTime))
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 12
                }
            }
            Text {
                width: parent.width
                text: "No production write is performed."
                color: DqeTheme.faintText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    width: 120
                    text: DqeI18n.t("Cancel", "取消", "取消")
                    onClicked: checkinDialog.close()
                }
                ActionButton {
                    objectName: "lifecycleCheckinConfirmButton"
                    width: 160
                    text: "Check in (mock)"
                    accentColor: DqeTheme.pass
                    onClicked: {
                        bar.checkinRequested(bar.jobKey)
                        checkinDialog.close()
                    }
                }
            }
        }
    }

    // ── Purge（mock）：必要的確認畫面，Confirm 只發 signal ──
    Dialog {
        id: purgeDialog
        objectName: "lifecyclePurgeDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        anchors.centerIn: parent
        width: parent ? Math.min(460, Math.round(parent.width * 0.5)) : 460
        padding: DqeTheme.spacingL

        background: Rectangle {
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidthStrong
            border.color: DqeTheme.fail
            radius: DqeTheme.radiusPanel
        }

        contentItem: Column {
            spacing: DqeTheme.spacingS
            Text {
                text: "Purge job — MOCK"
                color: DqeTheme.fail
                font.family: DqeTheme.fontFamily
                font.pixelSize: 14
            }
            Text {
                width: parent.width
                text: "Confirm mock purge of job " + (bar.hasJob ? bar.jobKey : "—")
                      + "? This is a non-production action and is reverted on restart."
                color: DqeTheme.text
                font.family: DqeTheme.fontFamily
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    objectName: "lifecyclePurgeCancelButton"
                    width: 120
                    text: DqeI18n.t("Cancel", "取消", "取消")
                    onClicked: purgeDialog.close()
                }
                ActionButton {
                    objectName: "lifecyclePurgeConfirmButton"
                    width: 180
                    text: "Confirm purge (mock)"
                    danger: true
                    onClicked: {
                        bar.purgeRequested(bar.jobKey)
                        purgeDialog.close()
                    }
                }
            }
        }
    }

    // ── Retention editor（mock）：只改 harness 顯示，Apply 發 signal 並帶出挑選值 ──
    Dialog {
        id: retentionDialog
        objectName: "lifecycleRetentionDialog"
        property int pendingDays: bar.retentionDays
        parent: Overlay.overlay
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        anchors.centerIn: parent
        width: parent ? Math.min(420, Math.round(parent.width * 0.45)) : 420
        padding: DqeTheme.spacingL

        background: Rectangle {
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidthStrong
            border.color: DqeTheme.accent
            radius: DqeTheme.radiusPanel
        }

        contentItem: Column {
            spacing: DqeTheme.spacingS
            Text {
                text: "Retention N — MOCK"
                color: DqeTheme.accent
                font.family: DqeTheme.fontFamily
                font.pixelSize: 14
            }
            Text {
                width: parent.width
                text: "Display-only mock retention. No persistent policy edit; value resets on restart."
                color: DqeTheme.faintText
                font.family: DqeTheme.fontFamily
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    objectName: "lifecycleRetentionDecButton"
                    width: 44
                    text: "−"
                    onClicked: retentionDialog.pendingDays = Math.max(1, retentionDialog.pendingDays - 1)
                }
                Text {
                    objectName: "lifecycleRetentionPending"
                    width: 120
                    text: retentionDialog.pendingDays + " days"
                    color: DqeTheme.text
                    font.family: DqeTheme.fontFamily
                    font.pixelSize: 14
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    height: 30
                }
                ActionButton {
                    objectName: "lifecycleRetentionIncButton"
                    width: 44
                    text: "+"
                    onClicked: retentionDialog.pendingDays = Math.min(365, retentionDialog.pendingDays + 1)
                }
            }
            Row {
                spacing: DqeTheme.spacingS
                ActionButton {
                    width: 120
                    text: DqeI18n.t("Cancel", "取消", "取消")
                    onClicked: retentionDialog.close()
                }
                ActionButton {
                    objectName: "lifecycleRetentionApplyButton"
                    width: 160
                    text: "Apply (mock)"
                    onClicked: {
                        bar.requestedRetentionDays = retentionDialog.pendingDays
                        bar.retentionEditRequested()
                        retentionDialog.close()
                    }
                }
            }
        }
    }
}
