# 开发日志

本页按周记录开发进展。每条都链接到当周在该仓库中的真实提交记录，可逐条点开核对。

| 概览 | |
|---|---|
| 起始 | 2026-04-19 |
| 累计提交 | 275 次 |
| 覆盖周数 | 23 周（2026 年第 16–38 周） |
| 当前状态 | 四份报告链路全部打通；已对 24 支参赛队伍产出报告，其中 20 支四份齐全 |

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

<!--
维护说明（本段不会显示在页面上）：
每周在此处追加一条，格式为
- 第 N 周（MM-DD ~ MM-DD，X 次提交）：[提交记录](链接)：这一周做了什么、达成了什么。
当周提交链接形如 https://github.com/niucurry/oskernel_agent/commits/main/?since=YYYY-MM-DD&until=YYYY-MM-DD
日期取当周的周一与周日。没有提交的周也要如实记一条"无提交"，避免日志看起来比实际更连续。
-->
