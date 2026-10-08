# 已停止的描述研究：清理与归档

2026-10-02 开始记录，2026-10-03 完成文档收尾。按用户要求停止旧研究主线，移除后续不再使用的原型、运行产物、专用测试和过时方案，准备重新选题。此次未改动生产代码，没有调用模型。

## 已移除的内容

- `research/description_scope/` 的 Python 先导代码、模型适配器、任务夹具、请求/回复与重复诊断产物。
- `research/scope_repair/` 的阶段差异修复原型、C/Rust 宿主夹具、上游 vendor 源码、模型请求/回复、探针和分析脚本。
- `tests/test_description_scope_experiment.py`、`tests/test_scope_repair.py` 两个实验专用测试文件。
- 三份过时研究方案：`agent-runtime-and-evaluation-design.md`、`contrastive-description-research-proposal-2026-09-27.md`、`description-scope-research-decision-2026-09-27.md`。
- 描述完整性目录中不再执行的先导研究协议与空白标注模板。
- 工作区外的 `/home/niu/.cache/oskernel-scope-repair/` 原生编译缓存。

共从工作区移除 690 个文件，约 5.06 MB 逻辑内容；另移除 267 个编译缓存文件，约 42.88 MB 逻辑内容。逻辑字节数不等于实际释放的磁盘块数。

收尾另删除两个已退休测试的 `.pyc` 缓存，工作区合计移除 692 个文件。这两个可再生成缓存不含独立实验材料，未另行归档；路径与哈希记入清理结果。

## 保留的内容

生产系统 `src/oskernel_agent/` 的 132 个 Python 文件保持清理前哈希；生产修复及相关回归测试保留。描述完整性回放、修复前源码快照和所需 17 份历史摘要保留。

两个退休研究目录现在只保存精简结论、调用账本、最终分析、原生验证摘要和归档入口，不再包含可执行实验代码。所有负结果、原始调用和冻结材料仍完整保存在外部归档。

生产测试仍使用的 `oskernel-description-test-deps` 和 `oskernel-description-fonts` 缓存保留。模型私有配置没有修改或纳入归档。用户此前删除的 `docs/finals-report-plan.md` 和 `docs/progress-presentation.pptx` 未恢复。

## 归档与完整性

- [实验归档](/home/niu/agent-archives/retired-description-20261002T155545Z/experiments.tar.gz)，10,221,061 字节。
- [逐文件清单](/home/niu/agent-archives/retired-description-20261002T155545Z/manifest.json)。
- [清理计划](/home/niu/agent-archives/retired-description-20261002T155545Z/cleanup-plan.json)。
- [完整删除记录](/home/niu/agent-archives/retired-description-20261002T155545Z/cleanup-result.json)。
- 归档 SHA-256：`bc9583655fe305eab2b7335f639905291d6c9e88d2e8dab7608b076e05b026d0`。

删除前已完成归档内 974 个成员的逐文件核对；最近一次实验冻结的 491 个文件均校验一致。归档还包含被更新文档的原始快照，以及该冻结清单引用的生产文件/测试快照。

原始 `research/scope_repair/final-freeze.json` 保存在归档内。清理后的活跃目录有意不再满足“所有原实验文件仍原地存在”，不能把移除视为证据被改写。保留摘要的原始文件内容没有修改；清理后的 README 和报告归档入口属于维护更新，原版本可从归档恢复。

## 恢复及历史链接

请只解压到一个新建的空目录，不要覆盖现有项目。归档中的项目文件使用原相对路径，例如 `research/description_scope/model.py`；外部编译缓存位于归档的 `external-cache/oskernel-scope-repair/`。

历史报告中的脚本、输入和原始结果路径应在解压目录中查找。原运行命令也需要在恢复后的相应环境中执行。归档不是完整的项目安装包或 Python/系统依赖环境；需要相应依赖。重新选题不需要全量恢复旧实验。

## 清理后验证

保留的生产描述、报告、开发过程与 PDF 相关测试重新运行：**337 passed**。清理前的 350 项包含已经退休的 13 项 scope_repair 机制测试；计数变化不是删除生产测试或修复退化。两个实验专用测试文件都已归档，其中旧 Python 先导测试不在那次 350 项命令内。

机器记录见 [清理结果](../research/description_integrity/cleanup-scope-result-2026-10-02.json) 和 [清理后验证](../research/description_integrity/cleanup-scope-validation-2026-10-02.json)。当前任务入口为[重新选题交接](research-handoff-next-method-2026-10-02.md)。旧主线不再作为当前研究计划。
