// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 明確 previewKind=missing 的安全狀態。

import QtQuick
import "../i18n"
import "../theme"

Item {
    objectName: "missingAttachmentState"
    property string message: DqeI18n.t("This mock attachment is marked as missing.", "此 mock 附件被標記為遺失。", "此 mock 附件被标记为缺失。")

    StateMessage {
        anchors.centerIn: parent
        width: parent.width - DqeTheme.spacingXl * 2
        marker: "×"
        markerColor: DqeTheme.fail
        message: parent.message
        messageColor: DqeTheme.text
        messageSize: 13
    }
}
