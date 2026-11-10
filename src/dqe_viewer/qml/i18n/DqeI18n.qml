// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
pragma Singleton
import QtQuick

// DQE Viewer 的語言（i18n）唯一來源。任何 QML 以 DqeI18n.t(en, zhHant, zhHans) 取字串；
// language 變更時所有 binding 會重算 → UI 即時切換。啟動語言由 C++ 依 daemon
// preferences.get_language 注入（跟隨 terminal）；header 語言選單可本地覆寫。
QtObject {
    id: i18n

    // "en" | "zh-Hant" | "zh-Hans"。預設 en：測試 / mock / 視覺回歸維持英文；真實啟動由
    // C++ 依 daemon preferences.get_language 注入（跟隨 terminal），header 選單可覆寫。
    property string language: "en"

    // 三語就地翻譯：把英/繁/簡三個字串放在呼叫端，回傳當前語言者。
    function t(en, zhHant, zhHans) {
        if (language === "zh-Hant") {
            return zhHant
        }
        if (language === "zh-Hans") {
            return zhHans
        }
        return en
    }

    function setLanguage(lang) {
        if (lang === "en" || lang === "zh-Hant" || lang === "zh-Hans") {
            language = lang
        }
    }

    // 語言的顯示名稱（給選單用）。
    function displayName(lang) {
        if (lang === "en") {
            return "English"
        }
        if (lang === "zh-Hant") {
            return "繁體中文"
        }
        if (lang === "zh-Hans") {
            return "简体中文"
        }
        return lang
    }
}
