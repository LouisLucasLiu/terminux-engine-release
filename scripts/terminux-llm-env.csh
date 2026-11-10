#!/bin/tcsh
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# terminux-llm-env.csh — LLM runtime 設定範本（tcsh）
#
# 使用方式：複製一份到自己的設定，取消註解「其中一種」backend，再 source：
#   source /path/to/terminux-llm-env.csh
#
# 本範本不預設任何 backend；沒有設定 TERMINUX_LLM_BACKEND 時，LLM 功能維持關閉。
#
# 憑證（API key、base URL）不要寫在這裡，請放在 credentials 檔：
#   ~/.terminux/credentials.json（權限必須是 0600；可用 TERMINUX_CREDENTIALS_FILE 指定其他位置）
#   {"TERMINUX_OPENAI_API_KEY": "...", "TERMINUX_OPENAI_BASE_URL": "...",
#    "TERMINUX_GEMINI_API_KEY": "..."}
# 同名環境變數仍可使用，但只作為相容的替代方式；credentials 檔優先。

# ── A. openai_compatible：雲端服務（例如 DeepSeek）或本機 OpenAI 相容服務 ──
#    Agent（@@）功能使用這條路徑。base URL 與 API key 放在 credentials 檔；
#    本機服務例如 "TERMINUX_OPENAI_BASE_URL": "http://127.0.0.1:8080/v1"。
#    本機服務能否完成 Agent 流程取決於模型與服務本身，尚未經過驗證。
# setenv TERMINUX_LLM_BACKEND openai_compatible
# setenv TERMINUX_OPENAI_MODEL deepseek-chat

# ── B. gemini ──
#    API key 放在 credentials 檔的 TERMINUX_GEMINI_API_KEY。
# setenv TERMINUX_LLM_BACKEND gemini

# ── C. llama_cpp：在 process 內載入本機 GGUF 模型 ──
#    只有基本問答（basic chat），沒有 Agent 能力。模型路徑請改成自己的檔案。
# setenv TERMINUX_LLM_BACKEND llama_cpp
# setenv TERMINUX_LLM_MODEL_PATH /path/to/model.gguf
# setenv TERMINUX_LLM_DISABLE_THINKING 1   # Qwen3 這類有 thinking mode 的模型需要

# ── Runtime feature flags（預設關閉，需要時才取消註解）──
#    兩者都在命令執行前，把下列資料送到上面設定的 LLM provider：
#    - LINEAGE_EXPLAINER：lineage 檢查結果為 MIXED_LINEAGE 或 INSUFFICIENT_EVIDENCE 時，送出
#      命令、工作目錄、runfile 路徑、檢查結果與相關檔案的訊息，請 LLM 產生說明。
#    - INTENT_FALLBACK：固定格式解析器讀不出 runfile 的輸入輸出時，送出完整的 runfile 內容、
#      命令、工作目錄與 runfile 路徑，請 LLM 推斷。
#    設計腳本、路徑或設計名稱屬於機密時，不要開啟，或只搭配本機 provider 使用。
# setenv TERMINUX_ENABLE_LINEAGE_EXPLAINER_RUNTIME 1
# setenv TERMINUX_ENABLE_INTENT_FALLBACK_RUNTIME   1
