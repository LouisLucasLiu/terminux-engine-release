// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Evidence area。清單與 inline preview 並列；最大化不改 selection 或 shell split。

import QtQuick
import QtQuick.Controls
import "../theme"

Item {
    id: evidencePanel

    property var attachments: []
    property string selectedAttachmentId: ""
    property string previewKind: ""
    property var previewPayload: null
    property var previewProvider: null
    // DQE Viewer production binding production attachment 僅可顯示 metadata，不能顯示 mock content 字樣。
    property bool productionSource: false

    signal attachmentSelected(string attachmentId)
    signal attachmentActivated(string attachmentId)
    signal maximizeRequested()

    property string _dialogPreviewKind: ""
    property var _dialogPreviewPayload: null
    property string _dialogPreviewText: ""
    property url _dialogPreviewImageUrl: ""
    property string _dialogAttachmentName: ""
    readonly property var _selectedAttachment: _attachmentById(selectedAttachmentId)
    readonly property string _effectivePreviewKind: _selectedAttachment === null
                                                  ? previewKind
                                                  : (productionSource && previewProvider !== null
                                                     ? String(previewProvider.previewKind)
                                                     : (productionSource ? "metadata_only"
                                                                         : String(_selectedAttachment.previewKind)))
    readonly property var _effectivePreviewPayload: _selectedAttachment === null
                                                    ? previewPayload
                                                    : (productionSource ? null : _selectedAttachment.mockPayload)
    readonly property string _effectivePreviewText: productionSource && previewProvider !== null
                                                   ? String(previewProvider.previewText) : ""
    readonly property url _effectivePreviewImageUrl: productionSource && previewProvider !== null
                                                     ? previewProvider.previewImageUrl : ""
    readonly property string _effectiveAttachmentName: _selectedAttachment === null
                                                       ? "" : String(_selectedAttachment.displayName)
    readonly property bool previewDialogOpen: previewDialog.opened

    implicitWidth: 620
    implicitHeight: 260

    function _attachmentById(attachmentId) {
        const items = Array.isArray(attachments) ? attachments : []
        for (let index = 0; index < items.length; ++index) {
            if (items[index].attachmentId !== undefined && String(items[index].attachmentId) === attachmentId) {
                return items[index]
            }
        }
        return null
    }

    function _activateAttachment(attachmentId) {
        const attachment = _attachmentById(attachmentId)
        if (attachment === null) {
            return
        }
        if (productionSource && previewProvider !== null) {
            previewProvider.requestPreview(attachmentId)
            _dialogPreviewKind = String(previewProvider.previewKind)
            _dialogPreviewPayload = null
            _dialogPreviewText = String(previewProvider.previewText)
            _dialogPreviewImageUrl = previewProvider.previewImageUrl
        } else {
            _dialogPreviewKind = attachment.previewKind === undefined ? "unsupported" : String(attachment.previewKind)
            _dialogPreviewPayload = attachment.mockPayload
            _dialogPreviewText = ""
            _dialogPreviewImageUrl = ""
        }
        _dialogAttachmentName = attachment.displayName === undefined ? "—" : String(attachment.displayName)
        attachmentActivated(attachmentId)
        maximizeRequested()
        previewDialog.open()
    }

    // scenario／鍵盤驗證共用的明確入口；不改 selection，也不讀取外部資料。
    function openSelectedAttachmentPreview() {
        if (selectedAttachmentId.length > 0) {
            _activateAttachment(selectedAttachmentId)
        }
    }

    Rectangle {
        anchors.fill: parent
        color: DqeTheme.panel
        border.width: DqeTheme.borderWidth
        border.color: DqeTheme.border
    }

    AttachmentList {
        id: attachmentList
        objectName: "evidenceAttachmentList"
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: Math.min(260, Math.round(parent.width * 0.36))
        attachments: evidencePanel.attachments
        selectedAttachmentId: evidencePanel.selectedAttachmentId
        onAttachmentSelected: (attachmentId) => { evidencePanel.attachmentSelected(attachmentId) }
        onAttachmentActivated: (attachmentId) => { evidencePanel._activateAttachment(attachmentId) }
    }

    AttachmentPreview {
        id: inlinePreview
        objectName: "evidenceInlinePreview"
        anchors.left: attachmentList.right
        anchors.leftMargin: DqeTheme.spacingS
        anchors.right: parent.right
        anchors.rightMargin: DqeTheme.spacingS
        anchors.top: parent.top
        anchors.topMargin: DqeTheme.spacingS
        anchors.bottom: parent.bottom
        anchors.bottomMargin: DqeTheme.spacingS
        previewKind: evidencePanel._effectivePreviewKind
        previewPayload: evidencePanel._effectivePreviewPayload
        previewText: evidencePanel._effectivePreviewText
        previewImageUrl: evidencePanel._effectivePreviewImageUrl
        attachmentName: evidencePanel._effectiveAttachmentName
        productionSource: evidencePanel.productionSource
    }

    AttachmentPreviewDialog {
        id: previewDialog
        previewKind: evidencePanel.productionSource && evidencePanel.previewProvider !== null
                     ? String(evidencePanel.previewProvider.previewKind)
                     : evidencePanel._dialogPreviewKind
        previewPayload: evidencePanel._dialogPreviewPayload
        previewText: evidencePanel.productionSource && evidencePanel.previewProvider !== null
                     ? String(evidencePanel.previewProvider.previewText)
                     : evidencePanel._dialogPreviewText
        previewImageUrl: evidencePanel.productionSource && evidencePanel.previewProvider !== null
                         ? evidencePanel.previewProvider.previewImageUrl
                         : evidencePanel._dialogPreviewImageUrl
        attachmentName: evidencePanel._dialogAttachmentName
        productionSource: evidencePanel.productionSource
        onPreviewClosed: {
            if (evidencePanel.productionSource && evidencePanel.previewProvider !== null) {
                evidencePanel.previewProvider.clearPreview()
            }
        }
    }
}
