# OS Kernel Review Agent 项目地图

> 面向全国大学生操作系统比赛评委的内核作品分析系统。从源码、Git 历史与历年作品库中生成四份不可由参赛队修改的正式报告,用于**缩小人工复核范围**,不自动作出抄袭、违规或获奖结论。
>
> 本文档是对全项目 252 个已跟踪文件的逐模块地图:每个文件做什么、关键代码在哪、模块之间怎么流动。

---

## 1. 项目是什么

系统的最终交付物是四份正式报告(每队一份,生成在 `data/output/<队伍编号>/`):

| 报告 | 文件 | 内容 | 由谁生成 |
|---|---|---|---|
| 一页摘要 | `summary.pdf` | 一页 A4 评审摘要,AI 摘要智能体基于另三份 digest 撰写 | `finals/summary_pdf.py` |
| 作品描述 | `description.html` | 真实性、硬编码风险、内核模块分析 | `reports/html_tree.py`(基于 tree.json) |
| 开发过程 | `development.html` | 提交异常、开发阶段、关键提交 | `finals/development.py` |
| 同源对比 | `comparison.html` | 与最相似历史作品的同源证据对比 | `comparison/report/semantic_compare.py` |

核心设计哲学:**AI 只做判断,事实由程序复算**;每个结论必须能回溯到证据(提交 SHA、文件:行号);AI 输出不合格时**拒绝交付**,绝不带病出报告。

## 2. 技术栈

- **Python 3.11+**(`src/` 标准布局,单一命名空间 `oskernel_agent`)
- **LLM**:OpenCode CLI 子进程(`opencode run --agent ...`,DeepSeek deepseek-v4-flash)+ 少量 OpenAI 兼容 API 直连(语言护栏、语义复核)
- **源码解析**:Universal Ctags(符号表)+ tree-sitter(C/Rust AST);LSP 语义引擎 rust-analyzer / clangd(可选降级)
- **查重**:CodeT5p-110m 嵌入、FAISS HNSW、SimHash(特征/结构两代)、SQLite(FTS5 全文)
- **报告**:HTML(内联 Tailwind + ECharts)、reportlab 生成 PDF
- **前端**:Vue 3 + Vite + Express + sql.js;**测试**:pytest + 合成评测集
- **CI**:GitHub Actions(`.github/workflows/eval.yml`)+ GitLab CI(`.gitlab-ci.yml`)

## 3. 一张图看懂:四条链路

```text
                    ┌────────────────────────────────────────────────┐
                    │            历史作品库(建库,一次性)              │
                    │  config/repos.yaml ──► comparison/buildlib      │
                    │  (clone → 切分 → 归一化 → 嵌入 → 三代索引)      │
                    └───────────────┬────────────────────────────────┘
                                    ▼
   参赛作品仓库 ──► ┌────────────────────────────────────────────────┐
   (URL / 路径)    │  comparison.pipeline   六通道召回 → 精确比对     │
                   │  → 分段复核 → 基线扣除 → LLM 复核               │
                   │  → comparison.html + digest                     │
                   └────────────────────────┬────────────────────────┘
                                            ▼
                   ┌────────────────────────────────────────────────┐
                   │  cli.agent(描述)   ──► description.html+digest │
                   │  finals.development──► development.html+digest │
                   └────────────────────────┬────────────────────────┘
                                            ▼
                   ┌────────────────────────────────────────────────┐
                   │  finals.summary_pdf  三份 digest ──► summary.pdf│
                   └────────────────────────────────────────────────┘

   编排:frontend(Web)──► report_jobs(进程内,断点续跑)
        根目录 5 脚本 ──► cli.batch(子进程,API key 主备切换)
```

## 4. 代码结构总览

```text
.
├── config/                      # 9 个配置文件(见 §10)
├── docs/                        # 设计文档(本文件也在)
├── frontend/                    # Vue 控制台 + Express API
├── resources/reference_db/      # 参考 OS 指纹库(4 个 JSON)
├── scripts/                     # 离线运维工具(4 个)
├── src/oskernel_agent/
│   ├── __init__.py              # __version__ = "0.2.0"
│   ├── paths.py                 # 项目根定位地基
│   ├── config.py                # config.toml 加载器
│   ├── repository_identity.py   # URL → 存储键
│   ├── works_list.py            # xlsx 作品表解析
│   ├── report_quality.py        # ★ 全系统报告完整性门禁核心
│   ├── analysis/                # 单仓库事实抽取
│   ├── cli/                     # 命令入口 + 单步报告函数库
│   ├── comparison/              # ★ 查重子系统(最大,~75 文件)
│   ├── engines/                 # LLM 批量调度 + A/B/C 语义引擎
│   ├── finals/                  # 决赛四报告 + 清理门禁
│   ├── parsers/                 # C/Rust 解析 + SQLite 符号库
│   ├── pipeline/                # 描述报告树构建流水线
│   ├── prompts/                 # AI 提示模板(四层结构)
│   ├── report_jobs/             # 四件套统一编排(前端用)
│   ├── reports/                 # HTML 渲染构件
│   └── tools/                   # MCP 工具(11 个)+ 参考 OS 指纹
├── run_incremental.py           # 标准四报告驱动(改 --team/--url)
├── run_works_xlsx.py            # 批量下载作品仓库
├── continue_run.py              # 崩溃后快速续跑
├── rerun_compare_summary.py     # 只重跑对比+摘要
├── rerun_desc_summary.py        # 只重跑描述+摘要
└── tests/                       # 40 个测试文件 + 合成评测集
```

## 5. 模块详解

### 5.1 顶层包 — 基础设施

| 文件 | 作用 | 关键代码 |
|---|---|---|
| `paths.py` | 项目根定位,一切路径约定的地基 | `PROJECT_ROOT` / `SOURCE_ROOT`(支持 `OSKERNEL_PROJECT_ROOT` 覆盖) |
| `config.py` | 加载根目录 `config.toml`(tomllib,缺失回退内置默认值) | 导出 `api` / `data` / `target` / `engine` 四命名空间 |
| `repository_identity.py` | 仓库 URL → 稳定路径安全存储键 | `canonical_repository_identity()`、`repository_storage_key()`(显示名 slug + sha256 前 24 位) |
| `works_list.py` | 解析「内核赛作品仓库列表.xlsx」 | `WorksEntry`、`clone_target()`(GitHub 分支网页地址转换)、`read_works_xlsx()` |
| `report_quality.py` | **全系统质量门禁核心**(被引用最广) | `assert_report_complete()`:结构错误 / 占位正文 / 省略号截断 / 隐藏文字样式四类违规即抛 `IncompleteReportError` |

### 5.2 parsers — C/Rust 源码解析

**双通道解析,不用 LSP 做索引**:ctags 提符号 + tree-sitter 提调用关系;外加正则/指纹做统计性识别。

| 文件 | 作用 | 关键代码 |
|---|---|---|
| `code_parser.py`(30KB) | 核心解析引擎 | `run_ctags()`(Universal Ctags JSON 输出)、`classify_symbol()`(level1 进地图 / level2 进索引 / discard 噪声)、`generate_level1_map()`(注入 System Prompt 的仓库结构地图)、`find_function_calls()`(tree-sitter 提取调用)、`detect_primary_language()`、`detect_reference_os()`(三层指纹溯源参考 OS)、`detect_kernel_type()`(宏/微内核)、`detect_target_arch()`、`build_profile()` |
| `symbol_db.py` | SQLite 符号表 + FTS5 全文持久化 | `SymbolDB`(populate/lookup/搜索)、`repo_cache_path()`(按 SHA1 命名缓存库) |
| `os_tools.py` | 第二级符号索引层 | `build_repo_map()`(ctags + level1 + 入库,指纹缓存短路)、`Level2Index`(兼容旧接口的 SQL 视图) |

消费方:`cli/agent.py`(构建 structure)、`cli/mcp_server.py`、`analysis/repo_facts.py`、`pipeline/tree_builder.py`、`engines/path_a/b.py`、`tools/tool_dispatcher.py`。

### 5.3 engines — LLM 与语义引擎

**A/B/C 三条语义引擎路径(降级链)**,统一抽象在 `base.py` 的 `AnalysisEngine`(ABC):

| 文件 | 作用 | 关键点 |
|---|---|---|
| `base.py` | 统一接口 | `go_to_definition` / `find_references` / `get_call_chain` / `get_struct_fields` / `get_engine_info`(引擎信息注入 Prompt 让 LLM 知道精度) |
| `lsp_base.py` | LSP/JSON-RPC 通信中间层 | Content-Length 帧收发、`$/progress` 索引等待、大括号配对取代码块 |
| `path_a.py` | **Rust → rust-analyzer**(精度 high) | 无 Cargo.toml 或未安装则返回 False 降级 |
| `path_b.py` | **C → clangd**(精度 high) | `try_generate_compile_commands()` 四种策略生成编译数据库(bear → compiledb → cmake → 手工) |
| `path_c.py` | **tree-sitter 兜底**(精度 medium) | 全量扫描建内存索引,**永不失败** |
| `llm_batch.py`(837 行) | **批量 LLM 调度器**(全系统 AI 调用的心脏) | `run_batch_task()`:缓存查 → OpenCode 子进程(独立 XDG 隔离防 SQLite 锁)→ JSON 解析 → 失败知情重试(原因+输出片段追加进重试请求)→ json_repair 兜底;两层 validator(cache_validator 返回 `str` 则原因进入重试 prompt);`_error` 结果不写缓存 |

批量 AI 全部走 OpenCode CLI(`opencode run --agent ...`),provider 由 `setup_opencode.py` 固定为 DeepSeek,模型名取环境变量 `LLM_MODEL`(默认 deepseek-v4-flash)。

### 5.4 analysis + pipeline — 描述报告流水线

**analysis/repo_facts.py**:5 个 LLM 会话之前,一次性确定性采集共享事实档案——meta / syscall(正则计数)/ key_files / smp / commits / integrity(硬编码规则扫描)/ profile_lite。integrity 缺失直接拒绝启动。

**pipeline/**(`tree_builder.py` 2233 行是描述报告的心脏):

| 阶段 | 函数 | 做什么 |
|---|---|---|
| A 子系统枚举 | `enumerate_subsystems()` | 内容指纹归类 + 路径 token 回退,归入 9 大子系统 |
| B SUBSYS 并行 | `run_subsys_stage()` | 每子系统一次 LLM 会话(默认 10 并发),指纹断点续跑,失败整体拒绝 |
| C VERDICT 顶层 | `run_verdict_stage()` | 六维评分、亮点/问题、硬编码逐条复核、参考 OS 相似度 |
| D 语言护栏 | `lang_guard.py`(677 行) | 启发式检测英文散文 → LLM 确定性改写中文;残留英文超限拒绝生成 |

C 阶段值得注意的确定性清理:评分 0–10 制误用自动 ×10、总分由六维加权平均重算不采信 LLM、证据 `path:line` 必须指向仓库真实文件(定位失败确定性丢弃)、硬编码复核 signal_id 与扫描证据一一配对、`repair_verdict_*` 系列只修单一字段(一句话/硬编码/相似度)不重写整份。

### 5.5 comparison — 查重子系统(最大,~75 文件)

**建库**(`python -m oskernel_agent.comparison.buildlib`):

```text
ingest(克隆 repos.yaml 全部历史作品+基线)
  → normalize(发现/切分/AST 归一化/子系统归类 → functions.db)
  → embed(CodeT5p 向量 → Qdrant 本地 qdrant_local)
  → faiss(读 qdrant 建 HNSW 索引,21.5 万向量,单查 ~0.02ms)
  → simhash(IDF + 特征 SimHash)
  → code-simhash(4-gram 结构 SimHash)
  → 覆盖率门禁(缺仓拒绝出索引)
```

**召回**(`pipeline --repo <新作品>` → `embed/query.py` 的 `query_repo()`):每个函数做**六通道召回并集**——

| 通道 | 实现 | 说明 |
|---|---|---|
| 全局向量 top-k | `faiss_store.py` HNSW | 主信号;不按模块硬过滤防跨模块克隆漏召 |
| 特征 SimHash 候选池内 top-k | `simhash/build.py` | IDF 加权 64 位,4 段×16 位分段表 + multiprobe |
| 归一化指纹完全相同 | SQLite `normalized_hash` | 硬召回,不受 top-k 挤压 |
| 同名函数 | SQLite | 硬召回 |
| 结构 SimHash | `simhash/code_index.py` | 汉明 ≤15 确定性覆盖,行重排场景的查全兜底 |
| 身份邻域扩展 | `exact/identity.py` | 已命中候选所在文件域补回具体对应函数 |

**精确比对**(`exact/verify.py` → `exact/matcher.py`):双通道逐行比对(exact 原文 + renamed 掩码)→ 身份兼容分 → 分流 confirmed(>0.95)/ review(≥0.7)/ weak(≥0.5)/ 丢弃。指纹/结构/身份命中可**保档但不能伪造相似度**。

**复核与扣除**:`segment/verify.py`(分段嵌入 + 匈牙利匹配升降级)→ `metadata/`(独特字符串旁路召回、基线矩阵乘扣除、公共代码广度过滤)→ **报告**(`report/semantic_compare.py`,7810 行)。

**三代索引的代际一致性**:functions.db 的 `db_mapping_signature`(func_id→代码映射 SHA-256)是所有派生索引的"血缘"——**数据库变,索引全拒用**(`rebuild_derived_indexes` 重建)。

**报告生成**(`semantic_compare.py`):打标(库复用 / 上游基线 / 误报)→ 四道误配抑制门(支配候选/家族邻居/常量桩/无证据)→ LLM 逐对复核(职责门控→同源判断)→ 复核完整性断言(有候选未获结论即拒出报告)→ 选统一排名第一的历史作品为主对比对象展开详细证据 → 功能簇语义分析(DeepSeek 直连分批,统一字符预算)→ `normalize_labels`(档位统一:高置信同源/模型复核后仍存疑/暂未检出相似)。

### 5.6 finals — 决赛四报告与门禁

| 文件 | 作用 | 关键代码 |
|---|---|---|
| `models.py` | 四报告共享 pydantic 数据契约 | `Finding` / `ModuleDigest` / `ReportDigest` / `EvidenceRef` |
| `readability.py` | 确定性中文精炼(不替模型编造) | `remove_ai_filler()`(删"综上所述"类模板腔)、`explain_terms_on_first_use()`、`clip_at_sentence()`(完整句收束防括号截断) |
| `digests.py` | 从流水线产物提取三份 digest | `description_digest_from_tree()`、`comparison_digest()`、`normalize_description_claim()`(用事实档案约束"系统调用 N/M 已可用"类声明) |
| `development.py` | 开发过程报告 | `collect_commits()`(Git numstat 复算,AI 只判断不计算)、`validate_ai_development_result()`(强门禁:候选必须对应程序证据、SHA 必须真实、阶段必须连续覆盖全部提交、置信度 0–100) |
| `summary_pdf.py` | 一页 A4 摘要 PDF | `load_digests()`(三 digest 缺一即拒)、`_validate_ai_summary_result()`(severity 必须逐字等于来源、confidence 不得高于来源、不得引入语料外标识符)、`_repair_ai_summary_analysis()`(issue 级错误定向修复,不动未点名内容)、reportlab 排版(超一页切紧凑样式,仍超则拒) |
| `integrity.py` | 硬编码线索扫描 + 比赛镜像构建验证 | `scan_hardcode_signals()`(四类规则)、`verify_contest_build()`(临时副本、禁网、一次性容器,锁镜像 ID) |
| `cleanup.py` | 交付目录清理门禁 | 四件套齐全才清理、越界路径拒绝 |

### 5.7 prompts / reports / tools — 提示词、渲染、MCP

**prompts/builder.py**:四层提示结构——L1 角色声明 + L2 硬约束(代码内)+ L3 工作流 + L4 输出格式契约(从 `templates/*.md` 以 `<!-- format -->` 为界切分)。5 个模板:`subsys` / `verdict` / `development` / `summary` / `json_repair`。

**reports/**:`html.py`(CDN 头、`path:line` 链接化、目录锚点完整性校验)+ `html_tree.py`(tree.json → 评委速读版 HTML:结论与关键问题 / 硬编码复核 / 全部模块概览;产出前 `assert_report_complete` + 断链检测)。

**tools/**:LLM 与代码之间的唯一查询通道,11 个工具(T1-T8 编号):
- `tool_handlers.py`:T1 `read_file` / `search_code`(FTS5 快路径,正则兜底)
- `tool_dispatcher.py`:T2-T8 聚合层(`find_symbol_definition` / `find_symbol_references` / `list_implemented_syscalls`(4 策略)/ `get_subsystem_call_chain` / `expand_callees` / `compare_with_reference_os` / `get_index_status` / `analyze_subtree`)
- `mcp_tools.py`:统一入口 `OSKernelMCPTools.execute()` 路由表
- `reference_db.py`:参考 OS 函数级指纹库(归一化 + 三指标相似度;缺失自动按 `reference_sources.yaml` 固定 revision 重建)

### 5.8 cli — 命令入口

| 命令/模块 | 作用 |
|---|---|
| `oskernel-agent`(`agent.py`) | 描述报告:解析仓库来源(URL 克隆/本地路径/repo_id)→ 事实档案 → build_tree → digest → 终端打印 → HTML |
| `oskernel-setup`(`setup_opencode.py`) | 把 DeepSeek key 写入项目私有 auth.json,注册全部 agent + MCP 条目,不动全局配置 |
| `oskernel-compare` | → `comparison.pipeline.__main__` |
| `oskernel-finals` | → `finals.__main__`(development / summary 子命令) |
| `batch.py`(518 行) | 四报告单步执行库:do_comparison / do_description / do_development / do_summary、`publish_final_reports`(四份齐全才发布)、重试族(额度/网络/门禁抖动识别 + 指数退避 + API key 主备切换)、`normalize_comparison_identity`(存储键 → 队伍编号) |
| `mcp_server.py`(996 行) | OpenCode 的 MCP 工具服务器:16 工具、三重会话保护(步数上限 30 / 连续重复 5 次 / 幻觉符号扩展 3 次)、`_LazyEngine` 后台懒加载 |
| `fetch_repo.py` / `tree_renderer.py` | 克隆工具 / 终端 rich 树打印 |

### 5.9 report_jobs + 根脚本 + scripts — 编排层

**`report_jobs/`**(前端 Web 使用的统一入口):进程内 import 编排四件套(对比 → 描述 → 开发 → 摘要),`.report_jobs_state.json` 断点续跑(已 ok 的 kind 标记 skipped),任一失败 break 不跑下游;只保留最终交付物。与 `cli/batch.py` 是**两套平行编排机制,互不调用**。

**根目录 5 脚本**(全部 `from oskernel_agent.cli import batch as B`):

| 脚本 | 场景 |
|---|---|
| `run_incremental.py` | 标准四报告驱动:`--team` / `--url` 传参,比较失败自动 `--skip-ai-detect` 兜底 |
| `run_works_xlsx.py` | 批量下载作品仓库(线程池 4);`--reports` 逐队串行出报告(并发会拖垮内存) |
| `continue_run.py` | 崩溃后快速续跑:复用 `_filematch/_recall/_suspects` 等产物,跳过需加载 ~14GB 模型的 ai_detect |
| `rerun_compare_summary.py` | 只重跑对比+摘要(对比报告门禁抖动修复后) |
| `rerun_desc_summary.py` | 只重跑描述+摘要(`--summary-only` 只出摘要) |

**scripts/ 运维工具**:`build_reference_db.py`(指纹库主动重建)、`stitch_fragments.py`(LLM 分片离线拼接灾备)、`crawl_oscomp.py`(官方仓库获奖作品爬虫)、`extract_hisrepo_metadata.py`(比赛官网详情页元数据,浏览器渲染)。

### 5.10 frontend — 控制台

Vue 3 单页(`App.vue` 481 行):顶栏(报告类型复选 + 导入 xlsx + 生成缺失)→ 统计带 → 作品表格(搜索/年份/状态筛选)→ 详情面板(iframe 预览报告 + 新窗口)→ 最近任务(日志尾部折叠)。每 5 秒轮询。

Express API(`server/`,端口 3130):`/api/summary`、`/api/repositories`(全文搜索含报告正文)、`/api/import/xlsx`、`/api/generate`、`/api/jobs`(删除运行中任务会 kill 进程树)。数据存 `frontend/data/app.sqlite`(sql.js);报告落 `data/output/<repo_id>/`;后台任务 `PipelineQueue` 串行 spawn `python -m oskernel_agent.report_jobs`。

### 5.11 tests — 测试体系

40 个测试文件 + `tests/evaluation/` 合成评测集。分层:纯单测 → 集成(样例仓库端到端)→ **评测回归**(synthesize 从历史库抽函数经 T1-T4 变换构造已知克隆,注入五层统计召回;**`--check` 时 T1/T2 召回 <0.95、T3 <0.80 即非零退出**,FAISS 索引与 functions.db 不同代直接拒绝)→ 交付物质量回归(四件套/门禁)→ 工程护栏(CI 配置、前端构建门禁)。

依赖真实模型的用例(test_embed 等)离线自动 skip;其余全 mock。

## 6. 质量门禁体系(全系统)

| 门禁 | 位置 | 触发即 |
|---|---|---|
| 报告完整性 | `report_quality.assert_report_complete` | 结构错误/占位正文/省略号截断/隐藏文字样式 → 拒绝生成 |
| 召回完整性契约 | `comparison/retrieval_contract.py`(v4) | 六通道不全、历史库覆盖不全、静默截断 → 拒绝"暂未检出相似"结论 |
| 索引代际签名 | `db_mapping_signature` | functions.db 变化后旧 FAISS/SimHash 索引拒绝加载 |
| 开发报告门禁 | `finals/development.validate_ai_development_result` | 虚假提交、阶段重叠、历史缺口 → 拒绝交付 |
| 摘要门禁 | `finals/summary_pdf._validate_ai_summary_result` | severity 不符来源、置信度超源、语料外标识符、遗漏 critical → 定向修复后重校验,结构性错误直接失败 |
| 交付清理门禁 | `finals/cleanup.py` | 四件套不齐拒绝清理;越界路径 ValueError |
| 目录审计 | `comparison/report/audit.py` | 旧叫法残留、"原创"违规表述、覆盖缺口 |
| 语言护栏 | `pipeline/lang_guard.py` | 残留英文超限 → 拒绝生成 |

## 7. 配置与凭据体系

| 文件 | 管什么 | 谁读 |
|---|---|---|
| `config.toml`(gitignore) | API key/base_url、数据目录、引擎超时 | 顶层 `config.py` |
| `config/settings.yaml` | 查重算法参数(embedding/qdrant/retrieval/metadata/llm/ai_detect 六段) | comparison 各子模块按段读 |
| `config/repos.yaml` | 历史作品清单(151 条,2021–2025) | buildlib / ingest / report |
| `config/baselines.yaml` | 公共基线白名单(11 条,`baseline_` 前缀) | ingest / report |
| `config/upstream_baselines.yaml` | 上游路径/ABI 受限扣除规则 | report |
| `config/libraries.yaml` | 第三方库复用识别(8 库) | report |
| `config/module_rules.yaml` | 函数 → 子系统归类(15 tag) | normalize |
| `config/reference_sources.yaml` | 参考 OS 指纹固定 revision 来源(4 个) | tools/reference_db |
| `config/keep_symbols.txt` | 归一化保留符号白名单(~150 个) | normalize |
| `config/repos_unavailable.yaml` | 不可用仓库留档(无代码引用) | 人工台账 |
| `.env`(gitignore) | LLM 模型/密钥、GITLAB_TOKEN、QDRANT_URL、FINALS_MIN_COMMITS | load_dotenv 各处 |

## 8. 命令速查

```bash
# 四件套(Web 前端同款,支持断点续跑)
python -m oskernel_agent.report_jobs --repo <url> --repo-id <队号> --output-dir data/output/<队号>

# 标准四报告(带重试/主备 key)
python run_incremental.py --team <队号> --url <url>

# 建历史库
python -m oskernel_agent.comparison.buildlib --repos-root D:\@MyData\work\OS\historical_repos\by_year

# 对比报告
oskernel-compare --repo <path> --baselines

# 单报告
oskernel-agent --repo-path <path> --output description.html
python -m oskernel_agent.finals development --repo <path> --repo-id <ID> --output development.html

# 质量门禁
python -m oskernel_agent.comparison.report audit
python -m pytest -q
python -m tests.evaluation.synthesize --per-class 15 --out tests/fixtures/eval_set.json
python -m tests.evaluation.run --check
```

## 9. 常见运维操作

| 操作 | 命令 |
|---|---|
| 换 LLM key | `.env` 的 `LLM_API_KEY` + `config.toml [api]` + 重跑 `oskernel-setup`(或 batch 主备切换) |
| 新增历史作品 | 编辑 `config/repos.yaml`(repo_url/year/team_name/award_level)→ `buildlib` 重建索引 |
| 索引过期(改了 functions.db) | 必须重建三代索引(FAISS + 特征 SimHash + 结构 SimHash),否则拒绝加载 |
| 参考 OS 指纹损坏 | 自动按固定 revision 重建;主动重建 `python scripts/build_reference_db.py --all` |
| 描述报告 LLM 阶段跑完但合成中断 | `python scripts/stitch_fragments.py <work_dir>` 离线拼接 |
| 对比报告语义截断(残留 cluster id) | 先核批次行预算(25k),再 rerun_compare_summary.py |
| AI 检测 | `--ai-detect` 显式启用(默认关;starcoder2-3b 在 8GB 显卡必崩,子进程隔离) |

## 10. 关键设计哲学(读代码前先记住)

1. **AI 判断、程序复算**:提交次数/LOC/行号/相似度全部程序算,AI 只做判断与归纳。
2. **证据可回溯**:每个问题必须带 path:line 或 SHA,指向仓库真实存在的内容。
3. **拒绝带病交付**:AI 失败、输出无效、复核未全覆盖 → 不生成报告,而不是降级凑合。
4. **降级不丢弃**:库复用/上游基线/误报从 KPI 剔除但单列小节,供评委人工核。
5. **"暂未检出相似" ≠ "原创"**:标签语义严格,不越权认定。
6. **报告不可由参赛队后处理**:完全工具生成,参赛队无需且不得修改 HTML/PDF。
