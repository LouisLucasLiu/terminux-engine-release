# Third-Party Software Notices and Information

This project incorporates, links to, or depends on third-party software as described below.

---

## 1. Embedded Source Code (內嵌原始碼)

The following third-party source code is included directly within the repository:

### libvterm
- **Path**: `third_party/libvterm/`
- **Copyright**: Copyright (c) 2008 Paul Evans <leonerd@leonerd.org.uk>
- **License**: MIT License
- **Notice**:
```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

---

## 2. Qt Framework (C++ GUI & Terminal)

Terminux Engine C++ GUI components dynamically link to Qt 6 libraries:

- **Qt Modules**:
  - `Qt6::Core`
  - `Qt6::Gui`
  - `Qt6::Widgets`
  - `Qt6::Network`
  - `Qt6::Qml`
  - `Qt6::Quick`
  - `Qt6::QuickControls2`
  - `QtQuick.Shapes`（QML 模組）
  - `Qt6::QmlModels`
  - `Qt6::OpenGL`
  - `Qt6::DBus`
  - `Qt6::Test` (for testing only)
- **Linking Method**: Dynamic Linking (`.so` shared libraries). Users can replace the Qt libraries with their own compatible versions.
- **License**: GNU Lesser General Public License version 3 (LGPLv3) / GNU General Public License version 3 (GPLv3).
  - The Qt libraries are licensed under the GNU Lesser General Public License (LGPL) version 3. Dynamic linking complies with LGPLv3 Section 4.
  - Full LGPLv3 text: <https://www.gnu.org/licenses/lgpl-3.0.html>
- **ICU (International Components for Unicode)**:
  - Dynamically linked by Qt for Unicode/i18n support.
  - License: Unicode-DFS-2016 / ICU License (<https://unicode.org/copyright.html>).

---

## 3. Python Dependencies (Python Core & Extensions)

The Python engine declares the following dependencies in `pyproject.toml`. License information is derived directly from the installed package metadata:

### Core Dependencies (必要相依)

| Package | Installed Version | License | License Expression / Notes |
|---------|-------------------|---------|----------------------------|
| `pydantic` | 2.12.5 | MIT | MIT License |
| `pyyaml` | 6.0.3 | MIT | MIT License |
| `httpx` | 0.28.1 | BSD-3-Clause | BSD 3-Clause "New" or "Revised" License |

### Optional Dependencies (選擇性相依)

| Extra | Package | Installed Version | License | Notes |
|-------|---------|-------------------|---------|-------|
| `gemini` | `google-genai` | 1.71.0 | Apache-2.0 | Apache License 2.0 |
| `anthropic` | `anthropic` | 0.84.0 | MIT | MIT License |
| `rag` | `llama-index-core` | 0.14.22 | MIT | MIT License |
| `local-llm` | `llama-cpp-python` | (Optional) | MIT | Upstream PyPI License: MIT |

---

## 4. External EDA Tools (外部 EDA 工具，未隨附)

Terminux 的 ORFS 相關功能會呼叫下列開源 EDA 工具。Terminux **不內嵌、不隨附、不再散布**這些工具或其原始碼；使用者需自行取得 OpenROAD-flow-scripts 與 EDA 工具的主機安裝（通常由 CAD／IT 管理員提供）。`terminux-orfs-run` 以使用者自己的帳號在主機上執行 ORFS 的 `make` 流程；Engineering Work 的 flow task、formal pair 與 single-instance ECO 目前仍以使用者在設定中宣告的 ORFS container image 執行。Terminux 讀取 ORFS 的輸出檔（log、report、metrics、netlist 等），並沿用 ORFS 的介面慣例（Makefile 變數如 `DESIGN_CONFIG`／`FLOW_VARIANT`／`WORK_HOME`、stage 檔名、metrics key 名稱如 `finish__timing__setup__ws`、platform 與範例設計名稱）。

使用到這些工具的入口模組：

- `engine/src/terminux_engine/orchestration/orfs_adapter.py`
- `engine/src/terminux_engine/orchestration/orfs_native_adapter.py`
- `engine/src/terminux_engine/orchestration/orfs_flow_task_adapter.py`
- `engine/src/terminux_engine/orchestration/orfs_single_run_runner.py`
- `engine/src/terminux_engine/orchestration/orfs_cli.py`
- `engine/src/terminux_engine/packages/orfs_dqe/`
- `engine/src/terminux_engine/packages/orfs_run_intelligence/`

### OpenROAD-flow-scripts (ORFS)
- **Upstream Source**: <https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts>
- **Copyright**: Copyright (c) 2018-2023, The Regents of the University of California. All rights reserved.
- **License**: BSD 3-Clause License（依上游 `LICENSE_BUILD_RUN_SCRIPTS`）
- **Notes**: 上游聲明此授權**只**適用於 ORFS 的 build／run scripts；flow 所依賴的工具、platform 與設計各有自己的授權，詳見上游 README 的 LICENSE 一節。
- **開發驗證所用版本**：ORFS commit `be0dca0b1fd41df54792b3012350cd52bccd99bb` 的 `flow/` 與 `env.sh`。0.1.0 的驗證直接在主機上執行，不使用 container；搭配的工具為 OpenROAD `26Q3-1510-g6cb3f2b704`、Yosys `0.68+post`（git `a5af9d690a43744bf6b2cc3dea2717c16b54621c`）與 KLayout `0.28.16`。

### OpenROAD
- **Upstream Source**: <https://github.com/The-OpenROAD-Project/OpenROAD>
- **Copyright**: Copyright (c) 2018-2025, The OpenROAD Authors. All rights reserved.
- **License**: BSD 3-Clause License（依上游 `LICENSE`）
- **開發驗證所用版本**：ORFS 子模組 `tools/OpenROAD` commit `6cb3f2b704604a8961573218e1c8adaae9fe9937`（版本字串 `26Q3-1510-g6cb3f2b704`）。

---

## 5. Browser-Loaded Components (瀏覽器端載入的元件，未隨附)

### Mermaid
- **Upstream Source**: <https://github.com/mermaid-js/mermaid>
- **License**: MIT License
- **Notes**: Terminux 產生的 lineage graph 與 execution DAG HTML 在瀏覽器開啟時，才從 jsDelivr 下載 Mermaid（`mermaid@11.17.2/dist/mermaid.min.js`）並以 Subresource Integrity 驗證內容；Terminux 本身不內嵌、不隨附 Mermaid。版本與雜湊定義在 `engine/src/terminux_engine/lineage/mermaid_cdn.py`。
