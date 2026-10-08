# 后续继续研究的状态交接

总目标仍 active，尚未验证出合适的论文方法。用户要求继续实际开发实验，兼顾系统和 TSE/TOSEM，保留工程收益及负结果。不能恢复原交接列出的行为图、条件 JSON、条件压缩及其提示/模型/角色变体。原 [完整交接](research-handoff-next-method-2026-10-02.md) 和此前研究记录继续有效。

## 本轮已完成

1. [历史证据冻结实验](research-history-evidence-gate-2026-10-07.md)：三个新检查的通用 OS，465 个可见提交，两个可见 Redox 合并；一个 remerge 残余为零，另一个祖先缺失。至少两个项目的主假设未获支持。没有源码 blob 比对，不能宣称语义效果。普通 Git 已覆盖原型，停止新算法主张，禁止给该固定样本补历史/换项目/改阈值。初次脚本命名为 inspect.py 导致导入失败的材料已保留；仅更名后实际测量。
2. 独立工程控制及修正：五类真实本地 Git 历史，完整真值的一行替换为 2 LOC，浅统计却为 1,202；真正无父根导入正例保留。生产 `finals/development.py` 保存父关系、作者/提交者时间、原始 numstat，对四个实际浅边界和两个未测量合并保持未知；未知不参与大规模异常判定。HTML/digest 区分已测量小计和未知量，日期范围使用作者声明 UTC 日期的最小/最大值，不改提交顺序。全部 465 个 SHA 和顺序保持。相关报告回归 **204 passed**，第一次中文字体配置缺失的 **2 failed / 202 passed** 原日志保留，配置已有私有字体后通过。不要把回归算论文效果。
3. [自然上游适用性实验](research-natural-upstream-gate-2026-10-07.md)：固定 la-seL4/dev 与 seL4/master、xv6-loongarch-exp/main 与 xv6-riscv/riscv、rCoreloongArch/master 与 rCore-Tutorial-v3/ch8。六个新完整 blob-filtered bare 克隆获取成功，三个固定分支对均无共同祖先；独立 alternate-object 共享祖先正例通过，实际六图连通性和非浅检查通过，三对根集合互不相交。停止该固定祖先标签路线。没有源码读取、函数配对、分类器重放或模型效果实验，不能把零样本写成零缺陷，也不能说无共同历史证明无复用。

## 材料与保护

- `research/history_evidence_audit/final-audit.json`：273 个已关闭材料索引、242 份原始 stdout/stderr 校验；当前 finalize.log 排除以避免自哈希错误。固定旧生产副本 development-before.py 不覆盖。
- `research/natural_upstream_review/final-audit.json`：120 个索引材料、96 份 stdout/stderr 校验，并再次验证上一阶段材料及生产 SHA 一致。当前 finalize.log 排除。
- 六个自然来源 metadata 克隆在 `/home/niu/.cache/oskernel-natural-upstream-review`，8,564,250 字节；早先三个浅克隆仍在另一缓存，未加深。此前 seL4 当前上游头已暴露，不称为新的独立 OS 盲测。
- 事前取样修订在 sampling-amendment.json：发生于 metadata 获取后、任何祖先/diff/source 测量前；只将取样分母改为继承文件中的已修改路径，项目/阈值/预算未动。因前置祖先门槛全部失败，未影响结果；原协议和全部 SHA 均保留。
- 首前阶段 C 来源扣除与 L0 字符串指纹修复、A2 CRC 条件编译负结果保持不变。历史运行单元 **392** 未增加；本轮 Git 分类、图控制和测试不算内核运行。
- 不改用户已有删除、其他生产修复或旧负结果，不 reset/commit 未授权更改。最新仅 README、finals/development.py 和新增 test_history_evidence.py 涉及生产/测试改动，两个新研究目录及文档独立保存。
- 当前磁盘剩余约 359 MiB，低于既定大构建下限 512 MiB；小范围读取原型仍可做。没有后台编译或新 QEMU；NoAxiom 原准备因 target 总量超限已停止，不能无新资源证据和明确新协议偷偷重试。

## 下一阶段约束

已经向用户发出可选资料问题：提供已获授权且当前可读的正式参赛仓库、固定提交、当时上游版本、运行日志目录。暂无答复；这不是权限确认，不应停止所有独立工作或把沉默当同意。当前公开参考头不能被标成正式参赛真值。

本轮查阅 CONPLAG 原论文、JPlag/SourcererCC/Dolos 官方实现、CENTRIS 原论文及实现、Software Heritage 来源研究。模板扣除、修改/嵌套 OSS 识别、来源指纹已有强工作；不能将已知能力包装成新方法。JPlag 当前官方要求 Java 25，本机无 Java；这些外部检测器本轮未安装或运行，不能声称完成外部基线比较。CENTRIS 公共组件库不提供版本信息，组件识别不能直接充当精确本地修改真值。

总目标没有达到完成条件。后续可根据新的独立证据继续选择实证或方法研究；对当前已关闭候选应保持边界，不为追求正结果改样本、阈值或标签，也不能把个别候选失败泛化成所有 OS 评审研究不可行。
