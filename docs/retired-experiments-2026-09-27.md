# 旧实验清理记录

**2026-10-03 更新：本页记录 9 月 27 日的清理状态。** 下文当时保留的描述研究方案与模型适配器，后来也已随旧主线停止而归档；现状与路径见[描述研究清理记录](retired-description-experiments-2026-10-02.md)。

按用户要求，于 2026-09-27 在新实验前完成清理。正式系统 `src/oskernel_agent/` 没有导入旧 `repo_summary` 包；旧行为图路线已有停止投入的决策。

从当前工作目录移除了 18 个旧实验目录、旧 `src/repo_summary/`、25 个旧实验专用测试、22 份过期实验说明和旧实验数据中的冗余产物，共 7,672 个文件／链接、约 1.02 GB 逻辑文件内容。Python 和测试工具缓存同时清理。移除了对应的 `repo-summary` CLI、包发现配置和仅供旧研究使用的可选依赖声明。

以下内容仍保留：正式系统与此前修复；当前描述完整性回放；回放实际使用的 17 份历史摘要；现行研究方案；概括旧负结果的研究决策、证据审查和 CFC demo 记录。通用模型调用适配器仍有用途，原样复用到 `research/description_scope/model.py`，它不属于新的方法贡献。

**归档先完成逐文件校验，然后才删除原文件。**

- 归档：[experiments.tar.gz](/home/niu/agent-archives/retired-behavior-20260927T094538Z/experiments.tar.gz)
- 文件清单：[manifest.json](/home/niu/agent-archives/retired-behavior-20260927T094538Z/manifest.json)
- 压缩包约 355 MB；包含 7,689 个原始文件／链接，17 份保留摘要也在归档中备份。
- 压缩包 SHA-256：`7f93fec838700ea7ec4dfadcb0c3e4f224162a94436d61e106545ac7d523e795`
- 旧 CFC 系统冻结的 37 个实现文件，其归档内容全部与原冻结哈希一致。

这不是删除负结果或重置留出记录。旧文档中指向被移除目录的相对链接，属于历史记录；恢复归档到独立目录后可按原路径查看。CFC 正式模型对照仍是“未执行、等待独立人工 gold”，不能改写为已完成实验。

如需检查旧材料，请将归档解压到一个新的空目录，不覆盖当前项目。归档只保留旧实验材料，不代表完整冻结了 Python 依赖环境。清理脚本和机器可读结果位于 `research/description_integrity/cleanup_retired.py` 与 `cleanup-result.json`；脚本检测到已有清理记录后会拒绝重复执行。
