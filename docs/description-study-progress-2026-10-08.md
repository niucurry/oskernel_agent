# 描述研究继续记录

研究目标仍在执行，尚未找到超出适用强基线且经独立确认的论文方法。仅研究描述系统，已停三条主线和对比系统创新没有恢复。

- [职责预算预检](/home/niu/agent-research/description_responsibility_budget/README.md)：12 KB 有局部正差异，24 KB 与简单函数族取样功能族持平、完整服务数更低，未通过冻结门槛，停止。保留解析伪函数造成的无效首版结果及修正记录。
- [名称污染控制](/home/niu/agent-research/description_anchor_answerability/README.md)：两次重复中，职责错配但露出 API 名称仍答对 18/22；只列名称为 22/22；遮蔽名称后的相同错配为 0/22。独立答案来自维护者手册身份，不来自模型互评。这是有限定位评测的污染证据，不能解释为任意描述正确率。精确职责词检索也解决这些原文控制，因此只保留诊断，未立为新方法。
- [当前原 SUBSYS 有预算探测](/home/niu/agent-research/description_production_baseline/README.md)：7 个唯一请求、150,853 token 后在 12 次分析限制停止，无完整报告；适配器上限比原服务器更紧，不据此给生产语义效果定分。接口适配异常、预算公告和原回复复用均有记录。
- [最近公开实现预检](/home/niu/agent-research/description_repodoc_assessment/README.md)：实际运行 RepoDoc 作者原样 C 提取器，三批已暴露源码的位置一致性及差异完整保存；可编译片段确认伪函数 if 和指针返回函数遗漏。0 个模型请求。这只约束基线适用性，不代表新描述方法有效。
- [真实 OpenCode/MCP 运行](/home/niu/agent-research/description_runtime_grounding/README.md)：原始运行时在已暴露 ChibiOS 的两个消息源码文件上写出四份模块正文和总览，但快速模式引用验证要求被会话禁止的初始化，JSON 未写出。10 个唯一请求、110,727 token，达到下一调用预算保护后停止，没有放宽预算续跑；语义质量未测量。

第二阶段总用量 15 请求、163,209 已报告 token，未超过冻结父阶段 60 请求/200,000 token。此前首阶段 35 请求、136,170 已报告 token，其中 1 次 TLS 失败用量未知并另预留，不隐去累计成本。初始余额后来可见下降，不以余额短时不变推断免费。

第三阶段真实运行在首个调用前另行说明为 16 请求/120,000 token 的有限基线诊断，实际 10 请求/110,727 token、未知用量 0，CPU 9.578 秒。三个阶段累计 60 个真实请求、410,106 已报告 token；首阶段 35 次中包含 1 次 TLS 未知用量，另预留而不算免费，也不把它重复加成第 61 个请求。阶段二余额没有挪用到第三阶段。

保留工程修复：原始 C 宏可让部分语法树把 if 认作函数；清单现在过滤不能作为函数名的控制关键字，同时继续标明解析不全。真实宏形式的回归与 GCC 预处理后语法检查通过，相关描述/渲染回归 262 项通过。这些只是工程验证。

之后另行修正工具预算耗尽提示：分析查询停止后，报告写入和引用检查实际仍可调用，提示不再误称“不要再调用任何工具”。该提示变更通过 Python 语法编译检查；不把此前 262 项结果改写为这一新变更的质量证据。

真实运行随后确认两项工程缺陷并修复：MCP 未限制大版本而安装到不兼容的 2.x，现在限定 `<2.0.0`；快速模式 validate_refs 在无分析上下文时使用有效的配置仓库目录，不再引导调用被禁止的初始化。原样 MCP stdio RPC 的有效/失效引用及缺失配置 3 项回归通过，其中先耗尽分析预算再验证引用；原 OpenCode 配置回归 1 项通过。这些检查不核实中文陈述的语义，也不等同论文效果。

第三阶段的失败、部分报告、原始请求/SSE、运行前源码快照、修复及回归材料和近期原文检索记录，已归档至 `/home/niu/agent-archives/description-runtime-grounding-20261008T084740Z/experiment.tar.gz`，SHA-256 `fb7980ac59a8ac1b8893f4e5db5dce976960fb2d02314bd04efa6e652d7b2b74`。434 个成员全部重新读取核对哈希，并检查私有密钥未进入归档。独立离线重算无需读取配置或调用模型，入口见该目录 README。

近期 [候选筛选](/home/niu/agent-research/description_runtime_grounding/literature_candidates.md)继续核查执行消费者反馈、受控表示事实、需求定向解释及架构问题评价四类不同机制。DocSearch 原论文/作者架构文档、受控自然语言原文、refocusing 原文/作者 README，以及 RepoProbe 固定源码评分 API 已核对；尚无证据将它们的直接移植或提示拼装立为新方法。下载失败保留，不用模型事实清单评分充当独立真值。

原型、公开源码、失败及原始调用全部在仓库外；第二阶段三个原型已归档至 `/home/niu/agent-archives/description-stage2-20261008T063818Z/experiments.tar.gz`，SHA-256 `7ead1d10e7547b8fbddb75958d16f60236412592941b4016875a2b5c517b3fde`，686 个成员均重新读取并核对 SHA-256，入口也记录在各原型 preservation.json。后续公开实现预检单独保存，不冒称已进入之前归档。生产仓库只保留有用实现、必要回归与本简要交接。用户原有删除保持，未提交或推送。

后续 [实际提示传递对照](/home/niu/agent-research/description_prompt_transport/README.md) 发现第三项工程缺陷：OpenCode 使用 agent.prompt，而生产写入 agent.system，导致预期 7,908 字符 SUBSYS 系统提示从未进入第三阶段 10 份模型请求。已修正生成器。原 OpenCode 两组本机模拟对照确认旧字段未送达、新字段完整送达，各 2 次本地 HTTP，远端请求 0；相关回归 19 项通过。第三阶段仍是当时生产代码的真实失败及成本记录，但不是预期完整提示模板的语义质量基线。更正是归档后的追加说明，原始记录和旧归档没有改写；修复后也未追加有费用请求救旧预算。

提示传递追加材料单独归档至 `/home/niu/agent-archives/description-prompt-transport-20261008T092851Z/experiment.tar.gz`，SHA-256 `111b88c13af85983e08f81058400a2b83b2ac9c20d0f1ab6661887d10a49c1fb`，747 个成员重新读取核对哈希，私有密钥检查通过。
