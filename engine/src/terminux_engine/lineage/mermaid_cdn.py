# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Lineage HTML 載入的 Mermaid 版本。

產生的 HTML 在瀏覽器開啟時才從 jsDelivr 下載 Mermaid（MIT License），Terminux 不隨附。
版本固定，並以 Subresource Integrity 驗證下載內容；更新版本時要一併更新雜湊。
"""

MERMAID_VERSION = "11.17.2"

#: `dist/mermaid.min.js` 的 SHA-384（base64）。
MERMAID_SRI = "sha384-EOXBFmc3gx5mb+vn0vPvvGqACToJD24hhacX5Yx+8NUUQrHIle/Qi5Bg9o3zKwW2"

MERMAID_SCRIPT_TAG = (
    f'<script src="https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js" '
    f'integrity="{MERMAID_SRI}" crossorigin="anonymous"></script>'
)
