# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# Terminux EDA wrapper 共用函式庫
#
# 安裝路徑：~/.terminux/bin/_wrapper_lib.sh
# 用法：Z2-2 的 EDA wrapper（如 ~/.terminux/bin/dc_shell）`source` 本檔後呼叫
#       terminux_find_real_tool / _terminux_advance_trace_chain / terminux_log_event。
#
# 假設：Linux + GNU coreutils（date %N、stat %d:%i）。BSD/macOS 不支援。

# ── terminux_find_real_tool tool_name ─────────────────────────────────────────
# 在 $PATH 上找到非 wrapper 自己的同名 executable，echo 完整路徑。
# 用 device:inode 比對跳過自己——避免靠路徑字串猜（symlink、rename 都會誤判）。
# return 0 = found（stdout 有路徑）；return 1 = not found。
terminux_find_real_tool() {
    local tool_name="$1"
    [[ -z "$tool_name" ]] && return 1

    # 取得呼叫 wrapper 的 device:inode；-L 跟隨 symlink 到底層檔
    # ($0 在 sourced 函式裡是 caller script 的路徑——也就是 wrapper 自己)
    local self_id
    self_id=$(stat -L -c '%d:%i' "$0" 2>/dev/null) || return 1

    local IFS=:
    local dir
    for dir in $PATH; do
        [[ -z "$dir" ]] && continue
        local cand="$dir/$tool_name"
        [[ -x "$cand" && ! -d "$cand" ]] || continue
        local cand_id
        cand_id=$(stat -L -c '%d:%i' "$cand" 2>/dev/null) || continue
        [[ "$cand_id" == "$self_id" ]] && continue   # 跳過 wrapper 自己
        echo "$cand"
        return 0
    done
    return 1
}

# ── _terminux_advance_trace_chain ─────────────────────────────────────────────
# 推進 trace chain：PARENT ← 舊 SELF、SELF ← 新 invocation ID。
# ROOT/QUEUE/JOB 不變。
# return 0 = advanced；return 1 = 不在 Terminux 環境（TRACE_ROOT 為空）。
_terminux_advance_trace_chain() {
    [[ -z "${TERMINUX_TRACE_ROOT:-}" ]] && return 1

    # 確保 export bit（防呆：被 set 但未 export 的情境）
    export TERMINUX_TRACE_ROOT TERMINUX_TRACE_QUEUE TERMINUX_TRACE_JOB

    export TERMINUX_TRACE_PARENT="${TERMINUX_TRACE_SELF:-}"

    # 格式必須與 Python execution_trace.trace_id.generate_trace_self 一致：
    #   inv_{epoch_ns}_{pid}，regex ^inv_(\d+)_(\d+)$
    # date +%s%N 是 GNU 擴充；$BASHPID 對應 Python os.getpid()
    # ($$ 在 subshell 裡是 parent PID，會跟 Python 端 /proc/<pid>/environ 對不上)
    export TERMINUX_TRACE_SELF="inv_$(date +%s%N)_$BASHPID"
    return 0
}

# ── _terminux_json_escape value ───────────────────────────────────────────────
# 內部 helper：JSON string 轉義。處理 \ " 與三個常見控制字元 \n \r \t。
# 不處理 U+0000~U+001F 其餘控制字元（EDA argv 極罕見）。
_terminux_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"       # 反斜線必須最先（不然會吃掉後面新插的 \）
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

# ── terminux_log_event tool_name [argv...] ────────────────────────────────────
# 寫入一行 JSON 到本主機的 lineage.<nodename>.jsonl。Schema v1，見 Z2-1 spec Part 3。
# 行 < PIPE_BUF (4KB) 在 Linux ext4/xfs/btrfs 上 O_APPEND 操作上 atomic，
# 多 wrapper 並行寫安全，不需 flock。
# 不依賴 daemon——純 file append；daemon 端 ingest 是 Z3 範圍。
terminux_log_event() {
    local tool_name="$1"
    shift
    local log_dir="$HOME/.terminux"
    local nodename="${TERMINUX_NODENAME:-$(hostname)}"
    local log_file="$log_dir/lineage.${nodename}.jsonl"

    mkdir -p "$log_dir" 2>/dev/null || true

    # 構造 argv JSON array（含 tool_name 為第一個元素）
    local argv_json='['
    local first=1 arg esc
    for arg in "$tool_name" "$@"; do
        esc=$(_terminux_json_escape "$arg")
        if [[ $first -eq 1 ]]; then
            argv_json="${argv_json}\"${esc}\""
            first=0
        else
            argv_json="${argv_json},\"${esc}\""
        fi
    done
    argv_json="${argv_json}]"

    local cwd_esc tool_esc
    cwd_esc=$(_terminux_json_escape "$PWD")
    tool_esc=$(_terminux_json_escape "$tool_name")

    # 一行 JSON。所有 trace_* 用 ${VAR:-} 容忍未設情境。
    printf '{"v":1,"ts":%s,"event":"tool_exec","tool":"%s","argv":%s,"cwd":"%s","pid":%s,"trace_root":"%s","trace_queue":"%s","trace_job":"%s","trace_parent":"%s","trace_self":"%s","layer":"wrapper"}\n' \
        "$(date +%s.%N)" \
        "$tool_esc" \
        "$argv_json" \
        "$cwd_esc" \
        "$BASHPID" \
        "${TERMINUX_TRACE_ROOT:-}" \
        "${TERMINUX_TRACE_QUEUE:-}" \
        "${TERMINUX_TRACE_JOB:-}" \
        "${TERMINUX_TRACE_PARENT:-}" \
        "${TERMINUX_TRACE_SELF:-}" \
        >> "$log_file"
}

# ── _terminux_inject_slurm_env "$@" ───────────────────────────────────────────
# Slurm --export 注入：將 trace 必要 env vars 加入 --export list，使其 propagate
# 到 compute node。
#
# 三 case：
#   --export=NONE     → 替換為 --export=<trace+BASH_ENV>,PATH,HOME,USER
#                       （NONE 連 PATH 都掉，shell 起不來；補三個基本 var）
#   --export=foo,bar  → 改為 --export=foo,bar,<trace+BASH_ENV>
#   未含 --export     → argv 不動（slurm default ALL 自動帶整 env）
#
# Trace inject list 不含 TRACE_JOB（compute node 端 BASH_ENV hook 從 SLURM_JOB_ID 填）。
# 只支援 --export=val 等號形式；--export <val> 空格形式為 known limitation。
#
# Caller 約定：先初始化 _TERMINUX_MODIFIED_ARGV=("$@")；本函式只在偵測到
# --export 時覆寫。
_terminux_inject_slurm_env() {
    local has_export=false
    local new_argv=()
    local arg
    for arg in "$@"; do
        if [[ "$arg" == --export=* ]]; then
            has_export=true
            local export_val="${arg#--export=}"
            if [[ "$export_val" == "NONE" ]]; then
                new_argv+=("--export=TERMINUX_TRACE_ROOT,TERMINUX_TRACE_QUEUE,TERMINUX_TRACE_PARENT,TERMINUX_TRACE_SELF,TERMINUX_ACTIVE,BASH_ENV,PATH,HOME,USER")
            else
                new_argv+=("--export=${export_val},TERMINUX_TRACE_ROOT,TERMINUX_TRACE_QUEUE,TERMINUX_TRACE_PARENT,TERMINUX_TRACE_SELF,TERMINUX_ACTIVE,BASH_ENV")
            fi
        else
            new_argv+=("$arg")
        fi
    done
    if $has_export; then
        _TERMINUX_MODIFIED_ARGV=("${new_argv[@]}")
    fi
}

# ── _terminux_log_queue_event tool_name queue_system [argv...] ────────────────
# 寫入 event=queue_submit JSONL，與 terminux_log_event（event=tool_exec）的差別：
# 1. event field 為 "queue_submit"
# 2. 多一個 queue_system field（slurm/lsf/sge/unknown）
_terminux_log_queue_event() {
    local sc="$1" qs="$2"
    shift 2
    local log_dir="$HOME/.terminux"
    local nodename="${TERMINUX_NODENAME:-$(hostname)}"
    local log_file="$log_dir/lineage.${nodename}.jsonl"
    mkdir -p "$log_dir" 2>/dev/null || true

    local argv_json='['
    local first=1 arg esc
    for arg in "$sc" "$@"; do
        esc=$(_terminux_json_escape "$arg")
        if [[ $first -eq 1 ]]; then
            argv_json="${argv_json}\"${esc}\""
            first=0
        else
            argv_json="${argv_json},\"${esc}\""
        fi
    done
    argv_json="${argv_json}]"

    local cwd_esc tool_esc qs_esc
    cwd_esc=$(_terminux_json_escape "$PWD")
    tool_esc=$(_terminux_json_escape "$sc")
    qs_esc=$(_terminux_json_escape "$qs")

    printf '{"v":1,"ts":%s,"event":"queue_submit","tool":"%s","queue_system":"%s","argv":%s,"cwd":"%s","pid":%s,"trace_root":"%s","trace_queue":"%s","trace_job":"%s","trace_parent":"%s","trace_self":"%s","layer":"wrapper"}\n' \
        "$(date +%s.%N)" "$tool_esc" "$qs_esc" "$argv_json" "$cwd_esc" "$BASHPID" \
        "${TERMINUX_TRACE_ROOT:-}" "${TERMINUX_TRACE_QUEUE:-}" "${TERMINUX_TRACE_JOB:-}" \
        "${TERMINUX_TRACE_PARENT:-}" "${TERMINUX_TRACE_SELF:-}" \
        >> "$log_file"
}
