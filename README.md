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
reference_db_dir = "./reference_db"
reference_sources_config = "./config/reference_sources.yaml"
reference_sources_dir = "./data/reference_sources"

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
> （OpenCode 把 key 存在 `~/.local/share/opencode/auth.json`；漏第三步会导致描述报告 LLM 聚合失败，
> 流水线将拒绝生成不完整报告，不会再输出固定分数或空模块。）

---

## 三、描述报告（基本命令）

自底向上分析单个仓库，产出树状 HTML 报告。参考 OS 相似度只使用代码指纹；
指纹库缺失、JSON 损坏或结构不完整时，系统会按 `config/reference_sources.yaml`
固定的源码版本自动重建，禁止退化为函数名集合重叠率：

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

对比报告采用面向评委的九段式结构：评审结论摘要、共同上游判断、高置信同源功能簇、
模型复核与待处理队列、文件级和非函数代码证据、相对参考实现的候选创新、合法复用与许可证合规、
AI 代码检测、暂未检出相似函数附录。来源排名按唯一目标函数、有效相似行、涉及文件
和子系统统计，不以候选 pair 数放大；函数证据会按来源、子系统和功能域聚合为同源事件。
总体结果分布以全部解析函数为统一分母并按目标函数互斥归类，常显列出高置信同源、模型难例、
复核异常、暂未检出相似、复用库、基线衍生、上游/ABI、机械误报和公共样板的函数数及比例；
复用或排除项不再合并成一个无法区分的扇区。
子系统分类除调度、内存、文件系统、异常中断、驱动和架构外，还覆盖系统调用、信号、进程间通信、
并发同步、时钟定时、网络、安全权限、运行时诊断和宏。分类同时使用路径、函数名与代码职责锚点，
并按标识符边界匹配，避免聚合文件中的函数全部落入“其他”，也避免 `ext` 误命中 `context`。
无法识别具体功能域的函数保持独立证据簇，不再把同一来源下的无关 `other` 函数合并。

模型复核先判断两个函数的职责是否一致，职责一致或部分一致时才继续做代码同源判断。模型必须返回
非空职责依据、非空复核理由，以及能在输入代码中逐字定位的证据锚点；JSON 格式错误、空理由、无效
锚点或调用异常均视为复核未完整完成，正常流程拒绝生成交付报告，不会把失败项渲染成结论。报告中的
“匹配片段完全相同”只描述已命中的局部片段，并同时展示匹配行占整个目标函数的比例。
语义分析与创新归纳默认实际运行模型。语义说明按稳定功能簇 ID 与报告卡片一一对应，所有功能簇
全覆盖；输入超过统一字符预算时自动并发分批，不通过截断省略功能簇。模型不可用、返回功能簇
缺失/重复、内容不完整或仍含系统占位内容时，流水线拒绝生成交付报告。诊断时可显式传入
`--skip-global-semantic-analysis`，此时相关模块不渲染，不会用规则列表冒充语义分析。

“相对参考实现的候选创新”会把暂未形成有效相似命中的目标函数与主要历史来源的最近实现做代码级
比较。系统先按模块高置信命中确定前三个参考仓库，再在这些仓库的同语言、同子系统函数中执行
定向检索；首选仓库没有职责可对应的函数时才依次回退，避免全库 Top-K 偶然漏掉主要参考仓库。
每个候选都映射到目标/参考两侧源码，并补充调用或引用入口、影响范围、限制与反证和“待人工确认”
状态；README / 设计文档只能作为旁证，“未命中”本身不会被直接认定为创新。

创新函数的代码复杂度不使用仓库自定义加权分。报告采用
[McCabe 圈复杂度](https://doi.org/10.1109/TSE.1976.233837)，并由
[Lizard 1.23.0](https://github.com/terryyin/lizard) 对函数源码解析，同时列出 NLOC、token 数和参数数；
不支持的语言或无法解析的语法明确标记为不可用，不用正则估算值替代。圈复杂度描述控制流独立路径，
适合提示测试路径数量；它不等同于算法时间复杂度、性能或作品质量评分。
所有文件级、函数级和创新实现比较均限定为同一编程语言；跨语言候选在召回阶段直接过滤，
不会进入相似度分层、误报清单或创新实现地图。

流水线步骤：`ingest → fastpath → recall → exact → segment → metadata → ai_detect → report`；
`ai_detect` 默认实际加载检测模型运行。每步落盘中间 JSON。常用选项：

```bash
python -m src.pipeline --repo <路径或url> \
    [--resume-from <step>]          # 从指定步骤续跑（前序产物需已存在）
    [--baselines]                   # 启用基线扣除（需 Qdrant 已有基线数据）
    [--ai-detect]                   # 兼容已有命令；AI 模型检测默认运行
    [--skip-ai-detect]              # 仅诊断前序阶段；不会生成缺少 AI 检测的交付报告
    [--qdrant-path data/db/qdrant_local]   # 无 docker 时用本地磁盘向量库
```

新增历史仓库：把地址追加进 `config/repos.yaml`，重跑 `python -m src.buildlib`（默认全量重建）。

### 查全完整性硬门禁

对比报告只有同时满足以下条件才会生成：`repos.yaml` 中每个历史作品均有函数入库；FAISS、特征
SimHash、结构 SimHash 与 `functions.db` 为同一代；向量、特征 SimHash、归一化指纹、同名函数和
归一化代码结构 SimHash 五个召回通道全部开启；确定性/结构候选不做静默截断。任一条件不满足，
流水线以退出码 2 终止，不允许把“系统没查到”写成“原创”。结构 SimHash 是独立于函数名和 ANN
top-k 的补充通道，当前保证全局汉明距离不超过 15 的归一化代码结构候选进入后续验证。
召回契约同时要求 `same_language_only=true`，旧的跨语言召回产物不能续跑生成新报告。

报告中的绿色档统一表示“暂未检出相似”，不表示原创认定。召回契约仍在生成入口
强制校验，但不再把这一内部核验过程写进面向评委的报告正文。报告会完整列出暂未检出函数的
文件、行号、函数名和子系统，方便评委定位核查，同时明确不能把这份清单直接当作原创证明。

```bash
# 全量检查历史库与 data/output；退出码 0 才表示全部有效
python -m src.report audit
# 详细清单：data/output/recall_completeness_audit.json
```

---

## 五、AI 生成代码检测（基本命令）

免训练的两阶段信号检测（log-rank + NPR），打分模型 `bigcode/starcoder2-3b`（RTX 4060 bf16）。
该信号误判风险高且不能判断队伍是否披露、是否掌握代码，因此只作为评委报告的辅助检测结果，
不能单独作为违规或扣分依据。标准流水线默认运行实际模型：

```bash
# 首次需下打分模型权重（之后离线加载）
HF_ENDPOINT=https://hf-mirror.com huggingface-cli download bigcode/starcoder2-3b

python -m src.ai_detect --repo <新作品路径>  # 单独运行模型检测
python -m src.pipeline --repo <新作品路径> --baselines  # 标准流程，默认包含模型检测
```

模型、阈值等见 [config/settings.yaml](config/settings.yaml) 的 `ai_detect` 段。合规审查仍以队伍披露材料和现场解释为准。

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

导入后会根据仓库地址生成稳定的 `repo_xxxxxxxx` ID，并写入 `frontend/data/app.sqlite`。如果 `data/output/<repo_id>/` 下已有报告，后端会自动同步并标记为可用。

### 生成报告

前端按评委阅读顺序提供决赛四件套：

- `summary`：一页 A4 PDF 摘要，自动汇总另外三份报告；无超链接，正文不小于 10.5 磅。
- `description`：作品描述报告，问题和结论在前；四类硬编码候选必须经 AI 逐条复核，模块分析不超过 300 字。缺少事实扫描、AI 复核或中文化未完成时拒绝交付；调用 `agent.py --repo-path ... --output ...`。
- `development`：开发过程报告。程序从 Git 计算提交数、日期、LOC 和文件；专用 AI 结合这些证据复核问题、归纳阶段并给出置信度。AI 输出引用虚假提交、阶段漏项或分析失败时拒绝交付。
- `comparison`：对比分析报告，只展示最接近的一份历史作品。调用 `python -m src.pipeline --baselines`，AI 代码检测作为辅助信息并入该报告。

页面可以对单个作品选择报告类型生成，也可以批量生成缺失报告。选择 `summary` 时会自动补齐三份上游报告及其摘要数据。生成过程中可在任务列表查看日志；报告完成后默认先打开一页摘要。

### 运行产物目录

- `frontend/data/app.sqlite`：前端本地数据库。
- `data/output/<repo_id>/`：每个作品唯一的正式报告目录。
- `data/output/<repo_id>/summary.pdf`：一页评审摘要入口。
- `data/output/<repo_id>/description.html`：描述报告入口。
- `data/output/<repo_id>/development.html`：开发过程报告入口。
- `data/output/<repo_id>/comparison.html`：对比分析报告入口。

生成期间会临时产生仓库克隆、结构化摘要、AI 原始结果和工作目录；任务成功后自动清理，正式报告目录最终只保留以上四个文件。若只生成部分报告，也只保留已生成的正式 HTML/PDF，不保留中间文件。

这些产物位于 `.gitignore` 已覆盖的 `data/` 目录，不提交到 Git。需要迁移或备份时，拷贝 `frontend/data/` 和 `data/output/` 即可。

### 常用调参

描述报告的 OpenCode 并发可通过环境变量控制：

```bash
AGENT_SUBSYS_CONCURRENCY=10
AGENT_LLM_CONCURRENCY=10
AGENT_OPENCODE_ISOLATED_DATA=1
AGENT_TREE_NO_CACHE=1
# 按当届比赛章程填写；不设置时，开发过程报告不会自行认定“提交缺失”。
FINALS_MIN_COMMITS=<章程规定值>
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

给作品清单（`作品.txt`，JSON 数组，含 `队伍编号` / `Fork地址`）里的全部作品批量生成决赛四件套：

先在本地 `.env` 配置批处理凭据（真实值禁止提交）：

```dotenv
BATCH_PRIMARY_LLM_API_KEY=<主凭据>
BATCH_FALLBACK_LLM_API_KEY=<备用凭据>
# 可选：必须使用当届章程的真实最低提交次数；未知时留空。
FINALS_MIN_COMMITS=
```

批处理启动时会把当前槽位同步为 `LLM_API_KEY`；缺少当前槽位、或主备值相同都会给出不包含凭据值的配置错误。

```bash
python run_batch.py
# → data/output/<队伍编号>/summary.pdf
# → data/output/<队伍编号>/{description,development,comparison}.html
```

- 逐作品串行，四份报告及摘要数据均已存在则跳过（**断点续跑**）；
- 检测到 API key 欠费/限额时，自动切换已配置且不同于主凭据的备用 key（并自动完成上面的"三件套"）；
- 巨型仓库对比超时可调 `BATCH_CMP_TIMEOUT`（秒，默认 3600）。

---

## 八、报告一致性保证

**同一版本代码 + 同一命令，任何人跑出的报告格式与流程完全一致。** 这由以下机制保证：

1. **唯一生成路径**：对比报告只有 `src/report/semantic_compare.py` 一条渲染路径
   （`python -m src.pipeline` 与 `python -m src.report compare` 走同一个函数）；
   描述报告只有 `agent.py`（`oskernel_agent` 树状流水线）一条路径；开发过程和摘要统一由
   `python -m finals` 生成。旧的 Markdown
   报告流程（`src.report.generate` / `src.review` LLM 逐对复核）已删除。
   开发过程报告由专用 AI 生成问题判断和阶段结论，程序只复算 Git 事实并执行证据校验，
   不存在关键词规则生成最终阶段的兜底路径。
2. **写盘前强制归一**：档位命名（高置信同源代码 / 模型复核后仍存疑 / 复核失败或未完成 / 暂未检出相似）由
   `src/report/label_normalize.py` 在 HTML 写盘前统一（`semantic_compare` 内接线，幂等）；
   描述报告的英文正文/标题、代码摘录型点评由 `pipeline/lang_guard.py` 在渲染前中文化
   （`tree_builder` 内接线）。
3. **误报扣除内建**：上游 vendored/ABI 受限（`upstream_baselines.py` + `config/upstream_baselines.yaml`）、
   机械误报（`false_positives.py`）、复用库（`libraries.py` + `config/libraries.yaml`）
   都在流水线内自动剔除并在报告附录单列，无需人工后处理。复用库识别同时使用路径、包清单
   和真实导入关系：改名的依赖包可由清单恢复归属；登记过的适配层只有在依赖包确实存在且源码
   真实导入它时才继承归属，不会仅凭 `ext4`、`riscv` 等目录关键词排除自研实现。
4. **review 档严格复核**：中等相似函数先由 LLM 判断职责是否一致，再做代码同源复核
   （`semantic_compare` 内置，模型 `LLM_MODEL`）；模型认为借鉴只提高人工核查优先级，不升为确定性高置信。
   模型阴性若与具体函数身份和强直接代码证据冲突，保留供评委人工复核；
   格式或调用异常单列为复核失败；已经入队但未取得结论才算复核未完成。普通次级来源只附在
   已复核函数下；首选候选排除后，仅具有独立强代码证据的次级候选进入有限补充复核，不再生成
   面向评委的“次级候选未复核”模块。已有有效缓存会直接回填且不增加模型调用。
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
| `scripts/build_reference_db.py` | 主动重建参考 OS 指纹库（正式流程也会在异常时自动重建） |
| `scripts/crawl_oscomp.py` | 从公开竞赛资料采集历史作品元数据 |
| `scripts/extract_hisrepo_metadata.py` | 从赛事详情页提取历史仓库元数据 |
| `scripts/stitch_fragments.py` | LLM 分片已完成但总报告中断时，离线恢复 tree.json/HTML |
