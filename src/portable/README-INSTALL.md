# Terminux portable bundle — installation / 可攜式安裝包安裝說明

[English](#english) · [繁體中文](#繁體中文)

## English

### Download

Download `terminux-<version>-linux-x86_64.tar.gz` and the `SHA256SUMS` of the same release, then in the download directory:

```bash
sha256sum -c --ignore-missing SHA256SUMS
tar -xzf terminux-<version>-linux-x86_64.tar.gz
cd terminux-<version>
```

Supported: x86_64 Ubuntu 24.04, RHEL/Rocky/AlmaLinux 9, CentOS Stream 9.
Requires glibc 2.34 or later, a system libstdc++ that provides `GLIBCXX_3.4.29`, and enough space to extract and install.
No root is needed. Python 3.12 and the basic Python dependencies are included; installation does not use the network.
Qt is not included: install the **Desktop gcc 64-bit default components of Qt 6.7 or later** yourself and make sure that
SDK is compatible with the host OS. The SDK must contain the libraries, the QML imports, and the xcb and offscreen
platform plugins.

Ask your system administrator for the X11/xcb and graphics system libraries; common packages are listed below.
The installer's `terminux-qt-check` reports the exact missing SONAMEs and never installs anything itself.

| Purpose | EL9 | Ubuntu 24.04 |
| --- | --- | --- |
| xcb cursor | `xcb-util-cursor` | `libxcb-cursor0` |
| xcb utility / image / keysyms / renderutil / wm | `xcb-util`, `xcb-util-image`, `xcb-util-keysyms`, `xcb-util-renderutil`, `xcb-util-wm` | `libxcb-util1`, `libxcb-image0`, `libxcb-keysyms1`, `libxcb-render-util0`, `libxcb-icccm4` |
| XKB | `libxkbcommon`, `libxkbcommon-x11` | `libxkbcommon0`, `libxkbcommon-x11-0` |
| X11 / xcb | `libX11`, `libX11-xcb`, `libxcb` | `libx11-6`, `libx11-xcb1`, `libxcb1` |

Package names follow the [Red Hat package manifest](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/package_manifest/)
and the [Ubuntu package data](https://packages.ubuntu.com/noble/libxcb-cursor0); for any other missing library, the checker's output is authoritative.

### Install

First stop any Terminux daemon running under this account; if the owner of a shared HOME is on another host, stop it
there first. Use Ctrl+C in the terminal that started it, or your usual service manager — the installer never kills a daemon.

```bash
./install.sh --qt-dir /your/Qt/6.7.3/gcc_64
```

Confirm at the prompt, or add `--yes`. The default location is `~/.local/opt/terminux/<version>`; the installer creates a
`qt` symlink and switches `current` only after the Qt check passes. When `current/qt` already exists, `--qt-dir` may be
omitted; an upgrade keeps the previous version. Installing the same version again stops unless you add `--reinstall`.
On failure the new `.partial` directory is removed and `current` is not switched.
`--prefix <path>` changes the location; `--no-bin-link` skips the two entries in `~/.local/bin`.
`--force` only bypasses the unsupported-OS block; it cannot bypass the architecture, ABI, Qt or active-daemon checks.

To check again later:

```bash
~/.local/bin/terminux-qt-check --qt-dir ~/.local/opt/terminux/current/qt --prefix ~/.local/opt/terminux/current
```

To remove one version or all of them:

```bash
~/.local/opt/terminux/current/uninstall.sh --version <version>
~/.local/opt/terminux/current/uninstall.sh --all
```

Data, settings and logs under `~/.terminux` are kept by default. `--purge-data` deletes them, and even with `--yes` you
must type the full data path to confirm. The remover deletes wrappers and hooks conservatively; files in `~/.local/bin`
not created by this installation are never overwritten or deleted. No rc file is modified; after removal, delete unused
hook lines yourself as the output suggests.

### First Launch

Put `~/.local/bin` on your PATH — bash or tcsh:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

```tcsh
setenv PATH "$HOME/.local/bin:$PATH"
```

Start:

```bash
terminux
```

The GUI's built-in bash integrates automatically without rc changes; external terminals must opt in themselves.
For tcsh, add this line to `~/.tcshrc`, or to `~/.cshrc` only if there is no `.tcshrc`:

```tcsh
if ( $?TERMINUX_ACTIVE ) source ~/.terminux/tcsh_hooks.csh
```

`TERMINUX_ACTIVE` is set only in the GUI's built-in terminal, so ordinary external shells are unaffected.
The hooks are deployed by the automatic setup of the first daemon start; if the launcher warns that deployment failed
or is disabled, check the log as the message suggests.

`~/.local/bin` only contains `terminux` (starts the GUI) and `terminux-qt-check`. The other commands
(`terminux-orfs-run`, `terminux-orfs-report`, `terminux-launch`, `terminux-daemon`, …) live in
`~/.local/opt/terminux/current/venv/bin/`; to type them directly in a terminal — including Terminux's built-in
terminal — add that directory to your PATH yourself.

Configuring an LLM provider is optional. Without a provider no LLM request is sent; see the section "Configuring the LLM
provider and credentials" in the main README shipped with the product. This bundle contains only the basic dependencies;
the Gemini, Anthropic, local-llm and RAG extras are not included. Prepare those packages separately before using a
provider that needs them.

Known limits: a working graphical session is required to open the GUI; a passing offscreen self-check does not mean a
remote display is configured. In a multi-host environment the Qt directory and the installation path must be identical
on every host, and Qt and the system libraries must be available on each host. `LD_LIBRARY_PATH`, `QT_PLUGIN_PATH` and
`QML2_IMPORT_PATH` are not set; Qt is located through the installation's RUNPATH and `qt.conf`. Compatibility with other
operating systems, architectures, Qt SDKs or providers must be confirmed separately.

## 繁體中文

## Download

下載 `terminux-<版本>-linux-x86_64.tar.gz` 與同次發布的 `SHA256SUMS`，在下載目錄執行：

```bash
sha256sum -c SHA256SUMS
tar -xzf terminux-<版本>-linux-x86_64.tar.gz
cd terminux-<版本>
```

支援 x86_64 的 Ubuntu 24.04、RHEL／Rocky／AlmaLinux 9、CentOS Stream 9。
需要 glibc ≥ 2.34、提供 GLIBCXX_3.4.29 的系統 libstdc++，以及足夠的解壓與安裝空間。
不需要 root。Python 3.12 與基本 Python 相依已包含在安裝包，安裝時不連網。
Qt 不隨附：請先自行安裝 **Qt 6.7 以上、Qt 6 系列的 Desktop gcc 64-bit 預設元件**，
並確保該 Qt SDK 相容於主機 OS。SDK 需包含函式庫、QML imports、xcb 與 offscreen platform plugin。

請系統管理員提供 X11／xcb 與圖形相關系統函式庫，常見套件如下。
安裝器的 `terminux-qt-check` 會依實際缺少的 SONAME 顯示完整診斷，並不代為安裝。

| 用途 | EL9 | Ubuntu 24.04 |
| --- | --- | --- |
| xcb cursor | `xcb-util-cursor` | `libxcb-cursor0` |
| xcb utility／image／keysyms／renderutil／wm | `xcb-util`、`xcb-util-image`、`xcb-util-keysyms`、`xcb-util-renderutil`、`xcb-util-wm` | `libxcb-util1`、`libxcb-image0`、`libxcb-keysyms1`、`libxcb-render-util0`、`libxcb-icccm4` |
| XKB | `libxkbcommon`、`libxkbcommon-x11` | `libxkbcommon0`、`libxkbcommon-x11-0` |
| X11／xcb | `libX11`、`libX11-xcb`、`libxcb` | `libx11-6`、`libx11-xcb1`、`libxcb1` |

套件名稱依 [Red Hat 套件清單](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/package_manifest/)
與 [Ubuntu 套件資料](https://packages.ubuntu.com/noble/libxcb-cursor0)；其他缺庫仍以檢查器輸出為準。

## Install

先停止這個帳號正在執行的 Terminux daemon；共享 HOME 的 owner 若在另一台主機，須先到該主機停止。
使用原啟動終端的 Ctrl+C 或原服務管理方式停止，安裝器不會終止 daemon。

```bash
./install.sh --qt-dir /你的/Qt/6.7.3/gcc_64
```

依提示確認，或加 `--yes`。預設安裝到 `~/.local/opt/terminux/<版本>`，建立 `qt` symlink，
檢查 Qt 成功後才切換 `current`。已有 `current/qt` 時可省略 `--qt-dir`；升級保留舊版本。
同版本會停止，明確加 `--reinstall` 才重裝。失敗會移除新建的 `.partial`，不切換 `current`。
`--prefix <路徑>` 可改位置，`--no-bin-link` 不建立 `~/.local/bin` 的兩個入口。
`--force` 只略過不支援 OS 的阻擋，不能略過架構、ABI、Qt 或活動 daemon 檢查。

可再次檢查：

```bash
~/.local/bin/terminux-qt-check --qt-dir ~/.local/opt/terminux/current/qt --prefix ~/.local/opt/terminux/current
```

移除指定版本或全部版本：

```bash
~/.local/opt/terminux/current/uninstall.sh --version <版本>
~/.local/opt/terminux/current/uninstall.sh --all
```

預設保留 `~/.terminux` 的資料、設定與 log。加 `--purge-data` 才清除資料，且即使加 `--yes`，
仍須輸入完整資料路徑確認。移除器先保守移除 wrapper／hook；非本安裝建立的 `~/.local/bin` 檔案不會被覆蓋或刪除。
不會修改任何 rc 檔；移除後請依輸出自行刪除不再使用的 hook 行。

## First Launch

把 `~/.local/bin` 放進 PATH，bash 與 tcsh 擇一：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

```tcsh
setenv PATH "$HOME/.local/bin:$PATH"
```

啟動：

```bash
terminux
```

GUI 內建 bash 會自動整合，不必修改 rc；外部終端機仍需自行 opt-in。
tcsh 請在 `~/.tcshrc` 加入以下一行；沒有 `.tcshrc` 才放在 `~/.cshrc`：

```tcsh
if ( $?TERMINUX_ACTIVE ) source ~/.terminux/tcsh_hooks.csh
```

`TERMINUX_ACTIVE` 只在 GUI 內建終端機設定，避免影響一般外部 shell。
hook 由首次啟動 daemon 的自動 setup 部署；若 launcher 警告部署失敗或停用，請先依提示查看 log。

LLM provider 設定為選用。未設定 provider 不會發送 LLM 請求；請參閱隨產品發布的主 README
「設定 LLM provider 與憑證」段落。此包只含基本相依，未包含 Gemini、Anthropic、local-llm 或 RAG extras。
使用有額外相依的 provider 前需另行準備對應套件。

已知限制：需要可用的圖形工作階段才能正常開 GUI；offscreen 自我檢查通過不代表遠端顯示已配置。
Qt 目錄與安裝路徑在多主機環境的每台主機必須相同；Qt 與系統庫必須在各主機分別可用。
不設定 LD_LIBRARY_PATH、QT_PLUGIN_PATH 或 QML2_IMPORT_PATH；Qt 透過安裝目錄的 RUNPATH 與 qt.conf 定位。
其他 OS、架構、Qt SDK 或 provider 的相容性需另行確認。
