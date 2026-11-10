# Terminux

Terminux Engine

isvn 為獨立發布的產品。
isvn is an independently released product.

> 完整的使用者文件（安裝指南、User Guide、DQE Viewer、LLM 與憑證設定）預定發布在 Terminux
> 文件網站 https://terminux.mtda.dev/ ，目前**尚未上線**。在那之前，請以本 README 為準。

## 安裝 Python 套件

需要 Python 3.12 以上與 `uv`。在原始碼根目錄建立虛擬環境並安裝：

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install './engine[gemini]'
```

`[gemini]` 只在使用 Gemini 時需要；`openai_compatible` 不需要額外套件，`llama_cpp`
需要 `[local-llm]`。
`[rag]` 供 `scripts/terminux-build-rag-index` 建立 RAG 知識索引。`[anthropic]` 只給保留中的舊版
Layer 2 通知路徑使用；0.1.0 沒有啟用這條路徑的 plugin，一般使用不需要安裝。

## 建置與安裝 GUI（含 DQE Viewer）

需要 CMake 3.16 以上、C++17 編譯器，以及 Qt 6.7 以上的 **Desktop gcc 64-bit 預設元件**。
請把 GUI 安裝到與 Python 環境相同的 prefix：

```bash
cmake -S . -B build -DCMAKE_PREFIX_PATH=/path/to/Qt/6.7.3/gcc_64 -DTERMINUX_BUILD_TESTS=OFF
cmake --build build
cmake --install build --prefix "$VIRTUAL_ENV"
```

安裝後會有 `$VIRTUAL_ENV/bin/terminux`（GUI）與 `$VIRTUAL_ENV/bin/dqe_viewer`（DQE Viewer），
以及它們需要的 Qt runtime 與 QML module。Viewer 由收到 `launch_viewer` 回應的
GUI／CLI 在**本機**啟動，不由 daemon 啟動。`TERMINUX_DQE_VIEWER_BIN` 為選用覆寫：
GUI 預設使用與自身執行檔同目錄的 `dqe_viewer`；`terminux-chat` 依序搜尋環境變數、
`<sys.prefix>/bin/dqe_viewer`、`<sys.prefix>/../bin/dqe_viewer`、PATH。
回應攜帶 `tab`、`db`、
`temporary_db_path`，以及可選的 `focus_job_key`，不攜帶執行檔路徑。

### 使用外部 Qt

外部 Qt 模式不安裝或隨包附上 Qt。自行安裝 Qt **6.7 以上的 Desktop gcc 64-bit 預設元件**即可，
無須額外勾選附加模組。SDK 目錄須保留 `lib/`、`plugins/`、`qml/` 與
`lib/cmake/Qt6/` 的版本資訊；安裝前檢查會確認所需的 QML imports 及 xcb／offscreen plugins。

完整函式庫、QML imports 與 plugin 清單隨 Python wheel 的
`terminux_engine/resources/qt_runtime.json` 提供。先安裝 Python 套件，再檢查 Qt：

```bash
terminux-qt-check --qt-dir /absolute/path/to/Qt/gcc_64
```

若從原始碼安裝並選用外部 Qt，使用以下配置：

```bash
cmake -S . -B build-external -DCMAKE_PREFIX_PATH=/absolute/path/to/Qt/gcc_64 \
  -DTERMINUX_EXTERNAL_QT=ON -DTERMINUX_BUILD_TESTS=OFF -DTERMINUX_BUILD_INTERNAL_TARGETS=OFF
cmake --build build-external --target terminux dqe_viewer
cmake --install build-external --prefix "$VIRTUAL_ENV"
ln -s /absolute/path/to/Qt/gcc_64 "$VIRTUAL_ENV/qt"
terminux-qt-check --qt-dir "$VIRTUAL_ENV/qt" --prefix "$VIRTUAL_ENV"
```

安裝後的 `bin/terminux` 與 `bin/dqe_viewer` 使用 `$ORIGIN/../qt/lib` 的 RUNPATH；
同目錄的 `qt.conf` 指向 `../qt`，plugin 與 QML imports 分別從其 `plugins/`、`qml/` 載入。
不需設定 `LD_LIBRARY_PATH`、`QT_PLUGIN_PATH`、`QML2_IMPORT_PATH`，因此不會由這個安裝流程
把 Qt 搜尋路徑傳給內建終端機啟動的 EDA 工具。

帶 `--prefix` 的檢查會以 offscreen 執行兩個程式的 `--self-check`，不建立視窗或連接 daemon。
若 xcb 缺少系統函式庫，檢查會列出 SONAME 與已知的 EL9／Ubuntu 24.04 套件名稱；請系統
管理員安裝相符套件，不會由檢查工具自行安裝。多主機環境中，使用者的 Qt 目錄在每台主機
必須具有**相同的絕對路徑**，並在每台主機完成檢查。

## 可攜式安裝包（Linux x86_64）

不自行建置時，可以改用同一次發布附帶的 `terminux-<版本>-linux-x86_64.tar.gz`。安裝包內含
Python 3.12、Terminux 的 Python 套件與其相依、已建置的 GUI 與 DQE Viewer，以及第三方授權原文；
Qt 不隨附，安裝時以 `--qt-dir` 指向自行安裝的 Qt 6.7 以上 Desktop gcc 64-bit SDK。
安裝到 `~/.local/opt/terminux/<版本>`，不需要 root，不修改 rc 檔，安裝時不連網。

下載安裝包與同次發布的 `SHA256SUMS`，核對後解壓並安裝：

```bash
sha256sum -c --ignore-missing SHA256SUMS
tar -xzf terminux-<版本>-linux-x86_64.tar.gz
cd terminux-<版本>
./install.sh --qt-dir /path/to/Qt/6.7.3/gcc_64
```

支援的發行版、需要的系統函式庫、Qt 檢查、移除與首次啟動的細節見安裝包內的 `README-INSTALL.md`
（原始碼位置 [src/portable/README-INSTALL.md](src/portable/README-INSTALL.md)）。

## 設定 LLM provider 與憑證

憑證放在 `~/.terminux/credentials.json`，權限必須是 `0600`（其他權限會被整份忽略）：

```bash
mkdir -p ~/.terminux
cat > ~/.terminux/credentials.json <<'JSON'
{"TERMINUX_GEMINI_API_KEY": "你的 API key"}
JSON
chmod 600 ~/.terminux/credentials.json
```

支援的名稱有 `TERMINUX_GEMINI_API_KEY`、`TERMINUX_OPENAI_API_KEY`、`TERMINUX_OPENAI_BASE_URL`。
可用 `TERMINUX_CREDENTIALS_FILE` 指定其他位置。同名環境變數仍可使用，但只作為相容的替代
方式；credentials 檔優先。

backend 以 `TERMINUX_LLM_BACKEND` 選擇，沒有預設值：

| backend | 用途 |
| --- | --- |
| `gemini` | Gemini API |
| `openai_compatible` | DeepSeek 等 OpenAI 相容服務，或本機的 OpenAI 相容服務（`TERMINUX_OPENAI_BASE_URL`）。Agent（`@@`）使用這條路徑或 `gemini` |
| `llama_cpp` | 在本機載入 GGUF 模型（`TERMINUX_LLM_MODEL_PATH`），只有基本問答，沒有 Agent 能力 |

`scripts/terminux-llm-env.sh` 與 `.csh` 是設定範本，取消註解其中一種 backend 後再 `source`。
本機 OpenAI 相容服務能否完成 Agent 流程，取決於模型與服務本身，目前尚未驗證。

## 送往 LLM provider 的資料

沒有設定 `TERMINUX_LLM_BACKEND`、`TERMINUX_EMBEDDING_BACKEND`，也沒有 `~/.terminux/llm_router.yaml` 時，
Terminux 不會把資料送到任何 LLM provider。設定之後，下列功能會把資料送到所選的 provider。`gemini` 與 `openai_compatible`（例如
DeepSeek）是雲端服務；`llama_cpp` 與本機的 OpenAI 相容服務只在本機處理。設計腳本、路徑、設計名稱與
timing 結果屬於機密時，請先確認 provider 的資料處理條款，或只使用本機 provider。

| 功能 | 何時送出 | 送出的資料 |
| --- | --- | --- |
| AI 問答與 `@@` Agent | 你提問時 | 問題與對話紀錄；目前 session 的狀態（stage、corner、block、gate 結果、WNS，以及最近的 lineage 檢查，含命令與工作目錄）；工作摘要與相關的過去問答；其他 terminal 的狀態。Agent 另外只會送出它查到的執行證據：所查 run 的狀態與結束原因、step 與工具的識別、receipt 與 artifact manifest 的識別碼與雜湊、DQE 結果，以及 report 與 lineage 的索引（只有雜湊與大小，不含檔案內容）。timing path 的逐級資料預設不送出。Agent 不會讀取檔案內容，也不會執行 shell 指令。Profile 模式（見「使用 AI」）下只送出 Profile 的指示、這個視窗的對話紀錄、已綁定的 run id 與查到的證據 |
| ORFS run 的解說、比較與狀況評估 | 你透過 `@@` 要求時；狀況評估也會在新的 ORFS run 的 DQE 結果寫入後自動執行 | run 的識別資訊（design、platform、variant）、DQE 檢查結果與 metrics、ORFS report／log 的節錄，以及與 baseline 的比較 |
| 工作摘要 | 終端機閒置一段時間後再次使用時，以及 daemon 啟動時補做先前的摘要 | 時段內用過的 EDA 工具、corner、gate 結果、WNS 變化、問答次數與對話重點 |
| 對話整理 | 對話紀錄過長時 | 對話內容 |
| block 名稱推斷 | 載入 STA playbook 後，執行命令前判斷不出所屬 block 時（tcsh hooks 會對每個命令做這項判斷） | 命令與工作目錄 |
| Embedding（搜尋索引） | 背景執行；建立 RAG 知識索引時 | 問答、AI 回覆、提示訊息、lineage 檢查訊息、對話重點與工作摘要；搜尋時的問題；知識文件內容 |
| Lineage 說明（預設關閉） | 設定 `TERMINUX_ENABLE_LINEAGE_EXPLAINER_RUNTIME=1`，且執行 EDA 工具前的 lineage 檢查有疑慮時 | 命令、工作目錄、runfile 路徑、檢查結果與相關檔案的訊息 |
| runfile 解析備援（預設關閉） | 設定 `TERMINUX_ENABLE_INTENT_FALLBACK_RUNTIME=1`，且固定格式解析器讀不出 runfile 的輸入輸出時 | **完整的 runfile 內容**、命令、工作目錄與 runfile 路徑 |

- Embedding 的目的地與上面的 provider 分開決定：`TERMINUX_LLM_BACKEND=gemini`，或 `openai_compatible` 且
  credentials 檔有 `TERMINUX_GEMINI_API_KEY` 時，送到 Gemini embedding API。設定
  `TERMINUX_EMBEDDING_BACKEND=noop` 可關閉，`local` 則改用本機模型（`TERMINUX_EMBEDDING_MODEL_PATH`）。
- 自動的狀況評估、工作摘要與對話整理沒有個別開關；不設定 backend 就不會送出。
- 若 `/projusers/<project>/<帳號>/.terminux/event_store.sqlite` 存在且可讀（`<project>` 是 HOME 上一層目錄的
  名稱），AI 問答也會帶入同一 project 其他成員的帳號、block、corner 與 WNS 摘要。
- lineage graph 與 execution DAG 的 HTML 在瀏覽器開啟時，會從 jsDelivr 下載固定版本的 Mermaid，並以
  Subresource Integrity 驗證內容；這個下載不含任何 Terminux 資料。

## 啟動 Terminux

```bash
export TERMINUX_LLM_BACKEND=gemini
terminux-launch
```

`terminux-launch` 依共享 `owner.lock/owner.json` 在本機啟動 daemon，或在另一台 login server
啟動 `terminux-relay`，然後啟動 GUI。只要背景服務時使用 `terminux-launch --no-gui`；
`terminux-launch --reset` 會顯示 owner 資訊，輸入 `yes` 後只移除 lock，**不會停止遠端 daemon**。
`TERMINUX_GUI_BIN` 是選用的優先覆寫；未設定時，依序搜尋
`<sys.prefix>/bin/terminux`、`<sys.prefix>/../bin/terminux`、PATH。PATH 中只接受 ELF 執行檔，
跳過 launcher 本身與啟動 wrapper，避免遞迴啟動。daemon 與 relay 使用啟動 launcher 的同一個
Python 環境。沒有 owner 而由 launcher 啟動 daemon 時，最多等待 30 秒至 owner ready；
逾時會警告 daemon 尚未就緒、hook 可能未部署，再繼續啟動 GUI。

同一使用者的 daemon 只有一個。預設本機 Core／lineage socket 分別為
`<passwd home>/.terminux/run/<nodename>/terminux.sock`、
`<passwd home>/.terminux/run/<nodename>/lineage.sock`；owner metadata 位於
`<passwd home>/.terminux/run/owner.lock/owner.json`。`TERMINUX_HOME_DIR` 可覆寫私有
基底目錄，`TERMINUX_NODENAME` 可覆寫主機識別，`TERMINUX_ADVERTISE_HOST` 可覆寫
owner 公告的 TCP host。舊預設路徑的 `TERMINUX_CORE_SOCK`／`TERMINUX_SOCK`
覆寫會被忽略；其他明確覆寫仍有效。daemon 已被其他 owner 持有或偵測到舊 daemon 時
以 exit code 4 拒絕啟動；直接存取資料庫的 CLI 在 owner 位於其他主機時以 exit code 5
拒絕執行。

### 服務 log

daemon 的結構化訊息寫入 `~/.terminux/run/<nodename>/daemon.log`，relay 寫入同一主機
目錄的 `relay.log`；兩者各使用 10 MiB（10,485,760 bytes）上限、保留 3 份備份的
輪替檔案（`.1`～`.3`）。路徑與 socket 共用集中解析，`TERMINUX_HOME_DIR` 覆寫 home
基底，`TERMINUX_NODENAME` 覆寫主機目錄名；單獨設定 `HOME` 不會改變服務 log 位置。

daemon 取得 `owner.lock`、relay 取得本機單一實例後才開啟檔案 handler，釋放所有權前
關閉；取得所有權前的訊息只送到 stderr，取得後的結構化訊息只寫輪替檔。
`terminux-launch` 將程序 stdout／stderr 另外收集到 `daemon.stdout.log` 或
`relay.stdout.log`，可用來查看取得所有權前的失敗。

daemon 預設仍為 ERROR，`TERMINUX_LOG_LEVEL` 可設為 DEBUG／INFO／WARNING／ERROR／CRITICAL；
未設定或值無效時使用 ERROR。relay 維持 INFO。正常啟停在預設 daemon 等級下沒有訊息，
因此空 log 不代表 logging 故障。`TERM_LEGACY_SOCKET_OVERRIDE_IGNORED` 是 WARNING，
需將 daemon 等級設為 WARNING 或更詳細才會寫入；relay 運行中遇到 owner 連線／讀取
失敗時，以 INFO 記錄 `TERM_DAEMON_OWNER_UNAVAILABLE`。launcher 在取得所有權前的
同名錯誤碼只送到 stderr。

多 login server 部署需共享 HOME，且 Python 環境與 GUI 執行檔在各主機的同一路徑可用；
owner 的兩個 TCP port 須能從其他 login server 連接。TCP bridge 以 token 認證，**不加密**，
請限制可連網段。一般 AI 問答需要可用的 LLM
provider；STA playbook 為選用。

- 驗證狀態：**RELEASE_QUALIFIED_SIMULATED_MULTI_HOST**。
  單機及模擬多主機環境已驗證，正式站點尚未驗證；模擬環境使用兩台 VM、NFSv4.2。
- 尚未驗證：正式站點的網路、NFSv3、時鐘偏差、不同 OS 混用、真實 EDA、compute node、
  NFS 長時間卡住。
- 已知限制：
  - owner 轉移到其他主機後，原主機上的 GUI 要重新執行一次 `terminux-launch --no-gui` 才會恢復連線；
  - 多個 GUI 會同時顯示同一則通知的 action，重複點擊會執行多次；
  - GUI 斷線太久時，超過 500 筆緩衝範圍的通知會遺失；
  - 連線不加密，只適用於受信任的網段。

若要啟用 STA 的 stage 追蹤、quality gate 與 nudge，從已安裝的套件部署範本（範本隨套件安裝，
位於 `terminux_engine.ecc` 的 `templates/sta_signoff_playbook.yaml`）：

```bash
mkdir -p ~/.terminux/ecc
python3 -c "from importlib.resources import files; import shutil, pathlib; shutil.copy(files('terminux_engine.ecc') / 'templates' / 'sta_signoff_playbook.yaml', pathlib.Path.home() / '.terminux' / 'ecc')"
```

已啟動的 daemon 需重啟才會載入。可用 `TERMINUX_PLAYBOOK_PATH` 指定其他範本位置。套件安裝與
daemon 的 wrapper auto setup 不會自動部署 playbook。

## Shell hooks（終端機整合）

daemon 啟動時會自動安裝並更新 Bash／tcsh hooks、postcmd helper 與
`~/.terminux/hooks/bash_rcfile`，不修改任何使用者 rc 檔。

GUI 內建終端機選用 bash 時，會自動以 `--rcfile` 載入已部署的 rcfile：先 source
`~/.bashrc`，再 source `~/.terminux/bash_hooks.sh`，**不必修改 rc**。若 `.bashrc` 原本已經
source hook，也只會註冊一次。互動提示前會將 wrapper 目錄去重並恢復到 PATH 最前面。

外部 bash 終端機不會自動整合；若要啟用，需自行 source，或將這一行加入自己的 `.bashrc`：

```bash
[ -f ~/.terminux/bash_hooks.sh ] && source ~/.terminux/bash_hooks.sh
```

tcsh 仍需手動設定：**有 `~/.tcshrc` 就加在 `~/.tcshrc`，沒有才加在 `~/.cshrc`**：

```tcsh
if ( $?TERMINUX_ACTIVE ) source ~/.terminux/tcsh_hooks.csh
```

`TERMINUX_ACTIVE=1` 由 GUI 注入，讓這一行只在 Terminux 的內建終端機中載入 hook，避免影響
一般外部終端機。外部終端機若也要使用，可明確設定該變數後自行 source。GUI 在 csh／tcsh
啟動後 10 秒內未收到 OSC 133 hook 標記時，會顯示一次設定提示；不會自動修改 rc 檔。
傳統 csh 使用 `.cshrc`，沒有 tcsh 的自動 postcmd／precmd 機制；部分系統的 csh 是 tcsh 的別名。

- hooks 透過 `~/.terminux/bin/terminux-notify` 通知 daemon。這個檔案由 daemon 啟動時的 wrapper 設定安裝；
  設定 `TERMINUX_AUTO_SETUP_WRAPPERS=0` 時不會安裝，hooks 也就不會送出任何通知。
- 命令結束時，hooks 送出結束狀態、工作目錄與 shell PID；GUI 另外把該命令與最近的終端機輸出交給本機 daemon。
  tcsh 版也會送出展開後的命令列，並在每次顯示提示前把 `~/.terminux/bin` 放到 PATH 最前面。
- 成功結束的 EDA 命令若帶 `-f <runfile>`，daemon 會把 runfile 宣告的輸入與輸出記到
  `~/.terminux/lineage/ledger.jsonl`。daemon 只讀取這些檔案，不會建立或修改它們。
- wrapper 另寫入每台主機自己的 `~/.terminux/lineage.<nodename>.jsonl`；daemon 會讀取各主機
  檔案與舊版 `lineage.jsonl`。

## 從終端機執行 ORFS（`terminux-orfs-run`）

ORFS 直接在主機上、以你自己的帳號執行：由 CAD／IT 管理員安裝 ORFS 與 EDA 工具，你只需要一個
自己可寫入的輸出目錄。不需要 Docker、container、sudo 或 root，也不需要對 ORFS 安裝目錄有寫入權。

```bash
terminux-orfs-run --orfs-repo-root /path/to/OpenROAD-flow-scripts \
  --work-home ~/orfs-runs/my-run --design gcd --platform nangate45 --variant my-run \
  --design-config ./designs/nangate45/gcd/config.mk \
  --ledger-db ~/orfs-runs/my-run-ledger.sqlite --receipt-out ~/orfs-runs/my-run-receipt.json
```

- `--work-home` 是這次執行的輸出目錄（ORFS 的 `WORK_HOME`），必須由你擁有。
- 工具環境由 `--env-script`（預設 `<orfs-repo-root>/env.sh`）提供；除了 `HOME`、`LANG` 與 `PATH`，
  Terminux 不繼承你 shell 的環境。需要時可用 `--make-executable`、`--openroad-exe`、`--yosys-exe`
  指定絕對路徑。
- ORFS 的最後一步需要 KLayout。`klayout` 必須在執行 `terminux-orfs-run` 時的 `PATH` 上，或由
  `--env-script` 加進 `PATH`；找不到時 flow 會在 ORFS 的 `check-klayout` 停下，以 exit code 1 結束，
  先前各階段的輸出與 DQE 紀錄仍會保留。
- `--design-config` 是相對於 ORFS `flow/` 的路徑，設定檔必須位於 `flow/` 之下，不能指向其他位置。
  沒有 ORFS 安裝目錄的寫入權時，可以自備一份 `--env-script`：先 `source` 原本的 `env.sh`，再 `export`
  設定檔中以 `?=` 定義的變數（例如 `CORE_UTILIZATION`）。env script 的內容雜湊會記在 receipt 內。
- `--ledger-db` 必須是這次執行專用的路徑，不可以是 daemon 的
  `~/.terminux/orchestration/ledger.sqlite`（指向它時會直接拒絕）。
- 執行結果（receipt）預設寫入目前使用者的 EventStore，daemon 與 `@@` 因此查得到這個 run；
  `--no-receipt` 不寫入，`--receipt-store` 寫到指定的 EventStore。
- exit code：

  | exit code | 意義 |
  | --- | --- |
  | 0 | 執行成功，且 receipt 已寫入 |
  | 1 | 執行失敗 |
  | 2 | 參數或環境設定錯誤（含以 root 執行、`--work-home` 不屬於你） |
  | 3 | 執行成功，但 receipt 沒有寫入 |
  | 130／143 | 收到 Ctrl-C／SIGTERM：確認 flow 的 process group 已結束時寫入 `cancelled` receipt；無法確認時不寫 receipt |

## 使用 AI（`@@`）

在 Terminux 視窗的終端機提示字元後輸入 `@@`，後面接問題，再按 Enter。這一行不會交給 shell 執行，
回答顯示在視窗上方的通知列；回答出現幾秒後通知列會收合成標題列，點標題列，或在終端機按右鍵選
**Notification Bar ▸ Show**，可以重看。`@@` 後面的文字是交給 Terminux，shell 變數不會展開，路徑請寫
完整的絕對路徑。需要先設定 LLM backend（見「設定 LLM provider 與憑證」）；Agent 只在 `gemini` 與
`openai_compatible` 可用。

另一種提問方式：用滑鼠選取終端機上的文字，按右鍵選 **Ask AI about this**，選取的文字就是問題；之後再用
`@@` 追問，會接續同一段對話。

AI 只能查詢已記錄的執行證據（receipt、artifact manifest、DQE 結果）：不能讀取你的檔案、不能執行 shell
指令，也不會替你核准或執行任何動作。各項功能送出的資料見「送往 LLM provider 的資料」。

### 針對一次 ORFS 執行提問

1. 以 `terminux-orfs-run` 執行 flow（見上一節）。`--receipt-out` 寫出的 receipt 裡有這次執行的 `run_id`
   與 `attempt`；stdout 最後的 DQE 摘要也會列出 run id。
2. 綁定這次執行（需要下一節的 Profile 模式）：

   ```
   @@/bind-run /absolute/path/to/receipt.json
   ```

   成功時通知列顯示 `RUN_BOUND: run_… attempt 1 is now bound to this session (host_native nangate45/gcd).`。
   綁定只存在這個 Terminux 視窗的 session 內，daemon 重啟後消失；`@@/unbind-run` 解除。
3. 直接提問，例如：

   ```
   @@For the Run I just bound, which DQE checks are WARN or FAIL? Show the run evidence and where I can verify it.
   @@Based on the evidence from this run, what should I inspect next, and why?
   ```

   沒有 Profile 模式時，把 run id 寫在問題裡也可以讓 AI 查詢同一份證據。
4. 自己核對回答：`@@open DQE Viewer` 開啟 DQE Viewer（這個指令由 Terminux 本機處理，不會送出 AI 請求），
   在「失敗」篩選下看各項檢查；`terminux-orfs-report --receipt <receipt> --run-id <run_id> --attempt <attempt>`
   印出經 receipt 與 artifact manifest 驗證的 finish report。AI 看到的是 DQE 摘要，不是 report 本身，
   timing 數值要在 Viewer 與 report 上自己核對。

沒有綁定、問題裡也沒有 run id 時，AI 會說明沒有可查的證據，只回答一般性的說明。

### 啟用 Profile 模式（管理者設定）

Profile 模式把 AI 的對話限制在管理者指定的範圍內，並讓 `/bind-run` 可用；它只支援 `openai_compatible` backend。
daemon 啟動時讀取下列環境變數；
兩個檔案都必須是**本人擁有、權限 0600 的一般檔案**，用絕對路徑指定，內容有任何不合法的地方整份視為無效、
AI 請求會被拒絕，不會退回沒有限制的模式。

| 環境變數 | 內容 |
| --- | --- |
| `TERMINUX_AGENT_TASK_PROFILE_FILE` | Profile（JSON）：AI 可用的工具、可帶入的對話內容、允許的 run，以及 `/bind-run` 的規則 |
| `TERMINUX_OUTBOUND_PAYLOAD_POLICY_FILE` | 外送 policy（JSON）：允許的 provider endpoint 與 model、可外送的資料類別與各項上限 |
| `TERMINUX_EGRESS_AUDIT_DIGEST` | 外送紀錄的雜湊方式：`plain_sha`、`keyed_ephemeral` 或 `keyed_persisted`（後者另需 `TERMINUX_EGRESS_AUDIT_KEYRING_FILE`）。policy 用 `structural_c2` 時必須設定 |

Profile 範例（只允許查詢執行證據，允許綁定 nangate45／gcd 的 host-native 執行）：

```json
{
  "profile_id": "gcd-evidence",
  "tools": ["query_orchestration_evidence"],
  "context_sections": ["conversation_history"],
  "allowed_runs": {},
  "run_binding": {
    "max_runs_per_session": 2,
    "require_runner": "host_native",
    "allowed_designs": ["nangate45/gcd"],
    "max_age_sec": 604800
  }
}
```

- `tools` 目前只能是 `query_orchestration_evidence`；`context_sections` 只能是 `conversation_history`。
- `allowed_runs` 是預先允許的 run id 與 attempt；一般留空，改以 `run_binding` 讓使用者自己綁定。
- `run_binding` 四個鍵都必填：每個 session 最多綁定幾次（1～8）、receipt 的 runner（`terminux-orfs-run`
  是 `host_native`）、允許的 `<platform>/<design>`、receipt 距今的最長秒數。receipt 必須是 EventStore 內
  本人的正式紀錄，檔案內容的雜湊要與紀錄相符。

外送 policy 範例（v2，structural 模式；DeepSeek 的 `deepseek-chat`）：

```json
{
  "policy_version": "terminux.egress_policy.v2",
  "approval_mode": "structural_c2",
  "endpoint": "https://api.deepseek.com/v1/chat/completions",
  "model": "deepseek-chat",
  "allowed_payload_sha256": [],
  "approved_data_classes": ["system_instructions", "learner_text", "conversation_history",
                            "tool_schema", "tool_call", "evidence_bundle", "model_output",
                            "budget_summary"],
  "limits": {"learner_text_max_chars": 4000, "history_max_turns": 20, "history_max_chars": 60000,
             "model_output_max_chars": 60000, "requests_per_turn": 4},
  "non_agent_egress": "deny"
}
```

- `endpoint` 必須等於 provider 的 `<TERMINUX_OPENAI_BASE_URL>/v1/chat/completions`，`model` 必須等於
  `TERMINUX_OPENAI_MODEL`；不一致時每個請求都會被拒絕。
- `approved_data_classes` 列出允許外送的資料類別；`timing_path_evidence`（timing path 的逐級資料）
  不列出就不會送出。`limits` 五個鍵都必填，數值超出範圍整份無效。
- `allowed_payload_sha256` 在 `structural_c2` 模式必須是空的；`exact_digest` 模式則只放行清單內的請求內容。
- 每個外送決策都會記在 `~/.terminux/run/<nodename>/egress_audit/<日期>.jsonl`（只有中繼資料與雜湊，沒有內容）。

Profile 模式下，AI 的 system prompt 只有 Profile 的指示、這個視窗的對話紀錄與已綁定的 run id；
「送往 LLM provider 的資料」表中 session 狀態、工作摘要、其他 terminal 的狀態都不會送出。

## License

This project is licensed under the PolyForm Shield License 1.0.0. See the [LICENSE.md](LICENSE.md) file for full details.
For third-party software licenses and notices, see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- **Permitted Use**: Any organization (including design service companies and semiconductor design houses) may use this software free of charge, including for internal business purposes and on customer projects.
- **Noncompete Condition**: You may not use this software to provide any product or service that competes with Terminux.
- **Source-Available**: This project is source-available software, not open source as defined by the Open Source Initiative (OSI).
- **Inquiries**: For other licensing inquiries or custom arrangements, please contact louis.cat.work@gmail.com.

## 授權條款

本專案採用 PolyForm Shield License 1.0.0 授權，詳細條款以 [LICENSE.md](LICENSE.md) 為準。
第三方軟體之授權資訊與聲明請參閱 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

- **免費使用範圍**：任何組織（含設計服務公司、晶片設計公司）皆可免費使用，包括內部業務使用及為客戶專案提供服務。
- **反競爭限制**：不得用於提供與 Terminux 競爭的產品或服務。
- **原始碼可用（Source-Available）**：本專案為 source-available 軟體，並非開源促進會（OSI）定義之開放原始碼（Open Source）軟體。
- **其他授權需求**：若有其他授權需求或客製化安排，請聯絡 louis.cat.work@gmail.com。

## Contributing / 貢獻

目前不接受外部 Pull Request；問題請透過 issue 回報。
We do not accept external pull requests at this time. Please report issues via GitHub Issues.
