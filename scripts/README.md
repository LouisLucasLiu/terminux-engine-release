## 出貨的 scripts

| 檔案 | 用途 |
| --- | --- |
| `terminux-shell-hooks.bash`、`terminux-shell-hooks.csh`、`terminux-tcsh-postcmd` | Terminux 終端機的 shell hooks，設定方式見頂層 README 的「Shell hooks」一節 |
| `terminux-llm-env.sh`、`terminux-llm-env.csh` | LLM backend 設定範本；會送資料到 LLM provider 的功能預設關閉 |
| `terminux-chat` | 從 shell 對 daemon 提問（與終端機內的 `@@` 相同） |
| `terminux-lineage-graph` | 產生 artifact lineage graph 或 execution DAG 的 HTML，並用瀏覽器開啟 |
| `terminux-build-rag-index` | 建立 RAG 知識索引（需要 `[rag]` extra） |
| `terminux-dream` | 產生每日工作摘要，供 cron 使用（見下節） |

## terminux-dream（daily cron）

Daily cron 每天凌晨整理昨天的 events 成 period_digest + daily_digest。摘要由設定的 LLM provider 產生，
送出的資料見頂層 README 的「送往 LLM provider 的資料」。

### crontab

    0 2 * * * /path/to/python /path/to/scripts/terminux-dream --user-id $USER --date yesterday >> ~/.terminux/logs/dream.log 2>&1

### SGE

    0 2 * * * qsub -q <queue> -N terminux-dream -o ~/.terminux/logs/dream.log scripts/terminux-dream --user-id $USER --date yesterday

### Backfill（第一次部署或補救）

    terminux-dream --user-id $USER --backfill --days 30

### 注意

- `--date today` 可手動跑，但今天 events 仍在進行中，digest 不完整。
- 若 CLI exit code = 1，代表有 period 或 daily_digest 失敗，細節在 stdout。
