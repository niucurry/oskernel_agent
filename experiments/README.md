# 实验脚本与验证结果归档

本目录汇总近期围绕 OS 代码描述系统与可核验评价闭环开展的实验脚本、配置清单与验证结果摘要（已排除私有凭据、虚拟环境与大体积运行缓存）：

- `description_research/`：描述系统机理实验与生产基线诊断（接口注册取证 `description_interface_witness`、职责预算 `description_responsibility_budget`、名称先验污染控制 `description_anchor_answerability`、RepoDoc 提取器核对 `description_repodoc_assessment`、代数解释预检 `description_algebraic_explanation`、生产基线探测 `description_production_baseline`、真实 OpenCode/MCP 运行时诊断 `description_runtime_grounding`、系统提示传递对照 `description_prompt_transport`）。
- `read_audit_validation_20261011/`：第一步源码读取记录、自动补读与 Canary 访问审计验证产物（参见 `docs/research/read-audit-2026-10-11.md`）。
- `claim_review_validation_20261012/`：第二步关键结论独立复核与有限修订验证产物（参见 `docs/research/claim-review-2026-10-12.md`）。
- `evidence_plan_validation_20261013/`：评价项必要证据清单与漏读自动补齐对照实验产物（参见 `docs/research/evidence-plan-2026-10-13.md`）。
- `assertions_validation_20261013/`：按评价项生成带范围事实断言、来源核对与注入错误计数修订实验产物（参见 `docs/research/evaluation-assertions-2026-10-13.md`）。
- `recall_probe_20261029/`：报告后源码上下文保留与多模型无工具召回探针实验脚本、各运行评测结果及汇总分析（`analysis-by-method.json`、`analysis-weak-models.json`）。
- `recall_integration_20261101/`：生产 SUBSYS/VERDICT 报告后保留与召回核对集成验收配置与脚本（参见 `docs/research/recall-check-2026-11-01.md`）。
