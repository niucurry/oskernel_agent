# OS 内核代码分析 Agent

对学生提交的操作系统内核代码进行**完整性与原创性自动评估**的 AI Agent。

---

## 项目结构

```
agent/
├── agent.py                       # 入口 shim → oskernel_agent.cli.agent
├── setup_opencode.py              # 入口 shim → oskernel_agent.cli.setup_opencode
├── config.toml                    # 用户配置（git-ignored）
├── pyproject.toml                 # 包定义（src/ 布局 + console_scripts）
├── setup.sh                       # 一键安装脚本
├── requirements.txt
│
├── src/
│   └── oskernel_agent/            # 主包
│       ├── config.py              # config.toml 加载
│       ├── cli/                   # 命令行入口
│       │   ├── agent.py           # 主分析管道
│       │   ├── mcp_server.py      # MCP stdio server
│       │   ├── setup_opencode.py  # 注册到 OpenCode 全局配置
│       │   ├── fetch_repo.py      # 克隆远程仓库
│       │   └── tree_renderer.py   # 终端 rich.tree 渲染
│       ├── analysis/              # 高阶分析
│       │   └── repo_facts.py      # 仓库共享事实档案
│       ├── parsers/               # 静态解析
│       │   ├── code_parser.py     # tree-sitter + ctags + 启发式
│       │   ├── symbol_db.py       # SQLite 符号索引 + FTS5
│       │   └── os_tools.py        # Level2Index（SQLite 后端）
│       ├── engines/               # 符号解析引擎（A/B/C 三档降级）
│       │   ├── base.py
│       │   ├── lsp_base.py        # LSP 通信复用基类
│       │   ├── path_a.py          # rust-analyzer
│       │   ├── path_b.py          # clangd
│       │   ├── path_c.py          # tree-sitter 兜底
│       │   └── llm_batch.py       # LLM 批处理调度
│       ├── tools/                 # MCP 工具实现（T1–T6）
│       │   ├── mcp_tools.py
│       │   ├── tool_dispatcher.py
│       │   ├── tool_handlers.py
│       │   └── reference_db.py    # 参考 OS 指纹库
│       ├── pipeline/              # 自底向上 tree 构建
│       │   └── tree_builder.py
│       ├── prompts/               # 提示词
│       │   ├── builder.py         # 三层提示词组装
│       │   └── templates/         # dir.md / verdict.md / json_repair.md
│       └── reports/               # 报告渲染
│           ├── html.py            # 表格式 HTML
│           └── html_tree.py       # 多层折叠树 HTML
│
├── scripts/                       # 离线运维脚本
│   ├── build_reference_db.py      # 构建参考 OS 指纹库
│   ├── dump_session_prompts.py    # 导出 session prompt 到 md
│   └── validate_tone.py           # 校验 tree.json 语气分隔
│
├── data/                          # 运行时数据（git-ignored）
│   ├── historical_repos/          # 克隆下来的学生仓库
│   ├── reference_repos/           # 参考 OS 源码
│   ├── cache/                     # 符号索引缓存
│   └── reports/                   # 输出报告
└── reference_db/                  # 参考 OS 指纹 JSON
```

---

## 一、环境安装

### Linux

#### 前置依赖：OpenCode

本项目使用 [OpenCode](https://opencode.ai) 作为 AI 交互宿主，需要提前安装（要求 Node.js >= 18）：

```bash
npm install -g opencode-ai
```

安装完成后验证：`opencode --version`

#### 一键安装（推荐）

```bash
bash setup.sh
source .venv/bin/activate
```

脚本自动安装：`universal-ctags`、`clangd`、`bear`、`rust-analyzer`、Python 虚拟环境，并将 agent 注册到 OpenCode 全局配置。若 OpenCode 未安装且系统有 npm，脚本会自动安装。

#### 手动安装

```bash
# 安装 OpenCode（需要 npm）
npm install -g opencode-ai

sudo apt-get install -y universal-ctags clangd bear

curl -fL https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz \
  | gunzip -c | sudo tee /usr/local/bin/rust-analyzer > /dev/null
sudo chmod +x /usr/local/bin/rust-analyzer

pip install -r requirements.txt
# 以可编辑模式安装本项目（启用 oskernel-agent / oskernel-setup 命令）
pip install -e .

# 注册 agent 到 OpenCode 全局配置
python setup_opencode.py
```

### Windows

#### 第 0 步：基础环境准备（如果已有可跳过）

在开始之前，请确保你的电脑上已经安装了以下四款基础软件。如果没有，请点击下方链接去官网下载并默认安装（**注意：安装 Python 时，请务必勾选“Add Python.exe to PATH”**）：

1. **[Visual Studio Code](https://code.visualstudio.com/)**：我们的主力编辑器。
2. **[Python 3.x](https://www.python.org/downloads/)**：运行脚本的基础环境。
3. **[Git for Windows](https://git-scm.com/download/win)**：用于拉取代码仓库。
4. **[Rust 工具链](https://rustup.rs/)**：下载 `rustup-init.exe` 并运行，按照默认提示按 `1` 安装即可（用于解析 Rust 代码）。

---

#### 第 1 步：安装 VS Code 必备插件

打开 VS Code，点击左侧导航栏的 **“扩展”** 图标（或者按下快捷键 `Ctrl + Shift + X`），在搜索框中依次搜索并安装以下四款插件：

* **`Python`**（发布者：Microsoft）- 提供 Python 运行支持。
* **`C/C++`**（发布者：Microsoft）**或者** **`clangd`**（发布者：LLVM）- 二选一即可，用于提供 C/C++ 代码的跳转和解析。
* **`rust-analyzer`**（发布者：The Rust Programming Language）- 安装后它会在右下角提示下载语言服务器，点击允许即可。
* **`CMake Tools`**（发布者：Microsoft）- 关键插件！在 Windows 上我们将用它来替代 Linux 中的 `bear` 工具。

---

#### 第 2 步：安装全局环境依赖（关键）

虽然 VS Code 插件很强大，但我们的 Python 后台脚本仍然需要能够直接调用某些系统命令。我们需要把它们安装到系统全局。

1. 点击电脑左下角的“开始”菜单，搜索 **PowerShell**，右键选择**“以管理员身份运行”**。
2. **安装 universal-ctags**：在弹出的蓝底窗口中，复制粘贴以下命令并回车：
   ```powershell
   winget install --id=UniversalCtags.Ctags -e
   ```

*(如果提示是否同意协议，输入 `Y` 并回车)*
3. **安装 rust-analyzer 全局组件**：继续在 PowerShell 中输入以下命令并回车：

```powershell
   rustup component add rust-analyzer
```

#### 第 3 步：配置 CMake 以生成 `compile_commands.json`

Linux 系统通常使用 `bear` 来拦截编译过程并生成 `compile_commands.json`（C/C++ 代码解析必须的文件），但 Windows 不支持 `bear`。我们通过 CMake 插件来完美替代：

1. 回到 VS Code，按下快捷键 `Ctrl + ,`（逗号）打开**设置**界面。
2. 在顶部的搜索框中输入：`CMake: Export Compile Commands`。
3. 在搜索结果中，找到对应的选项并**打上勾**。
4. **如何生效**：当你用 VS Code 打开你的 C/C++ 项目，并在底部状态栏选择好编译器（Kit）后，CMake Tools 会自动进行配置（Configure），此时它就会默默在项目的 `build` 文件夹下为你生成 `compile_commands.json` 文件。

---

#### 第 4 步：初始化 Python 虚拟环境

最后一步，我们需要隔离 Python 的依赖包，防止弄乱你电脑原本的 Python 环境。

1. 在 VS Code 中打开你的项目文件夹。
2. 点击顶部菜单栏的 **“终端(Terminal)”** -> **“新建终端(New Terminal)”**（或者按 `Ctrl + \``）。
3. 确保终端类型是 PowerShell，然后依次逐行执行以下命令：

```powershell
# 1. 创建一个名为 .venv 的虚拟环境文件夹
python -m venv .venv

# 2. 激活这个虚拟环境（你会看到命令行前面多了一个绿色的 (.venv) 标识）
.\.venv\Scripts\activate

# 3. 安装项目所需的所有依赖包（需确保项目根目录下有 requirements.txt）
pip install -r requirements.txt
```

> **💡 小贴士**：如果由于系统安全策略导致第二条命令（激活虚拟环境）报错，请在**管理员权限**的 PowerShell 中执行一次 `Set-ExecutionPolicy RemoteSigned`，输入 `Y` 确认，然后重启 VS Code 再次尝试激活。

🎉 **至此，Windows 环境已彻底搭建完毕，可以继续进行后续的配置和使用了！**

---

## 二、配置

本项目固定使用 **DeepSeek（`deepseek/deepseek-chat`）** 模型，不可更换。
编辑 `config.toml`（git-ignored）时，你只需要关心两项：API 密钥与 `base_url`。

```toml
[api]
key      = "sk-..."                          # DeepSeek API Key
base_url = "https://api.deepseek.com/v1"     # 官方接入点；如需走代理，改这里

[data]
repos_dir    = "./data/historical_repos"   # 克隆下来的仓库存放目录
metadata_dir = "./data/metadata"

[target]
# 可选：填写后 python agent.py 不带参数时使用此仓库；留空则要求命令行传入参数
repo_id = ""

[engine]
rust_analyzer_timeout = 120   # 等待 rust-analyzer 索引完成的秒数
clangd_timeout        = 60    # 等待 clangd 索引完成的秒数
max_call_depth        = 3     # get_call_chain 默认展开层数
max_steps             = 200   # MCP 单会话工具调用预算
skip_dirs = ["vendor", "third_party", "target"]
```

> 不再支持自定义模型；`--model` 命令行参数已移除。
> `base_url` 是唯一影响接入点的开关：填官方地址走官方，填代理地址走代理。
> 改完 `config.toml` 后请重跑 `python setup_opencode.py` 让配置生效。

---

## 三、使用方法

所有仓库参数均可通过命令行直接指定，无需修改代码或配置文件。

### 分析单个仓库

```bash
# 指定远程 URL，自动克隆后分析
python agent.py --url https://gitlab.example.com/group/repo.git

# 指定本地仓库的完整路径
python agent.py --repo-path /path/to/repo

# 指定已克隆的仓库名（data/historical_repos/ 下的文件夹名）
python agent.py --repo-id REPO_NAME

# 若在 config.toml [target] 中填写了 repo_id，可不带参数直接运行
python agent.py
```

### 保存报告

```bash
python agent.py --url https://gitlab.example.com/group/repo.git --output report.md
python agent.py --repo-id REPO_NAME --output report.md
# 或者重定向（包含所有日志）
python agent.py --repo-path /path/to/repo > report.txt 2>&1
```

### 只克隆仓库（不分析）

```bash
python -m oskernel_agent.cli.fetch_repo https://gitlab.eduxiji.net/.../repo.git

# 自定义存放目录
python -m oskernel_agent.cli.fetch_repo https://... --output-dir ./data/historical_repos
```

### 构建参考指纹库（可选，用于原创性检测）

```bash
python scripts/build_reference_db.py --reference rcore-tutorial-v3 --repo-path /path/to/rCore-Tutorial-v3
```

---

## 四、命令行参数完整说明

### `agent.py`

| 参数                            | 说明                                         |
| ------------------------------- | -------------------------------------------- |
| `--repo-id ID`                | 分析 `data/historical_repos/` 下的指定仓库 |
| `--repo-path PATH`            | 分析任意本地路径下的仓库                     |
| `--url URL`                   | 克隆远程仓库后分析                           |
| `--output FILE` / `-o FILE` | 将报告写入文件（默认写入 `data/reports/`）  |

`--repo-id` / `--repo-path` / `--url` 三者互斥。

### `python -m oskernel_agent.cli.fetch_repo`

| 参数                 | 说明                                             |
| -------------------- | ------------------------------------------------ |
| `url`（位置参数）  | 仓库 HTTPS 地址                                  |
| `--output-dir DIR` | 本地存放目录（默认 `./data/historical_repos`） |
| `--meta-dir DIR`   | 元数据目录（默认 `./data/metadata`）           |

### 包安装后的等价命令

`pip install -e .` 之后，`pyproject.toml` 暴露的 console scripts 等价于：

| 短命令              | 等价调用                                          |
| ------------------- | ------------------------------------------------- |
| `oskernel-agent`    | `python -m oskernel_agent.cli.agent`              |
| `oskernel-setup`    | `python -m oskernel_agent.cli.setup_opencode`     |

---

## 五、作品查重引擎（feature/clone-detection-engine）

在「描述报告」之外，本项目正在构建一套**历史作品查重引擎**：对新提交作品，从约 200 个历史决赛仓库中找出相似代码并生成评审报告。查重链路（`src/` 下 `ingest/normalize/simhash/embed/segment/exact/metadata/review/report` 各子模块）与现有报告生成（`oskernel_agent.reports`）**完全独立、互不影响**。

四层漏斗：SimHash 粗筛 → 向量 ANN 召回（CodeT5+ + Qdrant）→ 分段向量验证 → 精确比对 + 元数据信号 → LLM 复核 + 评审报告。每个子模块都有独立 CLI 入口（`python -m src.<module>`），中间产物落盘 JSON/SQLite 解耦。

#### 最简用法：两条命令

整个引擎对日常使用只暴露两条命令——**建库**一次、**对比**多次。

```bash
# ① 建历史库：把真实仓库地址填进 config/repos.yaml，然后一条命令搞定
#    （拉取 → 归一化 → 向量化 → SimHash 索引，产物全部写入 data/db/）
export GITLAB_TOKEN=<你的 GitLab token>     # 私有仓库需要；公开仓库可省略
python -m src.buildlib

# ② 对比新作品：给一个本地路径或 git URL，一条命令出评审报告
export LLM_API_KEY=$(python -c "import tomllib;print(tomllib.load(open('config.toml','rb'))['api']['key'])")
export LLM_BASE_URL=$(python -c "import tomllib;print(tomllib.load(open('config.toml','rb'))['api']['base_url'])")
python -m src.pipeline --repo <新作品路径或 git url>
# → data/output/<作品名>_report.md
```

后续**新增历史仓库**：把新地址追加进 `config/repos.yaml`，重跑 `python -m src.buildlib` 即可（默认全量重建，避免向量库残留孤儿点）。仓库已在本地、只想重建索引时加 `--skip-ingest`。

> `src.buildlib` 只是把下面 `src.ingest → src.normalize → src.embed build --recreate → src.simhash build` 四步串成一条命令；需要单独调试某一步时仍可分开运行（见下文各模块）。

依赖服务（Qdrant 向量库）：

```bash
docker compose up -d        # 启动 Qdrant（http://localhost:6333/dashboard）
```

### 数据获取（`src.ingest`）

从 GitLab 批量克隆历史作品并抓取元数据。

**1. 准备环境变量**

```bash
cp .env.example .env
# 编辑 .env，填入 GITLAB_TOKEN（read_api / read_repository 权限即可）
# 自建 GitLab 实例还需设置 GITLAB_URL
```

`GITLAB_TOKEN` 从环境变量读取（也会自动加载 `.env`）。不设置时以匿名方式访问，仅能拉取公开仓库且受速率限制。

**2. 准备仓库清单**

```bash
# 生成含 3 条示例数据的模板（已存在则不覆盖）
python -m src.ingest --init-template --config config/repos.yaml
```

模板 `config/repos.yaml` 每条记录字段：`repo_url` / `year` / `team_name` / `award_level`。把示例替换为真实仓库即可。

**3. 批量拉取**

```bash
python -m src.ingest --config config/repos.yaml          # 断点续传：已克隆的仓库自动跳过
python -m src.ingest --config config/repos.yaml --force  # 强制重新克隆
```

仓库克隆到 `data/repos/{year}/{team_name}/`；每个仓库目录下额外生成 `_meta.json`，包含：

- **commit 历史**：每条含 `sha` / `author` / `date` / `message` / 变更文件数 / 增删行数；
- **fork 关系**：`forked_from_project` 字段；
- **项目元数据**：创建时间 `created_at`、贡献者列表 `contributors`。

| 参数                  | 说明                                                  |
| --------------------- | ----------------------------------------------------- |
| `--config PATH`       | repos.yaml 路径（默认 `config/repos.yaml`）           |
| `--repos-root DIR`    | 克隆输出根目录（默认 `data/repos`）                   |
| `--force`             | 强制重新克隆已存在的仓库                              |
| `--gitlab-url URL`    | GitLab 实例地址（覆盖 `GITLAB_URL`）                  |
| `--no-commit-stats`   | 跳过逐 commit 的增删行/变更文件数抓取（更快）         |
| `--init-template`     | 生成示例 `repos.yaml` 模板后退出                      |

**测试**

```bash
uv pip install -r requirements.txt   # 或 pip install -r requirements.txt
python -m pytest tests/              # ingest 用 mock 的 GitLab API 验证 _meta.json 字段完整性
```

### 代码归一化（`src.normalize`）

基于 tree-sitter 把仓库切分为函数，做 AST 级归一化，结果写入 SQLite（`data/db/functions.db`）。

```bash
python -m src.normalize --repo data/repos/2024/team-x   # 单个仓库
python -m src.normalize --all                            # 遍历 data/repos 下全部仓库
```

处理流程：

1. **文件发现**：处理 `.rs` / `.c` / `.h` / `.S` / `.asm`；排除 `target/`、`build/`、`.git/`、`vendor/`、`third_party/` 以及任何含 `LICENSE`/`COPYING` 的第三方子目录；超过 `--max-lines`（默认 10000）行的文件跳过。
2. **函数切分**：Rust 取 `function_item`（含 impl 方法），`macro_definition` 单独标 `macro`；C 取 `function_definition`；汇编按标号切段；小于 `--min-lines`（默认 5）行的函数跳过。
3. **模块归类**：按路径关键词归类（规则见 [config/module_rules.yaml](config/module_rules.yaml)），标签 `sched/mm/fs/trap/driver/arch/macro/other`。
4. **归一化**（生成 `normalized_code`）：用户标识符脱敏为 `VAR_n`/`FUNC_n`（同名同号、首现排序），保留类型名与白名单符号（[config/keep_symbols.txt](config/keep_symbols.txt)）；删注释；字符串→`STR`（长度 ≥ 8 的原文存入 `unique_strings` 表）；数字按量级→`INT_S`/`INT_M`/`INT_HEX`；按语句压缩空白。**改变量名后 `normalized_code` 完全一致。**

| 参数            | 说明                                            |
| --------------- | ----------------------------------------------- |
| `--repo PATH`   | 单个仓库目录（与 `--all` 互斥）                 |
| `--all`         | 遍历 `--repos-root` 下全部仓库                  |
| `--repo-id ID`  | 覆盖自动推断的 `repo_id`                        |
| `--db PATH`     | SQLite 输出（默认 `data/db/functions.db`）      |
| `--min-lines N` | 小于该行数的函数跳过（默认 5）                  |
| `--max-lines N` | 超过该行数的文件跳过（默认 10000）             |

输出库表：`functions(id, repo_id, file_path, start_line, end_line, func_name, module_tag, lang, raw_code, normalized_code, feature_tokens)` 与 `unique_strings(repo_id, func_id, string_value)`。按 `repo_id` 幂等写入（重跑同一仓库先删旧记录）。`feature_tokens` 是 `extract_feature_tokens` 抽取的特征集合（`cf:` 控制流 / `ty:` 类型 / `lib:` 保留库符号调用 / `call:` 其它调用 / asm 的 `op:` 助记符），供 Layer 1 SimHash 使用。

### Layer 1 SimHash 粗筛（`src.simhash`）

在向量检索之前用 SimHash 做廉价粗筛，快速排除明显不相似的函数对。

```bash
python -m src.simhash build                    # 从 functions.db 建 IDF + SimHash 索引
python -m src.simhash query --repo <仓库>      # 报告每函数的 SimHash 候选规模
```

- **特征**：用 `functions.feature_tokens`。
- **权重**：全库 IDF，`weight(t)=log(N/df(t))`，写 `data/db/idf.json`。
- **指纹**：每个 token 用 xxhash 算 64 位、按 IDF 加权累加再符号化得 64 位 SimHash；同时保存每位累加绝对值（量化为 uint8）用于比特松弛。
- **分段索引**：64 位切 4 段 × 16 位，每段一个 `dict[seg, [func_id]]`，pickle 存 `data/db/simhash_index.pkl`；`query` 4 段分别查表取并集。**比特松弛**（默认开）：每段额外翻转累加绝对值最低的 1 位再查一次（每段 2 次、共 8 次查表），召回少量比特差异的近似指纹。

集成：`src.embed query --with-simhash` 先 SimHash 召回候选 `func_id` 集合，作为 Qdrant id 过滤传入向量检索（Layer 1 → Layer 2 漏斗），召回 JSON 的 `simhash` 字段记录耗时与候选池规模。

**验收实测**：
- 改名版函数对（变量重命名不改变特征 token）SimHash 汉明距离 = 0（≤ 8）；随机无关函数对平均距离 ≈ 31（28–36 理论区间）。
- 开/关 SimHash 端到端对比（rCore-Tutorial-v3 全库 448 函数建索引，用其中 easy-fs 的改名副本作伪新作品）：**confirmed + review 档（真嫌疑）保留率 100%（17/17），SimHash 未漏任何真嫌疑**；被滤掉的 3 个全是 weak 档（score 0.5–0.62）的松散跨函数匹配。开关后 SimHash 反而把固定 top-20 向量召回挤出的结构克隆捞回（confirmed 40 vs 15），即粗筛同时**提升**了真克隆召回。
- 注：耗时优势是**大规模**（≈200 仓库）属性——本实验语料仅 448 函数、向量检索本就亚毫秒级，故此处看不出提速（OFF 0.12s / ON 0.18s）。

### Layer 2 向量召回（`src.embed`）

用 `Salesforce/codet5p-110m-embedding`（256 维）嵌入 `normalized_code`，写入 Qdrant，对新作品做 ANN 召回。模型名/滑窗/batch 等见 [config/settings.yaml](config/settings.yaml)。

> ⚠️ codet5p 的 `trust_remote_code` 模型**仅兼容 transformers 4.x**（5.x 会报 `is_decoder` 缺失），`requirements.txt` 已固定 `transformers>=4.40,<5`。

**建库**（遍历 `functions.db` 全部函数，增量入库）：

```bash
docker compose up -d                       # 启动 Qdrant（有 docker 时）
python -m src.embed build --all            # 默认连 http://localhost:6333

# 无 docker：用本地磁盘向量库 或 内存库
python -m src.embed --qdrant-path data/db/qdrant_local build --all
python -m src.embed --in-memory build --all
```

**检索**（对新作品：归一化 → 嵌入 → top-k 召回）：

```bash
python -m src.embed --qdrant-path data/db/qdrant_local query --repo path/to/new-submission
```

检索流程：调用 `src.normalize` 切分归一化新作品 → 嵌入每个函数 → Qdrant top-k（默认 20）；**排除同 `repo_id`，优先同 `module_tag` 内检索，不足再放开模块限制补足**。结果写 `data/output/{repo_name}_recall.json`（每条 = query 函数 + 候选列表，含分数与全部 payload）。

完成后终端打印快速统计：相似度 `>0.9 / 0.8-0.9 / 0.7-0.8` 的候选对数量，以及按历史仓库聚合的**命中 Top-5 排行**（最初步的「新作品最像哪几个历史作品」）。

Qdrant payload：`repo_id, year, file_path, start_line, end_line, func_name, module_tag, is_baseline`；点 id 复用 `functions.id`（保证增量去重）。

**验收实测**：改名对（归一化等价）cosine ≈ 1.0、无关对 ≈ 0.31；对 rCore-Tutorial-v3 建库 448 函数，用其轻微改名版查询，**Top-1 自命中 94.6%**（其余均为库内归一化完全相同的函数碰撞，无真实漏检）。

### Layer 4 精确比对与嫌疑对生成（`src.exact`）

对召回结果逐对做**行级精确比对**，分流成不同可信度的嫌疑对。

```bash
python -m src.exact verify --recall data/output/{repo}_recall.json --db data/db/functions.db
```

`ExactMatcher`（`difflib` 自建，无外部依赖）对每对函数跑两遍：
- **exact 通道**：原文逐行比对，原文相同的行标 `exact`；
- **renamed 通道**：对原文做**逐行保序的轻量掩码**（标识符→`ID`、数字→`NUM`、字符串→`STR`、去注释，**不重排行**）后比对，仅掩码后才相同的行标 `renamed`。

> 注：renamed 通道刻意用「逐行掩码」而非 `src.normalize` 的 `normalized_code`——后者按 AST 语句重排了行，行号无法回溯；逐行掩码保持行数不变，使匹配行区间能精确换算回**绝对文件行号**（`remap_spans`，函数内第 1 行 == 文件第 `start_line` 行）。

流水线：读 `*_recall.json` → 对 `vector_similarity > 0.7` 的候选从 `functions.db` 取出原文比对 → 组装 `SuspectPair`（填 `evidence.vector_similarity` / `exact_match_lines`，`matched_spans` 换算为绝对行号）→ 按 `similar_line_ratio` 分流：

| 档位 | `similar_line_ratio` | 含义 |
| --- | --- | --- |
| `confirmed` | > 0.95 | 铁证，后续 LLM 只写解释 |
| `review` | 0.7 – 0.95 | 核心复核区间 |
| `weak` | 0.5 – 0.7 | 弱信号 |
| （丢弃） | < 0.5 | 不输出 |

输出 `data/output/{repo}_suspects.json`。**验收实测**：延续上面的伪新作品实验，比对 6876 对 → 977 个嫌疑对（confirmed 564 / review 119 / weak 294），confirmed 级把改名版函数与原函数精确对上（行区间正确换算为绝对行号）。

### Layer 3 分段向量验证（`src.segment`）

对 `tier=review` / `weak` 的嫌疑对做**函数内分段**的细粒度比对，把"整体相似但实现不同"的对甄别出来。

```bash
python -m src.segment --suspects data/output/{repo}_suspects.json
# 输出 {repo}_suspects_v2.json，原文件保留
```

- **分段**（`normalize.segment_function`）：函数 < 15 行整体作一段；否则按顶层控制流切——每个 if/else 分支体、loop/while/for 体、match 臂各一段，其余连续语句每 10 行聚一段，< 4 行的段并入相邻段。每段带绝对行号与逐段 `normalized_text`。
- **匹配**：双方各段用 Embedder 嵌入（跨所有对一次性 batch），两两算 cosine，用匈牙利算法（`scipy.optimize.linear_sum_assignment`）做最优一对一匹配，cosine > 0.85 记一次命中，避免一段重复命中多段。
- **覆盖率**：`q_coverage`/`c_coverage`=命中段数/各自总段数，连同命中段对写入 `evidence.segment_hits`。
- **重打分**：`final_score = 0.4·min(q_cov,c_cov) + 0.3·exact_match_ratio + 0.3·vector_similarity`。
- **升降级**：`weak` 且重打分 > 0.75 → 升 `review`；`review` 且双向覆盖 < 0.3 且 `exact_match_ratio` < 0.2 → 降 `dismissed`（写明原因）。命中段对会传入 Layer 5 嫌疑卡片。

**验收实测**：优先级扫描调度器 vs 其改名版（真克隆）双向覆盖 = 1.0；vs 轮转调度器（同主题不同算法）覆盖 = 0.0，被正确降级 `dismissed`；一个被误判 `weak` 的克隆经分段重打分（0.76）升级 `review`——降级/升级均合理。

### Layer 5 LLM 复核（`src.review`）

对 `tier=review` 的嫌疑对调用 LLM（OpenAI 兼容 API，默认 `deepseek-chat`）复核，给出结构化判定。模型名在 [config/settings.yaml](config/settings.yaml) 的 `llm` 段，`base_url`/`api_key` 从环境变量 `LLM_BASE_URL`/`LLM_API_KEY` 读（写入 `.env` 即可）。

```bash
python -m src.review --suspects data/output/{repo}_suspects.json
```

流程：
1. **嫌疑卡片**：每对生成文本卡片——双方 `raw_code`（标注绝对行号）、仓库/文件/模块信息、下层全部证据（向量相似度、精确匹配行数、`matched_spans`）；代码超长时只保留匹配区 ±10 行，其余用「... [省略 n 行] ...」，总长压在 6000 token 内。
2. **复核 prompt**（[src/review/prompts.py](src/review/prompts.py)）：硬性规则要求只依据卡片内容、必须考虑 OS 内核教科书式通用模式（RR 调度/buddy 分配/RISC-V trap 等）、每条证据引用双方行号、不确定时倾向 `false_positive`/低置信度，严格输出固定 JSON schema。
3. **投票**：`temperature=0.3` 调 3 次，verdict 取多数；平票/三次全不同 → `disputed` 并保留三次原始输出；confidence 取均值；JSON 解析失败追加纠正指令重试一次，仍失败标 `parse_error`。
4. **后置校验**：正则抽取 LLM evidence 行号回源验证是否落在对应函数行号范围内，越界条目删除并记警告；`likely_clone` 但证据清空则降级为 `disputed`。
5. **并发**：`asyncio` 并发（默认 5），带网络重试与限速。

输出 `data/output/{repo}_reviewed.json`。verdict 取值：`high_similarity` / `likely_clone` / `common_pattern` / `false_positive`（聚合态另有 `disputed` / `parse_error`）。

> LLM 客户端封装为可注入接口（[src/review/llm.py](src/review/llm.py)），单元测试用 mock LLM 覆盖正常 JSON、格式重试、行号越界过滤、三次投票分歧等路径，无需联网。`--limit N` 可只复核前 N 个 review 档（控制成本）。

**验收实测**：用真实 DeepSeek 复核 5 个嫌疑对（RR 调度 / buddy 分配 / RISC-V trap 上下文 / 链表头插 / `sys_getpid` vs `sys_getppid`），reasoning 言之有物且逐条引用行号——前四者正确判为 `common_pattern`（教科书通用模式），后者正确判为 `false_positive`；evidence 行号全部通过回源校验。

### 辅助信号通道（`src.metadata`）

在主漏斗之外叠加三个辅助信号，输出 `{repo}_suspects_final.json`：

```bash
python -m src.metadata --suspects data/output/{repo}_suspects_v2.json \
    [--query-repo <新作品路径>] [--baselines --qdrant-path data/db/qdrant_local]
```

1. **独特字符串**（独立召回路径）：基于 `unique_strings` 表建反向索引 `string_value → [(repo_id, func_id)]`，**出现在 > 5 个不同仓库的通用字符串剔除**。新作品函数的字符串命中历史函数时，bump 既有对的 `evidence.unique_string_matches`，或**新建 `SuspectPair`（tier=review，`source=string_channel`）**。
2. **基线白底库**：基线仓库（[config/baselines.yaml](config/baselines.yaml)：rCore-Tutorial / xv6-riscv / 组委会模板）经 ingest+normalize+embed 入库，Qdrant payload 标 `is_baseline=true`。对每个嫌疑对，双方函数分别查与基线库的最高相似度；**双方都与同一基线函数相似度 > 0.85 才** 标 `evidence.baseline_flag=true` 并降级 `baseline_derived`（不再进 LLM 复核，报告单独列出）。
3. **commit 异常信号**：`git blame` 定位引入嫌疑函数的 commit，结合 `_meta.json` 检查：单次新增 > 2000 行 / message 属模糊模板（init/add files/update，规则可配）/ 引入时间距比赛开始 < 3 天却已是完整实现。结果写 `evidence.commit_signals`，仅作报告附注、不改 tier。

规则参数见 [config/settings.yaml](config/settings.yaml) 的 `metadata` 段。通道 1 仅需 `functions.db`；通道 2 需 Qdrant 中已有基线数据；通道 3 需 `--query-repo`（含 `_meta.json`）。

**验收实测**：通用字符串（6 仓库）被正确过滤、独特字符串命中新建嫌疑对；基线扣除仅在双侧命中同一函数时触发（单侧不扣）；`git blame` 正确定位函数引入 commit。端到端：两支 rCore 衍生的 RISC-V trap 上下文函数都命中同一基线 trap 函数（sim 1.0），被吸收为 `baseline_derived`，不再进 review。

### 报告生成（`src.report`）

报告采用「模板 + 填空」控制幻觉：固定五章节，每章节只把**该章节相关的结构化数据**喂给 LLM；表格与统计由代码直接生成（不过 LLM）。

1. **溯源结论**：按 tier 加权命中排名的 Top-3 历史作品 + 嫌疑对/confirmed/review 分布 + 涉及模块（LLM 据此写结论）。
2. **模块级对照表**：sched/mm/fs/trap/driver 各模块最相似历史来源（纯代码生成 Markdown 表）。
3. **高相似清单**：所有 `confirmed` 与 `verdict=likely_clone` 对，双方 文件:行号 / 相似度 / clone_type（表格代码生成），reasoning 由 LLM 压到 50 字内。
4. **创新点**：新作品中与全历史库最高相似度 < 0.5 且行数 > 30 的函数 Top-10，LLM 据代码描述「独立实现部分」，**每条带 文件:行号 引用**。
5. **附注信号**：`baseline_derived` 统计、commit 异常信号、`disputed` 待人工复核清单。

**后置校验**：抽取报告所有 `文件:行号` 引用回源验证（落在真实函数区间内），**无法验证的整句删除**并在末尾附「已删除 n 条」。

历史作品档案（独立命令）：

```bash
python -m src.report profile --all     # 每个历史仓库一份 data/db/profiles/{repo_id}.md
```

档案输入为该仓库模块分布 + 各模块代表函数代码 + README，同样强制行号引用 + 后置校验；review 复核时会把候选方档案摘要（前 300 字）附进嫌疑卡片。

### 全流水线总入口（`src.pipeline`）

```bash
python -m src.pipeline --repo <新作品路径或 git url> [--top-k 20] [--skip-llm] \
    [--resume-from <step>] [--no-simhash] [--baselines] [--review-limit N]
```

按序执行 **ingest → recall(含 normalize) → exact → segment → metadata → review → report**，每步落盘中间 JSON，任一步失败可 `--resume-from <step>` 续跑；终端打印每步耗时与**漏斗数字**（候选逐层递减）。前提：历史库已离线建好（`src.normalize` → `src.embed build` → `src.simhash build`）。

**验收实测**：对 rCore-Tutorial-v3 全库（448 函数）建库后，把其中 easy-fs 作为伪新作品**一条命令**跑通全流水线，输出 `data/output/{repo}_report.md`。漏斗：recall 39 函数/118 候选 → exact 比对 77 → confirmed 40 / review 7 / weak 14 → review 复核（likely_clone 1 / common_pattern 4）。报告五章节数字与中间 JSON 完全一致；**报告中无法回源的 文件:行号 引用 = 0**。

---

## 六、评测体系（`tests/evaluation`）

合成「已知克隆」评测集 + 多维指标统计 + CI 回归基线。

```bash
python -m tests.evaluation.synthesize --per-class 50   # 生成 tests/fixtures/eval_set.json
python -m tests.evaluation.run [--with-llm] [--check]  # 评测并存历史结果
```

- **合成**（`synthesize`）：从 `functions.db` 随机抽函数，用 tree-sitter 生成四类已知克隆——
  **T1** 原样复制 / **T2** 系统性改名（一致重写标识符）/ **T3** 增删语句（随机删 ~20% + 插入日志语句）/
  **T4** 结构重写（if-else→match、for→while-let）。每类 50 对 + 等量随机负样本，每个变体都经 tree-sitter
  重解析校验合法（含语法错误则丢弃重采）。带 ground truth 标签存 `tests/fixtures/eval_set.json`。
- **评测**（`run`）：把样本注入各层，统计每类的 Layer1 SimHash / Layer2 向量 / 级联 / 最终召回，
  总体 precision/recall，可选 LLM verdict 准确率，及耗时；输出 Markdown 表并存
  `tests/evaluation/history/{date}.json` 便于跟踪劣化。
- **人工标注集**：`tests/fixtures/manual_labeled.yaml`（pair: A/B 函数定位 + label + note），评测时与合成集一并跑。
- **回归基线**：`--check` 时 T1/T2 最终召回 < 0.95 或 T3 < 0.80 即非零退出；CI（[.github/workflows/eval.yml](.github/workflows/eval.yml) / [.gitlab-ci.yml](.gitlab-ci.yml)）跑小型评测集做回归门禁。

**第一版基线**（rCore-Tutorial-v3 全库 448 函数，每类 50 + 200 负样本）：

| 类别 | L1 SimHash | L2 向量 | 级联 | 最终召回 |
| --- | --- | --- | --- | --- |
| T1 原样 | 1.00 | 1.00 | 1.00 | 1.00 |
| T2 改名 | 0.40 | 1.00 | 0.40 | 1.00 |
| T3 增删 | 0.64 | 0.98 | 0.64 | 0.88 |
| T4 重写 | 0.46 | 0.98 | 0.46 | 1.00 |

总体 **precision = 0.995**（200 负样本仅 1 误报）、**recall = 0.97**。

**关键结论**：Layer 2 向量召回对四类变换都很强（0.98–1.00）；但 **Layer 1 SimHash 对 T2 改名 / T4 结构重写召回偏低（0.40–0.46）**——特征 token 随重命名/重构而改变。因级联召回受 SimHash 上限制约（cascade≈L1），**SimHash 宜作为向量召回的补充通道（取并集）或仅用于大规模初筛，不应作为硬性前置过滤**，否则会漏掉改名/重构型克隆。这是评测体系给出的第一条系统性改进依据。
