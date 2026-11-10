# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# Terminux Shell Integration — tcsh hooks
# Source this file in .cshrc:
#   if ( -e ~/.terminux/tcsh_hooks.csh ) source ~/.terminux/tcsh_hooks.csh

# 只在 Terminux terminal 內啟用：
# 1. C++ 端啟動 PTY 時注入 TERMINUX_ACTIVE=1（最直接訊號）
# 2. 但 ssh 進子帳號 / sudo 切 user 不會帶 env → 改用 lineage.sock 存在當第二
#    判據（daemon 在線 → 視為 terminux 環境）。socket 還在的話 self-promote。
if (! $?TERMINUX_NODENAME) setenv TERMINUX_NODENAME "`hostname`"
if ("$TERMINUX_NODENAME" == "") setenv TERMINUX_NODENAME "`hostname`"
if ($?TERMINUX_ACTIVE) then
    if ( "$PATH" !~ "${HOME}/.terminux/bin:"* ) setenv PATH "${HOME}/.terminux/bin:$PATH"
endif
set _terminux_python = ""
set _terminux_python_file = "$HOME/.terminux/hooks/python_interpreter"
if ( -r "$_terminux_python_file" ) set _terminux_python = "`/bin/cat "$_terminux_python_file"`"
set _terminux_socket = ""
set _terminux_python_failed = 1
if ( -x "$_terminux_python" ) then
    # 以狀態行 0 表示正常離線，避免與直譯器執行失敗混淆。
    set _terminux_socket = "`$_terminux_python -c 'from terminux_engine.ipc.agent_socket import shell_lineage_socket_path; print(shell_lineage_socket_path() or 0)' |& /bin/grep -m1 -x -e '/.*' -e 0`"
    set _terminux_python_failed = $status
    if ("$_terminux_socket" == "0") set _terminux_socket = ""
endif
if ($_terminux_python_failed != 0) then
    echo "TERM_HOOK_PYTHON_UNAVAILABLE: 無法解析 lineage socket" > /dev/stderr
endif
if (! $?TERMINUX_ACTIVE) then
    if ( -S "$_terminux_socket" ) setenv TERMINUX_ACTIVE 1
endif

if ($?TERMINUX_ACTIVE) then
    # 防呆：TERMINUX_SOCK 未設或設為空字串時，補成真實 socket 路徑（而不是 ""）。
    # 設成 "" 會讓 terminux-notify 拿到空路徑 → connect("") fail → silent exit，
    # daemon 永遠收不到 pre_exec / post_exec。
    if ( -S "$_terminux_socket" ) then
        # --pid $$ 傳 shell PID 給 daemon（供 ancestor fallback 使用）
        # 必須先 `set _terminux_st = $status` 保存，否則 printf 執行後
        # $status 變 0（tcsh runtime expansion，每個命令都改寫 $status）。
        # postcmd 在 history expansion 後 / exec 前 fire；用 `history -h 1` 取
        # expanded form（含 !N / alias / glob 已展開），讓 daemon 拿到的 cmd
        # 是真實要執行的，而不是 user 敲鍵的 shorthand（fix `!982`-class bug）。
        # 順序：保存 status → 取 expanded cmd → 送 pre_exec → OSC 133;D → 送
        #       post_exec → OSC 133;A → unset
        # B 方案：terminux-tcsh-postcmd helper 處理 pre_exec / OSC 133;D /
        # post_exec / OSC 133;A 一條龍。alias 只做 status 保存與參數 forwarding，
        # 避開 tcsh alias 內 `& ;` parse quirks 與 background loop。helper 內
        # 自己 background fan-out，使用者感受不到 socket 阻塞。
        alias postcmd 'set _terminux_st = $status ; set _terminux_cmd = "`history -h 1`" ; ~/.terminux/bin/terminux-tcsh-postcmd $_terminux_st "$_terminux_cmd" `pwd` $$ ; unset _terminux_st _terminux_cmd'
    endif

    # ── PATH 維護：interactive precmd 重排；non-interactive 只 arm 一次。Known gap: mid-script module load 在 non-interactive 不重排
    alias _terminux_arm_path 'if ( "$PATH" !~ "${HOME}/.terminux/bin:"* ) setenv PATH "${HOME}/.terminux/bin:$PATH"'

    set _tx_old_precmd = "`alias precmd`"
    if ("$_tx_old_precmd" != "") then
        alias precmd "_terminux_arm_path; $_tx_old_precmd"
    else
        alias precmd '_terminux_arm_path'
    endif
    unset _tx_old_precmd
    _terminux_arm_path

    # ── TRACE_JOB 填充；nested if 避 tcsh "Undefined variable" 陷阱
    set _tx_need_fill = 0
    if (! $?TERMINUX_TRACE_JOB) then
        set _tx_need_fill = 1
    else
        if ("$TERMINUX_TRACE_JOB" == "") set _tx_need_fill = 1
    endif
    if ($_tx_need_fill) then
        if ($?SLURM_JOB_ID) then
            setenv TERMINUX_TRACE_JOB "slurm_job_${SLURM_JOB_ID}"
        else if ($?LSB_JOBID) then
            setenv TERMINUX_TRACE_JOB "lsf_job_${LSB_JOBID}"
        else if ($?JOB_ID) then
            setenv TERMINUX_TRACE_JOB "sge_job_${JOB_ID}"
        endif
    endif
    unset _tx_need_fill
endif
