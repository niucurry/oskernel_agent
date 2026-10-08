# 阶段差异局部修复实验已退休

结论：停止扩展该候选。三个项目、四个 C/Rust 功能、两轮生成，共八份最终文本全部与反例重写基线相同；候选生成与检查调用 37 次，基线 29 次。读者存在回复超限和评分波动，代理分数不是描述语义准确率。

本目录仅保留以下精简证据：

- [淘汰决定](decision.json)。
- [完成审计](completion-audit.json)：其中代码/原始结果路径是归档内路径。
- [最终对照分析](trial-v4/analysis.json)。
- [原生执行摘要](prepared/native-validation.json)：记录中的编译缓存已清理。
- [拒绝实例复读](rejection-replay-v1/results.json)：预设稳定伤害条件未通过。
- [调用账本](ledger.json)：整个系列 184 次 API 调用、503,586 token，包含失败控制和诊断。

原型、构建脚本、C/Rust 夹具、vendor、请求/回复、冻结清单及专用测试已完整校验归档后移除；这里不再提供运行入口。原始文件和完整报告旧版的哈希均在归档中保留。

[完整结论报告](../../docs/description-scope-c-rust-trial-2026-10-02.md)；[归档位置、校验与恢复方法](../../docs/retired-description-experiments-2026-10-02.md)；[重新选题交接](../../docs/research-handoff-next-method-2026-10-02.md)。

新研究应使用独立目录，不能把这条已停止的研究主线换名继续。
