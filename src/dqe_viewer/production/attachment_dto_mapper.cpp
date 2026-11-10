// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// 不把 response 的 ref、message、base64 或檔名傳入 QML。

#include "dqe_viewer/production/attachment_dto_mapper.h"

#include <QByteArray>
#include <QStringDecoder>

namespace terminux::dqe::production {
namespace {

constexpr qsizetype kMaximumTextCharacters = 256 * 1024;

AttachmentPreviewResult simpleResult(const QString &state, const QString &previewKind)
{
    AttachmentPreviewResult mapped;
    mapped.state = state;
    mapped.previewKind = previewKind;
    return mapped;
}

bool isMissingFailure(const QString &code)
{
    return code == QLatin1String("missing_attachment")
           || code == QLatin1String("attachment_not_found")
           || code == QLatin1String("blob_null");
}

} // namespace

AttachmentPreviewResult AttachmentDtoMapper::mapLoadResponse(const QJsonObject &response,
                                                             const QString &integrityState)
{
    // integrity 是 group/item metadata 的 authoritative state，不能由檔名推斷。
    if (integrityState == QLatin1String("incomplete")) {
        return simpleResult(QStringLiteral("integrity_incomplete"),
                            QStringLiteral("integrity_incomplete"));
    }

    const QString loadStatus = response.value(QStringLiteral("load_status")).toString();
    const QString failureCode = response.value(QStringLiteral("failure_code")).toString();
    if (loadStatus == QLatin1String("refused")
        || failureCode == QLatin1String("attachment_too_large")) {
        return simpleResult(QStringLiteral("refused"), QStringLiteral("refused"));
    }
    if (loadStatus == QLatin1String("unsupported")
        || failureCode == QLatin1String("unsupported_preview")
        || failureCode == QLatin1String("unsupported_attachment")) {
        return simpleResult(QStringLiteral("unsupported"), QStringLiteral("unsupported"));
    }
    if (isMissingFailure(failureCode)) {
        return simpleResult(QStringLiteral("missing"), QStringLiteral("missing"));
    }
    if (loadStatus != QLatin1String("loaded")) {
        return simpleResult(QStringLiteral("error"), QStringLiteral("error"));
    }

    // executable payload 絕不執行，即使 content classifier 誤判它可 preview。
    if (response.value(QStringLiteral("executable")).toBool(false)) {
        return simpleResult(QStringLiteral("unsupported"), QStringLiteral("unsupported"));
    }

    const QString previewKind = response.value(QStringLiteral("preview_kind")).toString();
    if (previewKind != QLatin1String("text") && previewKind != QLatin1String("image")) {
        return simpleResult(QStringLiteral("unsupported"), QStringLiteral("unsupported"));
    }

    const QByteArray encoded = response.value(QStringLiteral("content_base64")).toString().toLatin1();
    const auto decodedResult = QByteArray::fromBase64Encoding(encoded);
    if (!decodedResult) {
        return simpleResult(QStringLiteral("error"), QStringLiteral("error"));
    }
    const QByteArray bytes = *decodedResult;

    if (previewKind == QLatin1String("image")) {
        QImage image;
        if (!image.loadFromData(bytes)) {
            return simpleResult(QStringLiteral("error"), QStringLiteral("error"));
        }
        AttachmentPreviewResult mapped = simpleResult(QStringLiteral("ready"), QStringLiteral("image"));
        mapped.image = std::move(image);
        return mapped;
    }

    QStringDecoder decoder(QStringDecoder::Utf8);
    QString text = decoder.decode(bytes);
    if (decoder.hasError()) {
        return simpleResult(QStringLiteral("error"), QStringLiteral("error"));
    }
    if (text.size() > kMaximumTextCharacters) {
        text.truncate(kMaximumTextCharacters);
    }
    AttachmentPreviewResult mapped = simpleResult(QStringLiteral("ready"), QStringLiteral("text"));
    mapped.text = std::move(text);
    return mapped;
}

} // namespace terminux::dqe::production
