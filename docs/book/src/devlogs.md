# 开发日志

本页按周记录项目的开发与实验研究进展。前期条目链接到当周在该仓库中的提交记录，近期条目按周梳理代码描述系统与可核验评价体系的实验推进过程。

| 概览 | |
|---|---|
| 起始 | 2026-04-19 |
| 累计提交 | 284 次 |
| 覆盖周数 | 30 周（2026 年第 16–45 周） |
| 当前状态 | 四份报告链路全部打通（已对 24 支参赛队伍产出报告，其中 20 支四份齐全）；完成描述机理系列实验，建成“读取审计—证据清单—事实断言—保留召回—独立复核”五层可核验评价闭环 |

## 每周开发总结

### 第 1–10 周：从零到可用（2026-04 ~ 2026-06）

- 第 1 周（04-13 ~ 04-19，1 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-04-13&until=2026-04-19)：项目起步，搭建代码仓库分析 Agent 的基本框架。
- 第 2 周（04-20 ~ 04-26，11 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-04-20&until=2026-04-26)：语言与身份识别成型——Rust 语言解析、源码根目录定位、递归文档检索、主语言判定（rCore / uCore）、参考操作系统溯源、内核类型识别、命名风格与目录结构检测；Agent 具备函数调用链路提取、结构体查询与代码核实能力。
- 第 3 周（04-27 ~ 05-03，16 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-04-27&until=2026-05-03)：引入 ctags 符号扫描与 rust-analyzer 语义引擎；建成两级代码地图；运行参数统一到配置文件；项目目录结构规范化。
- 第 4 周（05-04 ~ 05-10，9 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-05-04&until=2026-05-10)：内核审查提示词模块成型；工具集重构，新增代码分析工具处理器与注册表；补齐 Windows 安装文档。
- 第 5 周（05-11 ~ 05-17，5 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-05-11&until=2026-05-17)：提示词与报告工具增强；新增 HTML 报告生成；搭建面向内核分析的 MCP 服务器。
- 第 6 周（05-18 ~ 05-24，4 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-05-18&until=2026-05-24)：代码解析器重构为基于 SQLite 的符号索引；HTML 报告支持图表、可折叠目录与深色模式；加入多会话支持。
- 第 7 周（05-25 ~ 05-31，2 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-05-25&until=2026-05-31)：新增文件读取、代码搜索、操作系统函数对比三类工具；提示词与工作流说明中文化。
- 第 8 周（06-01 ~ 06-07，1 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-06-01&until=2026-06-07)：精简 HTML 报告生成流程，修复报告内链接。
- 第 9 周（06-08 ~ 06-14，5 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-06-08&until=2026-06-14)：查重引擎起步，建立从归一化、指纹、向量嵌入到精确比对与复核的多级召回漏斗；补全元数据与报告模块、流水线总入口和评测体系。
- 第 10 周（06-15 ~ 06-21，2 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-06-15&until=2026-06-21)：建立历史作品元数据库与数据获取脚本；编写覆盖 15 个子系统的创新分析技能与模板。

### 第 11–12 周：查重准确性与交付形态攻坚（2026-06 ~ 2026-07）

- 第 11 周（06-22 ~ 06-28，57 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-06-22&until=2026-06-28)：查重准确性攻坚。建立公共基线库（教学操作系统与常见第三方库），解决教学作品共用代码造成的误报；改进克隆召回，找回"改名／重写"型重复；新增整文件复制检测；AI 生成代码检测接入报告；语义级对比管道取代逐对模型复核。
- 第 12 周（06-29 ~ 07-05，42 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-06-29&until=2026-07-05)：交付形态收敛。前端控制台上线，支持报告浏览与实时日志；批量运行脚本加入磁盘保护与克隆防挂死重试；修复 Windows 下的运行阻断；中文语言护栏生效，正文不再出现独立英文句；对比报告收敛到语义对比唯一路径。

### 第 13–17 周：报告成形与对外交付（2026-07 ~ 2026-08）

- 第 13 周（07-06 ~ 07-12）：无提交。
- 第 14 周（07-13 ~ 07-19，5 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-07-13&until=2026-07-19)：新增代码指纹索引并接入评估与报告；前端移动端报告浏览优化。
- 第 15 周（07-20 ~ 07-26，2 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-07-20&until=2026-07-26)：完善不可访问候选项的处理与报告生成；清理过时数据、更新数据库结构。
- 第 16 周（07-27 ~ 08-02，5 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-07-27&until=2026-08-02)：简化共同上游章节，加入全量对账与库复用标记；补充 AI 检测与报告完整性测试。
- 第 17 周（08-03 ~ 08-09，14 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-08-03&until=2026-08-09)：四份报告成形——一页摘要、作品描述、开发过程、同源对比；按评委视角重构分析报告；统一决赛报告输出与清理流程。

### 第 18–23 周：交付门禁与稳定性（2026-08 ~ 2026-09）

- 第 18 周（08-10 ~ 08-16，92 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-08-10&until=2026-08-16)：交付门禁攻坚。三份报告统一 AI 生成声明；语义分析结果的结构校验与缺口补全；中文语言护栏覆盖超长英文段落；摘要控制在一页版式内并设定置信度上限；硬编码复核证据全部可回溯到文件与行号；报告产出改为逐队独立执行，单队失败不影响其余；整体性能调优。
- 第 19 周（08-17 ~ 08-23，1 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-08-17&until=2026-08-23)：合并远端分支并收尾，阶段产出归档。
- 第 20 周（08-24 ~ 08-30）：无提交。
- 第 21 周（08-31 ~ 09-06）：无提交。
- 第 22 周（09-07 ~ 09-13）：无提交。
- 第 23 周（09-14 ~ 09-20，1 次提交）：[提交记录](https://github.com/niucurry/oskernel_agent/commits/main/?since=2026-09-14&until=2026-09-20)：流水线代码结构整理，逐行核对后不改变逻辑。

### 第 24–30 周：描述机理实验与可核验评价闭环（2026-09 ~ 2026-11）

- 第 24 周（09-21 ~ 09-27）：搭建 mdBook 开发文档站并梳理全仓模块职责地图（`docs/project-map.md`）；启动代码描述系统的机理探索，针对多级摘要中的行为条件失真开展先导调研，系统评估行为图、条件单元与跨调用状态卡方案的局限性，完成早期探索材料校验归档，将研究重心收敛至描述压缩中的条件保持与可核验性。
- 第 25 周（09-28 ~ 10-04）：上线仓库全量凭据扫描门禁（`scripts/check_secrets.py`）；修复共用摘要与树标题误剥离 `<`/`>` 纯文本比较符、模块摘要被短 `brief` 覆盖、PDF 中文字体可嵌入性检查、浅克隆边界统计及公共上游排除的完整源码身份校验等问题。开展描述条件失真先导实验与 C/Rust 阶段差异自动修复实验（`description_scope`、`scope_repair`），在 CPython（18 个场景）及 Zephyr、cJSON、heapless（154 个输入、308 次执行观测）上对比直接生成、源码自查、全部反例重写与阶段差异局部修复；证实单纯增加条件 JSON 或阶段差异局部补丁相对强基线无净收益（8/8 最终文本与反例重写完全一致）且调用成本更高，果断淘汰该候选并完成归档。
- 第 26 周（10-05 ~ 10-11）：聚焦 OS 代码描述系统开展多候选机制对比与生产运行时基线诊断。完成接口注册取证实验（`description_interface_witness`），在 RT-Thread、RIOT、Apache Mynewt 的 33 个操作表绑定（66 轮）上对比生产受限读取、BM25、CodeWiki 组件抽取与“简单查表 + Ctags 定位”强基线（均达 66/66），同时引入 C/C++ 轻量语法解析修复快速符号清单漏检（RIOT 样本观察符号数 5→23）并增加报告源码范围计数；完成 ThreadX/ChibiOS/NuttX 职责预算预检（`description_responsibility_budget`）、API 名称先验污染控制实验（`description_anchor_answerability`，证实露出 API 名称时错配职责仍答对 18/22、遮蔽名称后降至 0/22）、RepoDoc 原样 C 提取器位置一致性核对（`description_repodoc_assessment`）与代数等价表达式解释预检（`description_algebraic_explanation`）；在真实 OpenCode/MCP 运行时与提示传递对照（`description_runtime_grounding`、`description_prompt_transport`）中定位并修复 `mcp<2.0.0` 版本兼容、快速模式 `validate_refs` 仓库路径回退及 `agent.prompt` 系统提示传递（解决 7,908 字符 SUBSYS 系统提示未送达模型的问题）三项关键工程缺陷。
- 第 27 周（10-12 ~ 10-18）：确立“覆盖可审计、结论可核验、预算可控制”的比赛代码仓库评价改进方案（`docs/evaluation-improvement-plan.md`），区分访问真实性、证据完整性与判断正确性，落地前两步核心机制。第一步（`read_audit` / `read_canary` / `read_coverage`）通过 `opencode run --format json` 外部记录原生 `read/grep` 与 MCP `read_file/search_code/find_symbol_definition` 的真实返回行区间并逐行校验源码 SHA-256，对引用函数的阅读缺口触发最多两轮独立会话分段补读，并构建不改变行号的 128 位随机内联注释 Canary 副本实验与入口调用候选清点工具，在真实模型验收中分离验证“标记回报正确”与“完整函数覆盖”（212 项回归通过）；第二步（`claim_review`）从 SUBSYS/VERDICT 报告选出最多 6 条关键结论，启动两个互不可见的独立会话（正向核对与反例搜寻）重读源码并核验逐字引文，支持/反驳分歧时追加第三轮复核，发现有效反证或证据不足时触发一轮报告修订并向一页摘要传递未确认限定前缀（272 项回归通过）。
- 第 28 周（10-19 ~ 10-25）：解决“模型漏引关键文件导致读取审计盲区”与“整段自然语言难以逐项核验”的问题，落地评价项必要证据清单与带范围的事实断言机制。实现外部证据清单接口（`evidence_plan`，`AGENT_EVIDENCE_PLAN`），在调用模型前完成 C/C++/Rust 函数语法定位与配置行范围快照，将预定证据并入强制读取范围并衔接自动补读，对照实验证实即使首轮报告未引用配置文件 `config.h` 也能捕获缺口并在补读会话中补齐（299 项回归通过）；在此基础上要求模型按评价项输出包含 `statement`、`scope`、`conditions`、`evidence`、`counterevidence` 与 `validation_need` 的结构化事实断言（`evaluation_assertions`），建立“结构检查 → 逐字引文与完整函数来源检查 → 独立源码语义复核与单次修订”三层核验链路，并在受控注入错误计数结论实验中验证了越界引文拦截与自动修订重审闭环（328 项回归通过）。
- 第 29 周（10-26 ~ 11-01）：研究长上下文多轮工具调用后的源码保留与无工具回忆能力，构建报告后召回探针实验框架（`recall_probe`）。开发本地 HTTP 录制代理（`RecordingProxy`，自动注入凭据并记录完整请求体）与会话状态复制分叉机制（绕过 OpenCode 1.14.48 `run --fork` 空请求缺陷）；设计函数首/中/尾（start/mid/end）多位置 Canary 标记、真实代码行（`line`）、被调用函数（`callee`）、函数级调用列表多选题（`calls`）以及未标记对照（`nomarker`）与虚构函数对照（`fake`）。在真实比赛内核仓库（`T202510486995194-334` 虚拟内存 `vmem.c`、物理内存伙伴系统 `pmem.c`、线程同步 `futex.c`）上完成 `live_batch`、`live_single`、`batch_context`、无上下文 `fresh`、标记替换 `substituted` 及 100k/300k 上下文填充压力测试，并跨模型重放评测 `deepseek-flash` 与 6 个开源模型（`Qwen3-8B`、`Qwen3-14B`、`Qwen3.5-4B`、`Qwen3.5-9B`、`Qwen3-30B-A3B`、`Qwen2.5-72B`），累计生成 2,632 条重放评测记录，系统量化了上下文保留率、条件召回率、标记张冠李戴（`swapped`）与幻觉编造（`fabricated`）以及 Canary 命中对同位置源码内容召回的预测效度。
- 第 30 周（11-02 ~ 11-06）：将报告后源码保留与召回核对正式接入生产描述流水线（`recall_check`，`AGENT_RECALL_CHECK=1`），形成“读取覆盖审计—必要证据清单—结构化事实断言—报告后保留与召回抽查—独立源码复核”的完整闭环。主任务在带 start/mid/end 标记的工作副本上经本地录制代理运行，工具返回经副本校验并确认去除标记后无损映射回原仓库哈希；报告生成后按预算自动抽取必要证据函数、报告引用函数及 15% 已读非关键函数（含无标记与虚构函数对照），在禁用全部工具的分叉会话中批量提问，精确区分 `retained_recalled`、`dropped`、`present_not_recalled`、`misattributed` 与 `confabulation`，对丢失或未召回函数自动换用新随机值补读，对误归属或调用题答错的函数双向关联报告断言进入独立源码复核，并在交付前清除报告中的所有标记与随机值；完成睡眠锁子系统（`sleeplock.c`/`spinlock.c`）端到端验收脚本与全链路回归测试整理。

<!--
维护说明（本段不会显示在页面上）：
每周在此处追加一条，格式为
- 第 N 周（MM-DD ~ MM-DD，X 次提交）：[提交记录](链接)：这一周做了什么、达成了什么。
当周提交链接形如 https://github.com/niucurry/oskernel_agent/commits/main/?since=YYYY-MM-DD&until=YYYY-MM-DD
日期取当周的周一与周日。没有提交的周也要如实记一条"无提交"，避免日志看起来比实际更连续。
-->
