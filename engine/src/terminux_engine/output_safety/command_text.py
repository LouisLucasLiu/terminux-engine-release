# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""判斷一段模型輸出的文字是否**呈現為 shell／command invocation**。

這是 Common 的共用 model-output safety 機制，因此完全 domain-blind：這裡只
認識作業系統層級的命令詞彙，**不認識**任何 EDA／vendor／專案工具名。需要額外
domain 命令名的呼叫端自行以 `extra_commands` 傳入（同一個 domain-blind 原則）。

## 為什麼不用裸 substring

舊版對 `recommended_check` 做 `marker in lowered` 的裸子字串比對，marker 含
`"rm "`、`"sh "`、`"./"`、`";"`、`"|"`。真實模型寫出

    Check the SDC constraint definitions to confirm clock targets ...

時，`"confi[rm ]clock"` 命中 `"rm "`，一份完整、有證據支持的分析就被整份丟棄
。同一份清單也會誤傷 `perform `、`platform `、`transform `、
`warm `、`fresh `、`flush `，以及任何含分號或直線的正常英文句子。

## 改成什麼

1. **word／lexical boundary**：命令詞必須以完整 token 出現，不看子字串。
2. **command position aware**：只有出現在真正的命令位置——行首、bullet 之後、
   shell prompt 之後、或 `;` `|` `&&` `||` 這類串接運算子之後——而且後面接著
   command 形狀的延續（flag、path、引號字串、另一個命令詞，或整行只有它），
   才判為 invocation。
3. **無歧義的形式一律直接拒**：fenced code block、command substitution、
   `&&`／`||`、以及 `sh -c` 這種 interpreter＋flag 的呼叫形式。

fail-closed 的語意沒有被放寬：所有真正呈現為指令的內容仍然被拒絕，而且拒絕
一樣發生在任何 side effect 之前。改變的只是「什麼叫呈現為指令」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 作業系統層級的命令詞。刻意不含任何 EDA／vendor／專案工具名——那是 Package
#: 的知識，由 `extra_commands` 提供。
COMMAND_WORDS: frozenset[str] = frozenset({
    # 檔案系統與破壞性操作
    "rm", "rmdir", "mv", "cp", "dd", "shred", "truncate",
    "chmod", "chown", "chgrp", "ln", "mkfs", "mount", "umount",
    # 提權
    "sudo", "su", "doas",
    # interpreter / shell
    "sh", "bash", "zsh", "csh", "tcsh", "ksh", "fish",
    "env", "eval", "exec", "source", "xargs",
    "python", "python2", "python3", "perl", "ruby", "node", "php", "awk", "sed",
    # 網路
    "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "netcat", "telnet",
    # 行程與服務
    "kill", "pkill", "killall", "systemctl", "service", "reboot", "shutdown",
    # 套件與版本控制
    "apt", "apt-get", "yum", "dnf", "pacman", "brew", "pip", "pip3", "npm",
    "yarn", "git", "svn",
})

#: 這些命令詞加上一個 flag 就是明確的呼叫形式，不必倚賴位置判斷。
_INTERPRETERS: frozenset[str] = frozenset({
    "sh", "bash", "zsh", "csh", "tcsh", "ksh", "fish", "env", "python",
    "python2", "python3", "perl", "ruby", "node", "php", "xargs", "eval", "exec",
})

# ── stable reason codes ───────────────────────────────────────────────
FENCED_CODE_BLOCK = "fenced_code_block"
COMMAND_SUBSTITUTION = "command_substitution"
SHELL_CONTROL_OPERATOR = "shell_control_operator"
INTERPRETER_INVOCATION = "interpreter_invocation"
COMMAND_WITH_FLAG = "command_with_flag"
COMMAND_AT_COMMAND_POSITION = "command_at_command_position"
EXECUTION_VERB_INVOCATION = "execution_verb_invocation"
PATH_AT_COMMAND_POSITION = "path_at_command_position"

REASON_CODES: tuple[str, ...] = (
    FENCED_CODE_BLOCK, COMMAND_SUBSTITUTION, SHELL_CONTROL_OPERATOR,
    INTERPRETER_INVOCATION, COMMAND_WITH_FLAG, EXECUTION_VERB_INVOCATION,
    COMMAND_AT_COMMAND_POSITION, PATH_AT_COMMAND_POSITION,
)

#: 散文裡的祈使執行動詞。`Run make finish` / `Execute docker run …` 雖然不在
#: 行首命令位置，語意上仍是叫人去執行一條指令，必須拒絕。動詞後面要緊接
#: 命令詞才算——`run the timing analysis` 不會命中。
_EXECUTION_VERBS: frozenset[str] = frozenset({
    "run", "runs", "execute", "executes", "invoke", "invokes",
    "launch", "launches", "issue", "type",
})

#: 命令位置：行首（可帶縮排／bullet／prompt）、或串接運算子之後。
_COMMAND_POSITION = r"""
    (?:
        (?:\A|[\n\r])[ \t]*                  # 行首
        (?:[-*•·]|\d+[.)])?[ \t]*  # 可選的 bullet
        (?:[$\#%>][ \t]+)?                   # 可選的 shell prompt
      | (?<=[;|])[ \t]*                      # 串接／pipe 之後
      | (?<=&&)[ \t]*
      | (?<=\|\|)[ \t]*
      | (?:\A|[\s(])[$\#%][ \t]+             # 句中的 prompt marker
    )
"""

#: command 形狀的延續：flag、path、引號字串、另一個命令詞，或整行只有它。
#: `{words}` 由 `_tail_pattern()` 在執行期填入，讓 `sudo rm ...` 這種
#: 「命令詞接命令詞」的提權形式也被認出來。
_COMMAND_TAIL = r"""
    (?=
        [ \t]*(?:$|[\n\r])                   # 單獨成行的裸命令
      | [ \t]+-{{1,2}}[A-Za-z0-9]             # flag
      | [ \t]+(?:\.{{0,2}}/|~/)               # path
      | [ \t]+['"]                            # 引號字串
      | [ \t]+\S*/                            # 含斜線的 token
      | [ \t]+(?:{words})(?![\w.-])           # 另一個命令詞
    )
"""

_SUBSTITUTION = re.compile(r"\$\(|\$\{|`[^`]*`")
_FENCE = re.compile(r"```|~~~")
_CONTROL = re.compile(r"&&|\|\|")


@dataclass(frozen=True)
class CommandFinding:
    """一筆「這段文字呈現為指令」的判定。"""

    reason: str
    marker: str
    position: int
    excerpt: str

    def to_json_dict(self) -> dict[str, object]:
        return {"reason": self.reason, "marker": self.marker,
                "position": self.position, "excerpt": self.excerpt}


def _excerpt(text: str, start: int, length: int = 60) -> str:
    lo = max(0, start - 10)
    return text[lo:start + length].strip()


def _word_pattern(words: frozenset[str] | set[str]) -> str:
    # 長的先比，避免 `pip` 先吃掉 `pip3`
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def find_command_invocation(
    text: str, *, extra_commands: frozenset[str] | set[str] | tuple[str, ...] = (),
) -> CommandFinding | None:
    """回傳第一筆判定；文字不呈現為指令時回 `None`。

    `extra_commands` 讓呼叫端補上自己 domain 的命令名（Common 不知道那些）。
    """
    if not isinstance(text, str) or not text.strip():
        return None

    match = _FENCE.search(text)
    if match:
        return CommandFinding(FENCED_CODE_BLOCK, match.group(0), match.start(),
                              _excerpt(text, match.start()))
    match = _SUBSTITUTION.search(text)
    if match:
        return CommandFinding(COMMAND_SUBSTITUTION, match.group(0), match.start(),
                              _excerpt(text, match.start()))
    match = _CONTROL.search(text)
    if match:
        return CommandFinding(SHELL_CONTROL_OPERATOR, match.group(0), match.start(),
                              _excerpt(text, match.start()))

    words = set(COMMAND_WORDS) | {w.strip().lower() for w in extra_commands if w.strip()}
    lowered = text.lower()

    # interpreter ＋ flag：無論位置都是明確的呼叫形式（例如 `sh -c '...'`）
    interp = re.compile(
        rf"(?<![\w.-])({_word_pattern(_INTERPRETERS)})[ \t]+-{{1,2}}[A-Za-z0-9]")
    match = interp.search(lowered)
    if match:
        return CommandFinding(INTERPRETER_INVOCATION, match.group(1), match.start(),
                              _excerpt(text, match.start()))

    # 命令詞 ＋ flag：`rm -rf`、`git -C`。仍以 word boundary 比對，
    # 所以 `confirm`／`platform`／`transform` 內含的 `rm` 不會命中。
    with_flag = re.compile(
        rf"(?<![\w.-])({_word_pattern(words)})[ \t]+-{{1,2}}[A-Za-z0-9]")
    match = with_flag.search(lowered)
    if match:
        return CommandFinding(COMMAND_WITH_FLAG, match.group(1), match.start(),
                              _excerpt(text, match.start()))

    # 散文中的「執行動詞 ＋ 命令詞」
    verb_call = re.compile(
        rf"(?<![\w.-])(?:{_word_pattern(_EXECUTION_VERBS)})[ \t]+"
        rf"({_word_pattern(words)})(?![\w.-])")
    match = verb_call.search(lowered)
    if match:
        return CommandFinding(EXECUTION_VERB_INVOCATION, match.group(1),
                              match.start(1), _excerpt(text, match.start(1)))

    # 命令位置上的命令詞
    pattern = _word_pattern(words)
    at_position = re.compile(
        _COMMAND_POSITION + rf"(?<![\w.-])({pattern})(?![\w.-])"
        + _COMMAND_TAIL.format(words=pattern), re.VERBOSE)
    match = at_position.search(lowered)
    if match:
        return CommandFinding(COMMAND_AT_COMMAND_POSITION, match.group(1),
                              match.start(1), _excerpt(text, match.start(1)))

    # 命令位置上的可執行路徑（`./flow.sh`、`/usr/bin/x`、`~/bin/y`）
    path_at_position = re.compile(
        _COMMAND_POSITION + r"(\.{0,2}/|~/)\S*\S", re.VERBOSE)
    match = path_at_position.search(text)
    if match:
        return CommandFinding(PATH_AT_COMMAND_POSITION, match.group(1),
                              match.start(1), _excerpt(text, match.start(1)))
    return None


def looks_like_command(
    text: str, *, extra_commands: frozenset[str] | set[str] | tuple[str, ...] = (),
) -> bool:
    return find_command_invocation(text, extra_commands=extra_commands) is not None


__all__ = [
    "COMMAND_AT_COMMAND_POSITION",
    "COMMAND_SUBSTITUTION",
    "COMMAND_WITH_FLAG",
    "COMMAND_WORDS",
    "CommandFinding",
    "EXECUTION_VERB_INVOCATION",
    "FENCED_CODE_BLOCK",
    "INTERPRETER_INVOCATION",
    "PATH_AT_COMMAND_POSITION",
    "REASON_CODES",
    "SHELL_CONTROL_OPERATOR",
    "find_command_invocation",
    "looks_like_command",
]
