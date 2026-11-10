# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# Terminux Shell Integration — bash hooks
# Source this file in .bashrc:
#   [ -f ~/.terminux/bash_hooks.sh ] && source ~/.terminux/bash_hooks.sh

# 同一個 bash 僅註冊一次；子 shell 的 BASHPID 不同，仍可建立自己的 hooks。
[[ "${__terminux_interactive_hook_pid:-}" == "$BASHPID" ]] && return 0

# 只在 Terminux terminal 內啟用：
# 1. C++ 端啟動 PTY 時注入 TERMINUX_ACTIVE=1（最直接訊號）
# 2. 但 ssh 進子帳號 / sudo 切 user 不會帶 env → 改用 lineage.sock 存在當第二
#    判據（daemon 在線 → 視為 terminux 環境）。socket 還在的話 self-promote。
export TERMINUX_NODENAME="${TERMINUX_NODENAME:-$(hostname)}"
_terminux_python=""
if [[ -r "$HOME/.terminux/hooks/python_interpreter" ]]; then
    IFS= read -r _terminux_python < "$HOME/.terminux/hooks/python_interpreter"
fi
_terminux_socket=""
_terminux_python_failed=1
if [[ "$_terminux_python" = /* && -x "$_terminux_python" ]]; then
    if _terminux_socket=$("$_terminux_python" -c 'from terminux_engine.ipc.agent_socket import shell_lineage_socket_path; print(shell_lineage_socket_path())' 2>/dev/null); then
        _terminux_python_failed=0
    fi
fi
if [[ "$_terminux_python_failed" != 0 ]]; then
    printf '%s\n' 'TERM_HOOK_PYTHON_UNAVAILABLE: 無法解析 lineage socket' >&2
fi
if [ -z "${TERMINUX_ACTIVE:-}" ] && [ -S "$_terminux_socket" ]; then
    export TERMINUX_ACTIVE=1
fi
[ -z "${TERMINUX_ACTIVE:-}" ] && return
# 保留其他 PATH 項目（含空項目）的順序，只留下最前面的一個 wrapper 目錄。
__terminux_arm_path() {
    local wrapper_bin="$HOME/.terminux/bin"
    local remaining="${PATH-}" entry tail=""
    while [[ "$remaining" == *:* ]]; do
        entry="${remaining%%:*}"
        remaining="${remaining#*:}"
        [[ "$entry" == "$wrapper_bin" ]] || tail+=":$entry"
    done
    [[ "$remaining" == "$wrapper_bin" ]] || tail+=":$remaining"
    export PATH="$wrapper_bin$tail"
}
__terminux_arm_path

# 即使 daemon 尚未建立 socket，互動提示前仍須維護 wrapper PATH。
if [[ "$-" == *i* && "${__terminux_path_hook_pid:-}" != "$BASHPID" ]]; then
    if [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
        PROMPT_COMMAND+=(__terminux_arm_path)
    else
        PROMPT_COMMAND="${PROMPT_COMMAND:-}"$'\n__terminux_arm_path'
    fi
    __terminux_path_hook_pid=$BASHPID
fi

# 啟用條件：daemon socket 存在
if [ -S "$_terminux_socket" ]; then

    __terminux_in_prompt=0

    __terminux_preexec() {
        [ "$__terminux_in_prompt" = "1" ] && return
        [ -z "$BASH_COMMAND" ] && return
        # 過濾自身 hook 和 terminux-notify 呼叫
        case "$BASH_COMMAND" in
            __terminux_*|*terminux-notify*) return ;;
        esac
        # 不從這裡 fire pre_exec。EDA tool 的 pre_exec 由 terminux-wrapper-entry
        # 主動發送（wrapper 知道權威 tool name + cwd），雙發會讓 C++ NotificationBar
        # 顯示兩條同樣 warn。非 EDA cmd 不發 pre_exec → daemon SKIPPED，無副作用。
        # OSC 133;C — command begin marker（C++ 端清 ring buffer）
        printf '\e]133;C\a'
    }

    __terminux_postexec() {
        local exit_code=$?
        local trace_flags=()
        [ -n "${TERMINUX_TRACE_ROOT:-}" ] &&
            trace_flags+=(--trace-root "$TERMINUX_TRACE_ROOT")
        [ -n "${TERMINUX_TRACE_SELF:-}" ] &&
            trace_flags+=(--trace-self "$TERMINUX_TRACE_SELF")
        [ -n "${TERMINUX_TRACE_PARENT:-}" ] &&
            trace_flags+=(--trace-parent "$TERMINUX_TRACE_PARENT")
        __terminux_in_prompt=1
        # OSC 133;D 必須在 $? 抓進 local var 後立刻送，
        # 後續 printf / terminux-notify 不會覆蓋它
        printf '\e]133;D;%s\a' "$exit_code"
        ~/.terminux/bin/terminux-notify post_exec "$exit_code" "$(pwd)" --pid $$ \
            "${trace_flags[@]}" &
        # OSC 133;A — pre-prompt（prompt 即將顯示）
        printf '\e]133;A\a'
        __terminux_in_prompt=0
    }

    __terminux_interactive_hook_pid=$BASHPID
    trap '__terminux_preexec' DEBUG
    # postexec 先保存命令狀態；使用者的 prompt callbacks 完成後才重排 PATH。
    if [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
        PROMPT_COMMAND=(__terminux_postexec "${PROMPT_COMMAND[@]}")
    else
        PROMPT_COMMAND="__terminux_postexec;${PROMPT_COMMAND:-}"
    fi

fi
