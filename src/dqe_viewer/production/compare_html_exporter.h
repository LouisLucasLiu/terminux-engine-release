// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// Compare 結果 HTML 匯出：QML 無法直接寫檔，透過本 C++ helper 落地。
// 只負責把 QML 產生好的 HTML 字串寫到指定檔案，不做任何資料計算或格式化。

#pragma once

#include <QObject>
#include <QString>
#include <QUrl>

namespace terminux::dqe::production {

class CompareHtmlExporter : public QObject
{
    Q_OBJECT

public:
    explicit CompareHtmlExporter(QObject *parent = nullptr);

    // 將 html 寫入 fileUrl（file:// URL 或本地路徑）。成功回傳空字串；失敗回傳（英文、
    // 中性）錯誤訊息，由 QML 端決定如何在地化包裝。
    Q_INVOKABLE QString writeHtml(const QUrl &fileUrl, const QString &html);

    // 建議檔名（含時間戳），供 FileDialog 預填。
    Q_INVOKABLE QString suggestedFileName() const;

    // 建議的完整存檔 URL：home 目錄 + 時間戳檔名（含目錄，FileDialog 才能正確預填）。
    Q_INVOKABLE QUrl suggestedFileUrl() const;
};

} // namespace terminux::dqe::production
