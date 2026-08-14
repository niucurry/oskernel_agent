# OS Kernel Review Agent

面向全国大学生操作系统比赛评委的内核作品分析系统。系统从源码、Git 历史和历年作品库中生成四份不可由参赛队修改的正式报告：

- `summary.pdf`：一页评审摘要；
- `description.html`：真实可用性、硬编码风险和内核模块分析；
- `development.html`：提交异常、开发阶段和关键提交；
- `comparison.html`：与唯一最接近历史作品的同源证据对比。

报告用于缩小人工复核范围，不自动作出抄袭、违规或获奖结论。

## 文档

- [设计方案与技术文档](docs/design-specification.pdf)
- [决赛报告设计说明](docs/finals-report-plan.md)
- [项目进展演示稿](docs/progress-presentation.pptx)

## 安装

要求 Python 3.11 及以上版本。描述报告还需要 OpenCode、Universal Ctags，以及与目标语言对应的 `clangd` 或 `rust-analyzer`。

Linux 推荐执行：

```bash
bash setup.sh
source .venv/bin/activate
```

手动安装：

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .\.venv\Scripts\activate
python -m pip install -e .
npm install -g opencode-ai
```

复制本地配置模板，真实凭据不得提交：

```bash
cp .env.example .env
cp config.toml.example config.toml      # Windows: Copy-Item config.toml.example config.toml
```

填写 `config.toml` 的 API key 后执行 `oskernel-setup`。该命令将 OpenCode 的认证和项目配置写入
`data/opencode/`，不会修改用户目录中的 OpenCode 配置。比赛最低提交次数只能通过
`FINALS_MIN_COMMITS` 配置为当届章程的真实值；未配置时系统不会自行认定“提交缺失”。

## 快速使用

### 描述报告

```bash
oskernel-agent --repo-path /path/to/repository --output description.html
oskernel-agent --url https://gitlab.example/group/project.git
```

默认只静态核对根目录 Makefile 的 `kernel-rv` 与 `kernel-la`。需要在比赛统一镜像中真实编译时显式启用：

```bash
oskernel-agent --repo-path /path/to/repository --output description.html \
  --verify-build --build-image zhouzhouyi/os-contest:20260510
```

本地还没有镜像时可追加 `--pull-build-image`；前端和批处理生成描述报告时会默认启用这两个参数。首次拉取镜像体积较大，工具最多等待一小时；后续直接复用本地镜像。真实编译使用一次性仓库副本、关闭容器网络并依次执行两个 Make 目标；实际运行锁定到已检查的镜像 ID，报告记录镜像 digest、资源限制、退出状态、耗时、产物大小和 SHA-256，不会修改原仓库。描述报告会先列出构建证据、硬编码作弊风险和其他严重问题，再展示各内核模块。硬编码候选必须经过 AI 逐条复核；缺少必要事实、复核未完成或证据链接无效时拒绝交付。

### 历史库与对比报告

先在 `config/repos.yaml` 维护历史作品清单。推荐将大型历史仓库保存在项目外的
`D:\@MyData\work\OS\historical_repos\by_year\<year>\<repo_key 或 team_name>\`，再建库：

```bash
python -m oskernel_agent.comparison.buildlib --repos-root D:\@MyData\work\OS\historical_repos\by_year
```

目录名必须与 `config/repos.yaml` 的 `year` 和 `repo_key`（没有时为 `team_name`）一一对应；可让下载任务只负责
按此结构克隆，建库命令负责归一化、索引和覆盖率校验。

对比新作品：

```bash
oskernel-compare --repo /path/to/repository --baselines
```

对比报告在完整历史库中执行召回、公共上游排除和误报复核，按证据强度动态展示若干最相似作品，不固定为五个；同时对统一证据排序第一的作品展开模块、函数、文件和代码证据。排名只用于安排人工核查，不推断直接来源、传播方向或抄袭。

### 开发过程报告

```bash
python -m oskernel_agent.finals development \
  --repo /path/to/git/repository \
  --repo-id TEAM_ID \
  --output development.html
```

Git 提交次数、日期、代码变更行数（LOC）和文件明细由程序复算；AI 只负责问题判断和阶段归纳。虚假提交、阶段重叠、历史缺口或模型输出不完整都会触发交付失败。

### 一对一报告产出

报告改为**逐个产出**，不再有事务式批处理（`oskernel-batch` 已移除）。对单个队伍，运行
`run_incremental.py`（改脚本头部的 `TEAM_ID` / `URL` 即可适配其他队伍）：

```bash
python run_incremental.py
```

每份报告（对比 / 描述 / 开发过程 / 一页摘要）独立执行、独立发布：任一报告成功即发布到
`data/output/<队伍编号>/`，失败只影响它自身，其余步骤照常进行。步骤复用
`oskernel_agent.cli.batch` 的单步函数与交付门禁；也可以直接调用单个命令
（`oskernel-compare` / `oskernel-agent` / `oskernel-finals`）逐一生成。

### 从作品列表批量下载目标仓库

批量分析前，可先从作品仓库列表（`内核赛作品仓库列表.xlsx`，单列 `fork地址`）读取被分析仓库
的位置并一次性下载，落盘位置与批量分析各步骤读取的位置一致（`data/output/_repos/<存储键>/`）：

```bash
python run_works_xlsx.py --xlsx "D:\@MyData\work\OS\内核赛作品仓库列表.xlsx" --jobs 4
```

- 队号自动取 fork 地址最后一段（如 `T2026100069910651-2494`）；
- 默认分支没有源码的作品，用 `--branch URL=BRANCH`（可重复）指定真正含代码的分支；
- GitHub 的 `/tree/<分支>`、`/blob/<分支>/…` 网页地址自动转为「仓库地址 + 该分支」克隆，
  无法映射的浏览页（如 `/commit/`、`/pull/`）跳过并告警；
- 下载后逐队运行 `run_incremental.py` 会直接复用已下载的克隆，不再重复下载；
- 追加 `--reports` 会在下载后逐队串行产出四份报告（并发批次会拖垮内存，故串行）；
- 单个仓库失败不阻断其余；`--dry-run` 只列出队号、地址与目标路径；
- 未指定 `--xlsx` 时依次查找项目根目录与上级工作区中的 `内核赛作品仓库列表.xlsx`。

## 前端控制台

```bash
cd frontend
npm install
npm run dev:all
```

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:3130`

前端可导入作品表格、选择报告类型、查看实时日志和打开正式报告。本地数据库位于 `frontend/data/`，不进入版本控制。
如工作目录不允许写入源码树，可设置 `FRONTEND_DATA_DIR`、`FRONTEND_REPORTS_DIR` 和
`FRONTEND_DIST_DIR`，将前端数据库、报告目录和生产静态文件分别放到外部路径。

## 项目结构

```text
.
├── config/                         # 历史作品、分类、基线和检测规则
├── docs/                           # 设计文档与项目资料
├── frontend/                       # Vue 前端与 Node API
├── resources/reference_db/         # 已验证的参考操作系统指纹
├── scripts/                        # 离线数据准备和故障恢复工具
├── src/oskernel_agent/
│   ├── analysis/                   # 单仓库事实抽取
│   ├── cli/                        # 正式命令入口（含单步报告函数库）
│   ├── comparison/                 # 历史入库、召回、精确比对和对比报告
│   ├── engines/                    # LLM 与语言服务引擎
│   ├── finals/                     # 四报告摘要模型、开发报告与清理门禁
│   ├── parsers/                    # C/Rust 源码和符号解析
│   ├── pipeline/                   # 描述报告分析流水线
│   ├── prompts/                    # AI 提示模板
│   ├── reports/                    # 描述报告渲染
│   └── tools/                      # MCP 与参考实现查询工具
└── tests/                          # 单元、集成和评测回归
```

项目采用标准 `src` 布局，所有可安装代码都位于单一 `oskernel_agent` 命名空间中。`src` 只是源码根目录，不是运行时 Python 包。

## 数据与生成物

以下内容均为本地状态，不提交到 Git：

- `data/db/`：函数库、向量库和 SimHash 索引；
- `data/historical_repos/`：通过 `oskernel-agent --url` 拉取的目标仓库缓存；
- `D:\@MyData\work\OS\historical_repos\by_year/`：用于建库的历史仓库（推荐放在项目外）；
- `data/output/`：正式报告；
- `frontend/data/`：前端数据库；
- `.env`、`config.toml`：本地凭据与运行配置；
- `.venv/`、`node_modules/`、缓存和构建产物。

参考操作系统指纹位于 `resources/reference_db/`，缺失或损坏时会根据 `config/reference_sources.yaml` 中固定的来源重建。主动重建可执行：

```bash
python scripts/build_reference_db.py --all
```

## 质量门禁

```bash
python -m pip install -e ".[dev]"
ruff check src tests scripts
python -m pytest -q
python -m oskernel_agent.comparison.report audit
cd frontend && npm run build
```

核心约束：

- 严重问题和模块不因版面配额被静默截断；
- 报告中的提交、函数、文件和行号必须能回到原始证据；
- 公共上游、第三方库、比赛基线、ABI 受限实现和机械误报不得混入同源比例；
- 召回库或索引代际不完整时拒绝生成“未检出相似”结论；
- AI 调用失败、结构化输出无效或复核未覆盖全部候选时拒绝交付；
- 正式报告完全由工具生成，参赛队无需且不得后处理 HTML/PDF。

## 维护工具

| 命令 | 用途 |
|---|---|
| `python scripts/build_reference_db.py` | 重建参考操作系统指纹 |
| `python scripts/crawl_oscomp.py` | 采集公开竞赛作品元数据 |
| `python scripts/extract_hisrepo_metadata.py` | 从赛事详情页提取仓库元数据 |
| `python scripts/stitch_fragments.py` | 在诊断模式下恢复已完成的模型分片 |

许可证见 [LICENSE](LICENSE)。
