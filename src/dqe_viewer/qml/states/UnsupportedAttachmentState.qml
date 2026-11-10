// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 明確 previewKind=unsupported 的安全狀態。

import QtQuick
import "../i18n"
import "../theme"

Item {
    objectName: "unsupportedAttachmentState"
    property string message: DqeI18n.t("Preview is not supported for this mock attachment.", "此 mock 附件不支援預覽。", "此 mock 附件不支持预览。")

    StateMessage {
        anchors.centerIn: parent
        width: parent.width - DqeTheme.spacingXl * 2
        marker: "?"
        markerColor: DqeTheme.warn
        message: parent.message
        messageColor: DqeTheme.text
        messageSize: 13
    }
}
