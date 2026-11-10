// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 明確 previewKind=integrity_incomplete 的安全狀態。

import QtQuick
import "../i18n"
import "../theme"

Item {
    objectName: "integrityIncompleteState"
    property string message: DqeI18n.t("This mock attachment has incomplete integrity information.", "此 mock 附件的完整性資訊不完整。", "此 mock 附件的完整性信息不完整。")

    StateMessage {
        anchors.centerIn: parent
        width: parent.width - DqeTheme.spacingXl * 2
        marker: "!"
        markerColor: DqeTheme.warn
        message: parent.message
        messageColor: DqeTheme.text
        messageSize: 13
    }
}
