// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 只依明確 previewKind 呈現 mock preview。
// image 為純 QML 示意圖，text 只使用已提供的 mock payload；不讀取檔案或 production bytes。

import QtQuick
import QtQuick.Controls
import "../states" as States
import "../theme"
import "../i18n"

Item {
    id: preview

    property string previewKind: ""
    property var previewPayload: null
    property string previewText: ""
    property url previewImageUrl: ""
    property string attachmentName: ""
    property bool maximized: false
    property bool productionSource: false

    readonly property string textPayload: preview.productionSource
                                       ? preview.previewText
                                       : (previewPayload !== null && previewPayload !== undefined
                                       && previewPayload.text !== undefined
                                       ? String(previewPayload.text) : "—"
                                          )
    readonly property string imageCaption: previewPayload !== null && previewPayload !== undefined
                                         && previewPayload.caption !== undefined
                                         ? String(previewPayload.caption) : "Mock image preview"

    Rectangle {
        anchors.fill: parent
        color: DqeTheme.panelAlternate
        border.width: DqeTheme.borderWidth
        border.color: DqeTheme.border
    }

    Text {
        id: noPreviewPrompt
        objectName: "noAttachmentPreviewPrompt"
        anchors.centerIn: parent
        visible: preview.previewKind.length === 0
        text: preview.productionSource ? DqeI18n.t("Select an attachment to view its metadata.", "選取附件以檢視其中繼資料。", "选取附件以查看其元数据。")
                                       : DqeI18n.t("Select an attachment to preview its mock content.", "選取附件以預覽其 mock 內容。", "选取附件以预览其 mock 内容。")
        color: DqeTheme.mutedText
        font.family: DqeTheme.fontFamily
        font.pixelSize: 13
    }

    Item {
        id: imagePreview
        objectName: "inlineImagePreview"
        anchors.fill: parent
        visible: preview.previewKind === "image"

        Rectangle {
            anchors.fill: parent
            anchors.margins: DqeTheme.spacingL
            color: DqeTheme.panel
            border.width: DqeTheme.borderWidth
            border.color: DqeTheme.borderStrong
            radius: DqeTheme.radiusSmall
        }

        Text {
            anchors.left: parent.left
            anchors.leftMargin: DqeTheme.spacingXl
            anchors.top: parent.top
            anchors.topMargin: DqeTheme.spacingXl
            text: preview.productionSource ? "Memory-only image preview" : preview.imageCaption
            color: DqeTheme.mutedText
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
        }

        Rectangle {
            anchors.left: parent.left
            anchors.leftMargin: DqeTheme.spacingXl * 2
            anchors.right: parent.right
            anchors.rightMargin: DqeTheme.spacingXl * 2
            anchors.bottom: parent.bottom
            anchors.bottomMargin: DqeTheme.spacingXl * 2
            height: Math.max(70, parent.height * 0.56)
            color: DqeTheme.panelRaised
            border.width: DqeTheme.borderWidth
            border.color: DqeTheme.border

            Rectangle {
                anchors.left: parent.left
                anchors.leftMargin: DqeTheme.spacingL
                anchors.bottom: parent.bottom
                anchors.bottomMargin: DqeTheme.spacingL
                width: parent.width * 0.72
                height: DqeTheme.borderWidthStrong
                color: DqeTheme.warn
                rotation: -2
                transformOrigin: Item.Left
            }
            Image {
                objectName: "productionPreviewImage"
                anchors.fill: parent
                anchors.margins: DqeTheme.spacingM
                visible: preview.productionSource && preview.previewImageUrl !== ""
                source: preview.previewImageUrl
                fillMode: Image.PreserveAspectFit
                // opaque token 換選或關閉後不保留 QML image cache 的舊 payload。
                cache: false
                asynchronous: true
            }
            Rectangle {
                anchors.left: parent.left
                anchors.leftMargin: DqeTheme.spacingL
                anchors.bottom: parent.bottom
                anchors.bottomMargin: DqeTheme.spacingL * 2
                width: parent.width * 0.84
                height: DqeTheme.borderWidthStrong
                color: DqeTheme.fail
                rotation: -8
                transformOrigin: Item.Left
            }
        }
    }

    Flickable {
        id: textPreview
        objectName: "inlineTextPreview"
        anchors.fill: parent
        anchors.margins: DqeTheme.spacingL
        visible: preview.previewKind === "text"
        clip: true
        contentWidth: width
        contentHeight: textPayloadItem.implicitHeight + DqeTheme.spacingL * 2

        ScrollBar.vertical: ScrollBar {}

        Text {
            id: textPayloadItem
            x: DqeTheme.spacingM
            y: DqeTheme.spacingM
            width: textPreview.width - DqeTheme.spacingL * 2
            text: preview.textPayload
            color: DqeTheme.text
            font.family: DqeTheme.fontFamily
            font.pixelSize: 12
            wrapMode: Text.Wrap
        }
    }

    States.UnsupportedAttachmentState {
        anchors.fill: parent
        visible: preview.previewKind === "unsupported"
    }
    // DQE Viewer production binding production binding 僅提供 metadata；此處明確不宣稱或回退為 mock preview。
    States.UnsupportedAttachmentState {
        anchors.fill: parent
        visible: preview.previewKind === "metadata_only"
        message: DqeI18n.t("Attachment metadata is available; preview content is not loaded.", "附件中繼資料可用；預覽內容未載入。", "附件元数据可用；预览内容未载入。")
    }
    States.UnsupportedAttachmentState {
        anchors.fill: parent
        visible: preview.previewKind === "loading"
        message: DqeI18n.t("Loading attachment preview…", "載入附件預覽中…", "载入附件预览中…")
    }
    States.UnsupportedAttachmentState {
        anchors.fill: parent
        visible: preview.previewKind === "refused"
        message: DqeI18n.t("Attachment preview was refused by the size limit.", "附件預覽因大小限制被拒。", "附件预览因大小限制被拒。")
    }
    States.UnsupportedAttachmentState {
        anchors.fill: parent
        visible: preview.previewKind === "error"
        message: DqeI18n.t("Attachment preview could not be decoded safely.", "附件預覽無法安全解碼。", "附件预览无法安全解码。")
    }
    States.MissingAttachmentState {
        anchors.fill: parent
        visible: preview.previewKind === "missing"
    }
    States.IntegrityIncompleteState {
        anchors.fill: parent
        visible: preview.previewKind === "integrity_incomplete"
    }
    States.UnsupportedAttachmentState {
        anchors.fill: parent
        visible: preview.previewKind.length > 0 && preview.previewKind !== "image"
                 && preview.previewKind !== "text" && preview.previewKind !== "unsupported"
                 && preview.previewKind !== "metadata_only" && preview.previewKind !== "missing"
                 && preview.previewKind !== "integrity_incomplete"
                 && preview.previewKind !== "loading" && preview.previewKind !== "refused"
                 && preview.previewKind !== "error"
        message: DqeI18n.t("Preview kind is not supported by this mock-only viewer.", "此 mock viewer 不支援該預覽類型。", "此 mock viewer 不支持该预览类型。")
    }
}
