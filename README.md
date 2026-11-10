# Terminux

Terminux Engine

isvn is an independently released product. / isvn 為獨立發布的產品。

**Language / 語言：** [English](#english) · [繁體中文](#繁體中文)

> The full user documentation (installation guide, user guide, DQE Viewer, LLM and credential setup) will be published
> on the Terminux documentation site at https://terminux.mtda.dev/ , which is **not online yet**. Until then, this README
> is the reference.
>
> 完整的使用者文件（安裝指南、User Guide、DQE Viewer、LLM 與憑證設定）預定發布在 Terminux
> 文件網站 https://terminux.mtda.dev/ ，目前**尚未上線**。在那之前，請以本 README 為準。

---

# English

## Installing the Python package

Requires Python 3.12 or later and `uv`. Create a virtual environment in the source root and install:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install './engine[gemini]'
```

`[gemini]` is only needed when you use Gemini; `openai_compatible` needs no extra packages, and `llama_cpp`
needs `[local-llm]`. `[rag]` is for `scripts/terminux-build-rag-index`, which builds the RAG knowledge index.
`[anthropic]` only serves a retained legacy notification path; 0.1.x ships no plugin that enables it, so
ordinary use does not need it.

## Building and installing the GUI (including the DQE Viewer)

Requires CMake 3.16 or later, a C++17 compiler, and the **Desktop gcc 64-bit default components** of Qt 6.7 or later.
Install the GUI into the same prefix as the Python environment:

```bash
cmake -S . -B build -DCMAKE_PREFIX_PATH=/path/to/Qt/6.7.3/gcc_64 -DTERMINUX_BUILD_TESTS=OFF
cmake --build build
cmake --install build --prefix "$VIRTUAL_ENV"
```

After installation you have `$VIRTUAL_ENV/bin/terminux` (the GUI) and `$VIRTUAL_ENV/bin/dqe_viewer` (the DQE Viewer),
together with the Qt runtime and QML modules they need. The Viewer is started **locally** by the GUI or CLI that
receives a `launch_viewer` response; the daemon never starts it. `TERMINUX_DQE_VIEWER_BIN` is an optional override:
the GUI defaults to the `dqe_viewer` next to its own executable; `terminux-chat` searches the environment variable,
`<sys.prefix>/bin/dqe_viewer`, `<sys.prefix>/../bin/dqe_viewer`, then PATH, in that order.
The response carries `tab`, `db`, `temporary_db_path` and an optional `focus_job_key`; it never carries an executable path.

### Using an external Qt

External-Qt mode neither installs nor bundles Qt. Install the **Desktop gcc 64-bit default components of Qt 6.7 or later**
yourself; no additional modules are required. The SDK directory must keep the version information in `lib/`, `plugins/`,
`qml/` and `lib/cmake/Qt6/`; the pre-install check confirms the required QML imports and the xcb/offscreen plugins.

The full list of libraries, QML imports and plugins ships with the Python wheel as
`terminux_engine/resources/qt_runtime.json`. Install the Python package first, then check Qt:

```bash
terminux-qt-check --qt-dir /absolute/path/to/Qt/gcc_64
```

To install from source with an external Qt, configure like this:

```bash
cmake -S . -B build-external -DCMAKE_PREFIX_PATH=/absolute/path/to/Qt/gcc_64 \
  -DTERMINUX_EXTERNAL_QT=ON -DTERMINUX_BUILD_TESTS=OFF -DTERMINUX_BUILD_INTERNAL_TARGETS=OFF
cmake --build build-external --target terminux dqe_viewer
cmake --install build-external --prefix "$VIRTUAL_ENV"
ln -s /absolute/path/to/Qt/gcc_64 "$VIRTUAL_ENV/qt"
terminux-qt-check --qt-dir "$VIRTUAL_ENV/qt" --prefix "$VIRTUAL_ENV"
```

The installed `bin/terminux` and `bin/dqe_viewer` use a RUNPATH of `$ORIGIN/../qt/lib`; the `qt.conf` next to them
points to `../qt`, and plugins and QML imports load from its `plugins/` and `qml/`. `LD_LIBRARY_PATH`, `QT_PLUGIN_PATH`
and `QML2_IMPORT_PATH` are not needed, so this installation never passes Qt search paths to EDA tools started from
the built-in terminal.

With `--prefix`, the check runs both programs' `--self-check` offscreen; it opens no window and connects to no daemon.
If xcb lacks system libraries, the check lists the SONAMEs with the known package names for EL9 and Ubuntu 24.04;
ask your system administrator to install them — the tool never installs anything itself. In a multi-host environment
the user's Qt directory must have the **same absolute path** on every host, and the check must pass on every host.

## Portable bundle (Linux x86_64)

If you do not want to build, use the `terminux-<version>-linux-x86_64.tar.gz` shipped with the same release.
The bundle contains Python 3.12, the Terminux Python package and its dependencies, the prebuilt GUI and DQE Viewer,
and the third-party license texts. Qt is not included: point `--qt-dir` at a Qt 6.7+ Desktop gcc 64-bit SDK you
installed yourself. It installs into `~/.local/opt/terminux/<version>`, needs no root, modifies no rc files, and
does not use the network during installation.

Download the bundle and the `SHA256SUMS` of the same release, verify, extract and install:

```bash
sha256sum -c --ignore-missing SHA256SUMS
tar -xzf terminux-<version>-linux-x86_64.tar.gz
cd terminux-<version>
./install.sh --qt-dir /path/to/Qt/6.7.3/gcc_64
```

Supported distributions, required system libraries, the Qt check, removal and first launch are described in the
bundle's `README-INSTALL.md` (source: [src/portable/README-INSTALL.md](src/portable/README-INSTALL.md)).

## Configuring the LLM provider and credentials

Credentials live in `~/.terminux/credentials.json`, which must have mode `0600` (any other mode makes the whole file
ignored):

```bash
mkdir -p ~/.terminux
cat > ~/.terminux/credentials.json <<'JSON'
{"TERMINUX_GEMINI_API_KEY": "your API key"}
JSON
chmod 600 ~/.terminux/credentials.json
```

Supported names are `TERMINUX_GEMINI_API_KEY`, `TERMINUX_OPENAI_API_KEY` and `TERMINUX_OPENAI_BASE_URL`.
`TERMINUX_CREDENTIALS_FILE` selects another location. Environment variables of the same names still work, but only as
a compatibility fallback; the credentials file wins.

The backend is selected with `TERMINUX_LLM_BACKEND`; there is no default:

| backend | Purpose |
| --- | --- |
| `gemini` | Gemini API |
| `openai_compatible` | DeepSeek and other OpenAI-compatible services, or a local OpenAI-compatible service (`TERMINUX_OPENAI_BASE_URL`). The Agent (`@@`) uses this path or `gemini` |
| `llama_cpp` | Loads a GGUF model locally (`TERMINUX_LLM_MODEL_PATH`); basic Q&A only, no Agent capability |

`scripts/terminux-llm-env.sh` and `.csh` are configuration templates: uncomment one backend, then `source` the file.
Whether a local OpenAI-compatible service can complete the Agent flow depends on the model and the service; this is
not verified yet.

## Data sent to the LLM provider

When `TERMINUX_LLM_BACKEND`, `TERMINUX_EMBEDDING_BACKEND` and `~/.terminux/llm_router.yaml` are all absent,
Terminux sends nothing to any LLM provider. Once configured, the features below send data to the selected provider.
`gemini` and `openai_compatible` (for example DeepSeek) are cloud services; `llama_cpp` and a local OpenAI-compatible
service stay on your machine. If design scripts, paths, design names and timing results are confidential, check the
provider's data-processing terms first, or use a local provider only.

| Feature | When it sends | What it sends |
| --- | --- | --- |
| AI Q&A and the `@@` Agent | When you ask | The question and the conversation history; the current session state (stage, corner, block, gate results, WNS, and the most recent lineage checks with command and working directory); the work summary and related past Q&A; the state of other terminals. In addition the Agent sends only the execution evidence it looked up: the status and terminal outcome of the queried run, step and tool identifiers, receipt and artifact-manifest identifiers and hashes, DQE results, and the index of reports and lineage (hashes and sizes only, no file contents). Per-stage timing-path data is not sent by default. The Agent never reads file contents and never runs shell commands. In Profile mode (see "Using AI") only the Profile instructions, this window's conversation history, the bound run ids and the evidence looked up are sent |
| Explanation, comparison and situation assessment of ORFS runs | When you request it through `@@`; situation assessment also runs automatically after a new ORFS run's DQE results are written | Run identity (design, platform, variant), DQE check results and metrics, excerpts of ORFS reports/logs, and the comparison with the baseline |
| Work summary | When the terminal is used again after being idle for a while, and when the daemon starts and catches up on earlier summaries | EDA tools used in the period, corners, gate results, WNS changes, number of questions and conversation highlights |
| Conversation condensation | When the conversation history grows too long | The conversation |
| Block-name inference | After an STA playbook is loaded, when the block cannot be determined before a command runs (tcsh hooks do this for every command) | Command and working directory |
| Embedding (search index) | In the background; when building the RAG knowledge index | Questions, AI replies, prompt messages, lineage-check messages, conversation highlights and work summaries; the query when searching; knowledge-document contents |
| Lineage explainer (off by default) | When `TERMINUX_ENABLE_LINEAGE_EXPLAINER_RUNTIME=1` is set and the lineage check before an EDA tool raises a concern | Command, working directory, runfile path, check results and messages about the related files |
| Runfile parsing fallback (off by default) | When `TERMINUX_ENABLE_INTENT_FALLBACK_RUNTIME=1` is set and the fixed-format parser cannot read the runfile's inputs and outputs | **The complete runfile contents**, command, working directory and runfile path |

- The embedding destination is chosen separately from the provider above: with `TERMINUX_LLM_BACKEND=gemini`, or
  `openai_compatible` plus a `TERMINUX_GEMINI_API_KEY` in the credentials file, embeddings go to the Gemini embedding
  API. `TERMINUX_EMBEDDING_BACKEND=noop` turns this off; `local` uses a local model instead (`TERMINUX_EMBEDDING_MODEL_PATH`).
- Automatic situation assessment, work summaries and conversation condensation have no individual switches; without a
  configured backend nothing is sent.
- If `/projusers/<project>/<account>/.terminux/event_store.sqlite` exists and is readable (`<project>` is the name of
  the directory above HOME), AI Q&A also includes the accounts, blocks, corners and WNS summaries of other members of
  the same project.
- When the lineage-graph and execution-DAG HTML pages are opened in a browser, they download a pinned version of Mermaid
  from jsDelivr and verify it with Subresource Integrity; that download carries no Terminux data.

## Starting Terminux

```bash
export TERMINUX_LLM_BACKEND=gemini
terminux-launch
```

`terminux-launch` starts the daemon locally according to the shared `owner.lock/owner.json`, or starts
`terminux-relay` when the owner is on another login server, and then starts the GUI. Use `terminux-launch --no-gui`
for the background service only; `terminux-launch --reset` shows the owner information and, after you type `yes`,
removes only the lock — it **does not stop a remote daemon**. `TERMINUX_GUI_BIN` is an optional override; when unset,
the launcher searches `<sys.prefix>/bin/terminux`, `<sys.prefix>/../bin/terminux`, then PATH. On PATH only ELF
executables are accepted, and the launcher itself and start-up wrappers are skipped to avoid recursion. The daemon and
the relay use the same Python environment as the launcher. When there is no owner and the launcher starts the daemon,
it waits up to 30 seconds for the owner to become ready; on timeout it warns that the daemon is not ready and hooks may
not be deployed, then continues to start the GUI.

Each user has exactly one daemon. The default local Core and lineage sockets are
`<passwd home>/.terminux/run/<nodename>/terminux.sock` and
`<passwd home>/.terminux/run/<nodename>/lineage.sock`; the owner metadata is
`<passwd home>/.terminux/run/owner.lock/owner.json`. `TERMINUX_HOME_DIR` overrides the private base directory,
`TERMINUX_NODENAME` the host identifier, and `TERMINUX_ADVERTISE_HOST` the TCP host advertised by the owner.
`TERMINUX_CORE_SOCK`/`TERMINUX_SOCK` overrides pointing at the old default paths are ignored; other explicit overrides
still apply. The daemon refuses to start with exit code 4 when another owner holds it or an old daemon is detected;
CLIs that access the databases directly refuse with exit code 5 when the owner is on another host.

### Service logs

The daemon's structured messages go to `~/.terminux/run/<nodename>/daemon.log`, the relay's to `relay.log` in the same
host directory; both are rotating files with a 10 MiB (10,485,760 bytes) limit and 3 backups (`.1`–`.3`). Paths and
sockets share one resolver: `TERMINUX_HOME_DIR` overrides the home base and `TERMINUX_NODENAME` the host directory name;
setting `HOME` alone does not move the service logs.

The daemon opens the file handler only after acquiring `owner.lock`, the relay after acquiring its local single
instance, and both close it before releasing ownership; messages before ownership go to stderr only, structured messages
after ownership go to the rotating file only. `terminux-launch` additionally collects the process stdout/stderr into
`daemon.stdout.log` or `relay.stdout.log`, which show failures that happen before ownership is acquired.

The daemon default level is still ERROR; `TERMINUX_LOG_LEVEL` accepts DEBUG/INFO/WARNING/ERROR/CRITICAL, and an unset
or invalid value means ERROR. The relay stays at INFO. A normal start and stop produces no messages at the default daemon
level, so an empty log does not mean logging is broken. `TERM_LEGACY_SOCKET_OVERRIDE_IGNORED` is a WARNING and only
appears at WARNING or more verbose; a running relay logs `TERM_DAEMON_OWNER_UNAVAILABLE` at INFO when the owner
connection or read fails. The launcher prints the same error codes to stderr only before ownership is acquired.

A multi-login-server deployment needs a shared HOME, the Python environment and the GUI executable at the same path on
every host, and the owner's two TCP ports reachable from the other login servers. The TCP bridge authenticates with a
token and is **not encrypted**; restrict the reachable network segment. Ordinary AI Q&A needs a working LLM provider;
the STA playbook is optional.

- Verification status: **RELEASE_QUALIFIED_SIMULATED_MULTI_HOST**. Verified on a single host and in a simulated
  multi-host environment (two VMs, NFSv4.2); not verified on a production site.
- Not verified: production-site networking, NFSv3, clock skew, mixed operating systems, real EDA tools, compute nodes,
  long NFS stalls.
- Known limitations:
  - after the owner moves to another host, the GUI on the original host must run `terminux-launch --no-gui` once more to reconnect;
  - several GUIs show the same notification's action at the same time, and repeated clicks execute it repeatedly;
  - when the GUI is disconnected for too long, notifications beyond the 500-entry buffer are lost;
  - the connection is not encrypted and is only suitable for trusted network segments.

To enable STA stage tracking, quality gates and nudges, deploy the template from the installed package (it ships inside
the package, at `templates/sta_signoff_playbook.yaml` of `terminux_engine.ecc`):

```bash
mkdir -p ~/.terminux/ecc
python3 -c "from importlib.resources import files; import shutil, pathlib; shutil.copy(files('terminux_engine.ecc') / 'templates' / 'sta_signoff_playbook.yaml', pathlib.Path.home() / '.terminux' / 'ecc')"
```

A running daemon must be restarted to load it. `TERMINUX_PLAYBOOK_PATH` selects another template location. Neither the
package installation nor the daemon's wrapper auto-setup deploys the playbook.

## Shell hooks (terminal integration)

When the daemon starts it installs and updates the Bash/tcsh hooks, the postcmd helper and
`~/.terminux/hooks/bash_rcfile`, without modifying any user rc file.

When the built-in terminal of the GUI uses bash, it loads the deployed rcfile with `--rcfile`: it sources `~/.bashrc`
first, then `~/.terminux/bash_hooks.sh`, so **no rc change is needed**. If `.bashrc` already sources the hook, it is
registered only once. Before each interactive prompt the wrapper directory is de-duplicated and moved back to the front
of PATH.

External bash terminals are not integrated automatically; to enable them, source the hook yourself or add this line to
your own `.bashrc`:

```bash
[ -f ~/.terminux/bash_hooks.sh ] && source ~/.terminux/bash_hooks.sh
```

tcsh still needs a manual step: **add the line to `~/.tcshrc` if it exists, otherwise to `~/.cshrc`**:

```tcsh
if ( $?TERMINUX_ACTIVE ) source ~/.terminux/tcsh_hooks.csh
```

`TERMINUX_ACTIVE=1` is injected by the GUI, so the line loads the hook only inside Terminux's built-in terminal and
leaves ordinary external terminals alone. An external terminal can set the variable explicitly and source the file.
When csh/tcsh sends no OSC 133 hook marker within 10 seconds of starting, the GUI shows a one-time setup hint; it never
modifies rc files. Classic csh uses `.cshrc` and has no tcsh-style automatic postcmd/precmd; on some systems csh is an
alias of tcsh.

- The hooks notify the daemon through `~/.terminux/bin/terminux-notify`, installed by the wrapper setup when the daemon
  starts; with `TERMINUX_AUTO_SETUP_WRAPPERS=0` it is not installed and the hooks send nothing.
- When a command finishes, the hooks send its exit status, working directory and shell PID; the GUI additionally hands the
  command and the most recent terminal output to the local daemon. The tcsh version also sends the expanded command line
  and puts `~/.terminux/bin` at the front of PATH before every prompt.
- For an EDA command that finishes successfully with `-f <runfile>`, the daemon records the inputs and outputs declared by
  the runfile in `~/.terminux/lineage/ledger.jsonl`. The daemon only reads those files; it never creates or modifies them.
- The wrapper also writes a per-host `~/.terminux/lineage.<nodename>.jsonl`; the daemon reads every host's file and the
  legacy `lineage.jsonl`.

## Running ORFS from the terminal (`terminux-orfs-run`)

ORFS runs directly on the host under your own account: the CAD/IT administrator installs ORFS and the EDA tools, and you
only need an output directory you can write to. No Docker, container, sudo or root is needed, and no write access to the
ORFS installation.

```bash
terminux-orfs-run --orfs-repo-root /path/to/OpenROAD-flow-scripts \
  --work-home ~/orfs-runs/my-run --design gcd --platform nangate45 --variant my-run \
  --design-config ./designs/nangate45/gcd/config.mk \
  --ledger-db ~/orfs-runs/my-run-ledger.sqlite --receipt-out ~/orfs-runs/my-run-receipt.json
```

- `--work-home` is this run's output directory (ORFS `WORK_HOME`) and must be owned by you.
- The tool environment comes from `--env-script` (default `<orfs-repo-root>/env.sh`); apart from `HOME`, `LANG` and
  `PATH`, Terminux does not inherit your shell environment. `--make-executable`, `--openroad-exe` and `--yosys-exe`
  accept absolute paths when needed.
- The last ORFS step needs KLayout. `klayout` must be on `PATH` when `terminux-orfs-run` runs, or be added to `PATH` by
  `--env-script`; otherwise the flow stops at ORFS's `check-klayout` with exit code 1, and the outputs and DQE records of
  the earlier stages are kept.
- `--design-config` is relative to the ORFS `flow/` directory; the config must live under `flow/` and cannot point
  elsewhere. Without write access to the ORFS installation you can bring your own `--env-script`: `source` the original
  `env.sh`, then `export` the variables the config defines with `?=` (for example `CORE_UTILIZATION`). The env script's
  content hash is recorded in the receipt.
- `--ledger-db` must be a path dedicated to this run; it must not be the daemon's
  `~/.terminux/orchestration/ledger.sqlite` (that path is refused).
- The result (receipt) is written to the current user's EventStore by default, so the daemon and `@@` can find the run;
  `--no-receipt` skips it, `--receipt-store` writes to the given EventStore.
- Exit codes:

  | exit code | Meaning |
  | --- | --- |
  | 0 | The run succeeded and the receipt was written |
  | 1 | The run failed |
  | 2 | Argument or environment error (including running as root, or `--work-home` not owned by you) |
  | 3 | The run succeeded but the receipt was not written |
  | 130/143 | Ctrl-C/SIGTERM received: a `cancelled` receipt is written when the flow's process group is confirmed finished; otherwise no receipt |

## Using AI (`@@`)

At the prompt of the terminal in the Terminux window, type `@@` followed by your question and press Enter. The line is
not passed to the shell; the answer appears in the Notification Bar at the top of the window. A few seconds after the
answer arrives the bar collapses to a header line; click the header, or right-click in the terminal and choose
**Notification Bar ▸ Show**, to read it again. Text after `@@` goes to Terminux: shell variables are not expanded, so
write full absolute paths. An LLM backend must be configured first (see "Configuring the LLM provider and credentials");
the Agent is available only with `gemini` and `openai_compatible`.

Another way to ask: select text in the terminal with the mouse and right-click → **Ask AI about this**; the selected
text is the question. A later `@@` question continues the same conversation.

The AI can only query recorded execution evidence (receipts, artifact manifests, DQE results): it cannot read your
files, cannot run shell commands, and never approves or performs any action on your behalf. See "Data sent to the LLM
provider" for what each feature sends.

### Asking about one ORFS run

1. Run the flow with `terminux-orfs-run` (previous section). The receipt written by `--receipt-out` contains the run's
   `run_id` and `attempt`; the DQE summary at the end of stdout also lists the run id.
2. Bind the run (requires Profile mode, next section):

   ```
   @@/bind-run /absolute/path/to/receipt.json
   ```

   On success the bar shows `RUN_BOUND: run_… attempt 1 is now bound to this session (host_native nangate45/gcd).`.
   The binding exists only in this Terminux window's session and disappears when the daemon restarts; `@@/unbind-run`
   removes it.
3. Ask directly, for example:

   ```
   @@For the Run I just bound, which DQE checks are WARN or FAIL? Show the run evidence and where I can verify it.
   @@Based on the evidence from this run, what should I inspect next, and why?
   ```

   Without Profile mode, putting the run id in the question also lets the AI query the same evidence.
4. Check the answer yourself: `@@open DQE Viewer` opens the DQE Viewer (handled locally by Terminux; no AI request is
   sent) — use the "Fail" filter to see each check; `terminux-orfs-report --receipt <receipt> --run-id <run_id> --attempt <attempt>`
   prints the finish report verified against the receipt and the artifact manifest. The AI sees the DQE summary, not
   the report itself, so verify timing numbers in the Viewer and the report.

Without a bound run and without a run id in the question, the AI states that it has no evidence to query and gives only
general information.

### Enabling Profile mode (administrator setup)

Profile mode confines the AI conversation to the scope the administrator defines and enables `/bind-run`; it supports the
`openai_compatible` backend only. The daemon reads the environment variables below at start-up. Both files must be
**regular files owned by you with mode 0600**, given as absolute paths; anything invalid makes the whole file void and AI
requests are refused — there is no fallback to the unrestricted mode.

| Environment variable | Content |
| --- | --- |
| `TERMINUX_AGENT_TASK_PROFILE_FILE` | Profile (JSON): the tools the AI may use, the conversation content it may include, the allowed runs, and the `/bind-run` rules |
| `TERMINUX_OUTBOUND_PAYLOAD_POLICY_FILE` | Outbound policy (JSON): the allowed provider endpoint and model, the data classes that may leave, and their limits |
| `TERMINUX_EGRESS_AUDIT_DIGEST` | Digest mode of the egress audit: `plain_sha`, `keyed_ephemeral` or `keyed_persisted` (the last also needs `TERMINUX_EGRESS_AUDIT_KEYRING_FILE`). Required when the policy uses `structural_c2` |

Example Profile (evidence queries only; binding host-native nangate45/gcd runs is allowed):

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

- `tools` currently accepts only `query_orchestration_evidence`; `context_sections` only `conversation_history`.
- `allowed_runs` pre-approves run ids and attempts; it is usually left empty, and `run_binding` lets users bind runs themselves.
- All four `run_binding` keys are required: the maximum number of bindings per session (1–8), the receipt's runner
  (`terminux-orfs-run` is `host_native`), the allowed `<platform>/<design>`, and the receipt's maximum age in seconds.
  The receipt must be your own official record in the EventStore, and the file's hash must match that record.

Example outbound policy (v2, structural mode; DeepSeek `deepseek-chat`):

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

- `endpoint` must equal the provider's `<TERMINUX_OPENAI_BASE_URL>/v1/chat/completions` and `model` must equal
  `TERMINUX_OPENAI_MODEL`; on a mismatch every request is refused.
- `approved_data_classes` lists the data classes allowed to leave; `timing_path_evidence` (per-stage timing-path data)
  is not sent unless listed. All five `limits` keys are required, and an out-of-range value voids the whole file.
- `allowed_payload_sha256` must be empty in `structural_c2` mode; `exact_digest` mode releases only the request payloads
  on the list.
- Every egress decision is recorded in `~/.terminux/run/<nodename>/egress_audit/<date>.jsonl` (metadata and hashes
  only, no contents).

In Profile mode the AI system prompt contains only the Profile instructions, this window's conversation history and the
bound run ids; the session state, work summary and other terminals' state listed in "Data sent to the LLM provider" are
not sent.

## License

This project is licensed under the PolyForm Shield License 1.0.0. See the [LICENSE.md](LICENSE.md) file for full details.
For third-party software licenses and notices, see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- **Permitted Use**: Any organization (including design service companies and semiconductor design houses) may use this software free of charge, including for internal business purposes and on customer projects.
- **Noncompete Condition**: You may not use this software to provide any product or service that competes with Terminux.
- **Source-Available**: This project is source-available software, not open source as defined by the Open Source Initiative (OSI).
- **Inquiries**: For other licensing inquiries or custom arrangements, please contact louis.cat.work@gmail.com.

---

# 繁體中文

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
Layer 2 通知路徑使用；0.1.x 沒有啟用這條路徑的 plugin，一般使用不需要安裝。

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
