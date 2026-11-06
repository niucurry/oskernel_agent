# OS Kernel Review Agent

面向全国大学生操作系统比赛评委的内核作品分析系统。系统从源码、Git 历史和历年作品库中生成四份不可由参赛队修改的正式报告：

- `summary.pdf`：一页评审摘要；
- `description.html`：有源码依据的模块与实现分析、硬编码线索复核；
- `development.html`：提交异常、开发阶段和关键提交；
- `comparison.html`：与唯一最接近历史作品的同源证据对比。

报告用于缩小人工复核范围，不自动作出抄袭、违规或获奖结论。

## 文档

- [新任务提示词：只研究 OS 代码描述系统](docs/description-research-restart-2026-10-08.md)
- [工程交接：保留的修复与验证范围](docs/engineering-handoff-2026-10-08.md)
- [描述系统首轮实验：强基线持平与保留的工程改进](docs/description-interface-witness-2026-10-08.md)
- [项目结构](docs/project-map.md)
- [设计方案与技术文档](docs/design-specification.pdf)

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

升级后应重新执行 `oskernel-setup`，以使用 OpenCode 的 `agent.prompt` 配置字段；旧版生成的
`agent.system` 没有将描述系统提示传递给模型。

## 快速使用

### 描述报告

```bash
oskernel-agent --repo-path /path/to/repository --output description.html
oskernel-agent --url https://gitlab.example/group/project.git
```

当前描述入口从源码生成模块、功能与实现说明，并复核硬编码线索；不执行比赛镜像编译或内核启动。硬编码候选必须经过 AI 逐条复核；缺少必要事实、复核未完成或证据链接无效时拒绝交付。报告中的静态实现说明不证明运行可用性。

模块概览附有源码范围计数，区分索引、实际提示清单和模块代表来源；实际源码阅读覆盖保持未知。恢复旧阶段产物时，历史提示数量也保持未知。快速符号清单支持 C/C++ 函数定义及函数式宏名称，语法解析不完整或不可用时会明确提示；符号出现不证明已经编译或启用。

新运行的描述会话通过 `opencode run --format json` 记录原生和 MCP 工具的实际返回，
在独立的 `data/read_audit/<任务键>/<批次>/<运行ID>/` 保存 `tool_events.jsonl` 与 `audit.json`
（可用 `AGENT_READ_AUDIT_DIR` 指定其他目录；不会随成功报告的临时目录清理而删除）。
每次重试单独记录，不合并成一次阅读；超时保留已收到的部分事件。当前能核对原生
`read/grep`、项目 MCP `read_file/search_code/find_symbol_definition` 返回的源码行，
头尾截断、单行截断和只搜索到函数名不会被当作全文读取。`bash`、子 agent 或未识别
格式的调用也会记录，但源码范围保持未知。原生工具权限不因审计而改变。
报告的“源码引用核对”展示本次会话的返回记录，并按函数范围检查缺口；新运行的
SUBSYS/VERDICT 对缺口最多自动补读两轮，每轮重新核对并保留草稿。删除引用不会移除
原先的补读目标，不同会话的返回行不会拼接成完整阅读。仍未覆盖的范围在报告中明确保留。
提示正文、附件、子会话和后续上下文
压缩不在完整测量范围内，记录不能证明模型理解、相关代码查全或评价正确，也不直接
触发拒绝交付。旧阶段产物恢复时不伪造读取历史。原始工具记录可能含源码，不应提交到 Git。
可选 canary 实验只在新仓库副本的已确认函数中插入随机注释，分别统计完整函数读取、
标记回报与人工相关清单的漏选情况。具体机制、测量边界与复现命令见
[源码读取审计说明](docs/read-audit-2026-10-11.md)。

新生成的描述报告还会优先复核最多 6 条关键结论：两个独立会话读取源码并给出原文证据，
程序核对证据真实性与阅读记录；有支持/反驳分歧时追加一次复核。有有效纠错依据时修订
报告一轮，再核对新版本。未复核、分歧、证据不足都会在报告中说明，模型一致不等于正确。
升级后需重新运行 `oskernel-setup` 注册复核角色。日志默认位于 `data/claim_reviews/`。
具体规则、成本边界和独立验证命令见[关键结论复核说明](docs/claim-review-2026-10-12.md)。

可通过 `AGENT_EVIDENCE_PLAN=/仓库外/plan.json` 指定评测方预先确认的评价项与必要证据。
清单在生成报告前固定；即使报告从未引用某个配置或函数，程序仍会核对并对可读取的缺口
执行最多两轮补读。清单中的缺失位置和源码变化会保留在报告中；清单读齐不证明所有相关
证据查全，也不产生已验证分数。默认不猜测比赛规则或目标清单。具体格式、离线检查与
模型验证见[评价项证据清单说明](docs/evidence-plan-2026-10-13.md)，总体路线以
[用户确认的计划](docs/evaluation-improvement-plan.md)为准。

启用评价清单后，新生成的 SUBSYS/VERDICT 还必须逐项提供带范围和成立条件的事实断言，
并将引文对应到各项必要证据。程序核对原文、源码版本和实际读取；材料齐全的断言优先进入
独立复核，未选中或证据不足的保留待核验状态。结论、条件或报告变化后旧复核不沿用。
具体运行行为仍显示“执行验证未运行”，不会因模型支持就计算已验证分数。机制、输出格式和
`--claims` 独立验证入口见[结构化评价断言说明](docs/evaluation-assertions-2026-10-13.md)。

可选 `AGENT_RECALL_CHECK=1` 在 SUBSYS/VERDICT 报告生成后核对源码保留与召回：
仅在副本插入随机注释，由本地代理记录实际模型输入，再复制被采纳会话，以无工具权限批量提问。
异常函数进入有限补读和相关断言复核，每轮重新生成随机值；报告清除所有标记并保留核对历史。
默认最多核对 12 个函数名（含对照），抽查 15% 已读非关键函数；标记保留、完整源码保留、
召回、调用列表分别记录。关闭时不改变原有流程。配置、成本、验收和边界见
[报告后核对说明](docs/recall-check-2026-11-01.md)。

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

### 外部运行日志核对

```bash
python -m oskernel_agent.finals audit-run --log qemu.log --output runtime-audit.html
```

当前识别 RT-Thread utest 的完整测试轮次，保留原汇总判定与断言失败位置；断言失败后仍汇总通过标为 `inconsistent`，未结束、零测例或未识别协议标为 `unknown`。退出码为 0（该轮判定一致且报告通过）、1（失败或矛盾）、2（未知或输入错误）；输出支持 `.json`。这项检查不执行仓库代码；日志通过只能说明该轮判定未发现矛盾，不能证明内核能力、测试充分性或日志真实性。它是独立核对入口，未自动接入作品描述报告。

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
- 浅克隆边界和未测量父差分的合并变更量保持未知；作者声明日期不作为实际工作起止时间；
- 公共上游、第三方库、比赛基线、ABI 受限实现和机械误报不得混入同源比例；
- 召回库或索引代际不完整时拒绝生成“未检出相似”结论；
- AI 调用失败、结构化输出无效或复核未覆盖全部候选时拒绝交付；
- 正式报告完全由工具生成，参赛队无需且不得后处理 HTML/PDF。

## 维护工具

后续研究仅面向描述系统，要求见 [新任务提示词](docs/description-research-restart-2026-10-08.md)。
生产仓库保留系统代码与必要回归测试；已停止的实验代码和记录已移至仓库外，归档入口见 [工程交接](docs/engineering-handoff-2026-10-08.md)。

| 命令 | 用途 |
|---|---|
| `python scripts/build_reference_db.py` | 重建参考操作系统指纹 |
| `python scripts/crawl_oscomp.py` | 采集公开竞赛作品元数据 |
| `python scripts/extract_hisrepo_metadata.py` | 从赛事详情页提取仓库元数据 |
| `python scripts/stitch_fragments.py` | 在诊断模式下恢复已完成的模型分片 |

许可证见 [LICENSE](LICENSE)。
