# OS 内核代码分析 Agent

对学生提交的操作系统内核代码做**描述报告 / 历史作品查重 / AI 生成代码检测**三条独立分析链路的 AI Agent。

## 项目背景

全国大学生操作系统比赛（OS Kernel Competition）每年产生大量学生自研内核仓库，评审需要人工判断每份作品的**完整性**（实现了哪些子系统、达到什么深度）与**原创性**（是否抄袭历史参赛作品、是否大量使用 AI 生成代码而未声明）。人工逐仓库阅读代码、比对历年提交、甄别 AI 生成痕迹的成本极高，且容易遗漏。

本项目用 AI Agent 自动化这一评审流程，面向赛事组织方 / 指导教师：

- **描述报告**：自底向上静态分析单个仓库，产出可视化的子系统树状报告，呈现该作品实现了什么、关键设计与调用链路；
- **查重对比报告**：基于历年仓库构建的向量库做语义级查重，识别改名复制、跨文件挪用等借鉴行为，并扣除上游 vendored / ABI 受限代码等机械误报；
- **AI 生成代码检测**：免训练的 log-rank + NPR 两阶段信号检测，标记疑似未声明使用 AI 生成的代码段，供人工复核。

三条链路均产出 HTML 报告，作为评审的辅助参考而非最终裁决依据。

## 演示视频

[百度网盘](https://pan.baidu.com/s/1jAw4KIv3td3Sb-mKqPTpbg?pwd=5e6u)（提取码：5e6u）

## 技术文档

项目技术文档位于 [`技术文档/OS功能挑战赛道-设计方案与技术文档.pdf`](技术文档/OS功能挑战赛道-设计方案与技术文档.pdf)。

---

## 一、环境安装（部署）

### Linux

```bash
# 1. 前置：OpenCode（AI 交互宿主，需 Node.js >= 18）
npm install -g opencode-ai          # 验证：opencode --version

# 2. 一键安装（推荐）：装 ctags/clangd/bear/rust-analyzer + Python venv，并注册 agent 到 OpenCode
bash setup.sh
source .venv/bin/activate
```

手动安装（替代一键脚本）：

```bash
npm install -g opencode-ai
sudo apt-get install -y universal-ctags clangd bear
curl -fL https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz \
  | gunzip -c | sudo tee /usr/local/bin/rust-analyzer > /dev/null && sudo chmod +x /usr/local/bin/rust-analyzer
pip install -r requirements.txt
pip install -e .                    # 启用 oskernel-agent / oskernel-setup 命令
python setup_opencode.py            # 注册 agent 到 OpenCode 全局配置
```

### Windows

0. 装好 [VS Code](https://code.visualstudio.com/)、[Python 3.x](https://www.python.org/downloads/)（勾选 **Add Python.exe to PATH**）、[Git for Windows](https://git-scm.com/download/win)、[Rust 工具链](https://rustup.rs/)。
1. VS Code 插件：`Python`、`C/C++` 或 `clangd`（二选一）、`rust-analyzer`、`CMake Tools`。
2. 全局依赖（**管理员 PowerShell**）：
   ```powershell
   winget install --id=UniversalCtags.Ctags -e
   rustup component add rust-analyzer
   ```
3. C/C++ 解析需要 `compile_commands.json`：VS Code 设置里勾选 `CMake: Export Compile Commands`（替代 Linux 的 `bear`）。
4. Python 虚拟环境 + 依赖：
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   pip install -e .
   ```
   > 若激活报安全策略错误，管理员 PowerShell 执行一次 `Set-ExecutionPolicy RemoteSigned`，重启 VS Code 再试。
5. OpenCode（npm）+ 注册 agent：
   ```powershell
   npm install -g opencode-ai
   python setup_opencode.py
   ```

---

## 二、配置

编辑 `config.toml`（git-ignored）：

```toml
[api]
key      = "sk-..."                                             # API Key
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 阿里云百炼；官方 DeepSeek 用 https://api.deepseek.com/v1

[data]
repos_dir    = "./data/historical_repos"
metadata_dir = "./data/metadata"

[target]
repo_id = ""                  # 可选：填写后 python agent.py 不带参数即分析此仓库

[engine]
rust_analyzer_timeout = 120   # 等待 rust-analyzer 索引秒数
clangd_timeout        = 60    # 等待 clangd 索引秒数
max_call_depth        = 3     # get_call_chain 默认展开层数
max_steps             = 30    # MCP 单会话探索类工具调用预算
skip_dirs = ["vendor", "third_party", "target"]
```

`.env`（查重 / AI 检测链路读取）：

```bash
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=deepseek-v4-flash         # LLM 模型（描述报告 + 语义分析）
GITLAB_URL=https://gitlab.eduxiji.net
GITLAB_TOKEN=<你的 token>           # 私有仓库克隆需要，公开仓库可省略
```

> **改完 `config.toml` 后必须重跑 `python setup_opencode.py`** 让 OpenCode 配置生效（模型、max_steps 等）。
>
> **换 API key 是三件套**：`config.toml` 的 `[api].key`、`.env` 的 `LLM_API_KEY`、再重跑 `python setup_opencode.py`
> （OpenCode 把 key 存在 `~/.local/share/opencode/auth.json`，漏第三步会导致描述报告 LLM 聚合静默失败、
> 整份报告降级为"规则兜底"）。

---

## 三、描述报告（基本命令）

自底向上分析单个仓库，产出树状 HTML 报告：

```bash
python agent.py --url https://gitlab.example.com/group/repo.git   # 远程，自动克隆
python agent.py --repo-path /path/to/repo                          # 本地路径
python agent.py --repo-id REPO_NAME                                # data/historical_repos/ 下的仓库名
python agent.py                                                    # 用 config.toml [target].repo_id

python agent.py --repo-path /path/to/repo -o report.html          # 指定输出文件
```

只克隆不分析：

```bash
python -m oskernel_agent.cli.fetch_repo https://gitlab.eduxiji.net/.../repo.git [--output-dir ./data/historical_repos]
```

---

## 四、查重对比报告（基本命令）

引擎对日常使用只暴露两条命令——**建库**一次、**对比**多次。

```bash
# 依赖服务（向量库）：有 docker 则起 Qdrant；无 docker 用本地 FAISS（见下方 --qdrant-path）
docker compose up -d                # 可选，http://localhost:6333/dashboard

# ① 建历史库：把真实仓库地址填进 config/repos.yaml，一条命令拉取→归一化→向量化→SimHash 索引（写 data/db/）
python -m src.buildlib              # 私有仓库先 export GITLAB_TOKEN；已在本地只重建索引加 --skip-ingest

# ② 对比新作品：一条命令出对比报告（规范用法，始终带 --baselines）
python -m src.pipeline --repo <新作品路径或 git url> --baselines
# → data/output/<作品名>/<作品名>_comparison.html
```

流水线步骤：`ingest → fastpath → recall → exact → segment → metadata → ai_detect → report`，每步落盘中间 JSON。常用选项：

```bash
python -m src.pipeline --repo <路径或url> \
    [--resume-from <step>]          # 从指定步骤续跑（前序产物需已存在）
    [--baselines]                   # 启用基线扣除（需 Qdrant 已有基线数据）
    [--skip-ai-detect]              # 跳过 AI 生成代码检测
    [--qdrant-path data/db/qdrant_local]   # 无 docker 时用本地磁盘向量库
```

新增历史仓库：把地址追加进 `config/repos.yaml`，重跑 `python -m src.buildlib`（默认全量重建）。

### 查全完整性硬门禁

对比报告只有同时满足以下条件才会生成：`repos.yaml` 中每个历史作品均有函数入库；FAISS、特征
SimHash、结构 SimHash 与 `functions.db` 为同一代；向量、特征 SimHash、归一化指纹、同名函数和
归一化代码结构 SimHash 五个召回通道全部开启；确定性/结构候选不做静默截断。任一条件不满足，
流水线以退出码 2 终止，不允许把“系统没查到”写成“原创”。结构 SimHash 是独立于函数名和 ANN
top-k 的补充通道，当前保证全局汉明距离不超过 15 的归一化代码结构候选进入后续验证。

报告中的绿色档统一表示“暂未检出相似”，不表示原创认定。报告头会记录历史库覆盖数、召回契约
版本与通道；缺少这些信息的旧报告会显示红色“已失效、必须重跑”提示。

```bash
# 全量检查历史库与 reports_by_work_id；退出码 0 才表示全部有效
python -m src.report audit
# 详细清单：data/output/recall_completeness_audit.json
```

---

## 五、AI 生成代码检测（基本命令）

免训练的两阶段信号检测（log-rank + NPR），打分模型 `bigcode/starcoder2-3b`（RTX 4060 bf16）。作为查重流水线的 `ai_detect` 步自动并入对比报告，也可独立运行：

```bash
# 首次需下打分模型权重（之后离线加载）
HF_ENDPOINT=https://hf-mirror.com huggingface-cli download bigcode/starcoder2-3b

python -m src.ai_detect --repo <新作品路径>
```

模型、阈值等见 [config/settings.yaml](config/settings.yaml) 的 `ai_detect` 段。无 GPU/模型时该步骤优雅跳过，报告章节给出说明。

---

## 六、前端控制台

`frontend/` 提供一个本地 Web 控制台，用于导入作品清单、发起报告生成任务、查看任务日志和打开已生成的 HTML 报告。前端只提交源码与配置；`node_modules/`、`dist/`、`data/`、`reports/*` 等运行产物由 `frontend/.gitignore` 排除。

### 启动方式

首次进入前端目录安装依赖：

```bash
cd frontend
npm install
```

开发模式同时启动 API 服务和 Vite：

```bash
npm run dev:all
```

默认地址：

- Web 页面：`http://127.0.0.1:5173`
- API 服务：`http://127.0.0.1:3130`
- 报告静态目录：`http://127.0.0.1:3130/reports/...`

也可以分开启动：

```bash
npm run api      # 仅启动 Node API
npm run dev      # 仅启动 Vite Web
npm run build    # 构建前端静态资源到 frontend/dist
npm run start    # 生产模式启动 Node API，并在 dist 存在时托管静态页面
```

端口可用环境变量覆盖：

```bash
FRONTEND_API_PORT=3130
FRONTEND_WEB_HOST=127.0.0.1
FRONTEND_WEB_PORT=5173
PYTHON_BIN=../.venv/Scripts/python.exe   # Windows 可选；不填会自动找项目 .venv
```

### 导入作品清单

页面支持上传 `.xlsx`。工作簿第一张表需要包含以下列名：

- `年份`
- `赛事`
- `子赛事`
- `学校`
- `队伍名称`
- `仓库地址`

导入后会根据仓库地址生成稳定的 `repo_xxxxxxxx` ID，并写入 `frontend/data/app.sqlite`。如果 `frontend/reports/<repo_id>/` 下已有报告，后端会自动同步并标记为可用。

### 生成报告

前端当前支持两类报告：

- `comparison`：查重对比报告，调用 `python -m src.pipeline --baselines`。**AI 生成代码检测已并入本报告第六章**（由流水线内部的 `ai_detect` 步产出，只针对非借鉴代码；无 GPU/模型时该章自动省略），不再单独出报告。
- `description`：项目描述报告，调用 `agent.py --repo-path ... --output ...`，依赖 OpenCode 和 `oskernel_agent` MCP。

页面可以对单个作品选择报告类型生成，也可以批量生成缺失报告。生成过程中可在任务列表查看日志；报告完成后从作品详情页直接打开。

### 运行产物目录

- `frontend/data/app.sqlite`：前端本地数据库。
- `frontend/reports/<repo_id>/`：每个作品的报告目录。
- `frontend/reports/<repo_id>/_repos/`：前端流水线克隆的新作品仓库。
- `frontend/reports/<repo_id>/comparison.html`：查重报告入口（含 AI 生成代码检测章节）。
- `frontend/reports/<repo_id>/description.html`：描述报告入口。

这些产物默认不提交到 Git。需要迁移或备份时，直接拷贝 `frontend/data/` 和 `frontend/reports/` 即可。

### 常用调参

描述报告的 OpenCode 并发可通过环境变量控制：

```bash
AGENT_SUBSYS_CONCURRENCY=10
AGENT_LLM_CONCURRENCY=10
AGENT_OPENCODE_ISOLATED_DATA=1
AGENT_TREE_NO_CACHE=1
```

AI 检测使用 GPU 时建议显式限制批量和扰动次数，避免显存峰值过高：

```bash
AI_DETECT_MODEL=Qwen/Qwen2.5-Coder-1.5B
AI_DETECT_DEVICE=cuda
AI_DETECT_BATCH_SIZE=1
AI_DETECT_K=2
```

如果只想验证前端构建是否正常，执行：

```bash
cd frontend
npm run build
```

---

## 七、批量出报告（run_batch）

给作品清单（`作品.txt`，JSON 数组，含 `队伍编号` / `Fork地址`）里的全部作品批量生成 描述+对比 两份报告：

```bash
python run_batch.py
# → data/output/<队伍编号>/<队伍编号>_{description,comparison}.html
```

- 逐作品串行，两份 HTML 均已存在则跳过（**断点续跑**）；
- 检测到 API key 欠费/限额自动切换备用 key（并自动完成上面的"三件套"）；
- 巨型仓库对比超时可调 `BATCH_CMP_TIMEOUT`（秒，默认 3600）。

---

## 八、报告一致性保证

**同一版本代码 + 同一命令，任何人跑出的报告格式与流程完全一致。** 这由以下机制保证：

1. **唯一生成路径**：对比报告只有 `src/report/semantic_compare.py` 一条渲染路径
   （`python -m src.pipeline` 与 `python -m src.report compare` 走同一个函数）；
   描述报告只有 `agent.py`（`oskernel_agent` 树状流水线）一条路径。旧的 Markdown
   报告流程（`src.report.generate` / `src.review` LLM 逐对复核）已删除。
2. **写盘前强制归一**：档位命名（高度疑似借鉴 / 疑似借鉴（待复核）/ 暂未检出相似）由
   `src/report/label_normalize.py` 在 HTML 写盘前统一（`semantic_compare` 内接线，幂等）；
   描述报告的英文正文/标题、代码摘录型点评由 `pipeline/lang_guard.py` 在渲染前中文化
   （`tree_builder` 内接线）。
3. **误报扣除内建**：上游 vendored/ABI 受限（`upstream_baselines.py` + `config/upstream_baselines.yaml`）、
   机械误报（`false_positives.py`）、复用库（`libraries.py` + `config/libraries.yaml`）
   都在流水线内自动剔除并在报告附录单列，无需人工后处理。
4. **review 档保守复核**：中等相似函数由 LLM 语义复核（`semantic_compare` 内置，模型
   `LLM_MODEL`，默认 `deepseek-v4-flash`）；判为借鉴时升档，明确非借鉴时排除，结论为
   “疑似”或复核失败时保留待复核信号，绝不降成“暂未检出”。
5. **规范命令固定**：对比报告一律 `python -m src.pipeline --repo <..> --baselines`
   （`run_batch.py` 与前端控制台均已按此调用）。

> 如果某份报告缺少完整召回契约、仍使用旧档位或内容为空，它就是**失效的旧产物**，必须用
> 当前流水线重跑；不再允许用 HTML 修改脚本把旧结果包装成新结果。

### 系统审计与维护工具

报告有效性审计已并入正式 CLI：

```bash
python -m src.report audit
```

`scripts/` 只保留无法归入日常流水线的离线数据准备和故障恢复工具：

| 脚本 | 用途 |
|---|---|
| `scripts/build_reference_db.py` | 离线构建参考 OS 指纹库 |
| `scripts/crawl_oscomp.py` | 从公开竞赛资料采集历史作品元数据 |
| `scripts/extract_hisrepo_metadata.py` | 从赛事详情页提取历史仓库元数据 |
| `scripts/stitch_fragments.py` | LLM 分片已完成但总报告中断时，离线恢复 tree.json/HTML |
